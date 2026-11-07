from __future__ import annotations

import uuid
from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Min
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .admin_usage import get_database_capacity_metrics
from .database_capacity import get_postgres_table_estimates
from .models import Activity, AdminSecurityEvent
from .services import record_activity


def database_capacity_context(request):
    metrics = get_database_capacity_metrics()
    table_estimates = get_postgres_table_estimates()
    cutoff_raw = request.GET.get("cutoff", "").strip()
    cutoff_date = _parse_cutoff(cutoff_raw) if cutoff_raw else None
    cutoff_error = bool(cutoff_raw and cutoff_date is None)
    preview_records = []
    preview_counts = {"activity": 0, "security": 0}

    if cutoff_date:
        activity = _activity_before(cutoff_date)
        security = _security_before(cutoff_date)
        preview_counts = {"activity": activity.count(), "security": security.count()}
        for item in activity.select_related("actor").order_by("-created_at")[:75]:
            preview_records.append(
                {
                    "kind": "Activity",
                    "created_at": item.created_at,
                    "summary": item.message,
                    "detail": item.detail,
                    "actor": str(item.actor) if item.actor else "System",
                }
            )
        for item in security.select_related("user").order_by("-created_at")[:75]:
            preview_records.append(
                {
                    "kind": "Security event",
                    "created_at": item.created_at,
                    "summary": item.get_event_type_display(),
                    "detail": item.detail or item.attempted_identifier or str(item.ip_address or ""),
                    "actor": str(item.user) if item.user else "Unknown account",
                }
            )
        preview_records.sort(key=lambda item: item["created_at"], reverse=True)

    preview_page = Paginator(preview_records, 30).get_page(request.GET.get("preview_page", 1))
    metrics["history"] = _format_graph(metrics["history"])
    trash_count = (
        Activity.objects.filter(trashed_at__isnull=False).count()
        + AdminSecurityEvent.objects.filter(trashed_at__isnull=False).count()
    )
    return {
        "title": "Database Capacity Tool",
        "subtitle": "Database capacity and retention",
        "database_metrics": metrics,
        "table_estimates": table_estimates,
        "cutoff_raw": cutoff_raw,
        "cutoff_date": cutoff_date,
        "cutoff_error": cutoff_error,
        "preview_counts": preview_counts,
        "preview_total": sum(preview_counts.values()),
        "preview_page": preview_page,
        "audit_trash_count": trash_count,
        "project_archive_url": reverse("operations:project-archive"),
        "lead_trash_url": f'{reverse("operations:dashboard-section", kwargs={"section": "leads"})}?trash=1',
        "client_messages_url": reverse("admin:operations_clientmessage_changelist"),
    }


def database_capacity_page(admin_site, request):
    return admin_site._render_admin_page(
        request,
        "admin/database_capacity.html",
        database_capacity_context(request),
    )


def database_capacity_trash_context(request):
    batches = {}
    activity_batches = Activity.objects.filter(trashed_at__isnull=False).values(
        "retention_batch_id"
    ).annotate(first_trashed=Min("trashed_at"), count=Count("id"), trashed_by_id=Min("trashed_by_id"))
    security_batches = AdminSecurityEvent.objects.filter(trashed_at__isnull=False).values(
        "retention_batch_id"
    ).annotate(first_trashed=Min("trashed_at"), count=Count("id"), trashed_by_id=Min("trashed_by_id"))

    for row in activity_batches:
        if row["retention_batch_id"] is None:
            continue
        batches[row["retention_batch_id"]] = {
            "id": row["retention_batch_id"],
            "first_trashed": row["first_trashed"],
            "activity_count": row["count"],
            "security_count": 0,
            "trashed_by_id": row["trashed_by_id"],
        }
    for row in security_batches:
        if row["retention_batch_id"] is None:
            continue
        batch = batches.setdefault(
            row["retention_batch_id"],
            {
                "id": row["retention_batch_id"],
                "first_trashed": row["first_trashed"],
                "activity_count": 0,
                "security_count": 0,
                "trashed_by_id": row["trashed_by_id"],
            },
        )
        batch["security_count"] = row["count"]
        if row["first_trashed"] and (
            not batch["first_trashed"] or row["first_trashed"] < batch["first_trashed"]
        ):
            batch["first_trashed"] = row["first_trashed"]
        if batch["trashed_by_id"] is None:
            batch["trashed_by_id"] = row["trashed_by_id"]

    user_ids = {batch["trashed_by_id"] for batch in batches.values() if batch["trashed_by_id"]}
    users = get_user_model().objects.in_bulk(user_ids)
    for batch in batches.values():
        user = users.get(batch["trashed_by_id"])
        batch["trashed_by"] = str(user) if user else "Former admin"
        batch["total_count"] = batch["activity_count"] + batch["security_count"]
    batch_list = sorted(batches.values(), key=lambda item: item["first_trashed"], reverse=True)
    page = Paginator(batch_list, 30).get_page(request.GET.get("page", 1))
    return {
        "title": "Database Trash",
        "subtitle": "Recoverable audit records",
        "database_trash_page": page,
        "database_trash_total": sum(batch["total_count"] for batch in batch_list),
    }


def database_capacity_trash_page(admin_site, request):
    return admin_site._render_admin_page(
        request,
        "admin/database_capacity_trash.html",
        database_capacity_trash_context(request),
    )


def database_capacity_cutoff_action(request):
    if request.method != "POST":
        return redirect("admin:database-capacity")
    cutoff = _parse_cutoff(request.POST.get("cutoff_date", ""))
    if cutoff is None:
        messages.error(request, "Choose a valid cutoff date before continuing.")
        return redirect("admin:database-capacity")

    action = request.POST.get("action")
    activity = _activity_before(cutoff)
    security = _security_before(cutoff)
    activity_count, security_count = activity.count(), security.count()
    total_count = activity_count + security_count
    if total_count == 0:
        messages.info(request, "No audit records were found before that cutoff date.")
        return redirect(f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}')

    if action == "trash":
        batch_id = uuid.uuid4()
        now = timezone.now()
        with transaction.atomic():
            activity.update(retention_batch_id=batch_id, trashed_at=now, trashed_by=request.user)
            security.update(retention_batch_id=batch_id, trashed_at=now, trashed_by=request.user)
            record_activity(
                "Database audit records moved to trash",
                f"{activity_count} Activity and {security_count} security event record(s)",
                actor=request.user,
            )
        _invalidate_capacity_cache()
        messages.success(request, f"Moved {total_count:,} audit record(s) to Database Trash.")
        return redirect("admin:database-capacity-trash")

    if action == "purge":
        if request.POST.get("confirmed") != "1":
            return _confirmation_page(
                request,
                title="Permanently delete audit records?",
                detail=f"This will permanently delete {activity_count:,} Activity record(s) and {security_count:,} security event record(s) created before {cutoff.isoformat()}. This cannot be undone.",
                form_action=reverse("admin:database-capacity-cutoff"),
                hidden_fields={"cutoff_date": cutoff.isoformat(), "action": "purge"},
                counts={"Activity records": activity_count, "Security events": security_count},
                return_url=f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}',
            )
        with transaction.atomic():
            activity.delete()
            security.delete()
            record_activity(
                "Database audit records permanently deleted",
                f"{activity_count} Activity and {security_count} security event record(s) before {cutoff.isoformat()}",
                actor=request.user,
            )
        _invalidate_capacity_cache()
        messages.success(request, f"Permanently deleted {total_count:,} audit record(s).")
        return redirect("admin:database-capacity")

    messages.error(request, "Choose a valid database retention action.")
    return redirect(f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}')


def database_capacity_trash_action(request, batch_id):
    if request.method != "POST":
        return redirect("admin:database-capacity-trash")
    activity = Activity.objects.filter(trashed_at__isnull=False, retention_batch_id=batch_id)
    security = AdminSecurityEvent.objects.filter(trashed_at__isnull=False, retention_batch_id=batch_id)
    activity_count, security_count = activity.count(), security.count()
    total_count = activity_count + security_count
    if not total_count:
        messages.info(request, "That Database Trash batch is no longer available.")
        return redirect("admin:database-capacity-trash")

    action = request.POST.get("action")
    if action == "restore":
        with transaction.atomic():
            activity.update(retention_batch_id=None, trashed_at=None, trashed_by=None)
            security.update(retention_batch_id=None, trashed_at=None, trashed_by=None)
            record_activity(
                "Database audit records restored",
                f"{activity_count} Activity and {security_count} security event record(s)",
                actor=request.user,
            )
        _invalidate_capacity_cache()
        messages.success(request, f"Restored {total_count:,} audit record(s).")
        return redirect("admin:database-capacity-trash")

    if action == "purge":
        if request.POST.get("confirmed") != "1":
            return _confirmation_page(
                request,
                title="Permanently delete this Database Trash batch?",
                detail=f"This will permanently delete {activity_count:,} Activity record(s) and {security_count:,} security event record(s). This cannot be undone.",
                form_action=reverse("admin:database-capacity-trash-action", kwargs={"batch_id": batch_id}),
                hidden_fields={"action": "purge"},
                counts={"Activity records": activity_count, "Security events": security_count},
                return_url=reverse("admin:database-capacity-trash"),
            )
        with transaction.atomic():
            activity.delete()
            security.delete()
            record_activity(
                "Database audit trash permanently deleted",
                f"{activity_count} Activity and {security_count} security event record(s)",
                actor=request.user,
            )
        _invalidate_capacity_cache()
        messages.success(request, f"Permanently deleted {total_count:,} audit record(s) from Database Trash.")
        return redirect("admin:database-capacity-trash")

    messages.error(request, "Choose restore or permanent delete.")
    return redirect("admin:database-capacity-trash")


def _confirmation_page(request, *, title, detail, form_action, hidden_fields, counts, return_url):
    from .admin import grand_coast_admin_site

    return grand_coast_admin_site._render_admin_page(
        request,
        "admin/database_capacity_confirm.html",
        {
            "title": title,
            "detail": detail,
            "form_action": form_action,
            "hidden_fields": hidden_fields,
            "record_counts": counts.items(),
            "return_url": return_url,
        },
    )


def _activity_before(cutoff):
    return Activity.objects.filter(trashed_at__isnull=True, created_at__date__lt=cutoff)


def _security_before(cutoff):
    return AdminSecurityEvent.objects.filter(trashed_at__isnull=True, created_at__date__lt=cutoff)


def _parse_cutoff(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _format_graph(history):
    points = history.get("points", [])
    if history.get("status") != "ready" or not points:
        return {**history, "chart_polyline": "", "chart_points": [], "min_gb": None, "max_gb": None}

    values = [point["used_bytes"] for point in points]
    minimum, maximum = min(values), max(values)
    span = maximum - minimum
    plotted = []
    for index, point in enumerate(points):
        x = 24 + (752 * index / max(len(points) - 1, 1))
        y = 150 - (112 * ((point["used_bytes"] - minimum) / span if span else 0.5))
        plotted.append({"x": round(x, 1), "y": round(y, 1), "timestamp": point["timestamp"], "used_gb": point["used_bytes"] / 1_000_000_000})
    return {
        **history,
        "chart_polyline": " ".join(f"{point['x']},{point['y']}" for point in plotted),
        "chart_points": plotted,
        "min_gb": minimum / 1_000_000_000,
        "max_gb": maximum / 1_000_000_000,
        "chart_start": points[0]["timestamp"],
        "chart_end": points[-1]["timestamp"],
    }


def _invalidate_capacity_cache():
    from .admin_usage import invalidate_database_capacity_cache

    invalidate_database_capacity_cache()
