"""Transactional commands for the Grand Coast operating system.

Views, APIs, notifications, and future mobile/AI clients call these commands
instead of changing construction records directly. Every command is safe to
retry where an idempotency key is supplied and records an append-only event.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode, urlparse

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from .construction_policies import (
    can_manage_project_operations,
    can_manage_external_estimate,
    can_manage_sales,
    can_submit_field_work,
    can_view_financials,
    can_view_estimate,
    can_view_lead,
    can_view_project,
    external_estimate_enabled_for,
    is_staff_user,
    is_field,
    is_client,
    is_manager,
    is_owner,
    visible_estimates,
    visible_leads,
    visible_projects,
)
from .models import (
    Agreement,
    Blocker,
    BudgetLine,
    ChangeOrder,
    CloseoutItem,
    Commitment,
    Client,
    CostEntry,
    DailyReport,
    EmailOutbox,
    Estimate,
    EstimateLineItem,
    Inspection,
    Lead,
    MaterialRequest,
    Milestone,
    PaymentRecord,
    PaymentSchedule,
    Permit,
    ProblemReport,
    PreconstructionItem,
    Project,
    ProjectDocument,
    Selection,
    SiteVisit,
    Subcontractor,
    SubcontractorAssignment,
    Task,
    WarrantyItem,
    WorkflowEvent,
    validate_construction_document,
)
from .notifications import queue_client_notifications, queue_employee_notifications
from .services import get_or_create_client_for_lead, record_activity


READINESS_TEMPLATE = (
    ("design", "design", "Architectural / design work"),
    ("engineering", "engineering", "Engineering complete"),
    ("permits", "permit", "Permits approved"),
    ("selections", "selection", "Client selections complete"),
    ("subcontractor-bids", "bid", "Subcontractor bids confirmed"),
    ("procurement", "procurement", "Material procurement plan"),
    ("long-lead-items", "long_lead", "Long-lead items ordered"),
    ("schedule", "schedule", "Construction schedule approved"),
    ("approvals", "approval", "Required approvals complete"),
)

CLOSEOUT_TEMPLATE = (
    ("final-inspection", "final_inspection", "Final inspection passed"),
    ("punch-list", "punch_list", "Punch list complete"),
    ("closeout-documents", "documents", "Closeout documents delivered"),
    ("client-walkthrough", "client_walkthrough", "Client final walkthrough"),
    ("final-invoice", "final_invoice", "Final invoice reconciled"),
    ("warranty", "warranty", "Warranty information delivered"),
)


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "pk"):
        return str(value.pk)
    return value


def state_snapshot(instance, fields):
    return {field: _json_value(getattr(instance, field, None)) for field in fields}


def _workflow_event_key(event_type, related, idempotency_key):
    """Bind a client retry key to the command and object it was meant for."""
    related_model = related.__class__.__name__ if related is not None else ""
    related_id = str(related.pk) if related is not None and getattr(related, "pk", None) else ""
    raw = f"{event_type}|{related_model}|{related_id}|{idempotency_key}"
    return f"evt:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _related_project(related=None, project=None):
    if project is not None:
        return project
    if isinstance(related, Project):
        return related
    project_id = getattr(related, "project_id", None)
    return Project.objects.filter(pk=project_id).first() if project_id else None


def record_workflow_event(
    event_type,
    *,
    actor=None,
    related=None,
    project=None,
    lead=None,
    estimate=None,
    source="web",
    before_state=None,
    after_state=None,
    metadata=None,
    idempotency_key=None,
    event_key_override=None,
):
    """Record a non-secret before/after event, returning an existing retry."""
    project = _related_project(related, project)
    if lead is None and isinstance(related, Lead):
        lead = related
    if estimate is None and isinstance(related, Estimate):
        estimate = related
    related_model = related.__class__.__name__ if related is not None else ""
    related_id = str(related.pk) if related is not None and getattr(related, "pk", None) else ""
    event_key = (
        event_key_override
        or _workflow_event_key(event_type, related, idempotency_key)
        if idempotency_key
        else str(uuid.uuid4())
    )
    if idempotency_key:
        existing = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing:
            return existing
    defaults = {
        "event_type": event_type,
        "source": source,
        "actor": actor,
        "lead": lead,
        "estimate": estimate,
        "project": project,
        "related_model": related_model,
        "related_id": related_id,
        "before_state": before_state or {},
        "after_state": after_state or {},
        "metadata": metadata or {},
    }
    try:
        event, _created = WorkflowEvent.objects.get_or_create(
            idempotency_key=event_key,
            defaults=defaults,
        )
    except IntegrityError:
        event = WorkflowEvent.objects.get(idempotency_key=event_key)
    return event


def initialize_readiness(project, *, actor=None):
    if actor is not None and not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot initialize readiness for this project.")
    created = []
    for key, category, label in READINESS_TEMPLATE:
        item, was_created = PreconstructionItem.objects.get_or_create(
            project=project,
            key=key,
            defaults={
                "category": category,
                "label": label,
                "owner": project.project_manager,
            },
        )
        if was_created:
            created.append(item)
    return created


def initialize_closeout(project, *, actor=None):
    if actor is not None and not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot initialize closeout for this project.")
    created = []
    for key, category, label in CLOSEOUT_TEMPLATE:
        item, was_created = CloseoutItem.objects.get_or_create(
            project=project,
            key=key,
            defaults={
                "category": category,
                "label": label,
                "owner": project.project_manager,
            },
        )
        if was_created:
            created.append(item)
    return created


def initialize_warranty(project, *, actor=None):
    if actor is not None and not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot initialize warranty for this project.")
    warranty_until = timezone.localdate() + timedelta(days=365)
    item, created = WarrantyItem.objects.get_or_create(
        project=project,
        title="Post-closeout warranty coverage",
        defaults={
            "description": "Track warranty requests and resolutions after closeout.",
            "status": WarrantyItem.Status.OPEN,
            "assigned_to": project.project_manager,
            "warranty_until": warranty_until,
        },
    )
    return item, created


@transaction.atomic
def convert_lead(lead, *, actor, idempotency_key=None):
    if not can_manage_sales(actor):
        raise PermissionDenied("You cannot convert this lead.")
    locked = Lead.objects.select_for_update().get(pk=lead.pk)
    if not visible_leads(actor).filter(pk=locked.pk).exists():
        raise PermissionDenied("You cannot access this lead.")
    event_key = (
        _workflow_event_key("lead_converted_to_client", locked, idempotency_key)
        if idempotency_key
        else None
    )
    conversion_was_recorded = bool(
        event_key and WorkflowEvent.objects.filter(idempotency_key=event_key).exists()
    )
    if conversion_was_recorded:
        locked.refresh_from_db()
        existing_client = locked.client or Client.objects.filter(email__iexact=locked.email).first()
        if existing_client:
            if locked.client_id != existing_client.pk:
                locked.client = existing_client
                locked.save(update_fields=["client", "updated_at"])
            return existing_client, False

    # A prior conversion event can outlive its Client record (for example, if
    # that record was deleted). Repair the lead/client link instead of letting
    # an idempotent retry return None to the caller.
    existing = locked.client or Client.objects.filter(email__iexact=locked.email).first()
    client_was_created = existing is None
    client = get_or_create_client_for_lead(locked, actor=actor)
    before = state_snapshot(locked, ["client_id", "workflow_stage", "next_action"])
    if locked.workflow_stage == Lead.WorkflowStage.NEW:
        locked.workflow_stage = Lead.WorkflowStage.CONTACTED
        locked.next_action = locked.next_action or "Prepare estimate"
        locked.save(update_fields=["workflow_stage", "next_action", "updated_at"])
    if conversion_was_recorded:
        record_activity(
            "Client record restored for converted lead",
            f"{locked.name} · {client.email}",
            actor=actor,
            lead=locked,
        )
    else:
        record_workflow_event(
            "lead_converted_to_client",
            actor=actor,
            related=locked,
            lead=locked,
            after_state={
                **state_snapshot(locked, ["client_id", "workflow_stage", "next_action"]),
                "client_created": client_was_created,
                "client_id": str(client.pk),
            },
            before_state=before,
            idempotency_key=idempotency_key,
        )
        record_activity(
            "Lead converted to client",
            f"{locked.name} · {client.email}",
            actor=actor,
            lead=locked,
        )
    return client, client_was_created


@transaction.atomic
def create_lead(
    *,
    actor,
    name,
    email,
    phone="",
    service="",
    location="",
    budget="",
    budget_amount=None,
    timeline="",
    source="",
    note="",
    assigned_to=None,
    address_line1="",
    address_line2="",
    city="",
    state="",
    postal_code="",
    idempotency_key=None,
):
    if not can_manage_sales(actor):
        raise PermissionDenied("You cannot create leads.")
    if idempotency_key:
        existing = Lead.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if not can_view_lead(actor, existing):
                raise PermissionDenied("You cannot access the lead for this idempotency key.")
            return existing, False
    name = str(name or "").strip()
    email = str(email or "").strip().lower()
    service = str(service or "").strip()
    location = str(location or "").strip()
    if not name or len(name) > 160:
        raise ValidationError("A lead name of 160 characters or fewer is required.")
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ValidationError("A valid lead email is required.") from exc
    if not service or len(service) > 120:
        raise ValidationError("A project type of 120 characters or fewer is required.")
    if not location or len(location) > 160:
        raise ValidationError("A project location of 160 characters or fewer is required.")
    if assigned_to is not None and not is_staff_user(assigned_to):
        raise ValidationError("Leads may only be assigned to active staff.")
    if budget_amount is not None:
        try:
            budget_amount = Decimal(str(budget_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValidationError("Budget amount must be a valid amount.") from exc
        if not budget_amount.is_finite() or budget_amount < 0 or budget_amount > Decimal("9999999999.99"):
            raise ValidationError("Budget amount is outside the supported range.")
    lead = Lead.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        name=name,
        email=email,
        phone=str(phone or "").strip()[:40],
        service=service,
        location=location,
        budget=str(budget or "").strip()[:120],
        budget_amount=budget_amount,
        timeline=str(timeline or "").strip()[:120],
        source=str(source or "").strip()[:120] or "Operations",
        note=str(note or "").strip()[:20000],
        assigned_to=assigned_to,
        address_line1=str(address_line1 or "").strip()[:180],
        address_line2=str(address_line2 or "").strip()[:180],
        city=str(city or "").strip()[:100],
        state=str(state or "").strip()[:40],
        postal_code=str(postal_code or "").strip()[:20],
        created_by=actor,
        workflow_stage=Lead.WorkflowStage.NEW,
    )
    record_workflow_event(
        "lead_created",
        actor=actor,
        related=lead,
        lead=lead,
        after_state={
            "lead_id": str(lead.pk),
            "workflow_stage": lead.workflow_stage,
            "assigned_to_id": str(assigned_to.pk) if assigned_to else None,
        },
        idempotency_key=idempotency_key,
    )
    record_activity("Lead created", f"{lead.name} · {lead.source}", actor=actor, lead=lead)
    return lead, True


@transaction.atomic
def create_site_visit(
    lead,
    *,
    actor,
    project=None,
    assigned_to=None,
    scheduled_at=None,
    address="",
    scope="",
    measurements="",
    client_requests="",
    existing_conditions="",
    potential_additional_work="",
    notes="",
    idempotency_key=None,
):
    if project is not None:
        if not can_manage_project_operations(actor, project):
            raise PermissionDenied("You cannot create site visits for this project.")
    elif not can_manage_sales(actor):
        raise PermissionDenied("You cannot create site visits.")
    locked_lead = Lead.objects.select_for_update().get(pk=lead.pk)
    if project is not None:
        if locked_lead.pk != project.lead_id:
            raise ValidationError("The site visit lead must belong to this project.")
    elif not visible_leads(actor).filter(pk=locked_lead.pk).exists():
        raise PermissionDenied("You cannot access this lead.")
    if idempotency_key:
        existing = SiteVisit.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.lead_id != locked_lead.pk or existing.project_id != getattr(project, "pk", None):
                raise ValidationError("Idempotency key is already used for another site visit.")
            return existing, False
    if assigned_to is not None and not is_staff_user(assigned_to):
        raise ValidationError("Site visits may only be assigned to active staff.")
    visit = SiteVisit.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        lead=locked_lead,
        project=project,
        assigned_to=assigned_to,
        scheduled_at=scheduled_at,
        address=str(address or "").strip()[:240],
        scope=str(scope or "").strip()[:10000],
        measurements=str(measurements or "").strip()[:10000],
        client_requests=str(client_requests or "").strip()[:10000],
        existing_conditions=str(existing_conditions or "").strip()[:10000],
        potential_additional_work=str(potential_additional_work or "").strip()[:10000],
        notes=str(notes or "").strip()[:10000],
        created_by=actor,
    )
    locked_lead.workflow_stage = Lead.WorkflowStage.SITE_VISIT
    locked_lead.next_action = "Complete site visit"
    locked_lead.next_action_due = scheduled_at.date() if scheduled_at else locked_lead.next_action_due
    locked_lead.save(update_fields=["workflow_stage", "next_action", "next_action_due", "updated_at"])
    record_workflow_event(
        "site_visit_created",
        actor=actor,
        related=visit,
        lead=locked_lead,
        after_state={
            "site_visit_id": str(visit.pk),
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "assigned_to_id": str(assigned_to.pk) if assigned_to else None,
        },
        idempotency_key=idempotency_key,
    )
    record_activity("Site visit scheduled", locked_lead.name, actor=actor, lead=locked_lead)
    return visit, True


@transaction.atomic
def complete_site_visit(site_visit, *, actor, updates=None, idempotency_key=None):
    if not (can_manage_sales(actor) or site_visit.assigned_to_id == getattr(actor, "pk", None)):
        raise PermissionDenied("You cannot complete this site visit.")
    visit = SiteVisit.objects.select_for_update().select_related("lead", "project").get(pk=site_visit.pk)
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("site_visit_completed", visit, idempotency_key),
    ).exists():
        return visit
    if visit.status == SiteVisit.Status.COMPLETED:
        return visit
    before = state_snapshot(
        visit,
        ["status", "scope", "measurements", "client_requests", "existing_conditions", "potential_additional_work"],
    )
    for field, value in (updates or {}).items():
        if field in {
            "scope",
            "measurements",
            "client_requests",
            "existing_conditions",
            "potential_additional_work",
            "notes",
            "address",
        }:
            setattr(visit, field, str(value or "").strip())
    visit.status = SiteVisit.Status.COMPLETED
    visit.completed_at = timezone.now()
    visit.save()
    if visit.lead.workflow_stage in {
        Lead.WorkflowStage.NEW,
        Lead.WorkflowStage.CONTACTED,
        Lead.WorkflowStage.SITE_VISIT,
    }:
        visit.lead.workflow_stage = Lead.WorkflowStage.ESTIMATING
        visit.lead.next_action = "Build estimate"
        visit.lead.next_action_due = None
        visit.lead.save(update_fields=["workflow_stage", "next_action", "next_action_due", "updated_at"])
    record_workflow_event(
        "site_visit_completed",
        actor=actor,
        related=visit,
        lead=visit.lead,
        project=visit.project,
        before_state=before,
        after_state=state_snapshot(visit, ["status", "completed_at", "scope", "measurements"]),
        source="field" if visit.assigned_to_id == getattr(actor, "pk", None) else "web",
        idempotency_key=idempotency_key,
    )
    record_activity("Site visit completed", visit.lead.name, actor=actor, lead=visit.lead, project=visit.project)
    return visit


@transaction.atomic
def update_site_visit(site_visit, *, actor, updates=None, complete=False, idempotency_key=None):
    """Update field notes from the project hub and optionally complete the visit."""
    locked = SiteVisit.objects.select_for_update().select_related("lead", "project").get(pk=site_visit.pk)
    if not (
        can_manage_sales(actor)
        or can_manage_project_operations(actor, locked.project)
        or locked.assigned_to_id == getattr(actor, "pk", None)
    ):
        raise PermissionDenied("You cannot update this site visit.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("site_visit_updated", locked, idempotency_key),
    ).exists():
        return locked
    before = state_snapshot(
        locked,
        ["status", "scheduled_at", "address", "scope", "measurements", "client_requests", "existing_conditions", "potential_additional_work", "notes"],
    )
    for field, value in (updates or {}).items():
        if field in {
            "scheduled_at",
            "assigned_to",
            "address",
            "scope",
            "measurements",
            "client_requests",
            "existing_conditions",
            "potential_additional_work",
            "notes",
        }:
            if field == "assigned_to":
                if value is not None and not is_staff_user(value):
                    raise ValidationError("Site visits may only be assigned to active staff.")
                if is_field(actor) and value is not None and value.pk != actor.pk:
                    raise PermissionDenied("Field users cannot reassign a site visit.")
                setattr(locked, field, value)
            else:
                setattr(locked, field, value if field == "scheduled_at" else str(value or "").strip())
    if complete:
        locked.status = SiteVisit.Status.COMPLETED
        locked.completed_at = timezone.now()
        if locked.lead.workflow_stage in {
            Lead.WorkflowStage.NEW,
            Lead.WorkflowStage.CONTACTED,
            Lead.WorkflowStage.SITE_VISIT,
        }:
            locked.lead.workflow_stage = Lead.WorkflowStage.ESTIMATING
            locked.lead.next_action = "Build estimate"
            locked.lead.next_action_due = None
            locked.lead.save(update_fields=["workflow_stage", "next_action", "next_action_due", "updated_at"])
    locked.save()
    record_workflow_event(
        "site_visit_updated",
        actor=actor,
        related=locked,
        lead=locked.lead,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(
            locked,
            ["status", "scheduled_at", "address", "scope", "measurements", "client_requests", "existing_conditions", "potential_additional_work", "notes"],
        ),
        source="field" if locked.assigned_to_id == getattr(actor, "pk", None) else "web",
        idempotency_key=idempotency_key,
    )
    record_activity(
        "Site visit completed" if complete else "Site visit updated",
        locked.lead.name,
        actor=actor,
        lead=locked.lead,
        project=locked.project,
    )
    return locked


@transaction.atomic
def send_estimate(estimate, *, actor, idempotency_key=None):
    if not can_manage_sales(actor):
        raise PermissionDenied("You cannot send estimates.")
    locked = Estimate.objects.select_for_update().select_related("lead", "client").get(pk=estimate.pk)
    if not can_view_estimate(actor, locked):
        raise PermissionDenied("You cannot send this estimate.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("estimate_sent", locked, idempotency_key),
    ).exists():
        return locked
    if locked.status == Estimate.Status.ACCEPTED:
        return locked
    before = state_snapshot(locked, ["status", "sent_at", "locked_at"])
    locked.status = Estimate.Status.SENT
    locked.sent_at = locked.sent_at or timezone.now()
    locked.save(update_fields=["status", "sent_at", "updated_at"])
    if locked.lead_id and locked.lead.workflow_stage not in {
        Lead.WorkflowStage.SOLD_SCHEDULED,
        Lead.WorkflowStage.LOST,
        Lead.WorkflowStage.ON_HOLD,
    }:
        locked.lead.workflow_stage = Lead.WorkflowStage.PROPOSAL_SENT
        locked.lead.next_action = "Follow up on proposal"
        locked.lead.next_action_due = timezone.localdate() + timedelta(days=3)
        locked.lead.save(update_fields=["workflow_stage", "next_action", "next_action_due", "updated_at"])
    record_workflow_event(
        "estimate_sent",
        actor=actor,
        related=locked,
        estimate=locked,
        lead=locked.lead,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "sent_at"]),
        idempotency_key=idempotency_key,
    )
    record_activity(
        f"Estimate #{locked.number} sent",
        locked.title,
        actor=actor,
        estimate=locked,
        lead=locked.lead,
    )
    return locked


@transaction.atomic
def accept_estimate(estimate, *, actor, request=None, idempotency_key=None):
    locked = Estimate.objects.select_for_update().select_related("lead", "client").get(pk=estimate.pk)
    is_client_acceptance = bool(
        locked.client_id
        and locked.client.user_id == getattr(actor, "pk", None)
        and not getattr(actor, "is_staff", False)
    )
    if not is_client_acceptance:
        raise PermissionDenied("Only the linked client can accept an estimate.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("estimate_accepted", locked, idempotency_key),
    ).exists():
        return locked, False
    if locked.status == Estimate.Status.ACCEPTED:
        return locked, False
    if locked.status != Estimate.Status.SENT:
        raise ValidationError("Only estimates sent to the client can be accepted.")
    before = state_snapshot(locked, ["status", "accepted_at", "accepted_by_id"])
    locked.status = Estimate.Status.ACCEPTED
    locked.accepted_at = timezone.now()
    locked.accepted_by = actor
    locked.locked_at = locked.accepted_at
    locked.save(update_fields=["status", "accepted_at", "accepted_by", "locked_at", "updated_at"])
    if locked.lead_id:
        locked.lead.workflow_stage = Lead.WorkflowStage.APPROVED
        locked.lead.next_action = "Prepare agreement and project"
        locked.lead.save(update_fields=["workflow_stage", "next_action", "updated_at"])
    record_workflow_event(
        "estimate_accepted",
        actor=actor,
        related=locked,
        estimate=locked,
        lead=locked.lead,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "accepted_at", "accepted_by_id", "locked_at"]),
        metadata={"request_ip": request.META.get("REMOTE_ADDR", "") if request else ""},
        idempotency_key=idempotency_key,
    )
    record_activity("Estimate accepted by client", f"Estimate #{locked.number}", actor=actor, estimate=locked)
    return locked, True


def _external_url(value, field_name):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValidationError({field_name: "Links must use HTTPS."})
    if parsed.username or parsed.password:
        raise ValidationError({field_name: "Links cannot contain embedded credentials."})
    if len(value) > 500:
        raise ValidationError({field_name: "The link is too long."})
    return value


def _external_money(value, field_name):
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "Enter a valid amount."}) from exc
    if not result.is_finite() or result < 0 or result > Decimal("9999999999.99"):
        raise ValidationError({field_name: "Enter a non-negative amount within the supported range."})
    return result


def _assert_fresh_record(record, expected_updated_at):
    if expected_updated_at in (None, ""):
        return
    try:
        expected = int(str(expected_updated_at))
    except (TypeError, ValueError) as exc:
        raise ValidationError("This form is stale. Refresh and try again.") from exc
    if int(record.updated_at.timestamp()) != expected:
        raise ValidationError("This record changed in another tab. Refresh and try again.")


@transaction.atomic
def record_external_estimate_status(
    estimate,
    *,
    actor,
    status,
    external_url=None,
    external_reference=None,
    external_total=None,
    external_deposit_amount=None,
    external_status_note="",
    external_client_visible=None,
    expected_updated_at=None,
    idempotency_key=None,
):
    """Record an estimate status without importing its details."""
    locked = Estimate.objects.select_for_update().select_related("lead", "client").get(pk=estimate.pk)
    if not can_manage_external_estimate(actor, estimate=locked):
        raise PermissionDenied("You cannot update this external estimate status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("external_estimate_status_changed", locked, idempotency_key),
    ).exists():
        return locked, False
    _assert_fresh_record(locked, expected_updated_at)
    valid_statuses = dict(Estimate.ExternalEstimateStatus.choices)
    status = str(status or "").strip()
    if status not in valid_statuses:
        raise ValidationError("Choose a valid estimate status.")
    current = locked.external_status
    allowed = {
        Estimate.ExternalEstimateStatus.NOT_STARTED: {Estimate.ExternalEstimateStatus.NOT_STARTED, Estimate.ExternalEstimateStatus.SENT},
        Estimate.ExternalEstimateStatus.SENT: {
            Estimate.ExternalEstimateStatus.SENT,
            Estimate.ExternalEstimateStatus.PENDING,
            Estimate.ExternalEstimateStatus.APPROVED,
            Estimate.ExternalEstimateStatus.REJECTED,
        },
        Estimate.ExternalEstimateStatus.PENDING: {
            Estimate.ExternalEstimateStatus.PENDING,
            Estimate.ExternalEstimateStatus.APPROVED,
            Estimate.ExternalEstimateStatus.REJECTED,
        },
        Estimate.ExternalEstimateStatus.APPROVED: {Estimate.ExternalEstimateStatus.APPROVED},
        Estimate.ExternalEstimateStatus.REJECTED: {Estimate.ExternalEstimateStatus.REJECTED},
    }
    if status not in allowed.get(current, set()):
        raise ValidationError(f"An estimate cannot move from {valid_statuses.get(current, current)} to {valid_statuses[status]}.")
    if current == Estimate.ExternalEstimateStatus.APPROVED and status == Estimate.ExternalEstimateStatus.APPROVED:
        if external_total not in (None, "") and _external_money(external_total, "external_total") != locked.external_total:
            raise ValidationError("An approved estimate amount is immutable; create a new revision.")
        if external_deposit_amount not in (None, "") and _external_money(external_deposit_amount, "external_deposit_amount") != locked.external_deposit_amount:
            raise ValidationError("An approved estimate deposit is immutable; create a new revision.")
    if locked.status == Estimate.Status.ACCEPTED and status != Estimate.ExternalEstimateStatus.APPROVED:
        raise ValidationError("An accepted estimate cannot be moved back to a pending estimate status.")
    if status == Estimate.ExternalEstimateStatus.REJECTED and locked.status == Estimate.Status.ACCEPTED:
        raise ValidationError("An accepted estimate cannot be rejected; create a revision instead.")

    before = {
        "external_status": locked.external_status,
        "estimate_status": locked.status,
        "external_total": str(locked.external_total) if locked.external_total is not None else None,
        "external_deposit_amount": str(locked.external_deposit_amount) if locked.external_deposit_amount is not None else None,
        "external_url_present": bool(locked.external_url),
        "external_client_visible": locked.external_client_visible,
    }
    new_total = _external_money(external_total, "external_total") if external_total not in (None, "") else locked.external_total
    new_deposit = (
        _external_money(external_deposit_amount, "external_deposit_amount")
        if external_deposit_amount not in (None, "")
        else locked.external_deposit_amount
    )
    new_url = _external_url(external_url if external_url is not None else locked.external_url, "external_url")
    if status != Estimate.ExternalEstimateStatus.NOT_STARTED and not new_url:
        raise ValidationError({"external_url": "Add the estimate link before recording this status."})
    now = timezone.now()
    locked.external_url = new_url
    locked.external_reference = str(external_reference if external_reference is not None else locked.external_reference or "").strip()[:120]
    locked.external_total = new_total
    locked.external_deposit_amount = new_deposit
    locked.external_status = status
    locked.external_status_at = now
    locked.external_status_by = actor
    locked.external_status_note = str(external_status_note or "").strip()
    if external_client_visible is not None:
        locked.external_client_visible = bool(external_client_visible)

    update_fields = [
        "external_url",
        "external_reference",
        "external_total",
        "external_deposit_amount",
        "external_status",
        "external_status_at",
        "external_status_by",
        "external_status_note",
        "external_client_visible",
        "updated_at",
    ]
    if status in {Estimate.ExternalEstimateStatus.SENT, Estimate.ExternalEstimateStatus.PENDING}:
        if locked.status == Estimate.Status.DRAFT:
            locked.status = Estimate.Status.SENT
            locked.sent_at = locked.sent_at or now
            update_fields.extend(["status", "sent_at"])
    elif status == Estimate.ExternalEstimateStatus.APPROVED:
        if locked.status != Estimate.Status.ACCEPTED:
            locked.status = Estimate.Status.ACCEPTED
            locked.accepted_at = locked.accepted_at or now
            locked.locked_at = locked.locked_at or now
            update_fields.extend(["status", "accepted_at", "locked_at"])
        if locked.lead_id:
            locked.lead.workflow_stage = Lead.WorkflowStage.APPROVED
            locked.lead.next_action = "Prepare agreement and project"
            locked.lead.save(update_fields=["workflow_stage", "next_action", "updated_at"])
    elif status == Estimate.ExternalEstimateStatus.REJECTED:
        if locked.status != Estimate.Status.DECLINED:
            locked.status = Estimate.Status.DECLINED
            locked.declined_at = now
            update_fields.extend(["status", "declined_at"])
    locked.save(update_fields=update_fields)
    after = {
        "external_status": locked.external_status,
        "estimate_status": locked.status,
        "external_total": str(locked.external_total) if locked.external_total is not None else None,
        "external_deposit_amount": str(locked.external_deposit_amount) if locked.external_deposit_amount is not None else None,
        "external_url_present": bool(locked.external_url),
    }
    record_workflow_event(
        "external_estimate_status_changed",
        actor=actor,
        related=locked,
        estimate=locked,
        lead=locked.lead,
        before_state=before,
        after_state=after,
        metadata={"confirmation_note": locked.external_status_note},
        idempotency_key=idempotency_key,
    )
    record_activity(
        f"Estimate marked {valid_statuses[status].lower()}",
        f"Estimate #{locked.number}",
        actor=actor,
        estimate=locked,
        lead=locked.lead,
    )
    status_changed = status != current
    newly_visible = locked.external_client_visible and not before["external_client_visible"]
    if locked.client_id and locked.external_client_visible:
        client_event = None
        if status == Estimate.ExternalEstimateStatus.SENT and (status_changed or newly_visible):
            client_event = (
                "estimate-sent",
                "Estimate ready to review",
                "Grand Coast shared an estimate link. Open Estimate & files in your client portal to review it in the estimate service.",
            )
        elif status == Estimate.ExternalEstimateStatus.APPROVED and (status_changed or newly_visible):
            client_event = (
                "estimate-accepted",
                "Grand Coast confirmed estimate approval",
                "Our team checked the external estimate service and recorded this estimate as approved. Project setup is the next step.",
            )
        elif status == Estimate.ExternalEstimateStatus.REJECTED and (status_changed or newly_visible):
            client_event = (
                "estimate-declined",
                "Grand Coast confirmed estimate declined",
                "Our team checked the external estimate service and recorded this estimate as declined. Message us if you would like to discuss a revision.",
            )
        if client_event:
            queue_client_notifications(
                [locked.client],
                kind=client_event[0],
                title=client_event[1],
                body=client_event[2],
                destination_url=(
                    reverse("operations:portal-section", kwargs={"section": "estimate-files"})
                    + "?"
                    + urlencode({"estimate": locked.pk})
                ),
                metadata={"estimate_id": str(locked.pk)},
                created_by=actor,
                estimate=locked,
                lead=locked.lead,
            )
    if status_changed and status in {
        Estimate.ExternalEstimateStatus.APPROVED,
        Estimate.ExternalEstimateStatus.REJECTED,
    }:
        user_model = get_user_model()
        owner_recipients = list(
            user_model.objects.filter(is_active=True, is_staff=True, is_superuser=True)
        )
        outcome = "approved" if status == Estimate.ExternalEstimateStatus.APPROVED else "declined"
        queue_employee_notifications(
            owner_recipients,
            kind="estimate-accepted" if status == Estimate.ExternalEstimateStatus.APPROVED else "estimate-declined",
            title=f"External estimate {outcome}",
            body=(
                f"{locked.title} was recorded as {outcome} by {actor.get_full_name() or actor.get_username()} "
                "after checking the external estimate service."
            ),
            destination_url=(
                reverse("operations:dashboard-section", kwargs={"section": "estimates"})
                + "?"
                + urlencode({"estimate": locked.pk})
            ),
            metadata={"estimate_id": str(locked.pk)},
            created_by=actor,
            exclude_users=[actor],
            estimate=locked,
            lead=locked.lead,
        )
    return locked, True


@transaction.atomic
def record_external_invoice_status(
    schedule,
    *,
    actor,
    status,
    external_invoice_url=None,
    external_invoice_reference=None,
    external_invoice_status_note="",
    external_client_visible=None,
    expected_updated_at=None,
    idempotency_key=None,
):
    """Record an invoice event; never create a payment record."""
    locked = PaymentSchedule.objects.select_for_update().select_related("project").get(pk=schedule.pk)
    if not can_manage_external_estimate(actor, project=locked.project):
        raise PermissionDenied("You cannot update this external invoice status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("external_invoice_status_changed", locked, idempotency_key),
    ).exists():
        return locked, False
    _assert_fresh_record(locked, expected_updated_at)
    valid_statuses = dict(PaymentSchedule.ExternalInvoiceStatus.choices)
    status = str(status or "").strip()
    if status not in valid_statuses:
        raise ValidationError("Choose a valid invoice status.")
    current = locked.external_invoice_status
    allowed = {
        PaymentSchedule.ExternalInvoiceStatus.NOT_STARTED: {
            PaymentSchedule.ExternalInvoiceStatus.NOT_STARTED,
            PaymentSchedule.ExternalInvoiceStatus.SENT,
            PaymentSchedule.ExternalInvoiceStatus.PENDING,
            PaymentSchedule.ExternalInvoiceStatus.PAID,
            PaymentSchedule.ExternalInvoiceStatus.OVERDUE,
        },
        PaymentSchedule.ExternalInvoiceStatus.SENT: {
            PaymentSchedule.ExternalInvoiceStatus.SENT,
            PaymentSchedule.ExternalInvoiceStatus.PENDING,
            PaymentSchedule.ExternalInvoiceStatus.PAID,
            PaymentSchedule.ExternalInvoiceStatus.OVERDUE,
        },
        PaymentSchedule.ExternalInvoiceStatus.PENDING: {
            PaymentSchedule.ExternalInvoiceStatus.PENDING,
            PaymentSchedule.ExternalInvoiceStatus.PAID,
            PaymentSchedule.ExternalInvoiceStatus.OVERDUE,
        },
        PaymentSchedule.ExternalInvoiceStatus.OVERDUE: {
            PaymentSchedule.ExternalInvoiceStatus.OVERDUE,
            PaymentSchedule.ExternalInvoiceStatus.PAID,
        },
        PaymentSchedule.ExternalInvoiceStatus.PAID: {PaymentSchedule.ExternalInvoiceStatus.PAID},
    }
    if status not in allowed.get(current, set()):
        raise ValidationError(f"An invoice cannot move from {valid_statuses.get(current, current)} to {valid_statuses[status]}.")
    before = {
        "external_invoice_status": locked.external_invoice_status,
        "grand_coast_status": locked.status,
        "external_url_present": bool(locked.external_invoice_url),
    }
    locked.external_invoice_url = _external_url(
        external_invoice_url if external_invoice_url is not None else locked.external_invoice_url,
        "external_invoice_url",
    )
    locked.external_invoice_reference = str(
        external_invoice_reference if external_invoice_reference is not None else locked.external_invoice_reference or ""
    ).strip()[:120]
    locked.external_invoice_status = status
    locked.external_invoice_status_at = timezone.now()
    locked.external_invoice_status_by = actor
    locked.external_invoice_status_note = str(external_invoice_status_note or "").strip()
    if external_client_visible is not None:
        locked.external_client_visible = bool(external_client_visible)
    if status == PaymentSchedule.ExternalInvoiceStatus.SENT and locked.status in {
        PaymentSchedule.Status.PENDING,
        PaymentSchedule.Status.READY,
    }:
        locked.status = PaymentSchedule.Status.INVOICED
    elif status == PaymentSchedule.ExternalInvoiceStatus.OVERDUE and locked.status != PaymentSchedule.Status.PAID:
        locked.status = PaymentSchedule.Status.OVERDUE
    locked.save(update_fields=[
        "external_invoice_url",
        "external_invoice_reference",
        "external_invoice_status",
        "external_invoice_status_at",
        "external_invoice_status_by",
        "external_invoice_status_note",
        "external_client_visible",
        "status",
        "updated_at",
    ])
    after = {
        "external_invoice_status": locked.external_invoice_status,
        "grand_coast_status": locked.status,
        "external_url_present": bool(locked.external_invoice_url),
    }
    record_workflow_event(
        "external_invoice_status_changed",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=after,
        metadata={"confirmation_note": locked.external_invoice_status_note},
        idempotency_key=idempotency_key,
    )
    record_activity(
        f"Invoice marked {valid_statuses[status].lower()}",
        locked.description,
        actor=actor,
        project=locked.project,
    )
    return locked, True


@transaction.atomic
def accept_agreement(agreement, *, actor, request=None, idempotency_key=None):
    locked = Agreement.objects.select_for_update().select_related("project", "project__client").get(pk=agreement.pk)
    project = locked.project
    is_client_acceptance = bool(
        project.client_id
        and project.client.user_id == getattr(actor, "pk", None)
        and not getattr(actor, "is_staff", False)
    )
    if not is_client_acceptance:
        raise PermissionDenied("Only the linked client can accept this agreement.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("agreement_accepted", locked, idempotency_key),
    ).exists():
        return locked, False
    if locked.status == Agreement.Status.ACCEPTED:
        return locked, False
    if locked.status != Agreement.Status.ISSUED:
        raise ValidationError("Only issued agreements can be accepted.")
    now = timezone.now()
    snapshot = locked.content_snapshot or {
        "project": str(project.pk),
        "contract_value": str(locked.contract_value),
        "deposit_amount": str(locked.deposit_amount),
    }
    acceptance_hash = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    before = state_snapshot(locked, ["status", "accepted_at", "accepted_by_id", "locked_at"])
    locked.status = Agreement.Status.ACCEPTED
    locked.accepted_at = now
    locked.accepted_by = actor
    locked.accepted_ip = request.META.get("REMOTE_ADDR") if request else None
    locked.accepted_user_agent = (request.META.get("HTTP_USER_AGENT", "") if request else "")[:500]
    locked.acceptance_hash = acceptance_hash
    locked.content_snapshot = snapshot
    locked.locked_at = now
    locked.save()
    project.operational_phase = Project.OperationalPhase.PRECONSTRUCTION
    project.next_step = project.next_step or "Complete readiness checklist"
    project.save(update_fields=["operational_phase", "next_step", "updated_at"])
    record_workflow_event(
        "agreement_accepted",
        actor=actor,
        related=locked,
        project=project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "accepted_at", "accepted_by_id", "locked_at"]),
        metadata={"acceptance_hash": acceptance_hash},
        idempotency_key=idempotency_key,
    )
    record_activity("Agreement accepted by client", project.title, actor=actor, project=project)
    return locked, True


def _estimate_category(category):
    return {
        EstimateLineItem.Category.LABOR: BudgetLine.Category.LABOR,
        EstimateLineItem.Category.MATERIALS: BudgetLine.Category.MATERIALS,
        EstimateLineItem.Category.SUBCONTRACTOR: BudgetLine.Category.SUBCONTRACTOR,
        EstimateLineItem.Category.ALLOWANCE: BudgetLine.Category.MISCELLANEOUS,
        EstimateLineItem.Category.OWNER_PROVIDED: BudgetLine.Category.MISCELLANEOUS,
    }.get(category, BudgetLine.Category.MISCELLANEOUS)


def _ensure_milestones(project):
    titles = ["Walkthrough", "Estimate approved", "Selections", "Construction", "Final walkthrough"]
    for index, title in enumerate(titles, start=1):
        project.milestones.get_or_create(
            sort_order=index,
            defaults={"title": title, "is_complete": index <= 2},
        )


@transaction.atomic
def create_project_from_estimate(estimate, *, actor, idempotency_key=None):
    if not can_manage_sales(actor):
        raise PermissionDenied("You cannot create a project from this estimate.")
    locked = Estimate.objects.select_for_update().select_related("lead", "client").get(pk=estimate.pk)
    if not can_view_estimate(actor, locked):
        raise PermissionDenied("You cannot create a project from this estimate.")
    existing = locked.projects.order_by("-created_at").first()
    if existing:
        initialize_readiness(existing, actor=actor)
        return existing, False
    if (
        locked.status != Estimate.Status.ACCEPTED
        or locked.external_status != Estimate.ExternalEstimateStatus.APPROVED
        or not locked.external_url
    ):
        raise ValidationError("Only an externally approved estimate recorded in GCC can become a project.")
    client = locked.client
    if client is None and locked.lead is not None:
        client = get_or_create_client_for_lead(locked.lead, actor=actor)
    if client is None:
        raise ValidationError("A client is required before creating a project.")
    lead = locked.lead
    project = Project.objects.create(
        estimate=locked,
        lead=lead,
        client=client,
        title=locked.title,
        location=lead.location if lead else "",
        address_line1=lead.address_line1 if lead else "",
        address_line2=lead.address_line2 if lead else "",
        city=lead.city if lead else "",
        state=lead.state if lead else "",
        postal_code=lead.postal_code if lead else "",
        project_type=lead.service if lead and lead.service else "renovation",
        status=Project.Status.PLANNING,
        operational_phase=Project.OperationalPhase.PRECONSTRUCTION,
        is_published=False,
        next_step="Assign project staff",
        summary=locked.notes,
        project_manager=lead.assigned_to if lead and lead.assigned_to_id else None,
        created_by=actor,
        fallback_image="operations/images/progress-kitchen.png",
    )
    _ensure_milestones(project)
    # Carry the completed discovery record into the canonical project hub so
    # the site-visit notes, measurements, and media remain attached to the
    # same project without a second data-entry step.
    if lead is not None:
        SiteVisit.objects.filter(lead=lead, project__isnull=True).update(
            project=project,
            updated_at=timezone.now(),
        )
    agreement, _ = Agreement.objects.get_or_create(
        project=project,
        defaults={
            "estimate": locked,
            "status": Agreement.Status.ISSUED,
            "contract_value": locked.external_total if locked.external_total is not None else locked.total,
            "deposit_amount": locked.external_deposit_amount if locked.external_deposit_amount is not None else locked.deposit_amount,
            "issued_at": timezone.now(),
            "content_snapshot": {
                "estimate_id": str(locked.pk),
                "estimate_number": locked.number,
                "contract_value": str(locked.external_total if locked.external_total is not None else locked.total),
                "deposit_amount": str(locked.external_deposit_amount if locked.external_deposit_amount is not None else locked.deposit_amount),
                "title": locked.title,
            },
            "created_by": actor,
        },
    )
    for line in locked.line_items.all():
        BudgetLine.objects.get_or_create(
            project=project,
            source_estimate_line=line,
            defaults={
                "description": line.description,
                "category": _estimate_category(line.category),
                "cost_code": line.cost_code,
                "original_budget": line.estimated_cost or line.line_total,
                "is_allowance": line.is_allowance,
                "created_by": actor,
            },
        )
    deposit_value = locked.external_deposit_amount if locked.external_deposit_amount is not None else locked.deposit_amount
    if deposit_value > 0:
        PaymentSchedule.objects.get_or_create(
            project=project,
            sequence=1,
            defaults={
                "description": "Initial deposit",
                "amount": deposit_value,
                "status": PaymentSchedule.Status.READY,
                "created_by": actor,
            },
        )
    next_sequence = 2
    for row in locked.payment_schedule or []:
        if not isinstance(row, dict):
            continue
        try:
            amount = Decimal(str(row.get("amount", "0")))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if amount <= 0:
            continue
        PaymentSchedule.objects.get_or_create(
            project=project,
            sequence=next_sequence,
            defaults={
                "description": str(row.get("description") or f"Progress payment {next_sequence}")[:180],
                "amount": amount,
                "milestone": project.milestones.filter(sort_order=next_sequence + 1).first(),
                "status": PaymentSchedule.Status.PENDING,
                "created_by": actor,
            },
        )
        next_sequence += 1
    initialize_readiness(project, actor=actor)
    initialize_closeout(project, actor=actor)
    if lead:
        lead.status = Lead.Status.WON
        lead.workflow_stage = Lead.WorkflowStage.SOLD_SCHEDULED
        lead.next_action = "Complete readiness checklist"
        lead.save(update_fields=["status", "workflow_stage", "next_action", "updated_at"])
    record_workflow_event(
        "project_created_from_estimate",
        actor=actor,
        related=project,
        project=project,
        estimate=locked,
        lead=lead,
        after_state={"project_id": str(project.pk), "agreement_id": str(agreement.pk)},
        idempotency_key=idempotency_key,
    )
    record_activity(
        "Project created from accepted estimate",
        project.title,
        actor=actor,
        estimate=locked,
        project=project,
    )
    queue_client_notifications(
        [client],
        kind="project-created",
        title="Project setup is in progress",
        body=f"{project.title} has been created. Grand Coast is assigning an employee; project work has not started yet.",
        destination_url=(
            reverse("operations:portal")
            + "?"
            + urlencode({"project": project.pk})
        ),
        metadata={"project_id": str(project.pk), "estimate_id": str(locked.pk)},
        created_by=actor,
        estimate=locked,
        lead=lead,
        project=project,
    )
    return project, True


@transaction.atomic
def complete_readiness_item(item, *, actor, idempotency_key=None, notes=None):
    locked = PreconstructionItem.objects.select_for_update().select_related("project").get(pk=item.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update this readiness item.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("readiness_item_completed", locked, idempotency_key),
    ).exists():
        return locked
    if locked.status == PreconstructionItem.Status.COMPLETE:
        return locked
    remaining_required = locked.project.readiness_items.filter(required=True).exclude(
        status__in=[PreconstructionItem.Status.COMPLETE, PreconstructionItem.Status.SKIPPED]
    )
    if locked.required:
        remaining_required = remaining_required.exclude(pk=locked.pk)
    if not remaining_required.exists() and not locked.project.assigned_staff.exists():
        raise ValidationError("Assign at least one employee before advancing this project into execution.")
    before = state_snapshot(locked, ["status", "completed_at", "completed_by_id", "notes"])
    locked.status = PreconstructionItem.Status.COMPLETE
    locked.completed_at = timezone.now()
    locked.completed_by = actor
    if notes is not None:
        locked.notes = str(notes).strip()
    locked.save()
    if not locked.project.readiness_items.filter(
        required=True,
    ).exclude(status__in=[PreconstructionItem.Status.COMPLETE, PreconstructionItem.Status.SKIPPED]).exists():
        locked.project.construction_ready_at = timezone.now()
        locked.project.operational_phase = Project.OperationalPhase.CONSTRUCTION
        locked.project.status = Project.Status.CONSTRUCTION
        locked.project.next_step = "Schedule construction start"
        locked.project.start_date = locked.project.start_date or timezone.localdate()
        locked.project.save(
            update_fields=[
                "construction_ready_at",
                "operational_phase",
                "status",
                "next_step",
                "start_date",
                "updated_at",
            ]
        )
    record_workflow_event(
        "readiness_item_completed",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "completed_at", "completed_by_id", "notes"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def update_readiness_item(
    item,
    *,
    actor,
    owner=None,
    due_date=None,
    notes="",
    idempotency_key=None,
):
    """Update readiness ownership and planning details from the project hub."""
    locked = PreconstructionItem.objects.select_for_update().select_related("project").get(pk=item.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update this readiness item.")
    event_key = (
        _workflow_event_key("readiness_item_updated", locked, idempotency_key)
        if idempotency_key
        else None
    )
    if event_key and WorkflowEvent.objects.filter(idempotency_key=event_key).exists():
        return locked
    if owner is not None and not is_staff_user(owner):
        raise ValidationError("Readiness owners must be active staff members.")
    before = state_snapshot(locked, ["owner_id", "due_date", "notes"])
    locked.owner = owner
    locked.due_date = due_date
    locked.notes = str(notes or "").strip()[:20000]
    locked.save(update_fields=["owner", "due_date", "notes", "updated_at"])
    record_workflow_event(
        "readiness_item_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["owner_id", "due_date", "notes"]),
        idempotency_key=idempotency_key,
    )
    record_activity(
        "Readiness item updated",
        locked.label,
        actor=actor,
        project=locked.project,
    )
    return locked


@transaction.atomic
def create_change_order(
    project,
    *,
    actor,
    title,
    description,
    price_impact=Decimal("0.00"),
    schedule_impact_days=0,
    status=ChangeOrder.Status.DRAFT,
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot create a change order for this project.")
    if idempotency_key:
        existing = ChangeOrder.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    title = str(title or "").strip()
    description = str(description or "").strip()
    if not title or not description:
        raise ValidationError("A change order title and description are required.")
    if len(title) > 180:
        raise ValidationError("Change order titles must be 180 characters or fewer.")
    if len(description) > 20000:
        raise ValidationError("Change order descriptions are too long.")
    try:
        price_impact = Decimal(str(price_impact)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Price impact must be a valid amount.") from exc
    if not price_impact.is_finite() or abs(price_impact) > Decimal("9999999999.99"):
        raise ValidationError("Price impact is outside the supported range.")
    try:
        schedule_impact_days = int(schedule_impact_days or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("Schedule impact must be a whole number of days.") from exc
    if schedule_impact_days < 0 or schedule_impact_days > 3650:
        raise ValidationError("Schedule impact must be between 0 and 3650 days.")
    if status not in {ChangeOrder.Status.DRAFT, ChangeOrder.Status.SENT}:
        raise ValidationError("New change orders must be drafts or sent for client approval.")
    next_number = (
        ChangeOrder.objects.filter(project=project)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
        or 0
    ) + 1
    change_order = ChangeOrder.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        project=project,
        number=next_number,
        title=title,
        description=description,
        price_impact=price_impact,
        schedule_impact_days=schedule_impact_days,
        status=status,
        sent_at=timezone.now() if status == ChangeOrder.Status.SENT else None,
        created_by=actor,
    )
    record_workflow_event(
        "change_order_created",
        actor=actor,
        related=change_order,
        project=project,
        after_state=state_snapshot(
            change_order,
            ["number", "status", "price_impact", "schedule_impact_days"],
        ),
        idempotency_key=f"change-order-event:{change_order.pk}",
    )
    return change_order, True


@transaction.atomic
def approve_change_order(change_order, *, actor, request=None, idempotency_key=None):
    locked = ChangeOrder.objects.select_for_update().select_related("project", "project__client").get(pk=change_order.pk)
    project = locked.project
    is_client_approval = bool(
        project.client_id
        and project.client.user_id == getattr(actor, "pk", None)
        and not getattr(actor, "is_staff", False)
    )
    if not is_client_approval:
        raise PermissionDenied("Only the linked client can approve a change order.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("change_order_approved", locked, idempotency_key),
    ).exists():
        return locked, False
    if locked.status == ChangeOrder.Status.APPROVED:
        return locked, False
    if locked.status != ChangeOrder.Status.SENT:
        raise ValidationError("Only change orders sent for approval can be approved.")
    now = timezone.now()
    before = state_snapshot(locked, ["status", "price_impact", "schedule_impact_days"])
    locked.status = ChangeOrder.Status.APPROVED
    locked.approved_at = now
    locked.approved_by = actor
    locked.approval_ip = request.META.get("REMOTE_ADDR") if request else None
    locked.approved_snapshot = {
        "number": locked.number,
        "title": locked.title,
        "description": locked.description,
        "price_impact": str(locked.price_impact),
        "schedule_impact_days": locked.schedule_impact_days,
        "approved_at": now.isoformat(),
    }
    locked.locked_at = now
    locked.save()
    budget_line = BudgetLine.objects.select_for_update().filter(
        project=project,
        cost_code="change-orders",
    ).first()
    if budget_line is None:
        budget_line = BudgetLine.objects.create(
            project=project,
            description="Approved change orders",
            category=BudgetLine.Category.MISCELLANEOUS,
            cost_code="change-orders",
            created_by=project.project_manager or project.created_by,
        )
    budget_line.approved_change = (
        budget_line.approved_change + locked.price_impact
    ).quantize(Decimal("0.01"))
    budget_line.save(update_fields=["approved_change", "updated_at"])
    if locked.price_impact > 0:
        next_sequence = (
            PaymentSchedule.objects.filter(project=project)
            .order_by("-sequence")
            .values_list("sequence", flat=True)
            .first()
            or 0
        ) + 1
        PaymentSchedule.objects.create(
            project=project,
            sequence=next_sequence,
            description=f"Change order CO-{locked.number}",
            amount=locked.price_impact,
            status=PaymentSchedule.Status.READY,
            created_by=actor,
        )
    record_workflow_event(
        "change_order_approved",
        actor=actor,
        related=locked,
        project=project,
        before_state=before,
        after_state=state_snapshot(
            locked,
            ["status", "approved_at", "approved_by_id", "price_impact"],
        ),
        metadata={"approval_ip": locked.approval_ip or ""},
        idempotency_key=idempotency_key,
    )
    record_activity(
        "Change order approved by client",
        f"CO-{locked.number}: {locked.title}",
        actor=actor,
        project=project,
    )
    return locked, True


@transaction.atomic
def record_payment(
    project,
    *,
    actor,
    amount,
    schedule=None,
    received_on=None,
    method=PaymentRecord.Method.OTHER,
    reference="",
    notes="",
    idempotency_key=None,
):
    if not can_view_financials(actor, project):
        raise PermissionDenied("You cannot record payments for this project.")
    if idempotency_key:
        existing = PaymentRecord.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Payment amount must be a valid amount.") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
        raise ValidationError("Payment amount must be greater than zero.")
    if method not in {value for value, _label in PaymentRecord.Method.choices}:
        raise ValidationError("Choose a valid payment method.")
    if schedule is not None:
        schedule = PaymentSchedule.objects.select_for_update().get(pk=schedule.pk)
        if schedule.project_id != project.pk:
            raise ValidationError("The payment schedule must belong to this project.")
        if amount > schedule.remaining_amount:
            raise ValidationError("Payment cannot exceed the remaining scheduled balance.")
    payment = PaymentRecord.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        project=project,
        schedule=schedule,
        amount=amount,
        received_on=received_on or timezone.localdate(),
        method=method,
        reference=str(reference).strip()[:120],
        notes=str(notes).strip()[:20000],
        created_by=actor,
    )
    if schedule:
        schedule.status = (
            PaymentSchedule.Status.PAID
            if schedule.remaining_amount <= 0
            else PaymentSchedule.Status.INVOICED
        )
        schedule.save(update_fields=["status", "updated_at"])
    record_workflow_event(
        "payment_recorded",
        actor=actor,
        related=payment,
        project=project,
        after_state={
            "payment_id": str(payment.pk),
            "amount": str(payment.amount),
            "schedule_id": str(schedule.pk) if schedule else "",
        },
        idempotency_key=f"payment-event:{payment.pk}",
    )
    record_activity("Payment recorded", f"${payment.amount:.2f}", actor=actor, project=project)
    return payment, True


@transaction.atomic
def record_cost(
    project,
    *,
    actor,
    amount,
    description,
    vendor="",
    incurred_on=None,
    source="manual",
    budget_line=None,
    idempotency_key=None,
):
    if not can_view_financials(actor, project):
        raise PermissionDenied("You cannot record costs for this project.")
    if idempotency_key:
        existing = CostEntry.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Cost amount must be a valid amount.") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
        raise ValidationError("Cost amount must be greater than zero.")
    description = str(description or "").strip()
    if not description or len(description) > 180:
        raise ValidationError("A cost description of 180 characters or fewer is required.")
    vendor = str(vendor or "").strip()[:180]
    source = str(source or "manual").strip()[:40] or "manual"
    if budget_line is not None:
        budget_line = BudgetLine.objects.select_for_update().get(pk=budget_line.pk)
        if budget_line.project_id != project.pk:
            raise ValidationError("The budget line must belong to this project.")
    entry = CostEntry.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        project=project,
        budget_line=budget_line,
        vendor=vendor,
        description=description,
        amount=amount,
        incurred_on=incurred_on or timezone.localdate(),
        source=source,
        created_by=actor,
    )
    if budget_line is not None:
        budget_line.actual = (budget_line.actual + amount).quantize(Decimal("0.01"))
        budget_line.save(update_fields=["actual", "updated_at"])
    record_workflow_event(
        "cost_recorded",
        actor=actor,
        related=entry,
        project=project,
        after_state={
            "cost_id": str(entry.pk),
            "amount": str(entry.amount),
            "budget_line_id": str(budget_line.pk) if budget_line else "",
        },
        idempotency_key=f"cost-event:{entry.pk}",
    )
    record_activity("Cost recorded", f"${entry.amount:.2f}", actor=actor, project=project)
    return entry, True


@transaction.atomic
def create_commitment(
    project,
    *,
    actor,
    description,
    amount,
    subcontractor=None,
    budget_line=None,
    status=Commitment.Status.PLANNED,
    due_date=None,
    idempotency_key=None,
):
    """Create a forecast commitment and keep its budget line synchronized."""
    if not can_view_financials(actor, project):
        raise PermissionDenied("You cannot record commitments for this project.")
    if idempotency_key:
        existing = Commitment.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.project_id != project.pk:
                raise PermissionDenied("You cannot access the commitment for this idempotency key.")
            return existing, False
    description = str(description or "").strip()
    if not description or len(description) > 180:
        raise ValidationError("A commitment description of 180 characters or fewer is required.")
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Commitment amount must be a valid amount.") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
        raise ValidationError("Commitment amount must be greater than zero.")
    if status not in {Commitment.Status.PLANNED, Commitment.Status.COMMITTED}:
        raise ValidationError("New commitments must be planned or committed.")
    if budget_line is not None:
        budget_line = BudgetLine.objects.select_for_update().get(pk=budget_line.pk)
        if budget_line.project_id != project.pk:
            raise ValidationError("The budget line must belong to this project.")
    if subcontractor is not None and subcontractor.status != Subcontractor.Status.ACTIVE:
        raise ValidationError("Only active subcontractors can receive commitments.")
    commitment = Commitment.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        project=project,
        subcontractor=subcontractor,
        budget_line=budget_line,
        description=description,
        amount=amount,
        status=status,
        due_date=due_date,
        created_by=actor,
    )
    if budget_line is not None:
        budget_line.committed = (budget_line.committed + amount).quantize(Decimal("0.01"))
        budget_line.save(update_fields=["committed", "updated_at"])
    record_workflow_event(
        "commitment_created",
        actor=actor,
        related=commitment,
        project=project,
        after_state=state_snapshot(
            commitment,
            ["description", "amount", "status", "due_date", "budget_line_id", "subcontractor_id"],
        ),
        idempotency_key=f"commitment-event:{commitment.pk}",
    )
    record_activity("Commitment recorded", f"${commitment.amount:.2f} - {commitment.description}", actor=actor, project=project)
    return commitment, True


@transaction.atomic
def record_commitment_status(commitment, *, actor, status, idempotency_key=None):
    locked = Commitment.objects.select_for_update().select_related("project", "budget_line").get(pk=commitment.pk)
    if not can_view_financials(actor, locked.project):
        raise PermissionDenied("You cannot update commitments for this project.")
    if status not in {value for value, _label in Commitment.Status.choices}:
        raise ValidationError("Choose a valid commitment status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("commitment_status_updated", locked, idempotency_key),
    ).exists():
        return locked
    if locked.status == status:
        return locked
    before = state_snapshot(locked, ["status"])
    was_active = locked.status in {Commitment.Status.PLANNED, Commitment.Status.COMMITTED}
    will_be_cancelled = status == Commitment.Status.CANCELLED
    locked.status = status
    locked.save(update_fields=["status", "updated_at"])
    if was_active and will_be_cancelled and locked.budget_line_id:
        budget_line = BudgetLine.objects.select_for_update().get(pk=locked.budget_line_id)
        budget_line.committed = max(
            (budget_line.committed - locked.amount).quantize(Decimal("0.01")),
            Decimal("0.00"),
        )
        budget_line.save(update_fields=["committed", "updated_at"])
    record_workflow_event(
        "commitment_status_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def void_cost_entry(entry, *, actor, idempotency_key=None):
    locked = CostEntry.objects.select_for_update().select_related("project").get(pk=entry.pk)
    if not can_view_financials(actor, locked.project):
        raise PermissionDenied("You cannot void costs for this project.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("cost_voided", locked, idempotency_key),
    ).exists():
        return locked, False
    if locked.is_void:
        return locked, False
    locked.is_void = True
    locked.voided_at = timezone.now()
    locked.voided_by = actor
    locked.save()
    if locked.budget_line_id:
        budget_line = BudgetLine.objects.select_for_update().get(pk=locked.budget_line_id)
        budget_line.actual = max(
            (budget_line.actual - locked.amount).quantize(Decimal("0.01")),
            Decimal("0.00"),
        )
        budget_line.save(update_fields=["actual", "updated_at"])
    record_workflow_event(
        "cost_voided",
        actor=actor,
        related=locked,
        project=locked.project,
        after_state={
            "cost_id": str(locked.pk),
            "amount": str(locked.amount),
            "voided_at": locked.voided_at.isoformat(),
        },
        idempotency_key=idempotency_key,
    )
    record_activity("Cost voided", f"${locked.amount:.2f}", actor=actor, project=locked.project)
    return locked, True


@transaction.atomic
def record_deposit(
    project,
    *,
    actor,
    amount=None,
    received_on=None,
    method=PaymentRecord.Method.OTHER,
    reference="",
    notes="",
    idempotency_key=None,
):
    if not can_view_financials(actor, project):
        raise PermissionDenied("You cannot record a deposit for this project.")
    schedule = (
        PaymentSchedule.objects.select_for_update()
        .filter(project=project, sequence=1)
        .first()
    )
    agreement = Agreement.objects.filter(project=project).first()
    if amount in (None, ""):
        if schedule is not None:
            amount = schedule.remaining_amount
        elif agreement is not None:
            amount = agreement.deposit_amount
    if amount in (None, ""):
        raise ValidationError("A deposit amount is required.")
    payment, created = record_payment(
        project,
        actor=actor,
        amount=amount,
        schedule=schedule,
        received_on=received_on,
        method=method,
        reference=reference,
        notes=notes,
        idempotency_key=idempotency_key,
    )
    lead = project.lead
    if lead is not None and lead.workflow_stage not in {
        Lead.WorkflowStage.SOLD_SCHEDULED,
        Lead.WorkflowStage.LOST,
        Lead.WorkflowStage.ON_HOLD,
    }:
        lead.workflow_stage = Lead.WorkflowStage.DEPOSIT
        lead.next_action = "Begin preconstruction"
        lead.next_action_due = None
        lead.save(update_fields=["workflow_stage", "next_action", "next_action_due", "updated_at"])
    record_workflow_event(
        "deposit_recorded",
        actor=actor,
        related=payment,
        project=project,
        after_state={
            "payment_id": str(payment.pk),
            "amount": str(payment.amount),
            "created": created,
        },
        idempotency_key=idempotency_key,
    )
    return payment, created


@transaction.atomic
def create_permit(
    project,
    *,
    actor,
    permit_type,
    jurisdiction="",
    permit_number="",
    status=Permit.Status.PENDING,
    expires_at=None,
    notes="",
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot create permits for this project.")
    if idempotency_key:
        existing = Permit.objects.filter(idempotency_key=str(idempotency_key)).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    permit_type = str(permit_type or "").strip()
    if not permit_type or len(permit_type) > 140:
        raise ValidationError("A permit type of 140 characters or fewer is required.")
    if status not in {Permit.Status.PENDING, Permit.Status.SUBMITTED}:
        raise ValidationError("New permits may be pending or submitted.")
    now = timezone.now()
    permit = Permit.objects.create(
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        project=project,
        permit_type=permit_type,
        jurisdiction=str(jurisdiction or "").strip()[:160],
        permit_number=str(permit_number or "").strip()[:100],
        status=status,
        submitted_at=now if status == Permit.Status.SUBMITTED else None,
        expires_at=expires_at,
        notes=str(notes or "").strip()[:20000],
        created_by=actor,
    )
    record_workflow_event(
        "permit_created",
        actor=actor,
        related=permit,
        project=project,
        after_state={
            "permit_id": str(permit.pk),
            "permit_type": permit.permit_type,
            "status": permit.status,
        },
        idempotency_key=idempotency_key,
    )
    return permit, True


@transaction.atomic
def create_selection(
    project,
    *,
    actor,
    category,
    item_name,
    description="",
    vendor="",
    allowance=Decimal("0.00"),
    client_choice="",
    due_date=None,
    notes="",
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot create selections for this project.")
    if idempotency_key:
        event_key = _workflow_event_key("selection_created", project, idempotency_key)
        existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing_event:
            return Selection.objects.get(pk=existing_event.related_id), False
    category = str(category or "").strip()
    item_name = str(item_name or "").strip()
    if not category or len(category) > 100:
        raise ValidationError("A selection category of 100 characters or fewer is required.")
    if not item_name or len(item_name) > 180:
        raise ValidationError("A selection item name of 180 characters or fewer is required.")
    try:
        allowance = Decimal(str(allowance or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Selection allowance must be a valid amount.") from exc
    if not allowance.is_finite() or allowance < 0 or allowance > Decimal("9999999999.99"):
        raise ValidationError("Selection allowance is outside the supported range.")
    selection = Selection(
        project=project,
        category=category,
        item_name=item_name,
        description=str(description or "").strip()[:20000],
        vendor=str(vendor or "").strip()[:160],
        allowance=allowance,
        client_choice=str(client_choice or "").strip()[:20000],
        due_date=due_date,
        notes=str(notes or "").strip()[:20000],
        created_by=actor,
    )
    selection.full_clean()
    selection.save()
    record_workflow_event(
        "selection_created",
        actor=actor,
        related=selection,
        project=project,
        after_state=state_snapshot(selection, ["category", "item_name", "status", "allowance", "due_date"]),
        idempotency_key=idempotency_key,
        event_key_override=(
            _workflow_event_key("selection_created", project, idempotency_key)
            if idempotency_key
            else None
        ),
    )
    record_activity("Selection added", selection.item_name, actor=actor, project=project)
    return selection, True


@transaction.atomic
def create_project_task(
    project,
    *,
    actor,
    title,
    description="",
    milestone=None,
    assigned_to=None,
    status=Task.Status.OPEN,
    priority=Task.Priority.NORMAL,
    due_date=None,
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot create tasks for this project.")
    event_key = (
        _workflow_event_key("project_task_created", project, idempotency_key)
        if idempotency_key
        else None
    )
    if event_key:
        existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing_event:
            return Task.objects.get(pk=existing_event.related_id), False
    title = str(title or "").strip()
    if not title or len(title) > 180:
        raise ValidationError("A task title of 180 characters or fewer is required.")
    if milestone is not None:
        milestone = Milestone.objects.get(pk=milestone.pk)
        if milestone.project_id != project.pk:
            raise ValidationError("The selected milestone must belong to this project.")
    if assigned_to is not None and not is_staff_user(assigned_to):
        raise ValidationError("Tasks may only be assigned to active staff.")
    if status not in {value for value, _label in Task.Status.choices}:
        raise ValidationError("Choose a valid task status.")
    if priority not in {value for value, _label in Task.Priority.choices}:
        raise ValidationError("Choose a valid task priority.")
    task = Task(
        project=project,
        title=title,
        description=str(description or "").strip()[:20000],
        milestone=milestone,
        assigned_to=assigned_to,
        status=status,
        priority=priority,
        due_date=due_date,
        completed_at=timezone.now() if status == Task.Status.COMPLETE else None,
        created_by=actor,
    )
    task.full_clean()
    task.save()
    record_workflow_event(
        "project_task_created",
        actor=actor,
        related=task,
        project=project,
        after_state=state_snapshot(task, ["title", "status", "priority", "due_date", "assigned_to_id", "milestone_id"]),
        idempotency_key=idempotency_key,
        event_key_override=event_key,
    )
    record_activity("Project task created", task.title, actor=actor, project=project)
    return task, True


@transaction.atomic
def record_project_task_status(task, *, actor, status, idempotency_key=None):
    locked = Task.objects.select_for_update().select_related("project", "lead").get(pk=task.pk)
    if not (
        can_manage_project_operations(actor, locked.project)
        or (is_field(actor) and locked.assigned_to_id == actor.pk)
    ):
        raise PermissionDenied("You cannot update tasks for this project.")
    if status not in {value for value, _label in Task.Status.choices}:
        raise ValidationError("Choose a valid task status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("project_task_status_updated", locked, idempotency_key),
    ).exists():
        return locked
    before = state_snapshot(locked, ["status", "completed_at"])
    locked.status = status
    locked.completed_at = timezone.now() if status == Task.Status.COMPLETE else None
    locked.save(update_fields=["status", "completed_at", "updated_at"])
    record_workflow_event(
        "project_task_status_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        lead=locked.lead,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "completed_at"]),
        idempotency_key=idempotency_key,
    )
    record_activity("Project task status updated", locked.title, actor=actor, project=locked.project)
    return locked


@transaction.atomic
def create_project_document(
    project,
    *,
    actor,
    title,
    category="Project document",
    description="",
    file,
    visibility=ProjectDocument.Visibility.INTERNAL,
    idempotency_key=None,
):
    """Create a protected project document through the execution command layer."""
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot upload documents for this project.")
    event_key = (
        _workflow_event_key("project_document_created", project, idempotency_key)
        if idempotency_key
        else None
    )
    if event_key:
        existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing_event:
            return ProjectDocument.objects.get(pk=existing_event.related_id), False
    document = ProjectDocument(
        project=project,
        title=str(title or "").strip(),
        category=str(category or "Project document").strip(),
        description=str(description or "").strip(),
        file=file,
        visibility=visibility,
        uploaded_by=actor,
    )
    validate_construction_document(file)
    document.full_clean()
    document.save()
    record_workflow_event(
        "project_document_created",
        actor=actor,
        related=document,
        project=project,
        after_state=state_snapshot(document, ["title", "category", "visibility"]),
        idempotency_key=idempotency_key,
        event_key_override=event_key,
    )
    record_activity("Project document uploaded", document.title, actor=actor, project=project)
    return document, True


@transaction.atomic
def create_inspection(
    project,
    *,
    actor,
    inspection_type,
    permit=None,
    scheduled_at=None,
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot schedule inspections for this project.")
    if idempotency_key:
        event_key = _workflow_event_key("inspection_created", project, idempotency_key)
        existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing_event:
            return Inspection.objects.get(pk=existing_event.related_id), False
    inspection_type = str(inspection_type or "").strip()
    if not inspection_type or len(inspection_type) > 140:
        raise ValidationError("An inspection type of 140 characters or fewer is required.")
    if permit is not None:
        permit = Permit.objects.get(pk=permit.pk)
        if permit.project_id != project.pk:
            raise ValidationError("The selected permit must belong to this project.")
    inspection = Inspection(
        project=project,
        permit=permit,
        inspection_type=inspection_type,
        scheduled_at=scheduled_at,
        created_by=actor,
    )
    inspection.full_clean()
    inspection.save()
    record_workflow_event(
        "inspection_created",
        actor=actor,
        related=inspection,
        project=project,
        after_state=state_snapshot(inspection, ["inspection_type", "permit_id", "scheduled_at", "status"]),
        idempotency_key=idempotency_key,
        event_key_override=(
            _workflow_event_key("inspection_created", project, idempotency_key)
            if idempotency_key
            else None
        ),
    )
    record_activity("Inspection scheduled", inspection.inspection_type, actor=actor, project=project)
    return inspection, True


@transaction.atomic
def record_permit_status(
    permit,
    *,
    actor,
    status,
    permit_number=None,
    expires_at=None,
    notes=None,
    idempotency_key=None,
):
    locked = Permit.objects.select_for_update().select_related("project").get(pk=permit.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update permits for this project.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("permit_status_recorded", locked, idempotency_key),
    ).exists():
        return locked
    if status not in {value for value, _label in Permit.Status.choices}:
        raise ValidationError("Choose a valid permit status.")
    before = state_snapshot(locked, ["status", "permit_number", "expires_at", "notes"])
    locked.status = status
    if permit_number is not None:
        locked.permit_number = str(permit_number).strip()[:100]
    if expires_at is not None:
        locked.expires_at = expires_at
    if notes is not None:
        locked.notes = str(notes).strip()[:20000]
    if status == Permit.Status.SUBMITTED and locked.submitted_at is None:
        locked.submitted_at = timezone.now()
    if status == Permit.Status.APPROVED and locked.approved_at is None:
        locked.approved_at = timezone.now()
    locked.save()
    record_workflow_event(
        "permit_status_recorded",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "permit_number", "expires_at", "notes"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def submit_daily_report(
    project,
    *,
    actor,
    report_date,
    summary,
    work_completed="",
    labor_count=0,
    hours_worked=Decimal("0.00"),
    weather="",
    equipment="",
    notes="",
    idempotency_key=None,
):
    if not can_submit_field_work(actor, project):
        raise PermissionDenied("You cannot submit a field report for this project.")
    try:
        key = uuid.UUID(str(idempotency_key)) if idempotency_key else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Field report idempotency key must be a UUID.") from exc
    if key:
        existing = DailyReport.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    summary = str(summary or "").strip()
    if not summary:
        raise ValidationError("A daily report summary is required.")
    if len(summary) > 20000:
        raise ValidationError("Daily report summary is too long.")
    try:
        labor_count = int(labor_count or 0)
        hours_worked = Decimal(str(hours_worked or "0")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError("Daily report labor values are invalid.") from exc
    if labor_count < 0 or labor_count > 1000:
        raise ValidationError("Labor count must be between 0 and 1000.")
    if not hours_worked.is_finite() or hours_worked < 0 or hours_worked > Decimal("24.00"):
        raise ValidationError("Hours worked must be between 0 and 24.")
    report = DailyReport.objects.create(
        idempotency_key=key or uuid.uuid4(),
        project=project,
        submitted_by=actor,
        report_date=report_date,
        summary=summary,
        work_completed=str(work_completed or "").strip()[:20000],
        labor_count=labor_count,
        hours_worked=hours_worked,
        weather=str(weather or "").strip()[:120],
        equipment=str(equipment or "").strip()[:10000],
        notes=str(notes or "").strip()[:20000],
    )
    record_workflow_event(
        "daily_report_submitted",
        actor=actor,
        related=report,
        project=project,
        after_state={"report_id": str(report.pk), "report_date": report.report_date.isoformat()},
        source="field",
        idempotency_key=f"daily-report-event:{report.pk}",
    )
    return report, True


@transaction.atomic
def request_material(
    project,
    *,
    actor,
    description,
    quantity="",
    needed_by=None,
    vendor="",
    notes="",
    idempotency_key=None,
):
    if not can_submit_field_work(actor, project):
        raise PermissionDenied("You cannot request materials for this project.")
    try:
        key = uuid.UUID(str(idempotency_key)) if idempotency_key else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Material request idempotency key must be a UUID.") from exc
    if key:
        existing = MaterialRequest.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    description = str(description or "").strip()
    if not description:
        raise ValidationError("A material description is required.")
    if len(description) > 180:
        raise ValidationError("Material descriptions must be 180 characters or fewer.")
    request = MaterialRequest.objects.create(
        idempotency_key=key or uuid.uuid4(),
        project=project,
        requested_by=actor,
        description=description,
        quantity=str(quantity or "").strip()[:80],
        needed_by=needed_by,
        vendor=str(vendor or "").strip()[:180],
        notes=str(notes or "").strip()[:20000],
    )
    record_workflow_event(
        "material_requested",
        actor=actor,
        related=request,
        project=project,
        after_state={"material_request_id": str(request.pk), "description": request.description},
        source="field",
        idempotency_key=f"material-request-event:{request.pk}",
    )
    return request, True


@transaction.atomic
def record_material_request_status(material_request, *, actor, status, idempotency_key=None):
    locked = MaterialRequest.objects.select_for_update().select_related("project").get(pk=material_request.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update materials for this project.")
    if status not in {value for value, _label in MaterialRequest.Status.choices}:
        raise ValidationError("Choose a valid material request status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("material_request_status_updated", locked, idempotency_key),
    ).exists():
        return locked
    if locked.status == status:
        return locked
    sequence = [
        MaterialRequest.Status.REQUESTED,
        MaterialRequest.Status.APPROVED,
        MaterialRequest.Status.ORDERED,
        MaterialRequest.Status.RECEIVED,
    ]
    if status != MaterialRequest.Status.REJECTED:
        if status not in sequence or sequence.index(status) < sequence.index(locked.status):
            raise ValidationError("Material requests can only move forward through fulfillment.")
    before = state_snapshot(locked, ["status", "approved_at", "approved_by_id"])
    locked.status = status
    if status == MaterialRequest.Status.APPROVED:
        locked.approved_at = timezone.now()
        locked.approved_by = actor
    locked.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    record_workflow_event(
        "material_request_status_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "approved_at", "approved_by_id"]),
        idempotency_key=idempotency_key,
    )
    record_activity("Material request updated", f"{locked.description} - {locked.get_status_display()}", actor=actor, project=locked.project)
    return locked


@transaction.atomic
def submit_problem_report(
    project,
    *,
    actor,
    title,
    description,
    severity=ProblemReport.Severity.NORMAL,
    task=None,
    idempotency_key=None,
):
    if not can_submit_field_work(actor, project):
        raise PermissionDenied("You cannot report problems for this project.")
    try:
        key = uuid.UUID(str(idempotency_key)) if idempotency_key else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Problem report idempotency key must be a UUID.") from exc
    if key:
        existing = ProblemReport.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.project_id != project.pk:
                raise ValidationError("Idempotency key is already used for another project.")
            return existing, False
    title = str(title or "").strip()
    description = str(description or "").strip()
    if not title or len(title) > 180:
        raise ValidationError("A problem title of 180 characters or fewer is required.")
    if not description or len(description) > 20000:
        raise ValidationError("A problem description of 20,000 characters or fewer is required.")
    if severity not in {value for value, _label in ProblemReport.Severity.choices}:
        raise ValidationError("Choose a valid problem severity.")
    if task is not None:
        task = Task.objects.get(pk=task.pk)
        if task.project_id != project.pk:
            raise ValidationError("The selected task must belong to this project.")
    report = ProblemReport.objects.create(
        idempotency_key=key or uuid.uuid4(),
        project=project,
        task=task,
        reported_by=actor,
        title=title,
        description=description,
        severity=severity,
    )
    record_workflow_event(
        "problem_reported",
        actor=actor,
        related=report,
        project=project,
        after_state={
            "problem_report_id": str(report.pk),
            "title": report.title,
            "severity": report.severity,
        },
        source="field",
        idempotency_key=f"problem-report-event:{report.pk}",
    )
    record_activity("Construction problem reported", report.title, actor=actor, project=project)
    return report, True


@transaction.atomic
def resolve_problem_report(
    report,
    *,
    actor,
    resolution,
    status=ProblemReport.Status.RESOLVED,
    idempotency_key=None,
):
    locked = ProblemReport.objects.select_for_update().select_related("project").get(pk=report.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot resolve this construction problem.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("problem_report_resolved", locked, idempotency_key),
    ).exists():
        return locked
    if status not in {ProblemReport.Status.RESOLVED, ProblemReport.Status.DISMISSED}:
        raise ValidationError("A problem can only be resolved or dismissed by this command.")
    resolution = str(resolution or "").strip()
    if not resolution or len(resolution) > 20000:
        raise ValidationError("A problem resolution of 20,000 characters or fewer is required.")
    before = state_snapshot(locked, ["status", "resolution", "resolved_at", "resolved_by_id"])
    locked.status = status
    locked.resolution = resolution
    locked.resolved_at = timezone.now()
    locked.resolved_by = actor
    locked.save()
    record_workflow_event(
        "problem_report_resolved",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "resolution", "resolved_at", "resolved_by_id"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def complete_closeout_item(item, *, actor, status=CloseoutItem.Status.COMPLETE, notes=None, idempotency_key=None):
    locked = CloseoutItem.objects.select_for_update().select_related("project").get(pk=item.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update closeout for this project.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("closeout_item_completed", locked, idempotency_key),
    ).exists():
        return locked
    if status not in {CloseoutItem.Status.COMPLETE, CloseoutItem.Status.NOT_APPLICABLE}:
        raise ValidationError("Closeout items can only be completed or marked not applicable.")
    if locked.status in {CloseoutItem.Status.COMPLETE, CloseoutItem.Status.NOT_APPLICABLE}:
        return locked
    remaining_required = locked.project.closeout_items.filter(required=True).exclude(
        status__in=[CloseoutItem.Status.COMPLETE, CloseoutItem.Status.NOT_APPLICABLE]
    )
    if locked.required:
        remaining_required = remaining_required.exclude(pk=locked.pk)
    if not remaining_required.exists() and not locked.project.assigned_staff.exists():
        raise ValidationError("An unassigned project cannot be marked complete. Assign an employee before closeout.")
    before = state_snapshot(locked, ["status", "notes", "completed_at", "completed_by_id"])
    locked.status = status
    if notes is not None:
        locked.notes = str(notes).strip()[:20000]
    locked.completed_at = timezone.now()
    locked.completed_by = actor
    locked.save()
    if not locked.project.closeout_items.filter(
        required=True,
    ).exclude(status__in=[CloseoutItem.Status.COMPLETE, CloseoutItem.Status.NOT_APPLICABLE]).exists():
        locked.project.operational_phase = Project.OperationalPhase.WARRANTY
        locked.project.status = Project.Status.COMPLETE
        locked.project.next_step = "Warranty support"
        locked.project.save(update_fields=["operational_phase", "status", "next_step", "updated_at"])
        initialize_warranty(locked.project, actor=actor)
    record_workflow_event(
        "closeout_item_completed",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "notes", "completed_at", "completed_by_id"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def advance_selection(selection, *, actor, status, client_choice=None, idempotency_key=None):
    locked = Selection.objects.select_for_update().select_related("project", "project__client").get(pk=selection.pk)
    project = locked.project
    if is_client(actor):
        if not can_view_project(actor, project) or status != Selection.Status.SUBMITTED:
            raise PermissionDenied("Clients may only submit their own selection choices.")
        if not str(client_choice or "").strip():
            raise ValidationError("A selection choice is required.")
    elif not can_manage_project_operations(actor, project):
        raise PermissionDenied("Only assigned management may advance selections.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("selection_advanced", locked, idempotency_key),
    ).exists():
        return locked
    valid_statuses = {value for value, _label in Selection.Status.choices}
    if status not in valid_statuses:
        raise ValidationError("Choose a valid selection status.")
    if client_choice is not None and len(str(client_choice)) > 20000:
        raise ValidationError("Selection choices are too long.")
    if locked.status == status:
        return locked
    sequence = [
        Selection.Status.PENDING,
        Selection.Status.SUBMITTED,
        Selection.Status.APPROVED,
        Selection.Status.ORDERED,
        Selection.Status.RECEIVED,
        Selection.Status.INSTALLED,
    ]
    if status not in sequence or sequence.index(status) < sequence.index(locked.status):
        raise ValidationError("Selections can only move forward through their lifecycle.")
    before = state_snapshot(
        locked,
        ["status", "client_choice", "approved_at", "ordered_at", "received_at", "installed_at"],
    )
    locked.status = status
    if client_choice is not None:
        locked.client_choice = str(client_choice).strip()
    now = timezone.now()
    timestamp_fields = {
        Selection.Status.APPROVED: "approved_at",
        Selection.Status.ORDERED: "ordered_at",
        Selection.Status.RECEIVED: "received_at",
        Selection.Status.INSTALLED: "installed_at",
    }
    timestamp_field = timestamp_fields.get(status)
    if timestamp_field:
        setattr(locked, timestamp_field, now)
    if status == Selection.Status.APPROVED:
        locked.approved_by = actor
    locked.save()
    if status == Selection.Status.APPROVED:
        Task.objects.get_or_create(
            project=project,
            title=f"Order selection: {locked.item_name}"[:180],
            defaults={
                "description": f"Procure the approved {locked.category.lower()} selection.",
                "due_date": locked.due_date,
                "status": Task.Status.OPEN,
                "priority": Task.Priority.HIGH,
                "created_by": actor,
            },
        )
    record_workflow_event(
        "selection_advanced",
        actor=actor,
        related=locked,
        project=project,
        before_state=before,
        after_state=state_snapshot(
            locked,
            ["status", "client_choice", "approved_at", "ordered_at", "received_at", "installed_at"],
        ),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def record_inspection_result(
    inspection,
    *,
    actor,
    status,
    result_notes="",
    corrective_action="",
    rescheduled_at=None,
    idempotency_key=None,
):
    locked = Inspection.objects.select_for_update().select_related("project").get(pk=inspection.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot record an inspection result.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("inspection_result_recorded", locked, idempotency_key),
    ).exists():
        return locked
    valid_statuses = {value for value, _label in Inspection.Status.choices}
    if status not in valid_statuses:
        raise ValidationError("Choose a valid inspection status.")
    if rescheduled_at is not None and not isinstance(rescheduled_at, datetime):
        raise ValidationError("Rescheduled time must be a valid date and time.")
    before = state_snapshot(locked, ["status", "result_notes", "corrective_action", "rescheduled_at"])
    locked.status = status
    locked.result_notes = str(result_notes or "").strip()[:20000]
    locked.corrective_action = str(corrective_action or "").strip()[:20000]
    locked.rescheduled_at = rescheduled_at
    locked.completed_by = actor
    locked.save()
    if status == Inspection.Status.FAILED:
        Blocker.objects.get_or_create(
            project=locked.project,
            title=f"Failed inspection: {locked.inspection_type}",
            status=Blocker.Status.OPEN,
            defaults={
                "category": Blocker.Category.INSPECTION,
                "severity": Blocker.Severity.HIGH,
                "description": locked.corrective_action or locked.result_notes,
                "assigned_to": locked.project.project_manager,
                "created_by": actor,
            },
        )
    elif status == Inspection.Status.PASSED:
        Blocker.objects.filter(
            project=locked.project,
            category=Blocker.Category.INSPECTION,
            title=f"Failed inspection: {locked.inspection_type}",
            status=Blocker.Status.OPEN,
        ).update(
            status=Blocker.Status.RESOLVED,
            resolved_at=timezone.now(),
            resolved_by=actor,
            updated_at=timezone.now(),
        )
    record_workflow_event(
        "inspection_result_recorded",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(
            locked,
            ["status", "result_notes", "corrective_action", "rescheduled_at", "completed_by_id"],
        ),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def set_milestone_status(milestone, *, actor, is_complete, idempotency_key=None):
    """Complete a milestone and release any payment draw linked to it."""
    locked = Milestone.objects.select_for_update().select_related("project").get(pk=milestone.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update this project milestone.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("milestone_status_updated", locked, idempotency_key),
    ).exists():
        return locked
    complete = bool(is_complete)
    before = state_snapshot(locked, ["is_complete", "completed_at"])
    locked.is_complete = complete
    locked.completed_at = timezone.now() if complete else None
    locked.save(update_fields=["is_complete", "completed_at"])
    released_draws = []
    if complete:
        schedules = PaymentSchedule.objects.select_for_update().filter(
            milestone=locked,
            status=PaymentSchedule.Status.PENDING,
        )
        for schedule in schedules:
            schedule.status = PaymentSchedule.Status.READY
            schedule.save(update_fields=["status", "updated_at"])
            released_draws.append(str(schedule.pk))
            record_workflow_event(
                "payment_draw_ready",
                actor=actor,
                related=schedule,
                project=locked.project,
                after_state={"status": schedule.status, "milestone_id": str(locked.pk)},
                source="workflow",
                idempotency_key=f"milestone-draw:{locked.pk}:{schedule.pk}:{idempotency_key or locked.updated_at.isoformat()}",
            )
    record_workflow_event(
        "milestone_status_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["is_complete", "completed_at"]),
        metadata={"released_draw_ids": released_draws},
        idempotency_key=idempotency_key,
    )
    record_activity(
        "Milestone completed" if complete else "Milestone reopened",
        locked.title,
        actor=actor,
        project=locked.project,
    )
    return locked


@transaction.atomic
def create_subcontractor_assignment(
    project,
    *,
    actor,
    subcontractor,
    task=None,
    work_package,
    scope="",
    start_date=None,
    end_date=None,
    status=SubcontractorAssignment.Status.PROPOSED,
    notes="",
    idempotency_key=None,
):
    if not can_manage_project_operations(actor, project):
        raise PermissionDenied("You cannot assign subcontractor work for this project.")
    if subcontractor is None:
        raise ValidationError("Invalid subcontractor.")
    if subcontractor.status != Subcontractor.Status.ACTIVE:
        raise ValidationError("Only active subcontractors can receive work assignments.")
    if task is not None and task.project_id != project.pk:
        raise ValidationError("The selected task must belong to this project.")
    if status not in {value for value, _label in SubcontractorAssignment.Status.choices}:
        raise ValidationError("Choose a valid subcontractor assignment status.")
    if idempotency_key:
        event_key = _workflow_event_key("subcontractor_assignment_created", project, idempotency_key)
        existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
        if existing_event:
            existing = SubcontractorAssignment.objects.get(pk=existing_event.related_id)
            return existing, False
    assignment = SubcontractorAssignment(
        project=project,
        subcontractor=subcontractor,
        task=task,
        work_package=str(work_package or "").strip(),
        scope=str(scope or "").strip()[:20000],
        start_date=start_date,
        end_date=end_date,
        status=status,
        notes=str(notes or "").strip()[:20000],
        assigned_by=actor,
    )
    assignment.full_clean()
    assignment.save()
    record_workflow_event(
        "subcontractor_assignment_created",
        actor=actor,
        related=assignment,
        project=project,
        after_state=state_snapshot(assignment, ["subcontractor_id", "task_id", "work_package", "start_date", "end_date", "status"]),
        idempotency_key=idempotency_key,
        event_key_override=(
            _workflow_event_key("subcontractor_assignment_created", project, idempotency_key)
            if idempotency_key
            else None
        ),
    )
    record_activity("Subcontractor work assigned", assignment.work_package, actor=actor, project=project)
    return assignment, True


@transaction.atomic
def record_subcontractor_assignment_status(assignment, *, actor, status, idempotency_key=None):
    locked = SubcontractorAssignment.objects.select_for_update().select_related("project").get(pk=assignment.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot update subcontractor work for this project.")
    if status not in {value for value, _label in SubcontractorAssignment.Status.choices}:
        raise ValidationError("Choose a valid subcontractor assignment status.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("subcontractor_assignment_status_updated", locked, idempotency_key),
    ).exists():
        return locked
    before = state_snapshot(locked, ["status"])
    locked.status = status
    locked.save(update_fields=["status", "updated_at"])
    record_workflow_event(
        "subcontractor_assignment_status_updated",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status"]),
        idempotency_key=idempotency_key,
    )
    return locked


@transaction.atomic
def resolve_warranty_item(
    item,
    *,
    actor,
    resolution,
    status=WarrantyItem.Status.RESOLVED,
    idempotency_key=None,
):
    locked = WarrantyItem.objects.select_for_update().select_related("project").get(pk=item.pk)
    if not can_manage_project_operations(actor, locked.project):
        raise PermissionDenied("You cannot resolve this warranty item.")
    if idempotency_key and WorkflowEvent.objects.filter(
        idempotency_key=_workflow_event_key("warranty_item_resolved", locked, idempotency_key),
    ).exists():
        return locked
    if status not in {WarrantyItem.Status.RESOLVED, WarrantyItem.Status.CLOSED}:
        raise ValidationError("Warranty items can only be resolved or closed by this command.")
    resolution = str(resolution or "").strip()
    if not resolution:
        raise ValidationError("A warranty resolution is required.")
    if len(resolution) > 20000:
        raise ValidationError("Warranty resolution is too long.")
    before = state_snapshot(locked, ["status", "resolution", "resolved_at", "resolved_by_id"])
    locked.status = status
    locked.resolution = resolution
    locked.resolved_at = timezone.now()
    locked.resolved_by = actor
    locked.save()
    record_workflow_event(
        "warranty_item_resolved",
        actor=actor,
        related=locked,
        project=locked.project,
        before_state=before,
        after_state=state_snapshot(locked, ["status", "resolution", "resolved_at", "resolved_by_id"]),
        idempotency_key=idempotency_key,
    )
    return locked


def project_financial_summary(project):
    agreement = getattr(project, "agreement", None)
    original_contract = (
        agreement.contract_value
        if agreement
        else (project.estimate.total if project.estimate_id else Decimal("0.00"))
    )
    approved_changes = (
        project.change_orders.filter(status=ChangeOrder.Status.APPROVED)
        .aggregate(total=Sum("price_impact"))["total"]
        or Decimal("0.00")
    )
    current_contract = (original_contract + approved_changes).quantize(Decimal("0.01"))
    budget_total = sum(
        (line.current_budget for line in project.budget_lines.all()),
        Decimal("0.00"),
    )
    commitments = (
        project.commitments.filter(
            status__in=[Commitment.Status.PLANNED, Commitment.Status.COMMITTED]
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    cost_total = (
        project.cost_entries.filter(is_void=False).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    payments_received = (
        project.payment_records.filter(voided_at__isnull=True).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    outstanding = (current_contract - payments_received).quantize(Decimal("0.01"))
    forecast_cost = max(cost_total, Decimal("0.00")) + max(commitments, Decimal("0.00"))
    gross_profit = (current_contract - cost_total).quantize(Decimal("0.01"))
    forecast_profit = (current_contract - forecast_cost).quantize(Decimal("0.01"))
    gross_margin = (
        ((gross_profit / current_contract) * Decimal("100")).quantize(Decimal("0.01"))
        if current_contract
        else Decimal("0.00")
    )
    next_draw = sum(
        (
            schedule.remaining_amount
            for schedule in project.payment_schedules.all()
            if schedule.status in {
                PaymentSchedule.Status.READY,
                PaymentSchedule.Status.INVOICED,
                PaymentSchedule.Status.OVERDUE,
            }
        ),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    return {
        "original_contract": original_contract.quantize(Decimal("0.01")),
        "approved_changes": approved_changes.quantize(Decimal("0.01")),
        "current_contract": current_contract,
        "budget_total": budget_total.quantize(Decimal("0.01")),
        "committed_costs": commitments.quantize(Decimal("0.01")),
        "actual_costs": cost_total.quantize(Decimal("0.01")),
        "payments_received": payments_received.quantize(Decimal("0.01")),
        "outstanding_balance": outstanding,
        "gross_profit": gross_profit,
        "gross_margin": gross_margin,
        "forecast_profit": forecast_profit,
        "budget_remaining": (budget_total - cost_total - commitments).quantize(Decimal("0.01")),
        "next_draw": next_draw,
    }


def _attention_item(
    *,
    kind,
    title,
    description="",
    priority="normal",
    due_at=None,
    project=None,
    lead=None,
    estimate=None,
    source="",
):
    return {
        "kind": kind,
        "title": title,
        "description": description,
        "priority": priority,
        "due_at": due_at,
        "project": project,
        "lead": lead,
        "estimate": estimate,
        "source": source,
    }


def attention_feed(user, *, limit=80):
    """Build the role-specific action list from authorized querysets."""
    items = []
    projects = (
        visible_projects(user)
        .select_related("client", "project_manager")
        .prefetch_related("assigned_staff")
    )
    project_ids = list(projects.values_list("pk", flat=True))
    today = timezone.localdate()
    now = timezone.now()
    for blocker in Blocker.objects.filter(
        project_id__in=project_ids,
        status=Blocker.Status.OPEN,
    ).select_related("project", "assigned_to"):
        items.append(_attention_item(
            kind="blocker",
            title=blocker.title,
            description=blocker.description or blocker.get_category_display(),
            priority="urgent" if blocker.severity == Blocker.Severity.CRITICAL else blocker.severity,
            due_at=blocker.due_date,
            project=blocker.project,
            source="Blocker",
        ))
    for item in PreconstructionItem.objects.filter(
        project_id__in=project_ids,
        required=True,
        status__in=[PreconstructionItem.Status.OPEN, PreconstructionItem.Status.BLOCKED],
    ).select_related("project", "owner"):
        due = item.due_date
        items.append(_attention_item(
            kind="readiness",
            title=f"{item.project.title}: {item.label}",
            description="Required before construction can start.",
            priority="high" if item.status == PreconstructionItem.Status.BLOCKED or (due and due < today) else "normal",
            due_at=due,
            project=item.project,
            source="Ready for construction",
        ))
    inspection_filter = Q(status=Inspection.Status.FAILED) | Q(
        status=Inspection.Status.SCHEDULED,
        scheduled_at__lte=now + timedelta(days=14),
    )
    for inspection in Inspection.objects.filter(
        project_id__in=project_ids,
    ).filter(inspection_filter).select_related("project"):
        failed = inspection.status == Inspection.Status.FAILED
        items.append(_attention_item(
            kind="inspection",
            title=f"{inspection.project.title}: {'Correct failed inspection' if failed else 'Inspection coming up'}",
            description=inspection.corrective_action or inspection.inspection_type,
            priority="urgent" if failed else "high",
            due_at=inspection.scheduled_at,
            project=inspection.project,
            source="Inspection",
        ))
    for request in MaterialRequest.objects.filter(
        project_id__in=project_ids,
        status=MaterialRequest.Status.REQUESTED,
    ).select_related("project"):
        items.append(_attention_item(
            kind="material",
            title=f"{request.project.title}: Material request",
            description=request.description,
            priority="high" if request.needed_by and request.needed_by <= today else "normal",
            due_at=request.needed_by,
            project=request.project,
            source="Field request",
        ))
    for change_order in ChangeOrder.objects.filter(
        project_id__in=project_ids,
        status=ChangeOrder.Status.SENT,
    ).select_related("project"):
        items.append(_attention_item(
            kind="change_order",
            title=f"{change_order.project.title}: CO-{change_order.number} awaiting approval",
            description=change_order.title,
            priority="high",
            due_at=change_order.sent_at,
            project=change_order.project,
            source="Change order",
        ))
    if is_owner(user):
        financial_project_ids = set(project_ids)
    elif is_manager(user):
        financial_project_ids = set(
            projects.filter(
                Q(project_manager=user) | Q(assigned_staff=user)
            ).values_list("pk", flat=True).distinct()
        )
    else:
        financial_project_ids = set()
    for schedule in PaymentSchedule.objects.filter(
        project_id__in=financial_project_ids,
        status__in=[PaymentSchedule.Status.READY, PaymentSchedule.Status.OVERDUE],
    ).select_related("project"):
        items.append(_attention_item(
            kind="draw",
            title=f"{schedule.project.title}: Progress draw ready",
            description=schedule.description,
            priority="high" if schedule.status == PaymentSchedule.Status.OVERDUE else "normal",
            due_at=schedule.due_date,
            project=schedule.project,
            source="Payment schedule",
        ))
    for schedule in PaymentSchedule.objects.filter(
        project_id__in=financial_project_ids,
        external_invoice_status__in=[
            PaymentSchedule.ExternalInvoiceStatus.PENDING,
            PaymentSchedule.ExternalInvoiceStatus.OVERDUE,
            PaymentSchedule.ExternalInvoiceStatus.PAID,
        ],
    ).select_related("project"):
        if not external_estimate_enabled_for(user, project=schedule.project):
            continue
        if schedule.external_invoice_status == PaymentSchedule.ExternalInvoiceStatus.PENDING:
            title = f"{schedule.project.title}: Invoice needs confirmation"
            description = "Check the invoice service and record the invoice event."
            priority = "normal"
        elif schedule.external_invoice_status == PaymentSchedule.ExternalInvoiceStatus.OVERDUE:
            title = f"{schedule.project.title}: Invoice overdue"
            description = "Follow up in the invoice service and update the Grand Coast payment record when confirmed."
            priority = "high"
        elif schedule.payments.filter(voided_at__isnull=True).exists():
            continue
        else:
            title = f"{schedule.project.title}: Record payment"
            description = "Payment was manually confirmed; record the internal payment in Grand Coast."
            priority = "high"
        items.append(_attention_item(
            kind="external_invoice",
            title=title,
            description=description,
            priority=priority,
            due_at=schedule.due_date,
            project=schedule.project,
            source="Invoice",
        ))
    for selection in Selection.objects.filter(
        project_id__in=project_ids,
        status__in=[Selection.Status.SUBMITTED, Selection.Status.PENDING],
    ).select_related("project"):
        if selection.status == Selection.Status.PENDING and selection.due_date and selection.due_date > today:
            continue
        items.append(_attention_item(
            kind="decision",
            title=f"{selection.project.title}: Decision needed for {selection.item_name}",
            description=selection.category,
            priority="high" if selection.due_date and selection.due_date <= today else "normal",
            due_at=selection.due_date,
            project=selection.project,
            source="Client selection",
        ))
    for task in Task.objects.filter(
        project_id__in=project_ids,
        status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED],
        due_date__lt=today,
    ).select_related("project", "assigned_to"):
        items.append(_attention_item(
            kind="overdue_task",
            title=f"{task.project.title}: {task.title}",
            description="Task is overdue.",
            priority="high" if task.status == Task.Status.BLOCKED else "normal",
            due_at=task.due_date,
            project=task.project,
            source="Task",
        ))
    for problem in ProblemReport.objects.filter(
        project_id__in=project_ids,
        status__in=[
            ProblemReport.Status.OPEN,
            ProblemReport.Status.ACKNOWLEDGED,
            ProblemReport.Status.IN_PROGRESS,
        ],
    ).select_related("project", "reported_by", "assigned_to"):
        items.append(_attention_item(
            kind="problem",
            title=f"{problem.project.title}: {problem.title}",
            description=problem.description,
            priority=(
                "urgent" if problem.severity == ProblemReport.Severity.CRITICAL
                else "high" if problem.severity == ProblemReport.Severity.HIGH
                else "normal"
            ),
            due_at=problem.created_at,
            project=problem.project,
            source="Field problem report",
        ))
    for project in projects.filter(
        status__in=[
            Project.Status.PLANNING,
            Project.Status.SELECTIONS,
            Project.Status.CONSTRUCTION,
            Project.Status.FINAL,
        ],
        target_date__lt=today,
    ):
        items.append(_attention_item(
            kind="schedule_risk",
            title=f"{project.title}: Target completion is past due",
            description=project.next_step or "Review the schedule and reset the next action.",
            priority="high",
            due_at=project.target_date,
            project=project,
            source="Schedule risk",
        ))

    if can_manage_sales(user):
        leads = visible_leads(user).select_related("assigned_to", "client")
        for lead in leads.filter(next_action_due__lte=today).exclude(next_action_due=None):
            items.append(_attention_item(
                kind="lead_follow_up",
                title=f"Follow up with {lead.name}",
                description=lead.next_action or "Lead needs a next action.",
                priority="high" if lead.next_action_due < today else "normal",
                due_at=lead.next_action_due,
                lead=lead,
                source="Sales pipeline",
            ))
        for estimate in Estimate.objects.filter(
            Q(lead__in=leads) | Q(client_id__in=leads.values("client_id")),
            status=Estimate.Status.DRAFT,
        ).select_related("lead", "client")[:40]:
            items.append(_attention_item(
                kind="estimate",
                title=f"Estimate #{estimate.number} needs to be sent",
                description=estimate.title,
                priority="normal",
                due_at=estimate.updated_at,
                lead=estimate.lead,
                source="Estimate",
            ))
        for external_estimate in visible_estimates(user).filter(
            external_status__in=[
                Estimate.ExternalEstimateStatus.PENDING,
                Estimate.ExternalEstimateStatus.REJECTED,
                Estimate.ExternalEstimateStatus.APPROVED,
            ],
        ).select_related("lead", "client")[:80]:
            if not external_estimate_enabled_for(user, estimate=external_estimate):
                continue
            linked_project = external_estimate.projects.order_by("-created_at").first()
            if external_estimate.external_status == Estimate.ExternalEstimateStatus.PENDING:
                title = f"Estimate #{external_estimate.number}: Confirm estimate response"
                description = "Check the estimate service and record whether the estimate was approved or rejected."
                priority = "high"
            elif external_estimate.external_status == Estimate.ExternalEstimateStatus.REJECTED:
                title = f"Estimate #{external_estimate.number}: Estimate rejected"
                description = external_estimate.external_status_note or "Follow up or create a new revision."
                priority = "high"
            elif linked_project is None or not Agreement.objects.filter(
                project=linked_project,
                status=Agreement.Status.ACCEPTED,
            ).exists():
                title = f"Estimate #{external_estimate.number}: Approved, agreement or deposit incomplete"
                description = "Continue the Grand Coast agreement, deposit, and project workflow."
                priority = "high"
            else:
                continue
            items.append(_attention_item(
                kind="external_estimate",
                title=title,
                description=description,
                priority=priority,
                due_at=external_estimate.external_status_at or external_estimate.updated_at,
                project=linked_project,
                lead=external_estimate.lead,
                estimate=external_estimate,
                source="Estimate status",
            ))
    if is_owner(user) or is_manager(user):
        for outbox in EmailOutbox.objects.filter(
            status=EmailOutbox.Status.FAILED,
        ).select_related("project")[:20]:
            items.append(_attention_item(
                kind="email_failure",
                title="Email delivery failed",
                description=outbox.subject,
                priority="high",
                due_at=outbox.next_attempt_at,
                project=outbox.project,
                source="Email outbox",
            ))
        financial_projects = [
            project for project in projects
            if project.status != Project.Status.COMPLETE and can_view_financials(user, project)
        ]
        for project in financial_projects:
            summary = project_financial_summary(project)
            if (
                summary["budget_remaining"] < 0
                or (
                    summary["budget_total"] > 0
                    and summary["budget_remaining"] <= 0
                )
            ):
                items.append(_attention_item(
                    kind="budget_risk",
                    title=f"{project.title}: Budget needs review",
                    description=f"Remaining budget {summary['budget_remaining']:.2f} after costs and commitments.",
                    priority="urgent" if summary["budget_remaining"] < 0 else "high",
                    due_at=project.updated_at,
                    project=project,
                    source="Budget risk",
                ))
    priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    items.sort(
        key=lambda item: (
            priority_order.get(item["priority"], 2),
            item["due_at"] is None,
            item["due_at"] or now,
        )
    )
    return items[:limit]


def weekly_project_review(user, *, week_of=None):
    """Return a role-scoped action list for the weekly project review."""
    if not is_staff_user(user):
        raise PermissionDenied("Weekly project reviews are available to staff only.")
    week_of = week_of or timezone.localdate()
    projects = list(
        visible_projects(user)
        .exclude(status=Project.Status.COMPLETE)
        .select_related("client", "project_manager")
    )
    feed = attention_feed(user, limit=400)
    actions_by_project = {}
    company_actions = []
    for item in feed:
        project = item.get("project")
        safe_action = {
            "kind": item["kind"],
            "title": item["title"],
            "description": item["description"],
            "priority": item["priority"],
            "due_at": item["due_at"].isoformat() if hasattr(item["due_at"], "isoformat") else None,
            "source": item["source"],
            "project_id": str(project.pk) if project is not None else None,
            "lead_id": str(item["lead"].pk) if item.get("lead") is not None else None,
            "estimate_id": str(item["estimate"].pk) if item.get("estimate") is not None else None,
        }
        if project is not None:
            actions_by_project.setdefault(project.pk, []).append(safe_action)
        elif item.get("lead") is not None:
            company_actions.append(safe_action)
    reviews = []
    sensitive_events = {
        "payment_recorded",
        "deposit_recorded",
        "cost_recorded",
        "budget_updated",
    }
    for project in projects:
        actions = actions_by_project.get(project.pk, [])
        events = []
        for event in WorkflowEvent.objects.filter(project=project).order_by("-created_at")[:8]:
            if not can_view_financials(user, project) and event.event_type in sensitive_events:
                continue
            events.append({
                "type": event.event_type,
                "created_at": event.created_at.isoformat(),
            })
        review = {
            "project_id": str(project.pk),
            "project_title": project.title,
            "health_status": project.health_status,
            "current_phase": project.operational_phase,
            "next_action": project.next_step,
            "what_happened": events,
            "action_items": actions,
            "behind": project.health_status in {
                Project.HealthStatus.AT_RISK,
                Project.HealthStatus.BLOCKED,
            },
            "blocking": [
                action for action in actions
                if action["kind"] in {"blocker", "inspection", "material", "decision"}
            ],
            "next_payment": None,
        }
        if can_view_financials(user, project):
            summary = project_financial_summary(project)
            review["next_payment"] = {
                "amount": str(summary["next_draw"]),
                "outstanding_balance": str(summary["outstanding_balance"]),
            }
        reviews.append(review)
    return {
        "week_of": week_of.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "projects": reviews,
        "company_actions": company_actions,
    }


def company_metrics(user):
    projects = list(visible_projects(user).select_related("estimate"))
    today = timezone.localdate()
    active_projects = [
        project for project in projects
        if project.status != Project.Status.COMPLETE
    ]
    summaries = [
        project_financial_summary(project)
        for project in projects
        if can_view_financials(user, project)
    ]
    leads = visible_leads(user) if can_manage_sales(user) else Lead.objects.none()
    won_leads = leads.filter(status=Lead.Status.WON).count()
    closed_leads = leads.filter(status__in=[Lead.Status.WON, Lead.Status.LOST]).count()
    conversion_rate = (
        (Decimal(won_leads) / Decimal(closed_leads) * Decimal("100")).quantize(Decimal("0.1"))
        if closed_leads
        else Decimal("0.0")
    )
    financial_projects = [
        project for project in projects
        if can_view_financials(user, project)
    ]
    cash_projection = {
        30: Decimal("0.00"),
        60: Decimal("0.00"),
        90: Decimal("0.00"),
    }
    schedule_risk_projects = 0
    budget_risk_projects = 0
    for project in active_projects:
        overdue_task = project.tasks.filter(
            status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED],
            due_date__lt=today,
        ).exists()
        if (
            project.health_status in {Project.HealthStatus.AT_RISK, Project.HealthStatus.BLOCKED}
            or (project.target_date and project.target_date < today)
            or overdue_task
        ):
            schedule_risk_projects += 1
    for project, summary in zip(financial_projects, summaries):
        if (
            summary["budget_remaining"] < 0
            or (
                summary["budget_total"] > 0
                and summary["budget_remaining"] <= 0
            )
        ):
            budget_risk_projects += 1
        for schedule in project.payment_schedules.prefetch_related("payments").all():
            if schedule.status in {
                PaymentSchedule.Status.PAID,
                PaymentSchedule.Status.WAIVED,
            } or not schedule.due_date:
                continue
            remaining = schedule.remaining_amount
            days_until_due = (schedule.due_date - today).days
            for horizon in cash_projection:
                if days_until_due <= horizon:
                    cash_projection[horizon] += remaining
    pipeline_value = (
        leads.exclude(status__in=[Lead.Status.WON, Lead.Status.LOST])
        .aggregate(total=Sum("budget_amount"))["total"]
        or Decimal("0.00")
    )
    return {
        "pipeline_value": pipeline_value.quantize(Decimal("0.01")),
        "active_contract_value": sum(
            (summary["current_contract"] for summary in summaries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01")),
        "revenue_collected": sum(
            (summary["payments_received"] for summary in summaries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01")),
        "outstanding_receivables": sum(
            (summary["outstanding_balance"] for summary in summaries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01")),
        "upcoming_draws": sum(
            (summary["next_draw"] for summary in summaries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01")),
        "forecast_profit": sum(
            (summary["forecast_profit"] for summary in summaries),
            Decimal("0.00"),
        ).quantize(Decimal("0.01")),
        "active_projects": len(active_projects),
        "won_leads": won_leads,
        "closed_leads": closed_leads,
        "conversion_rate": conversion_rate,
        "schedule_risk_projects": schedule_risk_projects,
        "budget_risk_projects": budget_risk_projects,
        "cash_projection_30": cash_projection[30].quantize(Decimal("0.01")),
        "cash_projection_60": cash_projection[60].quantize(Decimal("0.01")),
        "cash_projection_90": cash_projection[90].quantize(Decimal("0.01")),
    }


def queue_email(
    *,
    recipient,
    subject,
    body,
    actor=None,
    project=None,
    client=None,
    idempotency_key=None,
):
    """Create an outbox entry; dispatch is deliberately separate from writes."""
    if not recipient:
        return None
    if idempotency_key:
        existing = EmailOutbox.objects.filter(
            idempotency_key=str(idempotency_key)
        ).first()
        if existing:
            return existing
    return EmailOutbox.objects.create(
        recipient=str(recipient).strip().lower(),
        subject=str(subject).strip()[:220],
        body=str(body),
        project=project,
        client=client,
        created_by=actor,
        idempotency_key=str(idempotency_key) if idempotency_key else None,
    )
