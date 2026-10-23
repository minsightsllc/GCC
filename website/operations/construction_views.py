from __future__ import annotations

from datetime import date
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import views as existing_views
from .construction_policies import (
    can_manage_external_estimate,
    can_manage_project_operations,
    can_submit_field_work,
    can_view_closeout,
    can_view_financials,
    can_view_media,
    can_view_permit,
    can_view_project_document,
    can_view_subcontractor_assignment,
    can_view_project,
    execution_loop_enabled_for,
    feature_enabled,
    is_field,
    is_owner,
    is_staff_user,
    external_estimate_enabled_for,
    visible_projects,
)
from .construction_services import (
    attention_feed,
    advance_selection,
    company_metrics,
    complete_closeout_item,
    complete_readiness_item,
    create_commitment,
    create_permit,
    create_change_order,
    create_inspection,
    create_project_document,
    create_site_visit,
    create_selection,
    create_project_task,
    create_subcontractor_assignment,
    project_financial_summary,
    record_external_estimate_status,
    record_external_invoice_status,
    record_commitment_status,
    record_cost,
    record_inspection_result,
    record_payment,
    record_permit_status,
    record_subcontractor_assignment_status,
    record_project_task_status,
    record_material_request_status,
    set_milestone_status,
    request_material,
    resolve_warranty_item,
    submit_problem_report,
    submit_daily_report,
    update_readiness_item,
    update_site_visit,
    weekly_project_review,
)
from .construction_forms import (
    AssignmentStatusForm,
    CommitmentForm,
    CommitmentStatusForm,
    CloseoutActionForm,
    CostEntryForm,
    ChangeOrderForm,
    DailyReportForm,
    InspectionForm,
    InspectionResultForm,
    ExternalEstimateStatusForm,
    ExternalInvoiceStatusForm,
    MaterialRequestForm,
    MaterialRequestStatusForm,
    PermitForm,
    PermitStatusForm,
    PaymentRecordForm,
    ProblemReportForm,
    PreconstructionItemForm,
    ProjectTaskForm,
    TaskStatusForm,
    SelectionAdvanceForm,
    SelectionForm,
    SiteVisitForm,
    SubcontractorAssignmentForm,
    WarrantyResolutionForm,
)
from .forms import ProjectDocumentForm, ProjectUpdateForm
from .models import (
    Activity,
    Blocker,
    ChangeOrder,
    Commitment,
    CloseoutItem,
    CostEntry,
    DailyReport,
    Estimate,
    Inspection,
    MaterialRequest,
    Milestone,
    Permit,
    PaymentSchedule,
    PreconstructionItem,
    Project,
    ProjectDocument,
    ScheduleEvent,
    Task,
    Selection,
    SiteVisit,
    Subcontractor,
    SubcontractorAssignment,
    WarrantyItem,
    WorkflowEvent,
)


def _staff_context(request, *, active_section="command-center"):
    can_access_dashboard = existing_views._can_access_dashboard(request.user)
    team_mode = not can_access_dashboard
    nav_counts = existing_views._operations_navigation_counts(
        request.user,
        team_mode=team_mode,
    )
    return {
        "active_section": active_section,
        "team_mode": team_mode,
        "can_access_dashboard": can_access_dashboard,
        "operations_nav_role": "admin" if can_access_dashboard else "employee",
        "operations_nav_counts": nav_counts,
        "can_manage_team": existing_views._can_manage_team(request.user),
        "unread_messages_count": nav_counts["messages"],
        "last_invite_url": request.session.pop("last_invite_url", ""),
        "last_invite_email_status": request.session.pop("last_invite_email_status", ""),
        "last_invite_recipient": request.session.pop("last_invite_recipient", ""),
    }


def _require_operating_system():
    if not feature_enabled("operating_system"):
        raise Http404


@never_cache
@require_GET
@login_required
def legacy_construction_workspace_redirect(request, pk=None):
    """Keep old construction-hub links working without exposing the hub."""
    if not is_staff_user(request.user):
        raise PermissionDenied
    destination = (
        "operations:dashboard-section"
        if existing_views._can_access_dashboard(request.user)
        else "operations:team-section"
    )
    if pk is None:
        return redirect(destination, section="overview")
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    url = reverse(destination, kwargs={"section": "projects"})
    return redirect(f"{url}?project={project.pk}")


def _require_execution_loop(request, project=None):
    _require_operating_system()
    if not execution_loop_enabled_for(request.user, project):
        raise Http404
    if project is not None and not can_view_project(request.user, project):
        raise Http404


def _project_for_execution(request, pk):
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(
        visible_projects(request.user).select_related("lead", "client", "project_manager"),
        pk=pk,
    )
    _require_execution_loop(request, project)
    return project


def _execution_idempotency_key(request, action, *parts):
    supplied = str(request.POST.get("idempotency_key") or "").strip()
    if supplied:
        return supplied
    seed = ":".join(
        ["gcc", "execution", action, str(getattr(request.user, "pk", ""))]
        + [str(part) for part in parts]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _action_url(item):
    if item.get("project"):
        anchor_by_kind = {
            "blocker": "risks",
            "readiness": "readiness",
            "inspection": "permits",
            "material": "field-requests",
            "problem": "field-requests",
            "decision": "selections",
            "change_order": "change-orders",
            "draw": "financials",
            "budget_risk": "financials",
            "overdue_task": "schedule",
            "schedule_risk": "schedule",
            "external_estimate": "commercial",
            "external_invoice": "commercial",
        }
        url = reverse("operations:project-operations", kwargs={"pk": item["project"].pk})
        anchor = anchor_by_kind.get(item.get("kind"))
        return f"{url}#{anchor}" if anchor else url
    if item.get("lead"):
        return reverse("operations:dashboard-section", kwargs={"section": "leads"}) + f"?lead={item['lead'].pk}"
    if item.get("estimate"):
        return reverse("operations:dashboard-section", kwargs={"section": "estimates"}) + f"?estimate={item['estimate'].pk}"
    return reverse("operations:dashboard", kwargs={})


def render_command_center(request):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    feed = attention_feed(request.user)
    for item in feed:
        item["action_url"] = _action_url(item)
        due = item.get("due_at")
        item["due_label"] = (
            due.strftime("%b %d").replace(" 0", " ")
            if hasattr(due, "strftime")
            else "No date"
        )
    projects = list(
        visible_projects(request.user)
        .exclude(status=Project.Status.COMPLETE)
        .select_related("client", "project_manager")
        .prefetch_related("milestones", "blockers")[:12]
    )
    metrics = company_metrics(request.user)
    for project in projects:
        project.open_blocker_count = project.blockers.filter(status=Blocker.Status.OPEN).count()
        project.health_display = project.get_health_status_display()
        if project.open_blocker_count and project.health_status == Project.HealthStatus.ON_TRACK:
            project.health_display = "Watch"
    context = _staff_context(request)
    context.update({
        "attention_items": feed,
        "command_projects": projects,
        "company_metrics": metrics,
        # The Owner is a company-level financial viewer, even when there are
        # no active projects in the dashboard slice. Managers remain scoped
        # to projects they are allowed to view.
        "show_financials": bool(
            is_owner(request.user)
            or any(can_view_financials(request.user, project) for project in visible_projects(request.user))
        ),
        "execution_loop_available": execution_loop_enabled_for(request.user),
    })
    return render(request, "operations/construction_command_center.html", context)


@never_cache
@require_GET
@login_required
def weekly_review(request):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    if not execution_loop_enabled_for(request.user):
        raise Http404
    week_of = timezone.localdate()
    raw_week = str(request.GET.get("week_of") or "").strip()
    if raw_week:
        try:
            week_of = date.fromisoformat(raw_week)
        except ValueError:
            raise ValidationError("The review date must be a valid date.")
    review = weekly_project_review(request.user, week_of=week_of)
    visible = {
        str(project.pk): project
        for project in visible_projects(request.user).select_related("client", "project_manager")
    }
    anchor_by_kind = {
        "readiness": "readiness",
        "inspection": "permits",
        "material": "field-requests",
        "problem": "field-requests",
        "decision": "selections",
        "change_order": "change-orders",
        "draw": "financials",
        "budget_risk": "financials",
        "overdue_task": "schedule",
        "schedule_risk": "schedule",
        "blocker": "risks",
    }
    for project_review in review["projects"]:
        project = visible.get(project_review["project_id"])
        project_review["project"] = project
        project_review["action_url"] = (
            reverse("operations:project-operations", kwargs={"pk": project.pk})
            if project is not None
            else reverse("operations:dashboard")
        )
        for action in project_review["action_items"]:
            if project is None:
                action["action_url"] = reverse("operations:dashboard")
                continue
            anchor = anchor_by_kind.get(action["kind"], "execution-loop")
            action["action_url"] = (
                reverse("operations:project-operations", kwargs={"pk": project.pk})
                + f"#{anchor}"
            )
    context = _staff_context(request, active_section="command-center")
    context.update({
        "weekly_review": review,
        "review_projects": review["projects"],
        "execution_loop_available": True,
    })
    return render(request, "operations/construction_weekly_review.html", context)


@never_cache
@require_GET
@login_required
def command_center(request):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    return redirect("operations:dashboard")


@never_cache
@require_GET
@login_required
def project_operations(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(
        visible_projects(request.user)
        .select_related("client", "project_manager", "estimate")
        .prefetch_related(
            "milestones",
            "permits",
            "assigned_staff",
            "documents",
            "media_assets",
            "site_visits",
            "daily_reports",
            "material_requests",
            "problem_reports",
            "subcontractor_assignments__subcontractor",
            "closeout_items",
            "warranty_items",
            "updates",
        ),
        pk=pk,
    )
    financial_access = can_view_financials(request.user, project)
    external_estimate_enabled = external_estimate_enabled_for(request.user, project=project)
    external_estimate = project.estimate if external_estimate_enabled and project.estimate_id else None
    can_manage_external_status = bool(
        external_estimate_enabled and can_manage_external_estimate(request.user, project=project)
    )
    external_payment_schedules = (
        list(project.payment_schedules.all())
        if external_estimate_enabled and (can_manage_external_status or financial_access)
        else []
    )
    readiness_queryset = project.readiness_items.select_related("owner", "completed_by")
    blockers_queryset = project.blockers.filter(status=Blocker.Status.OPEN).select_related("assigned_to")
    if is_field(request.user):
        readiness_queryset = readiness_queryset.filter(category="schedule")
        blockers_queryset = blockers_queryset.filter(
            Q(assigned_to=request.user)
            | Q(category__in=[
                Blocker.Category.INSPECTION,
                Blocker.Category.MATERIAL,
                Blocker.Category.SUBCONTRACTOR,
                Blocker.Category.SCHEDULE,
            ])
        )
    readiness = list(readiness_queryset)
    blockers = list(blockers_queryset)
    selections = list(project.selections.all())
    inspections = list(project.inspections.select_related("permit"))
    change_orders = list(project.change_orders.all())
    if is_field(request.user):
        change_orders = [
            item for item in change_orders
            if item.status != ChangeOrder.Status.DRAFT
        ]
    payment_schedules = (
        list(project.payment_schedules.prefetch_related("payments"))
        if financial_access
        else []
    )
    project_commitments = (
        list(project.commitments.select_related("subcontractor", "budget_line"))
        if financial_access
        else []
    )
    project_documents = [
        document for document in project.documents.all()
        if can_view_project_document(request.user, document)
    ]
    project_media = [
        asset for asset in project.media_assets.all()
        if can_view_media(request.user, asset)
    ]
    project_tasks = list(
        existing_views._visible_tasks_for_user(request.user)
        .filter(project=project)
        .prefetch_related("assigned_to")[:40]
    )
    project_site_visits = list(project.site_visits.all()[:12])
    if is_field(request.user):
        project_site_visits = [
            visit for visit in project_site_visits
            if visit.assigned_to_id == request.user.pk
        ]
    project_permits = [
        permit for permit in project.permits.all()
        if can_view_permit(request.user, permit)
    ]
    project_assignments = [
        assignment for assignment in project.subcontractor_assignments.all()
        if can_view_subcontractor_assignment(request.user, assignment)
    ]
    project_closeout = [
        item for item in project.closeout_items.all()
        if can_view_closeout(request.user, item)
    ]
    execution_enabled = execution_loop_enabled_for(request.user, project)
    execution_site_visit_rows = [
        {"visit": visit, "form": SiteVisitForm(instance=visit)}
        for visit in project_site_visits
    ] if execution_enabled else []
    execution_readiness_rows = [
        {
            "item": item,
            "form": PreconstructionItemForm(
                instance=item,
                owner_queryset=existing_views._staff_users(),
            ),
        }
        for item in readiness
    ] if execution_enabled and can_manage_project_operations(request.user, project) else []
    execution_inspection_rows = [
        {"inspection": inspection, "form": InspectionResultForm(instance=inspection)}
        for inspection in inspections
    ] if execution_enabled else []
    execution_permit_rows = [
        {"permit": permit, "form": PermitStatusForm(initial={
            "status": permit.status,
            "permit_number": permit.permit_number,
            "expires_at": permit.expires_at,
            "notes": permit.notes,
        })}
        for permit in project_permits
    ] if execution_enabled else []
    execution_selection_rows = [
        {"selection": selection, "form": SelectionAdvanceForm(initial={
            "status": selection.status,
            "client_choice": selection.client_choice,
        })}
        for selection in selections
    ] if execution_enabled else []
    execution_assignment_rows = [
        {"assignment": assignment, "form": AssignmentStatusForm(initial={"status": assignment.status})}
        for assignment in project_assignments
    ] if execution_enabled else []
    can_edit_operations = can_manage_project_operations(request.user, project)
    execution_task_rows = [
        {
            "task": task,
            "form": TaskStatusForm(initial={"status": task.status}),
            "can_update": can_edit_operations or task.assigned_to.filter(pk=request.user.pk).exists(),
        }
        for task in project_tasks
    ] if execution_enabled else []
    execution_material_rows = [
        {
            "request": request,
            "form": MaterialRequestStatusForm(initial={"status": request.status}),
        }
        for request in project.material_requests.all()[:30]
    ] if execution_enabled else []
    execution_closeout_rows = [
        {"item": item, "form": CloseoutActionForm()}
        for item in project_closeout
    ] if execution_enabled else []
    execution_warranty_rows = [
        {"item": item, "form": WarrantyResolutionForm(initial={"status": WarrantyItem.Status.RESOLVED})}
        for item in project.warranty_items.select_related("assigned_to").all()[:30]
    ] if execution_enabled else []
    project_schedule_queryset = ScheduleEvent.objects.filter(project=project).select_related(
        "task",
        "created_by",
    ).prefetch_related("assignees").order_by("start_at")
    if is_field(request.user):
        project_schedule_queryset = project_schedule_queryset.filter(
            Q(assignees=request.user) | Q(task__assigned_to=request.user)
        ).distinct()
    project_schedule_events = list(project_schedule_queryset[:20])
    project_activity = list(project.activities.select_related("actor").all()[:20])
    project_workflow_events = list(project.workflow_events.select_related("actor").all()[:20])
    sensitive_event_types = {
        "deposit_recorded",
        "payment_recorded",
        "cost_recorded",
        "cost_voided",
        "commitment_created",
        "commitment_status_updated",
        "change_order_approved",
        "change_order_declined",
    }
    sensitive_activity_messages = {
        "Deposit recorded",
        "Payment recorded",
        "Cost recorded",
        "Cost voided",
        "Commitment recorded",
        "Change order approved",
    }
    if not financial_access:
        project_activity = [
            activity for activity in project_activity
            if activity.message not in sensitive_activity_messages
        ]
        project_workflow_events = [
            event for event in project_workflow_events
            if event.event_type not in sensitive_event_types
        ]
    for event in project_workflow_events:
        event.display_event_type = event.event_type.replace("_", " ").title()
    project_updates = list(project.updates.select_related("created_by").all()[:12])
    if is_field(request.user):
        project_updates = [
            update for update in project_updates
            if update.visibility == "client"
        ]
    project_calendar_url = (
        f"{reverse('operations:dashboard-section', kwargs={'section': 'calendar'})}"
        f"?new=event&project={project.pk}"
    )
    data = _staff_context(request, active_section="projects")
    data.update({
        "operations_project": project,
        "readiness_items": readiness,
        "blockers": blockers,
        "selections": selections,
        "inspections": inspections,
        "change_orders": change_orders,
        "payment_schedules": payment_schedules,
        "project_documents": project_documents[:40],
        "project_media": project_media[:40],
        "project_site_visits": project_site_visits,
        "project_tasks": project_tasks,
        "project_daily_reports": list(project.daily_reports.select_related("submitted_by").all()[:12]),
        "project_material_requests": list(project.material_requests.select_related("requested_by").all()[:12]),
        "project_problem_reports": list(project.problem_reports.select_related("reported_by", "assigned_to").all()[:12]),
        "project_field_work_count": project.material_requests.count() + project.problem_reports.count(),
        "project_assignments": project_assignments[:30],
        "project_closeout": project_closeout[:30],
        "project_warranty": list(project.warranty_items.select_related("assigned_to").all()[:30]),
        "project_updates": project_updates,
        "project_update_form": (
            ProjectUpdateForm()
            if execution_enabled and can_manage_project_operations(request.user, project)
            else None
        ),
        "project_update_idempotency_key": str(uuid.uuid4()),
        "project_activity": project_activity,
        "project_workflow_events": project_workflow_events,
        "project_schedule_events": project_schedule_events,
        "project_calendar_url": project_calendar_url,
        "can_manage_schedule": existing_views._can_manage_schedule(request.user),
        "project_permits": project_permits,
        "execution_loop_enabled": execution_enabled,
        "execution_readiness_rows": execution_readiness_rows,
        "execution_site_visit_rows": execution_site_visit_rows,
        "execution_inspection_rows": execution_inspection_rows,
        "execution_permit_rows": execution_permit_rows,
        "execution_selection_rows": execution_selection_rows,
        "execution_assignment_rows": execution_assignment_rows,
        "execution_task_rows": execution_task_rows,
        "execution_material_rows": execution_material_rows,
        "execution_closeout_rows": execution_closeout_rows,
        "execution_warranty_rows": execution_warranty_rows,
        "site_visit_form": SiteVisitForm() if execution_enabled and can_manage_project_operations(request.user, project) else None,
        "permit_form": PermitForm() if execution_enabled and can_manage_project_operations(request.user, project) else None,
        "inspection_form": (
            InspectionForm(project=project, permit_queryset=project.permits.all())
            if execution_enabled and can_manage_project_operations(request.user, project)
            else None
        ),
        "selection_form": SelectionForm() if execution_enabled and can_manage_project_operations(request.user, project) else None,
        "daily_report_form": DailyReportForm() if execution_enabled and can_submit_field_work(request.user, project) else None,
        "material_request_form": MaterialRequestForm() if execution_enabled and can_submit_field_work(request.user, project) else None,
        "problem_report_form": ProblemReportForm() if execution_enabled and can_submit_field_work(request.user, project) else None,
        "assignment_form": (
            SubcontractorAssignmentForm(
                project=project,
                subcontractor_queryset=Subcontractor.objects.filter(status=Subcontractor.Status.ACTIVE),
                task_queryset=project.tasks.all(),
            )
            if execution_enabled and can_manage_project_operations(request.user, project)
            else None
        ),
        "task_form": (
            ProjectTaskForm(
                project=project,
                staff_queryset=existing_views._staff_users(),
            )
            if execution_enabled and can_manage_project_operations(request.user, project)
            else None
        ),
        "document_form": (
            ProjectDocumentForm(
                project_queryset=Project.objects.filter(pk=project.pk),
            )
            if execution_enabled and can_manage_project_operations(request.user, project)
            else None
        ),
        "payment_form": (
            PaymentRecordForm(project=project, schedule_queryset=project.payment_schedules.all())
            if execution_enabled and financial_access
            else None
        ),
        "cost_form": (
            CostEntryForm(project=project, budget_line_queryset=project.budget_lines.all())
            if execution_enabled and financial_access
            else None
        ),
        "commitment_form": (
            CommitmentForm(
                project=project,
                budget_line_queryset=project.budget_lines.all(),
                subcontractor_queryset=Subcontractor.objects.filter(status=Subcontractor.Status.ACTIVE),
            )
            if execution_enabled and financial_access
            else None
        ),
        "execution_commitment_rows": [
            {"commitment": commitment, "form": CommitmentStatusForm(initial={"status": commitment.status})}
            for commitment in project_commitments[:40]
        ] if execution_enabled and financial_access else [],
        "project_commitments": project_commitments,
        "change_order_form": ChangeOrderForm(),
        "project_financials": project_financial_summary(project) if financial_access else None,
        "can_view_project_financials": financial_access,
        "external_estimate_enabled": external_estimate_enabled,
        "external_status_visible": bool(external_estimate_enabled and external_estimate),
        "external_estimate": external_estimate,
        "external_estimate_form": (
            ExternalEstimateStatusForm(instance=external_estimate)
            if can_manage_external_status and external_estimate
            else None
        ),
        "can_manage_external_estimate": can_manage_external_status,
        "external_payment_rows": [
            {
                "schedule": schedule,
                "form": ExternalInvoiceStatusForm(instance=schedule),
            }
            for schedule in external_payment_schedules
        ] if can_manage_external_status else [],
        "external_payment_schedules": external_payment_schedules,
        "external_status_idempotency_key": str(uuid.uuid4()),
        "show_inspection_details": not is_field(request.user),
        "can_edit_operations": can_edit_operations,
        "today": timezone.localdate(),
    })
    return render(request, "operations/construction_project_operations.html", data)


@require_POST
@login_required
def external_estimate_status(request, pk):
    _require_operating_system()
    estimate = get_object_or_404(
        Estimate.objects.select_related("lead", "client"),
        pk=pk,
    )
    if not can_manage_external_estimate(request.user, estimate=estimate):
        raise PermissionDenied
    form = ExternalEstimateStatusForm(request.POST, instance=estimate)
    if not form.is_valid():
        raise ValidationError("Please provide a valid estimate link and status.")
    values = form.cleaned_data
    updated, changed = record_external_estimate_status(
        estimate,
        actor=request.user,
        status=values["external_status"],
        external_url=values.get("external_url"),
        external_client_visible=values.get("external_client_visible"),
        expected_updated_at=request.POST.get("expected_updated_at"),
        idempotency_key=_execution_idempotency_key(
            request,
            "external-estimate-status",
            estimate.pk,
            values["external_status"],
        ),
    )
    if changed and updated.external_status == Estimate.ExternalEstimateStatus.APPROVED:
        messages.success(request, "External approval recorded. Next: create the project from this estimate.")
    elif changed and updated.external_status == Estimate.ExternalEstimateStatus.REJECTED:
        messages.success(request, "External decline recorded. The estimate remains closed in GCC.")
    elif changed:
        messages.success(request, "Estimate link and status saved.")
    else:
        messages.info(request, "This estimate status was already recorded.")
    project = updated.projects.order_by("-created_at").first()
    if project is not None:
        return redirect("operations:project-operations", pk=project.pk)
    return redirect(f"{reverse('operations:dashboard-section', kwargs={'section': 'estimates'})}?estimate={updated.pk}")


@require_POST
@login_required
def external_invoice_status(request, pk):
    _require_operating_system()
    schedule = get_object_or_404(
        PaymentSchedule.objects.select_related("project"),
        pk=pk,
    )
    if not can_manage_external_estimate(request.user, project=schedule.project):
        raise PermissionDenied
    form = ExternalInvoiceStatusForm(request.POST, instance=schedule)
    if not form.is_valid():
        raise ValidationError("Please provide a valid invoice link and status.")
    values = form.cleaned_data
    record_external_invoice_status(
        schedule,
        actor=request.user,
        status=values["external_invoice_status"],
        external_invoice_url=values.get("external_invoice_url"),
        external_client_visible=values.get("external_client_visible"),
        expected_updated_at=request.POST.get("expected_updated_at"),
        idempotency_key=_execution_idempotency_key(
            request,
            "external-invoice-status",
            schedule.pk,
            values["external_invoice_status"],
        ),
    )
    return redirect("operations:project-operations", pk=schedule.project_id)


@require_POST
@login_required
def readiness_complete(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    item = get_object_or_404(PreconstructionItem.objects.select_related("project"), pk=pk)
    if not visible_projects(request.user).filter(pk=item.project_id).exists():
        raise Http404
    try:
        complete_readiness_item(
            item,
            actor=request.user,
            notes=request.POST.get("notes"),
            idempotency_key=request.POST.get("idempotency_key") or None,
        )
    except (PermissionDenied, ValidationError):
        raise
    return redirect("operations:project-operations", pk=item.project_id)


@require_POST
@login_required
def readiness_update(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    item = get_object_or_404(PreconstructionItem.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, item.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = PreconstructionItemForm(
        request.POST,
        instance=item,
        owner_queryset=existing_views._staff_users(),
    )
    if not form.is_valid():
        raise ValidationError("Please provide valid readiness details.")
    update_readiness_item(
        item,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "readiness-update",
            item.pk,
            item.updated_at,
            form.cleaned_data.get("owner"),
            form.cleaned_data.get("due_date"),
        ),
        **form.cleaned_data,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_change_order_create(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = ChangeOrderForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid change order.")
    values = form.cleaned_data
    status = (
        ChangeOrder.Status.SENT
        if request.POST.get("send")
        else ChangeOrder.Status.DRAFT
    )
    idempotency_key = request.POST.get("idempotency_key") or str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"gcc:change-order:{request.user.pk}:{project.pk}:{values['title']}:{values['price_impact']}:{status}",
        )
    )
    change_order, created = create_change_order(
        project,
        actor=request.user,
        status=status,
        idempotency_key=idempotency_key,
        **values,
    )
    if created and status == ChangeOrder.Status.SENT and project.client_id:
        from .notifications import queue_client_notifications

        queue_client_notifications(
            [project.client],
            kind="change-order-ready",
            title=f"Change order CO-{change_order.number} is ready",
            body=f"{change_order.title} is waiting for your approval.",
            destination_url=existing_views._portal_notification_url(
                project.client,
                project=project,
            ),
            metadata={"change_order_id": str(change_order.pk)},
            created_by=request.user,
            project=project,
            exclude_clients=[project.client],
        )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_site_visit_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    if not project.lead_id:
        raise ValidationError("This project is not linked to a lead yet.")
    form = SiteVisitForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid site visit.")
    values = form.cleaned_data
    create_site_visit(
        project.lead,
        actor=request.user,
        project=project,
        idempotency_key=_execution_idempotency_key(
            request,
            "site-visit-create",
            project.pk,
            values.get("scheduled_at"),
            values.get("scope"),
            values.get("address"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_site_visit_update(request, pk):
    visit = get_object_or_404(SiteVisit.objects.select_related("project", "lead"), pk=pk)
    if visit.project_id is None:
        raise Http404
    project = visit.project
    _require_execution_loop(request, project)
    if not is_staff_user(request.user):
        raise PermissionDenied
    form = SiteVisitForm(request.POST, instance=visit)
    if not form.is_valid():
        raise ValidationError("Please provide a valid site visit update.")
    values = form.cleaned_data
    update_site_visit(
        visit,
        actor=request.user,
        updates=values,
        complete=bool(request.POST.get("complete")),
        idempotency_key=_execution_idempotency_key(
            request,
            "site-visit-update",
            visit.pk,
            values.get("scheduled_at"),
            values.get("scope"),
            values.get("measurements"),
            bool(request.POST.get("complete")),
        ),
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_permit_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = PermitForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid permit.")
    values = form.cleaned_data
    create_permit(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "permit-create",
            project.pk,
            values.get("permit_type"),
            values.get("jurisdiction"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_permit_status(request, pk):
    permit = get_object_or_404(Permit.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, permit.project_id)
    if not can_view_permit(request.user, permit) or not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = PermitStatusForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid permit update.")
    values = form.cleaned_data
    record_permit_status(
        permit,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "permit-status", permit.pk, values.get("status")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_inspection_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = InspectionForm(request.POST, project=project, permit_queryset=project.permits.all())
    if not form.is_valid():
        raise ValidationError("Please provide a valid inspection.")
    values = form.cleaned_data
    create_inspection(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "inspection-create",
            project.pk,
            values.get("inspection_type"),
            values.get("scheduled_at"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_inspection_result(request, pk):
    inspection = get_object_or_404(Inspection.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, inspection.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = InspectionResultForm(request.POST, instance=inspection)
    if not form.is_valid():
        raise ValidationError("Please provide a valid inspection result.")
    values = form.cleaned_data
    record_inspection_result(
        inspection,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "inspection-result", inspection.pk, values.get("status")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_selection_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = SelectionForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid selection.")
    values = form.cleaned_data
    create_selection(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "selection-create",
            project.pk,
            values.get("category"),
            values.get("item_name"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_selection_advance(request, pk):
    selection = get_object_or_404(Selection.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, selection.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = SelectionAdvanceForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid selection status.")
    values = form.cleaned_data
    advance_selection(
        selection,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "selection-advance", selection.pk, values.get("status")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_payment_record(request, pk):
    project = _project_for_execution(request, pk)
    if not can_view_financials(request.user, project):
        raise PermissionDenied
    form = PaymentRecordForm(request.POST, project=project, schedule_queryset=project.payment_schedules.all())
    if not form.is_valid():
        raise ValidationError("Please provide a valid payment.")
    values = form.cleaned_data
    record_payment(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "payment-record", project.pk, values.get("amount"), values.get("received_on"), values.get("reference")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_cost_record(request, pk):
    project = _project_for_execution(request, pk)
    if not can_view_financials(request.user, project):
        raise PermissionDenied
    form = CostEntryForm(request.POST, project=project, budget_line_queryset=project.budget_lines.all())
    if not form.is_valid():
        raise ValidationError("Please provide a valid cost entry.")
    values = form.cleaned_data
    record_cost(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "cost-record", project.pk, values.get("description"), values.get("amount"), values.get("incurred_on")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_commitment_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_view_financials(request.user, project):
        raise PermissionDenied
    form = CommitmentForm(
        request.POST,
        project=project,
        budget_line_queryset=project.budget_lines.all(),
        subcontractor_queryset=Subcontractor.objects.filter(status=Subcontractor.Status.ACTIVE),
    )
    if not form.is_valid():
        raise ValidationError("Please provide a valid commitment.")
    values = form.cleaned_data
    create_commitment(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "commitment-create",
            project.pk,
            values.get("description"),
            values.get("amount"),
            values.get("due_date"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_commitment_status(request, pk):
    commitment = get_object_or_404(Commitment.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, commitment.project_id)
    if not can_view_financials(request.user, project):
        raise PermissionDenied
    form = CommitmentStatusForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid commitment status.")
    record_commitment_status(
        commitment,
        actor=request.user,
        status=form.cleaned_data["status"],
        idempotency_key=_execution_idempotency_key(
            request,
            "commitment-status",
            commitment.pk,
            form.cleaned_data["status"],
        ),
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_assignment_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = SubcontractorAssignmentForm(
        request.POST,
        project=project,
        subcontractor_queryset=Subcontractor.objects.filter(status=Subcontractor.Status.ACTIVE),
        task_queryset=project.tasks.all(),
    )
    if not form.is_valid():
        raise ValidationError("Please provide a valid subcontractor assignment.")
    values = form.cleaned_data
    create_subcontractor_assignment(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "assignment-create", project.pk, values.get("work_package"), values.get("subcontractor")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_assignment_status(request, pk):
    assignment = get_object_or_404(SubcontractorAssignment.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, assignment.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = AssignmentStatusForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid assignment status.")
    record_subcontractor_assignment_status(
        assignment,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "assignment-status", assignment.pk, form.cleaned_data["status"]),
        **form.cleaned_data,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_task_create(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = ProjectTaskForm(
        request.POST,
        project=project,
        staff_queryset=existing_views._staff_users(),
    )
    if not form.is_valid():
        raise ValidationError("Please provide a valid project task.")
    values = form.cleaned_data
    create_project_task(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(
            request,
            "task-create",
            project.pk,
            values.get("title"),
            values.get("due_date"),
        ),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_task_status(request, pk):
    task = get_object_or_404(Task.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, task.project_id)
    if not (
        can_manage_project_operations(request.user, project)
        or (is_field(request.user) and task.assigned_to.filter(pk=request.user.pk).exists())
    ):
        raise PermissionDenied
    form = TaskStatusForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid task status.")
    idempotency_key = str(
        request.POST.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip() or str(uuid.uuid4())
    record_project_task_status(
        task,
        actor=request.user,
        status=form.cleaned_data["status"],
        idempotency_key=idempotency_key,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_milestone_status(request, pk):
    milestone = get_object_or_404(Milestone.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, milestone.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    set_milestone_status(
        milestone,
        actor=request.user,
        is_complete=request.POST.get("is_complete") == "1",
        idempotency_key=str(request.POST.get("idempotency_key") or "").strip() or None,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_document_upload(request, pk):
    project = _project_for_execution(request, pk)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = ProjectDocumentForm(
        request.POST,
        request.FILES,
        project_queryset=Project.objects.filter(pk=project.pk),
    )
    if not form.is_valid():
        raise ValidationError("Please provide a valid protected project document.")
    document = form.save(commit=False)
    if document.project_id != project.pk:
        raise PermissionDenied
    document.uploaded_by = request.user
    create_project_document(
        project,
        actor=request.user,
        title=document.title,
        category=document.category,
        description=document.description,
        file=document.file,
        visibility=document.visibility,
        idempotency_key=_execution_idempotency_key(
            request,
            "document-upload",
            project.pk,
            document.title,
            document.category,
            getattr(document.file, "name", ""),
        ),
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_daily_report(request, pk):
    project = _project_for_execution(request, pk)
    if not can_submit_field_work(request.user, project):
        raise PermissionDenied
    form_data = request.POST.copy()
    form_data.setdefault("report_date", timezone.localdate().isoformat())
    form = DailyReportForm(form_data)
    if not form.is_valid():
        raise ValidationError("Please provide a valid daily report.")
    values = form.cleaned_data
    submit_daily_report(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "daily-report", project.pk, values.get("report_date"), values.get("summary")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_material_request(request, pk):
    project = _project_for_execution(request, pk)
    if not can_submit_field_work(request.user, project):
        raise PermissionDenied
    form = MaterialRequestForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid material request.")
    values = form.cleaned_data
    request_material(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "material-request", project.pk, values.get("description"), values.get("needed_by")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_material_request_status(request, pk):
    material_request = get_object_or_404(
        MaterialRequest.objects.select_related("project"),
        pk=pk,
    )
    project = _project_for_execution(request, material_request.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = MaterialRequestStatusForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid material request status.")
    record_material_request_status(
        material_request,
        actor=request.user,
        status=form.cleaned_data["status"],
        idempotency_key=_execution_idempotency_key(
            request,
            "material-request-status",
            material_request.pk,
            form.cleaned_data["status"],
        ),
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_problem_report(request, pk):
    project = _project_for_execution(request, pk)
    if not can_submit_field_work(request.user, project):
        raise PermissionDenied
    form = ProblemReportForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid problem report.")
    values = form.cleaned_data
    submit_problem_report(
        project,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "problem-report", project.pk, values.get("title"), values.get("description")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_closeout_action(request, pk):
    item = get_object_or_404(CloseoutItem.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, item.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = CloseoutActionForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid closeout action.")
    values = form.cleaned_data
    complete_closeout_item(
        item,
        actor=request.user,
        status=values["status"],
        notes=values.get("notes"),
        idempotency_key=_execution_idempotency_key(request, "closeout", item.pk, values.get("status")),
    )
    return redirect("operations:project-operations", pk=project.pk)


@require_POST
@login_required
def project_warranty_resolution(request, pk):
    item = get_object_or_404(WarrantyItem.objects.select_related("project"), pk=pk)
    project = _project_for_execution(request, item.project_id)
    if not can_manage_project_operations(request.user, project):
        raise PermissionDenied
    form = WarrantyResolutionForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid warranty resolution.")
    values = form.cleaned_data
    resolve_warranty_item(
        item,
        actor=request.user,
        idempotency_key=_execution_idempotency_key(request, "warranty", item.pk, values.get("status")),
        **values,
    )
    return redirect("operations:project-operations", pk=project.pk)


@never_cache
@require_GET
@login_required
def field_today(request):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    today = timezone.localdate()
    projects = list(
        visible_projects(request.user)
        .exclude(status=Project.Status.COMPLETE)
        .select_related("client", "project_manager")
        .prefetch_related("milestones", "assigned_staff")
    )
    visible_tasks = existing_views._visible_tasks_for_user(request.user)
    visible_events = existing_views._visible_team_schedule_for_user(request.user)
    for project in projects:
        project.today_tasks = list(
            visible_tasks.filter(project=project).exclude(status="complete").order_by("due_date", "-priority")[:12]
        )
        project.today_events = list(
            visible_events.filter(project=project, start_at__date=today).order_by("start_at")[:8]
        )
        project.material_count = project.material_requests.filter(status=MaterialRequest.Status.REQUESTED).count()
        project.recent_report = project.daily_reports.order_by("-report_date", "-created_at").first()
    nav_counts = existing_views._operations_navigation_counts(request.user, team_mode=True)
    context = {
        "active_section": "projects",
        "team_mode": True,
        "operations_nav_role": "employee",
        "operations_nav_counts": nav_counts,
        "can_manage_team": False,
        "unread_messages_count": 0,
        "field_projects": projects,
        "field_today": today,
        "daily_report_form": DailyReportForm(),
        "material_request_form": MaterialRequestForm(),
        "problem_report_form": ProblemReportForm(),
    }
    return render(request, "operations/construction_field_today.html", context)


@require_POST
@login_required
def field_daily_report(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    form_data = request.POST.copy()
    form_data.setdefault("report_date", timezone.localdate().isoformat())
    form = DailyReportForm(form_data)
    if not form.is_valid():
        raise ValidationError("Please provide a valid daily report.")
    values = form.cleaned_data
    idempotency_key = request.POST.get("idempotency_key") or str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"gcc:daily-report:{request.user.pk}:{project.pk}:{values['report_date'].isoformat()}",
        )
    )
    submit_daily_report(
        project,
        actor=request.user,
        idempotency_key=idempotency_key,
        **values,
    )
    return redirect("operations:field-today")


@require_POST
@login_required
def field_material_request(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    form = MaterialRequestForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid material request.")
    values = form.cleaned_data
    idempotency_key = request.POST.get("idempotency_key") or str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "gcc:material-request:"
            f"{request.user.pk}:{project.pk}:{values['description']}:{values['quantity']}:"
            f"{values['needed_by'] or ''}",
        )
    )
    request_material(
        project,
        actor=request.user,
        idempotency_key=idempotency_key,
        **values,
    )
    return redirect("operations:field-today")


@require_POST
@login_required
def field_problem_report(request, pk):
    _require_operating_system()
    if not is_staff_user(request.user):
        raise PermissionDenied
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    form = ProblemReportForm(request.POST)
    if not form.is_valid():
        raise ValidationError("Please provide a valid problem report.")
    values = form.cleaned_data
    idempotency_key = request.POST.get("idempotency_key") or str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"gcc:problem-report:{request.user.pk}:{project.pk}:{values['title']}:{values['description']}",
        )
    )
    submit_problem_report(
        project,
        actor=request.user,
        idempotency_key=idempotency_key,
        **values,
    )
    return redirect("operations:field-today")
