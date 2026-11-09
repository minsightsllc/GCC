from __future__ import annotations

import uuid
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction, models
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Cast, Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .admin_usage import get_database_capacity_metrics
from .database_capacity import get_postgres_table_estimates
from .models import Activity, AdminSecurityEvent
from .services import record_activity


AUDIT_KIND_FILTERS = {"all", "activity", "security"}
AUDIT_SORT_FIELDS = {
    "type": "record_kind",
    "record": "summary",
    "detail": "display_detail",
    "actor": "display_actor",
    "created": "created_at",
    "trashed": "trashed_at",
    "moved_by": "display_trashed_by",
}
AUDIT_SORT_DIRECTIONS = {"asc", "desc"}
AUDIT_PAGE_SIZE = 50
AUDIT_SELECTION_LIMIT = 50


def database_capacity_context(request):
    metrics = get_database_capacity_metrics()
    table_estimates = get_postgres_table_estimates()
    cutoff_raw = request.GET.get("cutoff", "").strip()
    cutoff_date = _parse_cutoff(cutoff_raw) if cutoff_raw else None
    cutoff_error = bool(cutoff_raw and cutoff_date is None)
    audit_context = _audit_records_context(request, cutoff_date=cutoff_date, trash=False)
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
        "preview_counts": {
            "activity": audit_context["audit_kind_counts"]["activity"],
            "security": audit_context["audit_kind_counts"]["security"],
        },
        "preview_total": audit_context["audit_filtered_total"],
        "preview_page": audit_context["audit_page"],
        "audit_trash_count": trash_count,
        "project_archive_url": reverse("operations:project-archive"),
        "lead_trash_url": f'{reverse("operations:dashboard-section", kwargs={"section": "leads"})}?trash=1',
        "client_messages_url": reverse("admin:operations_clientmessage_changelist"),
        **audit_context,
    }


def database_capacity_page(admin_site, request):
    if _is_audit_fragment_request(request):
        cutoff_raw = request.GET.get("cutoff", "").strip()
        cutoff_date = _parse_cutoff(cutoff_raw) if cutoff_raw else None
        context = _audit_records_context(request, cutoff_date=cutoff_date, trash=False)
        context.update(cutoff_date=cutoff_date, cutoff_raw=cutoff_raw)
        return HttpResponse(
            render_to_string("admin/database_audit_records_partial.html", context, request=request)
        )
    return admin_site._render_admin_page(
        request,
        "admin/database_capacity.html",
        database_capacity_context(request),
    )


def database_capacity_trash_context(request):
    audit_context = _audit_records_context(request, trash=True)
    return {
        "title": "Database Trash",
        "subtitle": "Recoverable audit records",
        "database_trash_total": Activity.objects.filter(trashed_at__isnull=False).count()
        + AdminSecurityEvent.objects.filter(trashed_at__isnull=False).count(),
        **audit_context,
    }


def database_capacity_trash_page(admin_site, request):
    if _is_audit_fragment_request(request):
        context = _audit_records_context(request, trash=True)
        return HttpResponse(
            render_to_string("admin/database_trash_records_partial.html", context, request=request)
        )
    return admin_site._render_admin_page(
        request,
        "admin/database_capacity_trash.html",
        database_capacity_trash_context(request),
    )


def database_capacity_record_detail_page(admin_site, request, kind, record_id):
    if kind == "activity":
        record = get_object_or_404(
            Activity.objects.select_related("actor", "lead", "estimate", "project", "trashed_by"),
            pk=record_id,
        )
        record_type = "Activity"
        title = record.message or "Activity record"
        details = [
            ("Record ID", record.pk),
            ("Message", record.message),
            ("Detail", record.detail),
            ("Actor", record.actor.get_username() if record.actor else "System"),
            ("Lead", record.lead if record.lead_id else ""),
            ("Estimate", record.estimate if record.estimate_id else ""),
            ("Project", record.project if record.project_id else ""),
            ("Created", record.created_at),
            ("Moved to Database Trash", record.trashed_at),
            ("Moved by", record.trashed_by.get_username() if record.trashed_by_id else ""),
        ]
    elif kind == "security":
        record = get_object_or_404(
            AdminSecurityEvent.objects.select_related("user", "reviewed_by", "trashed_by"),
            pk=record_id,
        )
        record_type = "Security event"
        title = record.get_event_type_display() or "Security event"
        details = [
            ("Record ID", record.pk),
            ("Event", record.get_event_type_display()),
            ("Outcome", record.get_outcome_display()),
            ("Account", record.user.get_username() if record.user_id else ""),
            ("Attempted identifier", record.attempted_identifier),
            ("IP address", record.ip_address),
            ("Request path", record.path),
            ("User agent", record.user_agent),
            ("Detail", record.detail),
            ("Email status", record.get_email_status_display()),
            ("Email attempts", record.email_attempt_count),
            ("Last email attempt", record.email_last_attempt_at),
            ("Email sent", record.email_sent_at),
            ("Email error", record.email_error),
            ("Reviewed", record.reviewed_at),
            ("Reviewed by", record.reviewed_by.get_username() if record.reviewed_by_id else ""),
            ("Created", record.created_at),
            ("Moved to Database Trash", record.trashed_at),
            ("Moved by", record.trashed_by.get_username() if record.trashed_by_id else ""),
        ]
    else:
        raise Http404("Audit record not found.")

    fallback_url = reverse(
        "admin:database-capacity-trash" if record.trashed_at else "admin:database-capacity"
    )
    requested_return_url = request.GET.get("return_to", "")
    return_url = (
        requested_return_url
        if requested_return_url
        and url_has_allowed_host_and_scheme(
            requested_return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        else fallback_url
    )
    details = [(label, value) for label, value in details if value is not None and value != ""]
    return admin_site._render_admin_page(
        request,
        "admin/database_audit_record_detail.html",
        {
            "title": "Audit record details",
            "subtitle": "Database audit record",
            "record_type": record_type,
            "record_title": title,
            "record_is_trashed": bool(record.trashed_at),
            "record_details": details,
            "record_return_url": return_url,
        },
    )


def database_capacity_cutoff_action(request):
    if request.method != "POST":
        return redirect("admin:database-capacity")
    cutoff = _parse_cutoff(request.POST.get("cutoff_date", ""))
    if cutoff is None:
        messages.error(request, "Choose a valid cutoff date before continuing.")
        return redirect("admin:database-capacity")

    action = request.POST.get("action")
    selected_records = _parse_selected_audit_records(request.POST.getlist("selected_records"))
    selection_mode = request.POST.get("selection_mode") == "1"
    if selection_mode and (not selected_records or len(selected_records) > AUDIT_SELECTION_LIMIT):
        messages.error(request, f"Select between 1 and {AUDIT_SELECTION_LIMIT} audit records first.")
        return redirect(_safe_return_url(request, f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}'))

    if selection_mode:
        activity_ids = [pk for kind, pk in selected_records if kind == "activity"]
        security_ids = [pk for kind, pk in selected_records if kind == "security"]
        activity = _activity_before(cutoff).filter(pk__in=activity_ids)
        security = _security_before(cutoff).filter(pk__in=security_ids)
    else:
        # Keep the original cutoff-wide operation available for existing callers.
        activity = _activity_before(cutoff)
        security = _security_before(cutoff)
    activity_count, security_count = activity.count(), security.count()
    total_count = activity_count + security_count
    if total_count == 0:
        messages.info(request, "No selected audit records are still eligible for cleanup.")
        return redirect(_safe_return_url(request, f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}'))

    if selection_mode and len(selected_records) != total_count:
        messages.warning(request, "Some selected records changed or no longer match the cutoff and were skipped.")

    return_url = _safe_return_url(
        request,
        f'{reverse("admin:database-capacity")}?cutoff={cutoff.isoformat()}',
    )

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
        messages.error(request, "Move audit records to Database Trash before permanently deleting them.")
        return redirect(return_url)

    messages.error(request, "Choose a valid database retention action.")
    return redirect(return_url)


def database_capacity_trash_selection_action(request):
    if request.method != "POST":
        return redirect("admin:database-capacity-trash")
    selected_records = _parse_selected_audit_records(request.POST.getlist("selected_records"))
    return_url = _safe_return_url(request, reverse("admin:database-capacity-trash"))
    if not selected_records or len(selected_records) > AUDIT_SELECTION_LIMIT:
        messages.error(request, f"Select between 1 and {AUDIT_SELECTION_LIMIT} audit records first.")
        return redirect(return_url)

    activity_ids = [pk for kind, pk in selected_records if kind == "activity"]
    security_ids = [pk for kind, pk in selected_records if kind == "security"]
    activity = Activity.objects.filter(trashed_at__isnull=False, pk__in=activity_ids)
    security = AdminSecurityEvent.objects.filter(trashed_at__isnull=False, pk__in=security_ids)
    activity_count, security_count = activity.count(), security.count()
    total_count = activity_count + security_count
    if not total_count:
        messages.info(request, "The selected Database Trash records are no longer available.")
        return redirect(return_url)
    if len(selected_records) != total_count:
        messages.warning(request, "Some selected records changed and were skipped.")

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
        return redirect(return_url)

    if action == "purge":
        if request.POST.get("confirmed") != "1":
            return _confirmation_page(
                request,
                title="Permanently delete selected Database Trash records?",
                detail=f"This will permanently delete the {total_count:,} selected audit record(s). This cannot be undone.",
                form_action=reverse("admin:database-capacity-trash-selected"),
                hidden_fields={
                    "action": "purge",
                    "selected_records": [f"{kind}:{pk}" for kind, pk in selected_records],
                    "return_to": return_url,
                },
                counts={"Activity records": activity_count, "Security events": security_count},
                return_url=return_url,
                selected_record_labels=_selected_audit_record_labels(activity, security),
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
        return redirect(return_url)

    messages.error(request, "Choose restore or permanent delete.")
    return redirect(return_url)


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


def _confirmation_page(
    request,
    *,
    title,
    detail,
    form_action,
    hidden_fields,
    counts,
    return_url,
    selected_record_labels=(),
):
    from .admin import grand_coast_admin_site

    hidden_pairs = []
    for name, value in hidden_fields.items():
        if isinstance(value, (list, tuple)):
            hidden_pairs.extend((name, item) for item in value)
        elif value != "":
            hidden_pairs.append((name, value))
    return grand_coast_admin_site._render_admin_page(
        request,
        "admin/database_capacity_confirm.html",
        {
            "title": title,
            "detail": detail,
            "form_action": form_action,
            "hidden_fields": hidden_fields,
            "hidden_field_pairs": hidden_pairs,
            "record_counts": counts.items(),
            "selected_record_labels": selected_record_labels,
            "return_url": return_url,
        },
    )


def _selected_audit_record_labels(activity, security):
    selected = []
    for queryset in (_activity_record_values(activity), _security_record_values(security)):
        selected.extend(
            queryset.values("record_kind", "summary", "display_detail", "display_actor")
        )
    return selected


def _audit_records_context(request, *, cutoff_date=None, trash=False):
    selected_kind = request.GET.get("kind", "all")
    if selected_kind not in AUDIT_KIND_FILTERS:
        selected_kind = "all"
    query = request.GET.get("q", "").strip()
    sort_field = request.GET.get("sort", "created")
    if sort_field not in AUDIT_SORT_FIELDS or (not trash and sort_field in {"trashed", "moved_by"}):
        sort_field = "created"
    direction = request.GET.get("direction", "desc")
    if direction not in AUDIT_SORT_DIRECTIONS:
        direction = "desc"

    activity = Activity.objects.all()
    security = AdminSecurityEvent.objects.all()
    if trash:
        activity = activity.filter(trashed_at__isnull=False)
        security = security.filter(trashed_at__isnull=False)
    else:
        activity = activity.filter(trashed_at__isnull=True)
        security = security.filter(trashed_at__isnull=True)
        if cutoff_date:
            activity = activity.filter(created_at__date__lt=cutoff_date)
            security = security.filter(created_at__date__lt=cutoff_date)
        else:
            activity = activity.none()
            security = security.none()

    activity = _filter_activity_search(activity, query)
    security = _filter_security_search(security, query)
    kind_counts = {
        "activity": activity.count(),
        "security": security.count(),
    }
    kind_counts["all"] = kind_counts["activity"] + kind_counts["security"]

    activity_rows = _activity_record_values(activity)
    security_rows = _security_record_values(security)
    if selected_kind == "activity":
        rows = activity_rows
    elif selected_kind == "security":
        rows = security_rows
    else:
        rows = activity_rows.union(security_rows, all=True)
    db_field = AUDIT_SORT_FIELDS[sort_field]
    order = f"-{db_field}" if direction == "desc" else db_field
    rows = rows.order_by(order, "record_id")
    page_param = "trash_page" if trash else "preview_page"
    page = Paginator(rows, AUDIT_PAGE_SIZE).get_page(request.GET.get(page_param, 1))
    page_rows = list(page.object_list)
    for row in page_rows:
        row["selection_key"] = f"{row['record_type']}:{row['record_id']}"
        row["detail"] = row.pop("display_detail")
        row["actor"] = row.pop("display_actor")
        row["trashed_by"] = row.pop("display_trashed_by")
    page.object_list = page_rows

    endpoint_name = "admin:database-capacity-trash" if trash else "admin:database-capacity"
    state = {
        "kind": selected_kind,
        "q": query,
        "sort": sort_field,
        "direction": direction,
    }
    if cutoff_date and not trash:
        state["cutoff"] = cutoff_date.isoformat()

    def build_url(**overrides):
        params = {**state, **overrides}
        return f"{reverse(endpoint_name)}?{_encode_query(params)}"

    filter_definitions = [
        ("all", "All records"),
        ("activity", "Activity"),
        ("security", "Security events"),
    ]
    filter_tabs = [
        {
            "key": key,
            "label": label,
            "count": kind_counts[key],
            "url": build_url(kind=key),
            "current": selected_kind == key,
        }
        for key, label in filter_definitions
    ]
    sort_definitions = [
        ("type", "Type"),
        ("record", "Record"),
        ("detail", "Detail"),
        ("actor", "Actor"),
        ("created", "Created"),
    ]
    if trash:
        sort_definitions.append(("trashed", "Moved to trash"))
        sort_definitions.append(("moved_by", "Moved by"))
    sort_headers = [
        {
            "key": key,
            "label": label,
            "ascending_url": build_url(sort=key, direction="asc"),
            "descending_url": build_url(sort=key, direction="desc"),
            "ascending_active": sort_field == key and direction == "asc",
            "descending_active": sort_field == key and direction == "desc",
            "aria_sort": "ascending" if sort_field == key and direction == "asc" else "descending" if sort_field == key else "none",
        }
        for key, label in sort_definitions
    ]
    pagination = {
        "number": page.number,
        "num_pages": page.paginator.num_pages,
        "has_previous": page.has_previous(),
        "has_next": page.has_next(),
        "previous_url": build_url(**{page_param: page.previous_page_number()}) if page.has_previous() else "",
        "next_url": build_url(**{page_param: page.next_page_number()}) if page.has_next() else "",
    }
    return_url = build_url(**{page_param: page.number})
    for row in page_rows:
        record_id = uuid.UUID(str(row["record_id"]))
        record_url = reverse(
            "admin:database-capacity-record",
            kwargs={"kind": row["record_type"], "record_id": record_id},
        )
        row["location_url"] = f"{record_url}?{urlencode({'return_to': return_url})}"
    return {
        "audit_kind_filter": selected_kind,
        "audit_query": query,
        "audit_sort": sort_field,
        "audit_sort_direction": direction,
        "audit_kind_counts": kind_counts,
        "audit_filtered_total": kind_counts[selected_kind],
        "audit_filter_tabs": filter_tabs,
        "audit_sort_headers": sort_headers,
        "audit_page": page,
        "audit_return_url": return_url,
        "audit_clear_url": build_url(q=""),
        "audit_pagination": pagination,
        "audit_trash_view": trash,
        "audit_selection_limit": AUDIT_SELECTION_LIMIT,
    }


def _filter_activity_search(queryset, query):
    for term in query.split():
        clause = (
            Q(message__icontains=term)
            | Q(detail__icontains=term)
            | Q(actor__username__icontains=term)
            | Q(actor__email__icontains=term)
            | Q(actor__first_name__icontains=term)
            | Q(actor__last_name__icontains=term)
            | Q(trashed_by__username__icontains=term)
        )
        if "activity" in term.casefold() or "record" in term.casefold():
            clause |= Q(pk__isnull=False)
        queryset = queryset.filter(clause)
    return queryset


def _filter_security_search(queryset, query):
    queryset = queryset.annotate(_search_ip=Cast("ip_address", models.TextField()))
    event_choices = list(AdminSecurityEvent.EventType.choices)
    for term in query.split():
        matching_labels = [value for value, label in event_choices if term.casefold() in label.casefold()]
        clause = (
            Q(event_type__icontains=term)
            | Q(detail__icontains=term)
            | Q(attempted_identifier__icontains=term)
            | Q(path__icontains=term)
            | Q(user_agent__icontains=term)
            | Q(user__username__icontains=term)
            | Q(user__email__icontains=term)
            | Q(user__first_name__icontains=term)
            | Q(user__last_name__icontains=term)
            | Q(trashed_by__username__icontains=term)
            | Q(_search_ip__icontains=term)
        )
        if matching_labels:
            clause |= Q(event_type__in=matching_labels)
        if term.casefold() in {"security", "event", "events"}:
            clause |= Q(pk__isnull=False)
        queryset = queryset.filter(clause)
    return queryset


def _activity_record_values(queryset):
    return queryset.annotate(
        record_type=Value("activity", output_field=models.TextField()),
        record_id=Cast("pk", output_field=models.TextField()),
        record_kind=Value("Activity", output_field=models.TextField()),
        summary=Cast("message", output_field=models.TextField()),
        display_detail=Cast("detail", output_field=models.TextField()),
        display_actor=Case(
            When(actor__isnull=True, then=Value("System")),
            default=Cast("actor__username", output_field=models.TextField()),
            output_field=models.TextField(),
        ),
        display_trashed_by=Coalesce(
            Cast("trashed_by__username", output_field=models.TextField()),
            Value("Former admin"),
            output_field=models.TextField(),
        ),
    ).values("record_type", "record_id", "record_kind", "summary", "display_detail", "display_actor", "created_at", "trashed_at", "display_trashed_by").order_by()


def _security_record_values(queryset):
    event_label = Case(
        *[
            When(event_type=value, then=Value(label))
            for value, label in AdminSecurityEvent.EventType.choices
        ],
        default=F("event_type"),
        output_field=models.TextField(),
    )
    return queryset.annotate(
        record_type=Value("security", output_field=models.TextField()),
        record_id=Cast("pk", output_field=models.TextField()),
        record_kind=Value("Security event", output_field=models.TextField()),
        summary=event_label,
        display_detail=Case(
            When(detail__gt="", then=Cast("detail", output_field=models.TextField())),
            When(attempted_identifier__gt="", then=Cast("attempted_identifier", output_field=models.TextField())),
            When(ip_address__isnull=False, then=Cast("ip_address", output_field=models.TextField())),
            default=Value("", output_field=models.TextField()),
            output_field=models.TextField(),
        ),
        display_actor=Case(
            When(user__isnull=False, then=Cast("user__username", output_field=models.TextField())),
            When(attempted_identifier__gt="", then=Cast("attempted_identifier", output_field=models.TextField())),
            default=Value("Unknown account", output_field=models.TextField()),
            output_field=models.TextField(),
        ),
        display_trashed_by=Coalesce(
            Cast("trashed_by__username", output_field=models.TextField()),
            Value("Former admin"),
            output_field=models.TextField(),
        ),
    ).values("record_type", "record_id", "record_kind", "summary", "display_detail", "display_actor", "created_at", "trashed_at", "display_trashed_by").order_by()


def _parse_selected_audit_records(values):
    parsed = []
    seen = set()
    for raw in values:
        try:
            kind, raw_id = str(raw).split(":", 1)
            record_id = uuid.UUID(raw_id)
        except (ValueError, TypeError, AttributeError):
            return None
        if kind not in {"activity", "security"}:
            return None
        key = (kind, record_id)
        if key not in seen:
            seen.add(key)
            parsed.append(key)
    return parsed


def _safe_return_url(request, fallback):
    candidate = request.POST.get("return_to", "")
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


def _is_audit_fragment_request(request):
    return request.GET.get("records") == "1" and request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _encode_query(params):
    from urllib.parse import urlencode

    return urlencode({key: value for key, value in params.items() if value not in (None, "")})


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
