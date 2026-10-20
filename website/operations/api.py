"""Versioned, permission-filtered JSON endpoints for native and AI clients."""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .construction_policies import (
    can_manage_construction,
    can_manage_sales,
    can_manage_subcontractors,
    can_submit_field_work,
    feature_enabled,
    can_view_closeout,
    can_view_financials,
    can_view_agreement,
    can_view_media,
    can_view_permit,
    can_view_project,
    can_view_project_document,
    can_view_subcontractor_assignment,
    can_submit_field_work,
    is_client,
    is_field,
    is_manager,
    is_owner,
    is_subcontractor,
    is_staff_user,
    visible_leads,
    visible_estimates,
    visible_projects,
)
from .construction_services import (
    accept_agreement,
    advance_selection,
    approve_change_order,
    complete_closeout_item,
    complete_readiness_item,
    complete_site_visit,
    convert_lead,
    create_lead,
    create_permit,
    create_change_order,
    create_project_from_estimate,
    create_site_visit,
    initialize_readiness,
    project_financial_summary,
    record_inspection_result,
    record_deposit,
    record_permit_status,
    record_payment,
    request_material,
    resolve_problem_report,
    resolve_warranty_item,
    submit_problem_report,
    submit_daily_report,
    weekly_project_review,
)
from .api_rate_limit import api_rate_limit as _api_rate_limit
from .models import (
    Agreement,
    Activity,
    ALLOWED_DOCUMENT_EXTENSIONS,
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_SIZE,
    Blocker,
    ChangeOrder,
    CloseoutItem,
    ClientMessage,
    ClientNotification,
    DailyReport,
    Estimate,
    EmployeeNotification,
    Inspection,
    Lead,
    MaterialRequest,
    MediaAsset,
    NativeUploadGrant,
    PaymentSchedule,
    Permit,
    PreconstructionItem,
    ProblemReport,
    Project,
    ProjectDocument,
    Selection,
    ScheduleEvent,
    SiteVisit,
    Task,
    WarrantyItem,
    WorkflowEvent,
    sanitize_uploaded_name,
    validate_construction_document,
    validate_contact_upload,
)


MAX_JSON_BODY = 1_000_000
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _api_login(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        return view(request, *args, **kwargs)

    return wrapped


def _api_access(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        from .construction_policies import can_access_operating_system

        if not can_access_operating_system(request.user):
            return JsonResponse({"error": "Access denied."}, status=403)
        return view(request, *args, **kwargs)

    return _api_rate_limit(_api_login(wrapped))


def _error(message, status=400):
    return JsonResponse({"error": str(message)[:500]}, status=status)


def _body(request):
    if len(request.body) > MAX_JSON_BODY:
        raise ValidationError("Request body is too large.")
    if not request.body:
        return {}
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Send a valid JSON object.") from exc
    if not isinstance(value, dict):
        raise ValidationError("Send a JSON object.")
    return value


def _idempotency_key(request, payload):
    header_value = request.headers.get("Idempotency-Key")
    payload_value = payload.get("idempotency_key")
    if (
        header_value not in (None, "")
        and payload_value not in (None, "")
        and str(header_value).strip() != str(payload_value).strip()
    ):
        raise ValidationError("Idempotency-Key header and payload values must match.")
    value = header_value or payload_value
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise ValidationError("Idempotency-Key contains unsupported characters.")
    return value


def _required_idempotency_key(request, payload):
    value = _idempotency_key(request, payload)
    if not value:
        raise ValidationError("Idempotency-Key is required for this operation.")
    return value


def _decimal(value, *, required=True, minimum=None):
    if value in (None, "") and not required:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("Amount must be a valid decimal value.") from exc
    if not result.is_finite() or abs(result) > Decimal("9999999999.99"):
        raise ValidationError("Amount is outside the supported range.")
    if minimum is not None and result < minimum:
        raise ValidationError(f"Amount must be at least {minimum}.")
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


NATIVE_UPLOAD_GRANT_TTL = timedelta(minutes=15)
NATIVE_MEDIA_TARGETS = {
    NativeUploadGrant.Target.PROJECT_MEDIA,
    NativeUploadGrant.Target.SITE_VISIT_MEDIA,
    NativeUploadGrant.Target.DAILY_REPORT_MEDIA,
    NativeUploadGrant.Target.PROBLEM_MEDIA,
    NativeUploadGrant.Target.CHANGE_ORDER_MEDIA,
    NativeUploadGrant.Target.EXISTING_CONDITION,
}


def _native_token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _native_upload_target(user, payload):
    target = str(payload.get("target") or "").strip()
    if target not in set(NativeUploadGrant.Target.values):
        raise ValidationError("Unsupported native upload target.")
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        raise ValidationError("A project is required for native uploads.")
    project = get_object_or_404(visible_projects(user), pk=project_id)
    target_id = str(payload.get("target_id") or "").strip()

    if target in NATIVE_MEDIA_TARGETS:
        if not can_submit_field_work(user, project):
            raise PermissionDenied("You cannot upload field media for this project.")
    elif target == NativeUploadGrant.Target.PROJECT_DOCUMENT:
        if not is_staff_user(user) or not can_view_project(user, project):
            raise PermissionDenied("You cannot upload project documents.")

    related = None
    if target in {
        NativeUploadGrant.Target.SITE_VISIT_MEDIA,
        NativeUploadGrant.Target.EXISTING_CONDITION,
    }:
        if not target_id:
            raise ValidationError("A site visit is required for this upload.")
        related = get_object_or_404(
            SiteVisit.objects.filter(project=project),
            pk=target_id,
        )
    elif target == NativeUploadGrant.Target.DAILY_REPORT_MEDIA:
        if not target_id:
            raise ValidationError("A daily report is required for this upload.")
        related = get_object_or_404(
            DailyReport.objects.filter(project=project),
            pk=target_id,
        )
    elif target == NativeUploadGrant.Target.PROBLEM_MEDIA:
        if not target_id:
            raise ValidationError("A problem report is required for this upload.")
        related = get_object_or_404(
            ProblemReport.objects.filter(project=project),
            pk=target_id,
        )
    elif target == NativeUploadGrant.Target.CHANGE_ORDER_MEDIA:
        if not target_id:
            raise ValidationError("A change order is required for this upload.")
        related = get_object_or_404(
            ChangeOrder.objects.filter(project=project),
            pk=target_id,
        )
        if related.status == ChangeOrder.Status.DRAFT and not can_manage_construction(user, project):
            raise PermissionDenied("Draft change orders are restricted.")
    elif target not in {
        NativeUploadGrant.Target.PROJECT_MEDIA,
        NativeUploadGrant.Target.PROJECT_DOCUMENT,
    }:
        raise ValidationError("A related construction record is required.")

    filename = sanitize_uploaded_name(payload.get("file_name"))
    extension = Path(filename).suffix.lower()
    document_target = target == NativeUploadGrant.Target.PROJECT_DOCUMENT
    allowed_extensions = ALLOWED_DOCUMENT_EXTENSIONS if document_target else ALLOWED_UPLOAD_EXTENSIONS
    if extension not in allowed_extensions:
        raise ValidationError("This file type is not supported for the selected upload target.")
    try:
        file_size = int(payload.get("file_size"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("A valid file size is required.") from exc
    if file_size < 1 or file_size > MAX_UPLOAD_SIZE:
        raise ValidationError("Files must be 50 MB or smaller.")

    requested_visibility = str(payload.get("visibility") or MediaAsset.Visibility.INTERNAL)
    allowed_visibility = {
        MediaAsset.Visibility.PUBLIC,
        MediaAsset.Visibility.CLIENT,
        MediaAsset.Visibility.INTERNAL,
    }
    if requested_visibility not in allowed_visibility:
        requested_visibility = MediaAsset.Visibility.INTERNAL
    if document_target and requested_visibility == MediaAsset.Visibility.PUBLIC:
        requested_visibility = MediaAsset.Visibility.CLIENT
    if not (is_owner(user) or is_manager(user)):
        requested_visibility = MediaAsset.Visibility.INTERNAL

    context_by_target = {
        NativeUploadGrant.Target.EXISTING_CONDITION: MediaAsset.Context.EXISTING_CONDITION,
        NativeUploadGrant.Target.SITE_VISIT_MEDIA: MediaAsset.Context.SITE_VISIT,
        NativeUploadGrant.Target.DAILY_REPORT_MEDIA: MediaAsset.Context.DAILY_REPORT,
        NativeUploadGrant.Target.PROBLEM_MEDIA: MediaAsset.Context.PROBLEM_REPORT,
        NativeUploadGrant.Target.CHANGE_ORDER_MEDIA: MediaAsset.Context.CHANGE_ORDER,
    }
    context = context_by_target.get(
        target,
        str(payload.get("context") or MediaAsset.Context.PROGRESS),
    )
    if context not in set(MediaAsset.Context.values):
        context = MediaAsset.Context.PROGRESS
    return {
        "project": project,
        "target": target,
        "target_id": target_id,
        "related": related,
        "visibility": (
            requested_visibility
            if document_target
            else requested_visibility
        ),
        "context": context,
        "file_name": filename[:255],
        "content_type": str(payload.get("content_type") or "")[:120],
        "file_size": file_size,
    }


def _native_upload_result(grant, *, replay=False):
    if grant.media_asset_id:
        asset = grant.media_asset
        return {
            "uploaded": True,
            "replayed": replay,
            "kind": "media",
            "id": str(asset.pk),
            "project_id": str(grant.project_id),
            "url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
            "visibility": asset.visibility,
        }
    if grant.project_document_id:
        document = grant.project_document
        return {
            "uploaded": True,
            "replayed": replay,
            "kind": "document",
            "id": str(document.pk),
            "project_id": str(grant.project_id),
            "url": reverse("operations:document-file", kwargs={"pk": document.pk}),
            "visibility": document.visibility,
        }
    return {"uploaded": False, "replayed": replay}


@never_cache
@require_POST
@_api_access
def api_v1_native_upload_grant(request):
    if not feature_enabled("native_media"):
        return _error("Native media uploads are disabled.", 404)
    try:
        payload = _body(request)
        target = _native_upload_target(request.user, payload)
        raw_token = secrets.token_urlsafe(32)
        grant = NativeUploadGrant.objects.create(
            token_hash=_native_token_hash(raw_token),
            actor=request.user,
            project=target["project"],
            target=target["target"],
            target_object_id=target["target_id"],
            visibility=target["visibility"],
            context=target["context"],
            idempotency_key=str(uuid.uuid4()),
            original_name=target["file_name"],
            content_type=target["content_type"],
            file_size=target["file_size"],
            expires_at=timezone.now() + NATIVE_UPLOAD_GRANT_TTL,
        )
        return JsonResponse({
            "grant_token": raw_token,
            "grant_id": str(grant.pk),
            "idempotency_key": grant.idempotency_key,
            "upload_url": request.build_absolute_uri(
                reverse("operations-api:native-upload-complete")
            ),
            "expires_at": grant.expires_at.isoformat(),
            "file_name": grant.original_name,
            "file_size": grant.file_size,
            "target": grant.target,
        }, status=201)
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


def _native_related_record(grant):
    if grant.target in {
        NativeUploadGrant.Target.SITE_VISIT_MEDIA,
        NativeUploadGrant.Target.EXISTING_CONDITION,
    }:
        return SiteVisit.objects.select_for_update().filter(
            pk=grant.target_object_id,
            project=grant.project,
        ).first()
    if grant.target == NativeUploadGrant.Target.DAILY_REPORT_MEDIA:
        return DailyReport.objects.select_for_update().filter(
            pk=grant.target_object_id,
            project=grant.project,
        ).first()
    if grant.target == NativeUploadGrant.Target.PROBLEM_MEDIA:
        return ProblemReport.objects.select_for_update().filter(
            pk=grant.target_object_id,
            project=grant.project,
        ).first()
    if grant.target == NativeUploadGrant.Target.CHANGE_ORDER_MEDIA:
        return ChangeOrder.objects.select_for_update().filter(
            pk=grant.target_object_id,
            project=grant.project,
        ).first()
    return None


@csrf_exempt
@never_cache
@require_POST
@_api_rate_limit
def api_v1_native_upload_complete(request):
    raw_token = (
        request.headers.get("X-Grand-Coast-Upload-Token")
        or request.POST.get("grant_token")
        or ""
    ).strip()
    if len(raw_token) < 20 or len(raw_token) > 200:
        return _error("Upload grant is invalid or expired.", 401)
    grant = NativeUploadGrant.objects.filter(
        token_hash=_native_token_hash(raw_token),
    ).select_related("project", "actor", "media_asset", "project_document").first()
    if grant is None:
        return _error("Upload grant is invalid or expired.", 401)

    with transaction.atomic():
        grant = NativeUploadGrant.objects.select_for_update().select_related(
            "project",
            "actor",
            "media_asset",
            "project_document",
        ).get(pk=grant.pk)
        if grant.used_at:
            return JsonResponse(_native_upload_result(grant, replay=True))
        if not grant.actor.is_active or grant.expires_at <= timezone.now():
            return _error("Upload grant is invalid or expired.", 410)
        upload = request.FILES.get("file")
        if upload is None:
            return _error("A file is required.", 400)
        if upload.size < 1 or upload.size > MAX_UPLOAD_SIZE:
            return _error("Files must be 50 MB or smaller.", 400)
        if grant.file_size and upload.size != grant.file_size:
            return _error("The upload size does not match the prepared file.", 400)
        upload.name = sanitize_uploaded_name(upload.name)
        extension = Path(upload.name).suffix.lower()
        prepared_extension = Path(grant.original_name).suffix.lower()
        if not prepared_extension or extension != prepared_extension:
            return _error("The upload does not match the prepared file type.", 400)
        try:
            if grant.target == NativeUploadGrant.Target.PROJECT_DOCUMENT:
                validate_construction_document(upload)
            else:
                if extension not in ALLOWED_UPLOAD_EXTENSIONS:
                    raise ValidationError("This media file type is not supported.")
                validate_contact_upload(upload)
        except ValidationError as exc:
            return _error(exc, 400)

        related = _native_related_record(grant)
        if grant.target in {
            NativeUploadGrant.Target.SITE_VISIT_MEDIA,
            NativeUploadGrant.Target.EXISTING_CONDITION,
            NativeUploadGrant.Target.DAILY_REPORT_MEDIA,
            NativeUploadGrant.Target.PROBLEM_MEDIA,
            NativeUploadGrant.Target.CHANGE_ORDER_MEDIA,
        } and related is None:
            return _error("The related construction record no longer exists.", 404)

        if grant.target == NativeUploadGrant.Target.PROJECT_DOCUMENT:
            document = ProjectDocument.objects.create(
                project=grant.project,
                title=Path(grant.original_name).stem[:180] or "Project document",
                category="Field upload",
                description="Uploaded from the Grand Coast mobile workspace.",
                file=upload,
                visibility=grant.visibility if grant.visibility in {
                    ProjectDocument.Visibility.CLIENT,
                    ProjectDocument.Visibility.INTERNAL,
                } else ProjectDocument.Visibility.INTERNAL,
                uploaded_by=grant.actor,
            )
            grant.project_document = document
            result_kind = "document"
            result_id = document.pk
        else:
            media_type = (
                MediaAsset.MediaType.VIDEO
                if extension in {".mp4", ".mov", ".webm"}
                else MediaAsset.MediaType.PHOTO
            )
            asset = MediaAsset.objects.create(
                project=grant.project,
                site_visit=(
                    related
                    if grant.target in {
                        NativeUploadGrant.Target.SITE_VISIT_MEDIA,
                        NativeUploadGrant.Target.EXISTING_CONDITION,
                    }
                    else None
                ),
                title=Path(grant.original_name).stem[:180] or "Project media",
                file=upload,
                media_type=media_type,
                context=grant.context,
                visibility=grant.visibility,
                uploaded_by=grant.actor,
            )
            if grant.target == NativeUploadGrant.Target.DAILY_REPORT_MEDIA:
                related.media_assets.add(asset)
            elif grant.target == NativeUploadGrant.Target.PROBLEM_MEDIA:
                related.supporting_media.add(asset)
            elif grant.target == NativeUploadGrant.Target.CHANGE_ORDER_MEDIA:
                related.supporting_media.add(asset)
            grant.media_asset = asset
            result_kind = "media"
            result_id = asset.pk

        grant.used_at = timezone.now()
        grant.save(update_fields=["media_asset", "project_document", "used_at", "updated_at"])
        WorkflowEvent.objects.create(
            idempotency_key=f"native-upload:{grant.idempotency_key}",
            event_type="native_media_uploaded",
            source="native",
            actor=grant.actor,
            project=grant.project,
            related_model="MediaAsset" if result_kind == "media" else "ProjectDocument",
            related_id=str(result_id),
            after_state={
                "target": grant.target,
                "file_name": grant.original_name,
                "visibility": grant.visibility,
            },
        )
        Activity.objects.create(
            message="Native project file uploaded",
            detail=f"{grant.original_name} · {grant.get_target_display()}",
            actor=grant.actor,
            project=grant.project,
        )
        return JsonResponse(_native_upload_result(grant))


def _date(value, *, required=False):
    if value in (None, "") and not required:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("Use an ISO date such as 2026-09-03.") from exc


def _datetime(value, *, required=False):
    if value in (None, "") and not required:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValidationError("Use an ISO date and time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _lead_data(lead, *, include_detail=False):
    data = {
        "id": str(lead.pk),
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "location": lead.location,
        "service": lead.service,
        "status": lead.status,
        "status_label": lead.get_status_display(),
        "workflow_stage": lead.workflow_stage,
        "budget_amount": str(lead.budget_amount) if lead.budget_amount is not None else None,
        "next_action": lead.next_action,
        "next_action_due": lead.next_action_due.isoformat() if lead.next_action_due else None,
        "assigned_to": lead.assigned_to.get_full_name() if lead.assigned_to_id else None,
        "referral_source": lead.source,
        "budget": lead.budget,
        "description": lead.note,
        "address": {
            "line1": lead.address_line1,
            "line2": lead.address_line2,
            "city": lead.city,
            "state": lead.state,
            "postal_code": lead.postal_code,
        },
    }
    if include_detail:
        data["site_visits"] = [
            {
                "id": str(visit.pk),
                "scheduled_at": visit.scheduled_at.isoformat() if visit.scheduled_at else None,
                "completed_at": visit.completed_at.isoformat() if visit.completed_at else None,
                "status": visit.status,
                "address": visit.address,
                "scope": visit.scope,
                "measurements": visit.measurements,
                "client_requests": visit.client_requests,
                "existing_conditions": visit.existing_conditions,
                "potential_additional_work": visit.potential_additional_work,
                "notes": visit.notes,
                "assigned_to": visit.assigned_to.get_full_name() if visit.assigned_to_id else None,
            }
            for visit in lead.site_visits.select_related("assigned_to").all()
        ]
    return data


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_leads(request):
    if request.method == "POST":
        try:
            payload = _body(request)
            key = _required_idempotency_key(request, payload)
            address_payload = payload.get("address")
            if not isinstance(address_payload, dict):
                address_payload = {}
            assigned_to = None
            if payload.get("assigned_to_id") not in (None, ""):
                from django.contrib.auth import get_user_model

                user_model = get_user_model()
                try:
                    assigned_to = user_model.objects.get(
                        pk=payload["assigned_to_id"],
                        is_active=True,
                    )
                except (TypeError, ValueError, user_model.DoesNotExist) as exc:
                    raise ValidationError("Assigned staff member was not found.") from exc
            lead, created = create_lead(
                actor=request.user,
                name=payload.get("name"),
                email=payload.get("email"),
                phone=payload.get("phone"),
                service=payload.get("service") or payload.get("project_type"),
                location=payload.get("location"),
                budget=payload.get("budget"),
                budget_amount=_decimal(payload.get("budget_amount"), required=False, minimum=Decimal("0.00")),
                timeline=payload.get("timeline"),
                source=payload.get("source"),
                note=payload.get("note") or payload.get("description"),
                assigned_to=assigned_to,
                address_line1=payload.get("address_line1") or address_payload.get("line1"),
                address_line2=payload.get("address_line2") or address_payload.get("line2"),
                city=payload.get("city") or address_payload.get("city"),
                state=payload.get("state") or address_payload.get("state"),
                postal_code=payload.get("postal_code") or address_payload.get("postal_code"),
                idempotency_key=key,
            )
            return JsonResponse(
                {"created": created, "lead": _lead_data(lead)},
                status=201 if created else 200,
            )
        except PermissionDenied as exc:
            return _error(exc, 403)
        except ValidationError as exc:
            return _error(exc, 400)
    leads = visible_leads(request.user).select_related("assigned_to", "client")
    return JsonResponse({"results": [_lead_data(lead) for lead in leads[:200]]})


@never_cache
@require_GET
@_api_access
def api_v1_lead_detail(request, pk):
    lead = get_object_or_404(
        visible_leads(request.user).select_related("assigned_to", "client"),
        pk=pk,
    )
    return JsonResponse({"lead": _lead_data(lead, include_detail=True)})


@never_cache
@require_POST
@_api_access
def api_v1_lead_convert_client(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        lead = get_object_or_404(visible_leads(request.user), pk=pk)
        client, created = convert_lead(
            lead,
            actor=request.user,
            idempotency_key=key,
        )
        return JsonResponse({
            "created": created,
            "lead_id": str(lead.pk),
            "client": {
                "id": str(client.pk),
                "name": client.name,
                "email": client.email,
            },
        })
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_lead_site_visits(request, pk):
    lead = get_object_or_404(visible_leads(request.user), pk=pk)
    if request.method == "GET":
        return JsonResponse({
            "results": [
                {
                    "id": str(visit.pk),
                    "scheduled_at": visit.scheduled_at.isoformat() if visit.scheduled_at else None,
                    "completed_at": visit.completed_at.isoformat() if visit.completed_at else None,
                    "status": visit.status,
                    "address": visit.address,
                    "scope": visit.scope,
                    "measurements": visit.measurements,
                    "client_requests": visit.client_requests,
                    "existing_conditions": visit.existing_conditions,
                    "potential_additional_work": visit.potential_additional_work,
                    "notes": visit.notes,
                    "assigned_to": visit.assigned_to.get_full_name() if visit.assigned_to_id else None,
                }
                for visit in lead.site_visits.select_related("assigned_to").all()
            ]
        })
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        assigned_to = None
        if payload.get("assigned_to_id") not in (None, ""):
            from django.contrib.auth import get_user_model

            user_model = get_user_model()
            try:
                assigned_to = user_model.objects.get(
                    pk=payload["assigned_to_id"],
                    is_active=True,
                )
            except (TypeError, ValueError, user_model.DoesNotExist) as exc:
                raise ValidationError("Assigned staff member was not found.") from exc
        visit, created = create_site_visit(
            lead,
            actor=request.user,
            assigned_to=assigned_to,
            scheduled_at=_datetime(payload.get("scheduled_at")),
            address=payload.get("address", ""),
            scope=payload.get("scope", ""),
            notes=payload.get("notes", ""),
            idempotency_key=key,
        )
        return JsonResponse(
            {
                "created": created,
                "site_visit": {
                    "id": str(visit.pk),
                    "status": visit.status,
                    "scheduled_at": visit.scheduled_at.isoformat() if visit.scheduled_at else None,
                },
            },
            status=201 if created else 200,
        )
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


def _project_data(project, user, *, include_detail=False):
    data = {
        "id": str(project.pk),
        "title": project.title,
        "location": project.location,
        "project_type": project.project_type,
        "status": project.status,
        "status_label": project.get_status_display(),
        "operational_phase": project.operational_phase,
        "health_status": project.health_status if not is_client(user) else "",
        "health_note": project.health_note if not is_client(user) else "",
        "address": {
            "line1": project.address_line1,
            "line2": project.address_line2,
            "city": project.city,
            "state": project.state,
            "postal_code": project.postal_code,
        },
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "target_date": project.target_date.isoformat() if project.target_date else None,
        "progress_percent": project.progress_percent,
        "next_step": project.next_step,
        "client_name": project.client.name if project.client_id else None,
        "project_manager": project.project_manager.get_full_name() if project.project_manager_id else None,
    }
    if can_view_financials(user, project):
        data["financials"] = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in project_financial_summary(project).items()
        }
    if not include_detail:
        return data
    readiness = project.readiness_items.all()
    if is_subcontractor(user):
        readiness = readiness.none()
    elif is_field(user):
        readiness = readiness.filter(category="schedule")
    data["readiness"] = [
        {
            "id": str(item.pk),
            "key": item.key,
            "label": item.label,
            "category": item.category,
            "status": item.status,
            "required": item.required,
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "notes": item.notes if not is_client(user) else "",
        }
        for item in readiness
    ]
    blockers = project.blockers.filter(status=Blocker.Status.OPEN)
    if is_client(user):
        blockers = blockers.filter(category=Blocker.Category.CLIENT_DECISION)
    elif is_subcontractor(user):
        blockers = blockers.none()
    elif is_field(user):
        blockers = blockers.filter(
            Q(assigned_to=user)
            | Q(category__in=[
                Blocker.Category.INSPECTION,
                Blocker.Category.MATERIAL,
                Blocker.Category.SUBCONTRACTOR,
                Blocker.Category.SCHEDULE,
            ])
        )
    data["blockers"] = [
        {
            "id": str(item.pk),
            "title": item.title,
            "description": item.description,
            "category": item.category,
            "severity": item.severity,
            "status": item.status,
            "due_date": item.due_date.isoformat() if item.due_date else None,
        }
        for item in blockers
    ]
    selections = project.selections.all()
    if is_subcontractor(user):
        selections = selections.none()
    data["selections"] = [
        {
            "id": str(item.pk),
            "category": item.category,
            "item_name": item.item_name,
            "description": item.description if not is_client(user) else "",
            "vendor": item.vendor if not is_client(user) else "",
            "status": item.status,
            "client_choice": item.client_choice,
            "due_date": item.due_date.isoformat() if item.due_date else None,
        }
        for item in selections
    ]
    change_orders = project.change_orders.all()
    if is_client(user):
        change_orders = change_orders.exclude(status=ChangeOrder.Status.DRAFT)
    elif is_subcontractor(user):
        change_orders = change_orders.none()
    data["change_orders"] = [
        {
            "id": str(item.pk),
            "number": item.number,
            "title": item.title,
            "description": item.description,
            "price_impact": (
                str(item.price_impact)
                if is_client(user) or can_view_financials(user, project)
                else None
            ),
            "schedule_impact_days": item.schedule_impact_days,
            "status": item.status,
        }
        for item in change_orders
        if not is_field(user) or item.status != ChangeOrder.Status.DRAFT
    ]
    inspections = (
        project.inspections.all()
        if not is_subcontractor(user)
        else project.inspections.none()
    )
    data["inspections"] = [
        {
            "id": str(item.pk),
            "inspection_type": item.inspection_type,
            "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
            "status": item.status,
            "result_notes": item.result_notes if not (is_field(user) or is_client(user)) else "",
            "corrective_action": item.corrective_action if not (is_field(user) or is_client(user)) else "",
        }
        for item in inspections
    ]
    payment_schedules = (
        project.payment_schedules.all()
        if is_client(user) or can_view_financials(user, project)
        else project.payment_schedules.none()
    )
    data["payment_schedule"] = [
        {
            "id": str(item.pk),
            "description": item.description,
            "amount": str(item.amount),
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "status": item.status,
        }
        for item in payment_schedules
    ]
    data["permits"] = [
        {
            "id": str(item.pk),
            "permit_type": item.permit_type,
            "jurisdiction": item.jurisdiction,
            "permit_number": item.permit_number,
            "status": item.status,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "notes": item.notes if not is_client(user) else "",
        }
        for item in project.permits.all()
        if can_view_permit(user, item)
    ]
    data["closeout"] = [
        {
            "id": str(item.pk),
            "key": item.key,
            "label": item.label,
            "category": item.category,
            "status": item.status,
            "required": item.required,
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "notes": item.notes if not is_client(user) else "",
        }
        for item in project.closeout_items.all()
        if can_view_closeout(user, item)
    ]
    assignments = project.subcontractor_assignments.select_related("subcontractor", "task")
    if is_subcontractor(user):
        assignments = assignments.filter(subcontractor__portal_user=user)
    data["assignments"] = [
        {
            "id": str(item.pk),
            "work_package": item.work_package,
            "scope": item.scope,
            "start_date": item.start_date.isoformat() if item.start_date else None,
            "end_date": item.end_date.isoformat() if item.end_date else None,
            "status": item.status,
            "task_id": str(item.task_id) if item.task_id else None,
        }
        for item in assignments
        if can_view_subcontractor_assignment(user, item)
    ]
    data["documents"] = [
        {
            "id": str(document.pk),
            "title": document.title,
            "category": document.category,
            "description": document.description,
            "visibility": document.visibility,
            "created_at": document.created_at.isoformat(),
            "url": reverse("operations:document-file", kwargs={"pk": document.pk}),
        }
        for document in project.documents.all()
        if can_view_project_document(user, document)
    ]
    data["media"] = [
        {
            "id": str(asset.pk),
            "title": asset.title,
            "media_type": asset.media_type,
            "context": asset.context,
            "site_visit_id": str(asset.site_visit_id) if asset.site_visit_id else None,
            "visibility": asset.visibility,
            "caption": asset.caption,
            "created_at": asset.created_at.isoformat(),
            "url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
        }
        for asset in project.media_assets.all()
        if can_view_media(user, asset)
    ]
    if is_client(user):
        data["updates"] = [
            {
                "id": str(update.pk),
                "title": update.title,
                "body": update.body,
                "created_at": update.created_at.isoformat(),
            }
            for update in project.updates.filter(visibility="client")
        ]
    if is_staff_user(user) and not is_client(user):
        data["tasks"] = [
            {
                "id": str(task.pk),
                "title": task.title,
                "description": task.description,
                "status": task.status,
                "priority": task.priority,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "assigned_to": task.assigned_to.get_full_name() if task.assigned_to_id else None,
            }
            for task in project.tasks.select_related("assigned_to").all()
            if not is_field(user)
            or task.assigned_to_id == user.pk
            or task.watchers.filter(pk=user.pk).exists()
        ]
    return data


def _problem_data(item, user):
    return {
        "id": str(item.pk),
        "project_id": str(item.project_id),
        "task_id": str(item.task_id) if item.task_id else None,
        "title": item.title,
        "description": item.description,
        "severity": item.severity,
        "status": item.status,
        "resolution": item.resolution if not is_field(user) else "",
        "created_at": item.created_at.isoformat(),
        "reported_by": item.reported_by.get_full_name() or item.reported_by.get_username(),
    }


def _permit_data(item, user):
    return {
        "id": str(item.pk),
        "project_id": str(item.project_id),
        "permit_type": item.permit_type,
        "jurisdiction": item.jurisdiction,
        "permit_number": item.permit_number,
        "status": item.status,
        "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "notes": item.notes if not (is_client(user) or is_field(user)) else "",
    }


def _closeout_data(item, user):
    return {
        "id": str(item.pk),
        "project_id": str(item.project_id),
        "key": item.key,
        "label": item.label,
        "category": item.category,
        "status": item.status,
        "required": item.required,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "notes": item.notes if not is_client(user) else "",
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def _assignment_data(item):
    return {
        "id": str(item.pk),
        "project_id": str(item.project_id),
        "work_package": item.work_package,
        "scope": item.scope,
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "end_date": item.end_date.isoformat() if item.end_date else None,
        "status": item.status,
        "task_id": str(item.task_id) if item.task_id else None,
    }


def _visible_estimates(user):
    return visible_estimates(user)


@never_cache
@require_GET
@_api_access
def api_v1_me(request):
    from .construction_policies import ai_retrieval_scope, role_label

    scope = ai_retrieval_scope(request.user)
    return JsonResponse({
        "user_id": str(request.user.pk),
        "display_name": request.user.get_full_name() or request.user.get_username(),
        "role": role_label(request.user),
        "financial_access": scope["financials"],
        "project_count": len(scope["project_ids"]),
    })


@never_cache
@require_GET
@_api_access
def api_v1_projects(request):
    projects = visible_projects(request.user).select_related("client", "project_manager")
    return JsonResponse({"results": [_project_data(project, request.user) for project in projects]})


@never_cache
@require_GET
@_api_access
def api_v1_estimates(request):
    estimates = _visible_estimates(request.user).select_related("client", "lead")
    return JsonResponse({
        "results": [
            {
                "id": str(item.pk),
                "number": item.number,
                "title": item.title,
                "status": item.status,
                "total": str(item.total),
                "deposit_amount": str(item.deposit_amount),
                "client_name": item.client.name if item.client_id else None,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                "accepted_at": item.accepted_at.isoformat() if item.accepted_at else None,
            }
            for item in estimates
        ]
    })


@never_cache
@require_POST
@_api_access
def api_v1_estimate_send(request, pk):
    try:
        get_object_or_404(_visible_estimates(request.user), pk=pk)
        return _error(
            "GCC no longer sends internal estimates. Send the saved external estimate link from the Owner workspace.",
            410,
        )
    except PermissionDenied as exc:
        return _error(exc, 403)


@never_cache
@require_POST
@_api_access
def api_v1_estimate_accept(request, pk):
    try:
        get_object_or_404(_visible_estimates(request.user), pk=pk)
        return _error(
            "Estimate responses are completed in the external estimate service. GCC records the result after staff verification.",
            410,
        )
    except PermissionDenied as exc:
        return _error(exc, 403)


@never_cache
@require_POST
@_api_access
def api_v1_estimate_project(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        estimate = get_object_or_404(_visible_estimates(request.user), pk=pk)
        project, created = create_project_from_estimate(
            estimate,
            actor=request.user,
            idempotency_key=key,
        )
        return JsonResponse({"created": created, "project_id": str(project.pk)})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_GET
@_api_access
def api_v1_project_agreement(request, pk):
    project = get_object_or_404(
        visible_projects(request.user).select_related("client"),
        pk=pk,
    )
    agreement = get_object_or_404(Agreement.objects.select_related("project"), project=project)
    if not (is_client(request.user) or can_view_financials(request.user, project)):
        return _error("Agreement access denied.", 403)
    return JsonResponse({
        "agreement": {
            "id": str(agreement.pk),
            "project_id": str(project.pk),
            "status": agreement.status,
            "status_label": agreement.get_status_display(),
            "contract_value": str(agreement.current_contract_value),
            "deposit_amount": str(agreement.deposit_amount),
            "issued_at": agreement.issued_at.isoformat() if agreement.issued_at else None,
            "accepted_at": agreement.accepted_at.isoformat() if agreement.accepted_at else None,
            "signed_pdf_available": bool(agreement.signed_pdf),
            "signed_pdf_url": (
                reverse("operations:agreement-file", kwargs={"pk": agreement.pk})
                if agreement.signed_pdf and can_view_agreement(request.user, agreement)
                else None
            ),
        }
    })


@never_cache
@require_POST
@_api_access
def api_v1_agreement_accept(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        agreement = get_object_or_404(
            Agreement.objects.select_related("project", "project__client"),
            pk=pk,
        )
        if not can_view_project(request.user, agreement.project):
            return _error("Record not found.", 404)
        agreement, accepted = accept_agreement(
            agreement,
            actor=request.user,
            request=request,
            idempotency_key=key,
        )
        return JsonResponse({"accepted": accepted, "status": agreement.status})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_GET
@_api_access
def api_v1_project_summary(request, pk):
    project = get_object_or_404(
        visible_projects(request.user).select_related("client", "project_manager", "estimate"),
        pk=pk,
    )
    return JsonResponse({"project": _project_data(project, request.user, include_detail=True)})


@never_cache
@require_GET
@_api_access
def api_v1_project_tasks(request, pk):
    if not is_staff_user(request.user):
        return _error("Task access denied.", 403)
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    tasks = project.tasks.select_related("assigned_to")
    if is_field(request.user):
        tasks = tasks.filter(
            Q(assigned_to=request.user) | Q(watchers=request.user)
        ).distinct()
    return JsonResponse({
        "results": [
            {
                "id": str(task.pk),
                "title": task.title,
                "description": task.description,
                "status": task.status,
                "priority": task.priority,
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "assigned_to": task.assigned_to.get_full_name() if task.assigned_to_id else None,
            }
            for task in tasks[:300]
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_project_blockers(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    blockers = project.blockers.filter(status=Blocker.Status.OPEN)
    if is_client(request.user):
        blockers = blockers.filter(category=Blocker.Category.CLIENT_DECISION)
    elif is_subcontractor(request.user):
        return _error("Blocker access denied.", 403)
    return JsonResponse({
        "results": [
            {
                "id": str(item.pk),
                "title": item.title,
                "description": item.description,
                "category": item.category,
                "severity": item.severity,
                "status": item.status,
                "due_date": item.due_date.isoformat() if item.due_date else None,
            }
            for item in blockers[:300]
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_project_financials(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if not can_view_financials(request.user, project):
        return _error("Financial access denied.", 403)
    return JsonResponse({
        "project_id": str(project.pk),
        "financials": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in project_financial_summary(project).items()
        },
        "budget_lines": [
            {
                "id": str(line.pk),
                "description": line.description,
                "category": line.category,
                "cost_code": line.cost_code,
                "original_budget": str(line.original_budget),
                "approved_change": str(line.approved_change),
                "committed": str(line.committed),
                "actual": str(line.actual),
                "current_budget": str(line.current_budget),
                "remaining_budget": str(line.remaining_budget),
            }
            for line in project.budget_lines.all()
        ],
        "costs": [
            {
                "id": str(entry.pk),
                "description": entry.description,
                "vendor": entry.vendor,
                "amount": str(entry.amount),
                "incurred_on": entry.incurred_on.isoformat(),
                "source": entry.source,
                "is_void": entry.is_void,
            }
            for entry in project.cost_entries.all()
        ],
        "commitments": [
            {
                "id": str(commitment.pk),
                "description": commitment.description,
                "amount": str(commitment.amount),
                "status": commitment.status,
                "due_date": commitment.due_date.isoformat() if commitment.due_date else None,
            }
            for commitment in project.commitments.all()
        ],
    })


@never_cache
@require_GET
@_api_access
def api_v1_calendar(request):
    if not is_staff_user(request.user):
        return _error("Calendar access is available to staff only.", 403)
    try:
        start = _date(request.GET.get("start"))
        end = _date(request.GET.get("end"))
    except ValidationError as exc:
        return _error(exc, 400)
    projects = visible_projects(request.user)
    events = ScheduleEvent.objects.filter(
        Q(project__in=projects) | Q(project__isnull=True, assignees=request.user)
    ).select_related("project", "task").distinct()
    if is_field(request.user):
        events = events.filter(assignees=request.user)
    if start:
        events = events.filter(end_at__date__gte=start)
    if end:
        events = events.filter(start_at__date__lte=end)
    return JsonResponse({
        "results": [
            {
                "id": str(event.pk),
                "title": event.title,
                "project_id": str(event.project_id) if event.project_id else None,
                "project_title": event.project.title if event.project_id else None,
                "task_id": str(event.task_id) if event.task_id else None,
                "start_at": event.start_at.isoformat(),
                "end_at": event.end_at.isoformat(),
                "location": event.location,
                "notes": event.notes,
            }
            for event in events[:500]
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_project_documents(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    documents = project.documents.all()
    return JsonResponse({
        "results": [
            {
                "id": str(document.pk),
                "title": document.title,
                "category": document.category,
                "description": document.description,
                "visibility": document.visibility,
                "created_at": document.created_at.isoformat(),
                "url": reverse("operations:document-file", kwargs={"pk": document.pk}),
            }
            for document in documents
            if can_view_project_document(request.user, document)
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_project_media(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    media = project.media_assets.all()
    return JsonResponse({
        "results": [
            {
                "id": str(asset.pk),
                "title": asset.title,
                "media_type": asset.media_type,
                "context": asset.context,
                "site_visit_id": str(asset.site_visit_id) if asset.site_visit_id else None,
                "visibility": asset.visibility,
                "caption": asset.caption,
                "created_at": asset.created_at.isoformat(),
                "url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
            }
            for asset in media
            if can_view_media(request.user, asset)
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_project_messages(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if is_subcontractor(request.user) or is_field(request.user):
        return _error("Message access denied.", 403)
    if not is_client(request.user) and not can_manage_construction(request.user, project):
        return _error("Message access denied.", 403)
    return JsonResponse({
        "results": [
            {
                "id": str(message.pk),
                "body": message.body,
                "created_at": message.created_at.isoformat(),
                "sent_by": message.sent_by.get_full_name() if message.sent_by_id else None,
                "from_staff": bool(message.sent_by_id and message.sent_by.is_staff),
            }
            for message in project.messages.select_related("sent_by").all()[:300]
        ]
    })


@never_cache
@require_GET
@_api_access
def api_v1_notifications(request):
    if is_client(request.user):
        notifications = ClientNotification.objects.filter(
            client__user=request.user,
            dismissed_at__isnull=True,
        )
    elif is_staff_user(request.user):
        notifications = EmployeeNotification.objects.filter(employee=request.user)
    else:
        return _error("Notification access denied.", 403)
    return JsonResponse({
        "results": [
            {
                "id": str(notification.pk),
                "title": notification.title,
                "body": notification.body,
                "kind": notification.kind,
                "read_at": notification.read_at.isoformat() if notification.read_at else None,
                "created_at": notification.created_at.isoformat(),
                "project_id": str(notification.project_id) if notification.project_id else None,
                "destination_url": notification.destination_url,
            }
            for notification in notifications[:200]
        ]
    })


@never_cache
@require_POST
@_api_access
def api_v1_notification_read(request, pk):
    if is_client(request.user):
        notification = get_object_or_404(
            ClientNotification.objects.filter(
                client__user=request.user,
                dismissed_at__isnull=True,
            ),
            pk=pk,
        )
    elif is_staff_user(request.user):
        notification = get_object_or_404(
            EmployeeNotification.objects.filter(employee=request.user),
            pk=pk,
        )
    else:
        return _error("Notification access denied.", 403)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return JsonResponse({"id": str(notification.pk), "read_at": notification.read_at.isoformat()})


@never_cache
@require_GET
@_api_access
def api_v1_attention(request):
    if not is_staff_user(request.user):
        return _error("Attention items are available to staff only.", 403)
    from .construction_services import attention_feed

    items = []
    for item in attention_feed(request.user):
        items.append({
            "kind": item["kind"],
            "title": item["title"],
            "description": item["description"],
            "priority": item["priority"],
            "due_at": item["due_at"].isoformat() if hasattr(item["due_at"], "isoformat") else None,
            "project_id": str(item["project"].pk) if item["project"] else None,
            "lead_id": str(item["lead"].pk) if item["lead"] else None,
            "source": item["source"],
        })
    return JsonResponse({"results": items})


@never_cache
@require_POST
@_api_access
def api_v1_readiness_complete(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        item = get_object_or_404(PreconstructionItem.objects.select_related("project"), pk=pk)
        if not can_view_project(request.user, item.project):
            return _error("Record not found.", 404)
        item = complete_readiness_item(item, actor=request.user, notes=payload.get("notes"), idempotency_key=key)
        return JsonResponse({"item": {"id": str(item.pk), "status": item.status, "completed_at": item.completed_at.isoformat()}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_change_orders(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if request.method == "GET":
        return JsonResponse({
            "results": [
                {
                    "id": str(item.pk),
                    "number": item.number,
                    "title": item.title,
                    "description": item.description,
                    "price_impact": (
                        str(item.price_impact)
                        if is_client(request.user) or can_view_financials(request.user, project)
                        else None
                    ),
                    "schedule_impact_days": item.schedule_impact_days,
                    "status": item.status,
                }
                for item in project.change_orders.all()
                if not is_client(request.user) or item.status != ChangeOrder.Status.DRAFT
            ]
        })
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        if not can_manage_construction(request.user, project):
            return _error("You cannot create change orders for this project.", 403)
        title = str(payload.get("title", "")).strip()
        description = str(payload.get("description", "")).strip()
        if not title or not description:
            raise ValidationError("Title and description are required.")
        status = ChangeOrder.Status.SENT if payload.get("send") else ChangeOrder.Status.DRAFT
        change_order, created = create_change_order(
            project,
            actor=request.user,
            title=title,
            description=description,
            price_impact=_decimal(payload.get("price_impact", "0"), minimum=Decimal("-9999999999.99")),
            schedule_impact_days=int(payload.get("schedule_impact_days", 0) or 0),
            status=status,
            idempotency_key=key,
        )
        return JsonResponse({
            "created": created,
            "change_order": {
                "id": str(change_order.pk),
                "number": change_order.number,
                "status": change_order.status,
                "price_impact": str(change_order.price_impact),
            },
        }, status=201 if created else 200)
    except (TypeError, ValueError) as exc:
        return _error("Invalid change-order values.", 400)
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_POST
@_api_access
def api_v1_change_order_approve(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        change_order = get_object_or_404(
            ChangeOrder.objects.select_related("project", "project__client"),
            pk=pk,
        )
        if not can_view_project(request.user, change_order.project):
            return _error("Record not found.", 404)
        change_order, approved = approve_change_order(
            change_order,
            actor=request.user,
            request=request,
            idempotency_key=key,
        )
        return JsonResponse({"approved": approved, "status": change_order.status})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_payments(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if request.method == "GET":
        if not can_view_financials(request.user, project):
            return _error("Financial access denied.", 403)
        from .models import PaymentRecord

        return JsonResponse({
            "results": [
                {
                    "id": str(item.pk),
                    "amount": str(item.amount),
                    "received_on": item.received_on.isoformat(),
                    "method": item.method,
                    "reference": item.reference,
                }
                for item in PaymentRecord.objects.filter(project=project, voided_at__isnull=True)
            ]
        })
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        schedule = None
        if payload.get("schedule_id"):
            schedule = get_object_or_404(PaymentSchedule, pk=payload["schedule_id"], project=project)
        payment, created = record_payment(
            project,
            actor=request.user,
            amount=_decimal(payload.get("amount"), minimum=Decimal("0.01")),
            schedule=schedule,
            received_on=_date(payload.get("received_on")) or timezone.localdate(),
            method=str(payload.get("method", "other")),
            reference=str(payload.get("reference", ""))[:120],
            notes=str(payload.get("notes", "")),
            idempotency_key=key,
        )
        return JsonResponse({"created": created, "payment": {"id": str(payment.pk), "amount": str(payment.amount)}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_POST
@_api_access
def api_v1_selection_advance(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        selection = get_object_or_404(Selection.objects.select_related("project"), pk=pk)
        if not can_view_project(request.user, selection.project):
            return _error("Record not found.", 404)
        status = str(payload.get("status", ""))
        selection = advance_selection(
            selection,
            actor=request.user,
            status=status,
            client_choice=payload.get("client_choice"),
            idempotency_key=key,
        )
        return JsonResponse({"selection": {"id": str(selection.pk), "status": selection.status, "client_choice": selection.client_choice}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_POST
@_api_access
def api_v1_inspection_result(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        inspection = get_object_or_404(Inspection.objects.select_related("project"), pk=pk)
        if not can_view_project(request.user, inspection.project):
            return _error("Record not found.", 404)
        inspection = record_inspection_result(
            inspection,
            actor=request.user,
            status=str(payload.get("status", "")),
            result_notes=str(payload.get("result_notes", "")),
            corrective_action=str(payload.get("corrective_action", "")),
            rescheduled_at=_datetime(payload.get("rescheduled_at")),
            idempotency_key=key,
        )
        return JsonResponse({"inspection": {"id": str(inspection.pk), "status": inspection.status}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_POST
@_api_access
def api_v1_warranty_resolve(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        item = get_object_or_404(WarrantyItem.objects.select_related("project"), pk=pk)
        if not can_view_project(request.user, item.project):
            return _error("Record not found.", 404)
        item = resolve_warranty_item(
            item,
            actor=request.user,
            resolution=str(payload.get("resolution", "")),
            status=str(payload.get("status", WarrantyItem.Status.RESOLVED)),
            idempotency_key=key,
        )
        return JsonResponse({"warranty_item": {"id": str(item.pk), "status": item.status}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_POST
@_api_access
def api_v1_site_visit_complete(request, pk):
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        visit = get_object_or_404(SiteVisit.objects.select_related("lead", "project"), pk=pk)
        if not visible_leads(request.user).filter(pk=visit.lead_id).exists() and visit.assigned_to_id != request.user.pk:
            return _error("Record not found.", 404)
        visit = complete_site_visit(visit, actor=request.user, updates=payload, idempotency_key=key)
        return JsonResponse({"site_visit": {"id": str(visit.pk), "status": visit.status, "completed_at": visit.completed_at.isoformat()}})
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_daily_reports(request, pk):
    if not is_staff_user(request.user):
        return _error("Field reports are available to staff only.", 403)
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if request.method == "GET":
        return JsonResponse({
            "results": [
                {
                    "id": str(item.pk),
                    "report_date": item.report_date.isoformat(),
                    "summary": item.summary,
                    "work_completed": item.work_completed,
                    "submitted_by": item.submitted_by.get_full_name() or item.submitted_by.get_username(),
                    "status": item.status,
                }
                for item in project.daily_reports.select_related("submitted_by").all()
            ]
        })
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        summary = str(payload.get("summary", "")).strip()
        if not summary:
            raise ValidationError("A daily report summary is required.")
        report, created = submit_daily_report(
            project,
            actor=request.user,
            report_date=_date(payload.get("report_date"), required=True),
            summary=summary,
            work_completed=str(payload.get("work_completed", "")),
            labor_count=int(payload.get("labor_count", 0) or 0),
            hours_worked=_decimal(payload.get("hours_worked", "0"), minimum=Decimal("0.00")),
            weather=str(payload.get("weather", "")),
            equipment=str(payload.get("equipment", "")),
            notes=str(payload.get("notes", "")),
            idempotency_key=key,
        )
        return JsonResponse({"created": created, "daily_report": {"id": str(report.pk), "status": report.status}}, status=201 if created else 200)
    except (TypeError, ValueError) as exc:
        return _error("Invalid daily-report values.", 400)
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)


@never_cache
@require_http_methods(["GET", "POST"])
@_api_access
def api_v1_material_requests(request, pk):
    if not is_staff_user(request.user):
        return _error("Material requests are available to staff only.", 403)
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if request.method == "GET":
        return JsonResponse({
            "results": [
                {
                    "id": str(item.pk),
                    "description": item.description,
                    "quantity": item.quantity,
                    "needed_by": item.needed_by.isoformat() if item.needed_by else None,
                    "status": item.status,
                    "requested_by": item.requested_by.get_full_name() or item.requested_by.get_username(),
                }
                for item in project.material_requests.select_related("requested_by").all()
            ]
        })
    try:
        payload = _body(request)
        key = _required_idempotency_key(request, payload)
        description = str(payload.get("description", "")).strip()
        if not description:
            raise ValidationError("A material description is required.")
        request_record, created = request_material(
            project,
            actor=request.user,
            description=description,
            quantity=str(payload.get("quantity", "")),
            needed_by=_date(payload.get("needed_by")),
            vendor=str(payload.get("vendor", "")),
            notes=str(payload.get("notes", "")),
            idempotency_key=key,
        )
        return JsonResponse({"created": created, "material_request": {"id": str(request_record.pk), "status": request_record.status}}, status=201 if created else 200)
    except PermissionDenied as exc:
        return _error(exc, 403)
    except ValidationError as exc:
        return _error(exc, 400)
