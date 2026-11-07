"""Admin-only pages and actions for the shared storage-retention workflow."""

from __future__ import annotations

import mimetypes
from uuid import UUID

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import FileField
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import StorageTrash
from .storage_retention import (
    StorageRetentionError,
    empty_storage_trash,
    move_file_to_trash,
    move_recovered_file_to_trash,
    permanently_delete_storage_item,
    reattach_recovered_file,
    restore_storage_item,
    storage_file_rows,
    storage_for_alias,
)


def storage_retention_context(request):
    rows = storage_file_rows()
    counts = {
        "all": len(rows),
        "active": sum(1 for row in rows if row["status"] == "active"),
        "trash": sum(
            1 for row in rows
            if row["status"] in {StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED}
        ),
        "recovered": sum(1 for row in rows if row["status"] == StorageTrash.Status.RECOVERED),
    }
    selected_status = request.GET.get("status", "all")
    if selected_status not in counts:
        selected_status = "all"
    if selected_status == "active":
        rows = [row for row in rows if row["status"] == "active"]
    elif selected_status == "trash":
        rows = [
            row for row in rows
            if row["status"] in {StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED}
        ]
    elif selected_status == "recovered":
        rows = [row for row in rows if row["status"] == StorageTrash.Status.RECOVERED]

    query = request.GET.get("q", "").strip()
    if query:
        lowered = query.casefold()
        rows = [
            row for row in rows
            if any(
                lowered in str(row.get(key, "")).casefold()
                for key in ("original_name", "record_title", "record_type", "project_title", "sharing_status", "content_type")
            )
        ]
    rows.sort(key=lambda row: (row["status"] != "active", row["original_name"].casefold()))
    page = Paginator(rows, 50).get_page(request.GET.get("page"))

    from .admin_usage import get_admin_storage_usage

    return {
        "title": "Storage & Retention",
        "subtitle": "GCC-managed files and recovery",
        "storage_usage": get_admin_storage_usage(),
        "storage_rows": list(page),
        "storage_page": page,
        "storage_counts": counts,
        "storage_status": selected_status,
        "storage_query": query,
        "trash_item_count": StorageTrash.objects.filter(
            status__in=[StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED]
        ).count(),
    }


def _may_manage(request):
    from .views import _can_access_dashboard

    return _can_access_dashboard(request.user)


def _action_redirect(request):
    from django.utils.http import url_has_allowed_host_and_scheme

    destination = request.POST.get("return_to", "")
    if destination and url_has_allowed_host_and_scheme(
        destination,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(destination)
    return redirect("admin:storage-retention")


def _check_owner(request):
    if not _may_manage(request):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied


def _render_admin_template(request, template, context, *, status=200):
    from .admin_site import grand_coast_admin_site

    page_context = grand_coast_admin_site.each_context(request)
    page_context.update(context)
    return render(request, template, page_context, status=status)


@login_required
@require_POST
def storage_file_trash(request):
    _check_owner(request)
    try:
        item = move_file_to_trash(
            request.POST.get("source_model", ""),
            request.POST.get("source_id", ""),
            request.POST.get("source_field", ""),
            actor=request.user,
        )
    except StorageRetentionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{item.original_name} moved to Storage Trash. It still uses storage until Trash is emptied.")
    return _action_redirect(request)


@login_required
@require_POST
def storage_bulk_action(request):
    _check_owner(request)
    selected = list(dict.fromkeys(request.POST.getlist("selected_files")))
    if not selected:
        messages.error(request, "Select at least one file first.")
        return _action_redirect(request)
    if len(selected) > 50:
        messages.error(request, "Select up to 50 files at a time. Select all applies to the current page.")
        return _action_redirect(request)

    source_refs = []
    trash_ids = []
    try:
        for value in selected:
            parts = value.split(":")
            if len(parts) == 4 and parts[0] == "source" and all(parts[1:]):
                model = apps.get_model(parts[1])
                if model is None or model._meta.app_label != "operations":
                    raise ValueError
                model._meta.pk.to_python(parts[2])
                if not isinstance(model._meta.get_field(parts[3]), FileField):
                    raise ValueError
                source_refs.append((parts[1], parts[2], parts[3]))
            elif len(parts) == 2 and parts[0] == "trash":
                trash_ids.append(UUID(parts[1]))
            else:
                raise ValueError
    except (LookupError, ValidationError, ValueError, TypeError):
        messages.error(request, "The selected file list is invalid. Refresh the inventory and try again.")
        return _action_redirect(request)

    selection_groups = {"in-use"} if source_refs else set()
    for item in StorageTrash.objects.filter(pk__in=trash_ids).only("status"):
        if item.status == StorageTrash.Status.RECOVERED:
            selection_groups.add("recovered")
        elif item.status in {StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED}:
            selection_groups.add("trash")
    if len(selection_groups) > 1:
        messages.error(request, "Select files from only one status group at a time. Clear the current selection before choosing another group.")
        return _action_redirect(request)

    action = request.POST.get("bulk_action", "")
    if action not in {"trash", "restore", "delete"}:
        raise Http404

    moved = restored = failed = skipped = 0
    trash_statuses = {StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED}
    if action == "delete":
        if selection_groups != {"trash"}:
            messages.error(request, "Select files in Trash to permanently delete them.")
            return _action_redirect(request)
        if request.POST.get("confirm_delete") != "yes":
            messages.error(request, "Confirm permanent deletion in the file list confirmation dialog.")
            return _action_redirect(request)
        items = list(
            StorageTrash.objects.filter(pk__in=trash_ids, status__in=trash_statuses)
            .order_by("original_name", "moved_at")
        )
        if not items:
            messages.error(request, "The selected files are no longer in Trash.")
            return _action_redirect(request)

        result = {"deleted": 0, "retained": 0, "failed": 0}
        for item in items:
            try:
                outcome = permanently_delete_storage_item(item.pk, actor=request.user)
            except StorageRetentionError:
                result["failed"] += 1
            else:
                result[outcome] += 1
        if result["deleted"]:
            from .admin_usage import invalidate_admin_storage_usage_cache

            invalidate_admin_storage_usage_cache()
            messages.success(request, f"Permanently deleted {result['deleted']} selected file(s) from storage.")
        if result["retained"]:
            messages.info(request, f"Kept {result['retained']} stored object(s) because another record still references them.")
        if result["failed"]:
            messages.error(request, f"Could not delete {result['failed']} file(s). They remain in Trash for retry.")
        return _action_redirect(request)

    if action == "trash":
        for source_model, source_id, source_field in source_refs:
            try:
                move_file_to_trash(source_model, source_id, source_field, actor=request.user)
            except StorageRetentionError:
                failed += 1
            else:
                moved += 1
        for item_id in trash_ids:
            item = StorageTrash.objects.filter(pk=item_id, status=StorageTrash.Status.RECOVERED).first()
            if item is None:
                skipped += 1
                continue
            try:
                move_recovered_file_to_trash(item_id, actor=request.user)
            except StorageRetentionError:
                failed += 1
            else:
                moved += 1
        if moved:
            messages.success(request, f"Moved {moved} selected file(s) to Trash. They still use storage until Trash is emptied.")
    else:
        skipped += len(source_refs)
        for item_id in trash_ids:
            item = StorageTrash.objects.filter(pk=item_id).first()
            if item is None or item.status not in trash_statuses:
                skipped += 1
                continue
            try:
                restore_storage_item(item_id, actor=request.user)
            except StorageRetentionError:
                failed += 1
            else:
                restored += 1
        if restored:
            messages.success(request, f"Restored {restored} selected file(s) privately to Recovered.")

    if failed:
        messages.error(request, f"Could not complete the action for {failed} selected file(s). Review their status and try again.")
    if skipped:
        messages.info(request, f"Skipped {skipped} selected file(s) because their current status does not support that action.")
    if not moved and not restored and not failed and not skipped:
        messages.info(request, "No selected files were eligible for that action.")
    return _action_redirect(request)


@login_required
@require_POST
def storage_file_restore(request, item_id):
    _check_owner(request)
    try:
        item = restore_storage_item(item_id, actor=request.user)
    except StorageRetentionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{item.original_name} is in Recovered files. Sharing and publication were not restored.")
    return _action_redirect(request)


@login_required
@require_POST
def storage_file_reattach(request, item_id):
    _check_owner(request)
    try:
        reattach_recovered_file(item_id, actor=request.user)
    except StorageRetentionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The file was reattached to its original record. Review the record's sharing and publishing settings.")
    return _action_redirect(request)


@login_required
@require_POST
def storage_recovered_to_trash(request, item_id):
    _check_owner(request)
    try:
        item = move_recovered_file_to_trash(item_id, actor=request.user)
    except StorageRetentionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{item.original_name} moved to Trash. It still uses storage until Trash is emptied.")
    return _action_redirect(request)


@login_required
@require_GET
def storage_recovered_download(request, item_id):
    _check_owner(request)
    item = StorageTrash.objects.filter(pk=item_id, status=StorageTrash.Status.RECOVERED).first()
    if item is None:
        raise Http404
    try:
        opened = storage_for_alias(item.storage_alias).open(item.storage_name, "rb")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404 from exc
    response = FileResponse(
        opened,
        as_attachment=True,
        filename=item.original_name,
        content_type=item.content_type or mimetypes.guess_type(item.original_name)[0] or "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def storage_empty_trash(request):
    _check_owner(request)
    items = list(
        StorageTrash.objects.filter(
            status__in=[StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED]
        ).order_by("original_name", "moved_at")
    )
    errors = []
    if request.method == "POST":
        if request.POST.get("confirm_empty") != "yes":
            errors.append("Confirm that you want to permanently delete the listed files from configured storage.")
        if not items:
            errors.append("Trash is already empty.")
        if errors:
            return _render_admin_template(
                request,
                "admin/storage_empty_trash_confirm.html",
                {"title": "Empty Storage Trash", "items": items, "errors": errors},
                status=400,
            )
        result = empty_storage_trash(actor=request.user)
        if result["deleted"]:
            messages.success(request, f"Permanently deleted {result['deleted']} file(s) from storage.")
        if result["retained"]:
            messages.info(request, f"Removed {result['retained']} Trash item(s), but kept the stored object because another record still references it.")
        if result["failed"]:
            messages.error(request, f"Could not delete {result['failed']} file(s). They remain in Trash for retry.")
        return redirect("admin:storage-retention")
    return _render_admin_template(
        request,
        "admin/storage_empty_trash_confirm.html",
        {"title": "Empty Storage Trash", "items": items, "errors": errors},
    )
