"""Disposable full-lifecycle simulation for Grand Coast Operations.

The runner uses the same transactional service commands as the web, API, and
WebView surfaces. It is intended for an isolated temporary database only.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from .construction_services import (
    accept_agreement,
    advance_selection,
    approve_change_order,
    complete_closeout_item,
    complete_readiness_item,
    complete_site_visit,
    convert_lead,
    create_change_order,
    create_commitment,
    create_inspection,
    create_lead,
    create_permit,
    create_project_document,
    create_project_from_estimate,
    create_project_task,
    create_selection,
    create_site_visit,
    create_subcontractor_assignment,
    initialize_readiness,
    initialize_warranty,
    project_financial_summary,
    record_cost,
    record_deposit,
    record_inspection_result,
    record_material_request_status,
    record_external_estimate_status,
    record_payment,
    record_permit_status,
    record_project_task_status,
    record_subcontractor_assignment_status,
    request_material,
    resolve_problem_report,
    resolve_warranty_item,
    set_milestone_status,
    submit_daily_report,
    submit_problem_report,
)
from .models import (
    Agreement,
    BudgetLine,
    ChangeOrder,
    Client,
    ClientNotification,
    Commitment,
    EmailOutbox,
    EmployeeNotification,
    EmployeeProfile,
    Estimate,
    Inspection,
    Lead,
    MaterialRequest,
    MediaAsset,
    PaymentRecord,
    PaymentSchedule,
    Permit,
    PreconstructionItem,
    ProblemReport,
    Project,
    ProjectDocument,
    ScheduleEvent,
    Selection,
    SiteVisit,
    Subcontractor,
    SubcontractorAssignment,
    Task,
    WarrantyItem,
    WorkflowEvent,
)
from .notifications import queue_client_notifications, queue_employee_notifications
from .services import ensure_role_groups
from .views import _calendar_conflicts, _execution_calendar_events


SIMULATION_PREFIX = "sim-"
SIMULATION_DOMAIN = "sim.invalid"


def _key(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"grand-coast-simulation:{label}"))


def _check(checks, name, condition, detail=""):
    if not condition:
        raise AssertionError(f"Simulation check failed: {name}. {detail}".strip())
    checks.append({"name": name, "passed": True, "detail": str(detail or "")})


def _make_user(*, username, email, password, group=None, job_title="", is_staff=False, is_superuser=False):
    user = get_user_model().objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=username.replace(SIMULATION_PREFIX, "").replace("-", " ").title(),
        is_staff=is_staff,
        is_superuser=is_superuser,
    )
    if group is not None:
        user.groups.add(group)
    if is_staff:
        EmployeeProfile.objects.create(user=user, job_title=job_title)
    return user


def _pdf_file(name="simulation.pdf"):
    return SimpleUploadedFile(
        name,
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n",
        content_type="application/pdf",
    )


def _photo_file(name="progress.jpg"):
    return SimpleUploadedFile(
        name,
        b"\xff\xd8\xff\xe0" + b"simulated-progress-photo",
        content_type="image/jpeg",
    )


def _create_accounts():
    groups = ensure_role_groups()
    passwords = {
        "owner": "SimOnly-Owner-2026!",
        "office": "SimOnly-Office-2026!",
        "manager": "SimOnly-Manager-2026!",
        "sales": "SimOnly-Sales-2026!",
        "field": "SimOnly-Field-2026!",
        "subcontractor": "SimOnly-Subcontractor-2026!",
        "client": "SimOnly-Client-2026!",
        "unauthorized": "SimOnly-Unauthorized-2026!",
    }
    common = {"is_staff": True}
    accounts = {
        "owner": _make_user(username="sim-owner", email=f"owner@{SIMULATION_DOMAIN}", password=passwords["owner"], group=groups["Owner"], job_title="Owner", is_superuser=True, **common),
        "office": _make_user(username="sim-office", email=f"office@{SIMULATION_DOMAIN}", password=passwords["office"], group=groups["Office"], job_title="Operations office", **common),
        "manager": _make_user(username="sim-manager", email=f"manager@{SIMULATION_DOMAIN}", password=passwords["manager"], group=groups["Manager"], job_title="Project manager", **common),
        "sales": _make_user(username="sim-sales", email=f"sales@{SIMULATION_DOMAIN}", password=passwords["sales"], group=groups["Sales"], job_title="Sales coordinator", **common),
        "field": _make_user(username="sim-field", email=f"field@{SIMULATION_DOMAIN}", password=passwords["field"], group=groups["Field"], job_title="Field lead", **common),
        "subcontractor_user": _make_user(username="sim-subcontractor", email=f"subcontractor@{SIMULATION_DOMAIN}", password=passwords["subcontractor"]),
        "client_user": _make_user(username="sim-client", email=f"client@{SIMULATION_DOMAIN}", password=passwords["client"]),
        "unauthorized": _make_user(username="sim-unauthorized", email=f"unauthorized@{SIMULATION_DOMAIN}", password=passwords["unauthorized"], group=groups["Field"], job_title="Unassigned field user", **common),
    }
    accounts["passwords"] = passwords
    return accounts


def _run_inquiry(accounts, checks):
    owner = accounts["owner"]
    manager = accounts["manager"]
    sales = accounts["sales"]
    field = accounts["field"]
    client_user = accounts["client_user"]

    lead, created = create_lead(
        actor=sales,
        name="Jordan and Casey Hart",
        email=client_user.email,
        phone="805-555-0199",
        service="Whole-home renovation",
        location="Ventura, CA",
        budget="$175,000 - $225,000",
        budget_amount=Decimal("200000.00"),
        timeline="Start in 8 weeks",
        source="Website inquiry",
        note="Renovate kitchen, primary suite, and the rear addition with an occupied-home plan.",
        assigned_to=manager,
        address_line1="418 Harbor View Drive",
        city="Ventura",
        state="CA",
        postal_code="93001",
        idempotency_key=_key("inquiry"),
    )
    lead_retry, retry_created = create_lead(
        actor=sales,
        name=lead.name,
        email=lead.email,
        service=lead.service,
        location=lead.location,
        assigned_to=manager,
        idempotency_key=_key("inquiry"),
    )
    _check(checks, "dummy role accounts created", get_user_model().objects.filter(username__startswith=SIMULATION_PREFIX).count() == 8)
    _check(checks, "lead retry is idempotent", lead.pk == lead_retry.pk and created and not retry_created)

    client, client_created = convert_lead(lead, actor=sales, idempotency_key=_key("lead-to-client"))
    client_retry, client_retry_created = convert_lead(lead, actor=sales, idempotency_key=_key("lead-to-client"))
    client.user = client_user
    client.save(update_fields=["user", "updated_at"])
    _check(checks, "lead conversion does not duplicate client", client.pk == client_retry.pk and client_created and not client_retry_created and Client.objects.filter(email=client.email).count() == 1)

    visit, visit_created = create_site_visit(
        lead,
        actor=sales,
        assigned_to=field,
        scheduled_at=timezone.now() + timedelta(days=1),
        address="418 Harbor View Drive, Ventura, CA 93001",
        scope="Kitchen reconfiguration, primary suite update, rear addition enclosure.",
        measurements="Kitchen 15 ft x 18 ft; addition 12 ft x 20 ft; ceiling 9 ft.",
        client_requests="Keep the home occupied and phase demolition around the school calendar.",
        existing_conditions="Aging electrical panel and uneven rear slab.",
        potential_additional_work="Panel upgrade and slab correction may be required.",
        notes="Bring finish samples and a preliminary schedule.",
        idempotency_key=_key("site-visit"),
    )
    visit_retry, visit_retry_created = create_site_visit(lead, actor=sales, assigned_to=field, scheduled_at=visit.scheduled_at, idempotency_key=_key("site-visit"))
    _check(checks, "site visit retry is idempotent", visit.pk == visit_retry.pk and visit_created and not visit_retry_created)
    visit = complete_site_visit(
        visit,
        actor=field,
        updates={"notes": "Field capture complete; move to estimating."},
        idempotency_key=_key("site-visit-complete"),
    )
    complete_site_visit(visit, actor=field, idempotency_key=_key("site-visit-complete"))
    lead.refresh_from_db()
    _check(checks, "site visit advances lead to estimating", lead.workflow_stage == Lead.WorkflowStage.ESTIMATING and visit.status == SiteVisit.Status.COMPLETED)

    estimate = Estimate.objects.create(
        number=9201,
        lead=lead,
        client=client,
        title="Harbor View Whole-home Renovation",
        status=Estimate.Status.DRAFT,
        notes="Staff note: Final scope verified in the external estimate service.",
        external_url="https://estimates.example.com/simulation/harbor-view",
        external_total=Decimal("190000.00"),
        external_deposit_amount=Decimal("38000.00"),
        created_by=sales,
    )
    _check(checks, "estimate uses an external link without copying provider line items", bool(estimate.external_url) and estimate.line_items.count() == 0 and estimate.external_total == Decimal("190000.00"))
    estimate, sent = record_external_estimate_status(
        estimate,
        actor=owner,
        status=Estimate.ExternalEstimateStatus.SENT,
        external_client_visible=True,
        idempotency_key=_key("estimate-send"),
    )
    estimate_retry, sent_retry = record_external_estimate_status(
        estimate,
        actor=owner,
        status=Estimate.ExternalEstimateStatus.SENT,
        external_client_visible=True,
        idempotency_key=_key("estimate-send"),
    )
    _check(checks, "external estimate delivery is published and retry-safe", sent and not sent_retry and estimate.pk == estimate_retry.pk and estimate.external_client_visible)
    estimate, approved = record_external_estimate_status(
        estimate,
        actor=owner,
        status=Estimate.ExternalEstimateStatus.APPROVED,
        idempotency_key=_key("estimate-approval"),
    )
    estimate_retry, approval_retry = record_external_estimate_status(
        estimate,
        actor=owner,
        status=Estimate.ExternalEstimateStatus.APPROVED,
        idempotency_key=_key("estimate-approval"),
    )
    _check(checks, "external approval is staff-recorded and retry-safe", approved and not approval_retry and estimate.pk == estimate_retry.pk and estimate.locked_at is not None and estimate.accepted_by_id is None and estimate.external_status_by_id == owner.pk)

    project, project_created = create_project_from_estimate(estimate, actor=owner, idempotency_key=_key("estimate-to-project"))
    project_retry, project_retry_created = create_project_from_estimate(estimate, actor=owner, idempotency_key=_key("estimate-to-project"))
    project.assigned_staff.add(manager, accounts["office"], field)
    for description, category, amount in (
        ("Project labor and supervision", BudgetLine.Category.LABOR, "70000.00"),
        ("Project materials and fixtures", BudgetLine.Category.MATERIALS, "60000.00"),
        ("Project subcontractor scopes", BudgetLine.Category.SUBCONTRACTOR, "45000.00"),
        ("Project permits and allowances", BudgetLine.Category.PERMITS, "15000.00"),
    ):
        BudgetLine.objects.create(
            project=project,
            description=description,
            category=category,
            original_budget=Decimal(amount),
            created_by=owner,
        )
    PaymentSchedule.objects.get_or_create(
        project=project,
        sequence=2,
        defaults={
            "description": "Selections milestone draw",
            "amount": Decimal("55000.00"),
            "status": PaymentSchedule.Status.PENDING,
            "milestone": project.milestones.get(sort_order=3),
            "created_by": owner,
        },
    )
    PaymentSchedule.objects.get_or_create(
        project=project,
        sequence=3,
        defaults={
            "description": "Final completion draw",
            "amount": Decimal("97000.00"),
            "status": PaymentSchedule.Status.PENDING,
            "milestone": project.milestones.get(sort_order=5),
            "created_by": owner,
        },
    )
    project.target_date = timezone.localdate() + timedelta(days=180)
    project.save(update_fields=["target_date", "updated_at"])
    _check(checks, "approved external estimate creates one setup project", project_created and not project_retry_created and project.pk == project_retry.pk and Project.objects.filter(estimate=estimate).count() == 1 and project.budget_lines.count() == 4 and project.site_visits.filter(pk=visit.pk).exists())

    agreement = Agreement.objects.get(project=project)
    agreement.signed_pdf = _pdf_file("signed-agreement.pdf")
    agreement.full_clean()
    agreement.save(update_fields=["signed_pdf", "updated_at"])
    agreement, agreement_created = accept_agreement(agreement, actor=client_user, idempotency_key=_key("agreement-accept"))
    agreement_retry, agreement_retry_created = accept_agreement(agreement, actor=client_user, idempotency_key=_key("agreement-accept"))
    _check(checks, "agreement acceptance is auditable and retry-safe", agreement_created and not agreement_retry_created and agreement.status == Agreement.Status.ACCEPTED and agreement.locked_at is not None)

    deposit, deposit_created = record_deposit(project, actor=owner, amount=Decimal("38000.00"), method=PaymentRecord.Method.ACH, reference="SIM-DEPOSIT-001", idempotency_key=_key("deposit"))
    deposit_retry, deposit_retry_created = record_deposit(project, actor=owner, amount=Decimal("38000.00"), method=PaymentRecord.Method.ACH, reference="SIM-DEPOSIT-001", idempotency_key=_key("deposit"))
    deposit_schedule = project.payment_schedules.get(sequence=1)
    deposit_schedule.refresh_from_db()
    _check(checks, "deposit updates payment status without duplicates", deposit_created and not deposit_retry_created and deposit.pk == deposit_retry.pk and deposit_schedule.status == PaymentSchedule.Status.PAID and PaymentRecord.objects.filter(project=project, idempotency_key=_key("deposit")).count() == 1)
    return {"accounts": accounts, "lead": lead, "client": client, "visit": visit, "estimate": estimate, "project": project, "agreement": agreement}


def _run_construction(context, checks):
    accounts = context["accounts"]
    owner = accounts["owner"]
    manager = accounts["manager"]
    field = accounts["field"]
    project = context["project"]
    visit = context["visit"]

    initialize_readiness(project, actor=owner)
    readiness_items = list(project.readiness_items.order_by("key"))
    _check(checks, "project readiness items are initialized", bool(readiness_items))
    for item in readiness_items:
        complete_readiness_item(item, actor=manager, notes="Verified in the full lifecycle simulation.", idempotency_key=_key(f"readiness:{item.key}"))
    complete_readiness_item(readiness_items[0], actor=manager, idempotency_key=_key(f"readiness:{readiness_items[0].key}"))
    project.refresh_from_db()
    _check(checks, "readiness completion enters construction", project.construction_ready_at is not None and project.operational_phase == Project.OperationalPhase.CONSTRUCTION and project.status == Project.Status.CONSTRUCTION and project.readiness_items.filter(status=PreconstructionItem.Status.OPEN, required=True).count() == 0)

    permit, permit_created = create_permit(
        project,
        actor=manager,
        permit_type="Building permit",
        jurisdiction="City of Ventura",
        permit_number="SIM-BLD-001",
        idempotency_key=_key("permit"),
    )
    record_permit_status(permit, actor=manager, status=Permit.Status.APPROVED, permit_number="SIM-BLD-001", notes="Approved for simulated construction.", idempotency_key=_key("permit-approved"))
    inspection, inspection_created = create_inspection(
        project,
        actor=manager,
        inspection_type="Framing inspection",
        permit=permit,
        scheduled_at=timezone.now() + timedelta(days=7),
        idempotency_key=_key("inspection"),
    )
    record_inspection_result(inspection, actor=manager, status=Inspection.Status.FAILED, result_notes="Blocking strap detail needs correction.", corrective_action="Install missing connector at north wall and request reinspection.", idempotency_key=_key("inspection-failed"))
    _check(checks, "failed inspection creates corrective attention", project.blockers.filter(title="Failed inspection: Framing inspection", status="open").exists())
    record_inspection_result(inspection, actor=manager, status=Inspection.Status.PASSED, result_notes="Correction verified.", idempotency_key=_key("inspection-passed"))
    _check(checks, "passed inspection resolves corrective attention", not project.blockers.filter(title="Failed inspection: Framing inspection", status="open").exists())

    construction_milestone = project.milestones.get(sort_order=4)
    task, task_created = create_project_task(
        project,
        actor=manager,
        title="Complete framing corrections",
        description="Complete the field correction and upload evidence.",
        milestone=construction_milestone,
        assigned_to=field,
        priority=Task.Priority.HIGH,
        due_date=timezone.localdate() + timedelta(days=5),
        idempotency_key=_key("field-task"),
    )
    record_project_task_status(task, actor=field, status=Task.Status.IN_PROGRESS, idempotency_key=_key("field-task-start"))
    record_project_task_status(task, actor=field, status=Task.Status.COMPLETE, idempotency_key=_key("field-task-complete"))
    daily_report, daily_created = submit_daily_report(
        project,
        actor=field,
        report_date=timezone.localdate(),
        summary="Framing corrections completed and inspected.",
        work_completed="North wall connector installed and photographed.",
        labor_count=3,
        hours_worked=Decimal("18.50"),
        weather="Clear",
        equipment="Laser level and compressor",
        idempotency_key=_key("daily-report"),
    )
    daily_retry, daily_retry_created = submit_daily_report(project, actor=field, report_date=timezone.localdate(), summary="Retry of daily report", idempotency_key=_key("daily-report"))
    material_request, material_created = request_material(
        project,
        actor=field,
        description="White oak flooring sample set",
        quantity="3 sample boards",
        needed_by=timezone.localdate() + timedelta(days=3),
        vendor="Pacific Finish Supply",
        notes="Needed for client review before ordering.",
        idempotency_key=_key("material-request"),
    )
    material_retry, material_retry_created = request_material(project, actor=field, description="White oak flooring sample set", idempotency_key=_key("material-request"))
    record_material_request_status(material_request, actor=manager, status=MaterialRequest.Status.APPROVED, idempotency_key=_key("material-approved"))
    record_material_request_status(material_request, actor=manager, status=MaterialRequest.Status.ORDERED, idempotency_key=_key("material-ordered"))
    record_material_request_status(material_request, actor=manager, status=MaterialRequest.Status.RECEIVED, idempotency_key=_key("material-received"))
    problem, problem_created = submit_problem_report(
        project,
        actor=field,
        task=task,
        title="Unexpected slab elevation",
        description="Rear addition slab is 1.5 inches low at the south edge.",
        severity=ProblemReport.Severity.HIGH,
        idempotency_key=_key("problem-report"),
    )
    problem_retry, problem_retry_created = submit_problem_report(project, actor=field, task=task, title="Unexpected slab elevation", description="Retry of the same report", severity=ProblemReport.Severity.HIGH, idempotency_key=_key("problem-report"))
    resolve_problem_report(problem, actor=manager, resolution="Leveling plan approved and added to the field scope.", idempotency_key=_key("problem-resolved"))
    material_request.refresh_from_db()
    problem.refresh_from_db()
    _check(checks, "field retries do not duplicate operational records", task_created and daily_created and not daily_retry_created and daily_report.pk == daily_retry.pk and material_created and not material_retry_created and material_request.pk == material_retry.pk and problem_created and not problem_retry_created and problem.pk == problem_retry.pk and material_request.status == MaterialRequest.Status.RECEIVED and problem.status == ProblemReport.Status.RESOLVED)

    media = MediaAsset(
        project=project,
        site_visit=visit,
        title="Framing correction progress",
        file=_photo_file(),
        media_type=MediaAsset.MediaType.PHOTO,
        context=MediaAsset.Context.PROGRESS,
        visibility=MediaAsset.Visibility.CLIENT,
        caption="Correction completed at the north wall.",
        uploaded_by=field,
    )
    media.full_clean()
    media.save()
    daily_report.media_assets.add(media)
    document, document_created = create_project_document(
        project,
        actor=manager,
        title="Approved construction scope",
        category="Plans",
        description="Simulation scope document for protected-file checks.",
        file=_pdf_file("approved-scope.pdf"),
        visibility=ProjectDocument.Visibility.CLIENT,
        idempotency_key=_key("scope-document"),
    )
    invalid_upload_rejected = False
    try:
        create_project_document(project, actor=manager, title="Unsafe upload", category="Plans", file=SimpleUploadedFile("unsafe.exe", b"not a permitted document"), idempotency_key=_key("unsafe-document"))
    except ValidationError:
        invalid_upload_rejected = True
    _check(checks, "protected upload validation rejects unsafe documents", document_created and invalid_upload_rejected and project.documents.filter(pk=document.pk).exists())

    selection, selection_created = create_selection(
        project,
        actor=manager,
        category="Flooring",
        item_name="White oak flooring",
        description="Select the final engineered oak finish.",
        vendor="Pacific Finish Supply",
        allowance=Decimal("15000.00"),
        due_date=timezone.localdate() + timedelta(days=8),
        idempotency_key=_key("selection"),
    )
    advance_selection(selection, actor=accounts["client_user"], status=Selection.Status.SUBMITTED, client_choice="Natural white oak, matte finish.", idempotency_key=_key("selection-submitted"))
    advance_selection(selection, actor=manager, status=Selection.Status.APPROVED, idempotency_key=_key("selection-approved"))
    advance_selection(selection, actor=manager, status=Selection.Status.ORDERED, idempotency_key=_key("selection-ordered"))
    advance_selection(selection, actor=manager, status=Selection.Status.RECEIVED, idempotency_key=_key("selection-received"))
    advance_selection(selection, actor=manager, status=Selection.Status.INSTALLED, idempotency_key=_key("selection-installed"))
    selection.refresh_from_db()
    _check(checks, "client selection flows through procurement", selection_created and selection.status == Selection.Status.INSTALLED and project.tasks.filter(title="Order selection: White oak flooring").exists())

    subcontractor = Subcontractor.objects.create(company="Simulated Pacific Framing LLC", contact_name="Alex Rivera", email=accounts["subcontractor_user"].email, phone="805-555-0188", portal_user=accounts["subcontractor_user"], created_by=owner)
    assignment, assignment_created = create_subcontractor_assignment(
        project,
        actor=manager,
        subcontractor=subcontractor,
        task=task,
        work_package="Framing correction package",
        scope="Install connectors, verify level, and provide closeout photos.",
        start_date=timezone.localdate() + timedelta(days=2),
        end_date=timezone.localdate() + timedelta(days=4),
        idempotency_key=_key("subcontractor-assignment"),
    )
    record_subcontractor_assignment_status(assignment, actor=manager, status=SubcontractorAssignment.Status.ASSIGNED, idempotency_key=_key("assignment-assigned"))
    record_subcontractor_assignment_status(assignment, actor=manager, status=SubcontractorAssignment.Status.IN_PROGRESS, idempotency_key=_key("assignment-progress"))
    record_subcontractor_assignment_status(assignment, actor=manager, status=SubcontractorAssignment.Status.COMPLETE, idempotency_key=_key("assignment-complete"))
    assignment.refresh_from_db()
    subcontractor_budget_line = project.budget_lines.filter(category=BudgetLine.Category.SUBCONTRACTOR).first()
    commitment, commitment_created = create_commitment(project, actor=manager, description="Pacific framing commitment", amount=Decimal("30000.00"), subcontractor=subcontractor, budget_line=subcontractor_budget_line, status=Commitment.Status.COMMITTED, due_date=timezone.localdate() + timedelta(days=30), idempotency_key=_key("subcontractor-commitment"))
    commitment_retry, commitment_retry_created = create_commitment(project, actor=manager, description="Pacific framing commitment", amount=Decimal("30000.00"), subcontractor=subcontractor, budget_line=subcontractor_budget_line, status=Commitment.Status.COMMITTED, idempotency_key=_key("subcontractor-commitment"))
    _check(checks, "subcontractor and commitment records are retry-safe", assignment_created and assignment.status == SubcontractorAssignment.Status.COMPLETE and commitment_created and not commitment_retry_created and commitment.pk == commitment_retry.pk)
    return {**context, "permit": permit, "inspection": inspection, "task": task, "daily_report": daily_report, "material_request": material_request, "problem": problem, "media": media, "document": document, "selection": selection, "subcontractor": subcontractor, "assignment": assignment, "commitment": commitment}


def _run_financials_and_reporting(context, checks):
    accounts = context["accounts"]
    owner = accounts["owner"]
    manager = accounts["manager"]
    office = accounts["office"]
    client = context["client"]
    project = context["project"]
    media = context["media"]
    task = context["task"]

    change_order, change_order_created = create_change_order(
        project,
        actor=manager,
        title="Upgrade rear addition insulation",
        description="Client requested upgraded insulation and an additional schedule allowance.",
        price_impact=Decimal("12000.00"),
        schedule_impact_days=4,
        status=ChangeOrder.Status.SENT,
        idempotency_key=_key("change-order"),
    )
    change_order.supporting_media.add(media)
    change_order, change_order_approved = approve_change_order(change_order, actor=client.user, idempotency_key=_key("change-order-approve"))
    change_order_retry, change_order_retry_created = approve_change_order(change_order, actor=client.user, idempotency_key=_key("change-order-approve"))
    _check(checks, "approved change order updates contract and balance", change_order_created and change_order_approved and not change_order_retry_created and change_order.pk == change_order_retry.pk and project.agreement.current_contract_value == Decimal("202000.00") and project.payment_schedules.filter(description="Change order CO-1", status=PaymentSchedule.Status.READY).exists())

    progress_schedule = project.payment_schedules.get(sequence=2)
    selections_milestone = project.milestones.get(sort_order=3)
    set_milestone_status(selections_milestone, actor=manager, is_complete=True, idempotency_key=_key("selections-milestone"))
    set_milestone_status(selections_milestone, actor=manager, is_complete=True, idempotency_key=_key("selections-milestone"))
    progress_schedule.refresh_from_db()
    _check(checks, "milestone completion releases the linked draw", progress_schedule.milestone_id == selections_milestone.pk and progress_schedule.status == PaymentSchedule.Status.READY)
    progress_payment, progress_payment_created = record_payment(
        project,
        actor=owner,
        amount=Decimal("55000.00"),
        schedule=progress_schedule,
        method=PaymentRecord.Method.CHECK,
        reference="SIM-PROGRESS-001",
        idempotency_key=_key("progress-payment"),
    )
    progress_payment_retry, progress_payment_retry_created = record_payment(
        project,
        actor=owner,
        amount=Decimal("55000.00"),
        schedule=progress_schedule,
        method=PaymentRecord.Method.CHECK,
        reference="SIM-PROGRESS-001",
        idempotency_key=_key("progress-payment"),
    )
    labor_budget_line = project.budget_lines.filter(category=BudgetLine.Category.LABOR).first()
    cost, cost_created = record_cost(
        project,
        actor=owner,
        amount=Decimal("20000.00"),
        description="Framing and supervision actuals",
        vendor="Grand Coast field labor",
        budget_line=labor_budget_line,
        idempotency_key=_key("labor-cost"),
    )
    labor_budget_line.refresh_from_db()
    _check(checks, "payments and costs are idempotent and budget-linked", progress_payment_created and not progress_payment_retry_created and progress_payment.pk == progress_payment_retry.pk and cost_created and labor_budget_line.actual == Decimal("20000.00"))

    event_start = timezone.now() + timedelta(days=3, hours=2)
    schedule_one = ScheduleEvent(title="Simulated framing coordination", project=project, task=task, start_at=event_start, end_at=event_start + timedelta(hours=2), location=project.location, notes="Primary field coordination block.", created_by=manager)
    schedule_one.full_clean()
    schedule_one.save()
    schedule_one.assignees.add(manager)
    schedule_two = ScheduleEvent(title="Simulated client coordination overlap", project=project, start_at=event_start + timedelta(hours=1), end_at=event_start + timedelta(hours=3), location=project.location, notes="Intentional conflict for smoke coverage.", created_by=office)
    schedule_two.full_clean()
    schedule_two.save()
    schedule_two.assignees.add(manager)
    calendar_events = list(ScheduleEvent.objects.filter(project=project).prefetch_related("assignees"))
    calendar_events.extend(_execution_calendar_events([project]))
    conflicts = _calendar_conflicts(calendar_events, {})
    _check(checks, "calendar aggregation detects assigned conflicts", len(calendar_events) >= 4 and any("overlapping" in conflict["reason"].lower() for conflict in conflicts))

    employee_alerts = queue_employee_notifications(
        [owner, manager, accounts["field"]],
        kind="simulation-alert",
        title="Simulation project update",
        body="The simulated framing correction is complete.",
        destination_url=f"/dashboard/projects/{project.pk}/operations/",
        project=project,
        created_by=manager,
    )
    client_alerts = queue_client_notifications(
        [client],
        kind="simulation-update",
        title="Your project has a progress update",
        body="The framing correction has been completed and inspected.",
        destination_url="/portal/",
        project=project,
        created_by=manager,
    )
    outbox = EmailOutbox.objects.create(
        idempotency_key=_key("simulation-email"),
        recipient=client.email,
        subject="Simulation project update",
        body="This email remains in the local outbox during simulation.",
        project=project,
        client=client,
        created_by=manager,
    )
    outbox.status = EmailOutbox.Status.FAILED
    outbox.attempt_count = 1
    outbox.last_error = "Simulated delivery failure; no external delivery attempted."
    outbox.save(update_fields=["status", "attempt_count", "last_error"])
    _check(checks, "notifications and email outbox stay local", len(employee_alerts) == 3 and len(client_alerts) == 1 and EmployeeNotification.objects.filter(project=project).count() >= 3 and ClientNotification.objects.filter(project=project).count() >= 1 and outbox.status == EmailOutbox.Status.FAILED)

    financials = project_financial_summary(project)
    _check(checks, "financial summary uses external contract total and project budgets", financials["original_contract"] == Decimal("190000.00") and financials["approved_changes"] == Decimal("12000.00") and financials["current_contract"] == Decimal("202000.00") and financials["payments_received"] == Decimal("93000.00") and financials["actual_costs"] == Decimal("20000.00") and financials["committed_costs"] == Decimal("30000.00") and financials["gross_margin"] > Decimal("0.00") and financials["forecast_profit"] == Decimal("152000.00"))

    from .construction_services import attention_feed, company_metrics, weekly_project_review

    attention = attention_feed(owner, limit=400)
    metrics = company_metrics(owner)
    review = weekly_project_review(manager)
    _check(checks, "command center and weekly review have actionable results", any(item["kind"] == "email_failure" for item in attention) and metrics["active_contract_value"] >= Decimal("202000.00") and metrics["cash_projection_90"] >= Decimal("0.00") and any(row["project_id"] == str(project.pk) for row in review["projects"]))
    return {**context, "change_order": change_order, "progress_payment": progress_payment, "documented_financials": financials, "metrics": metrics, "review": review}


def _run_closeout(context, checks):
    manager = context["accounts"]["manager"]
    project = context["project"]
    closeout_items = list(project.closeout_items.filter(required=True).order_by("key"))
    for item in closeout_items:
        complete_closeout_item(item, actor=manager, notes="Verified in the full lifecycle simulation.", idempotency_key=_key(f"closeout:{item.key}"))
    project.refresh_from_db()
    warranty = project.warranty_items.order_by("created_at").first()
    if warranty is None:
        warranty, _created = initialize_warranty(project, actor=manager)
    warranty = resolve_warranty_item(warranty, actor=manager, resolution="Warranty coverage initialized and simulation claim resolved.", idempotency_key=_key("warranty-resolve"))
    warranty_retry = resolve_warranty_item(warranty, actor=manager, resolution="Retry of the same warranty resolution.", idempotency_key=_key("warranty-resolve"))
    project.refresh_from_db()
    _check(checks, "closeout advances to warranty without duplicate items", project.operational_phase == Project.OperationalPhase.WARRANTY and project.status == Project.Status.COMPLETE and warranty.status == WarrantyItem.Status.RESOLVED and warranty.pk == warranty_retry.pk and project.warranty_items.count() == 1)
    return warranty


def run_full_lifecycle():
    if getattr(settings, "GCC_AI_ENABLED", False):
        raise AssertionError("The simulation requires GCC_AI_ENABLED=false.")
    checks = []
    with transaction.atomic():
        context = _run_inquiry(_create_accounts(), checks)
        context = _run_construction(context, checks)
        context = _run_financials_and_reporting(context, checks)
        warranty = _run_closeout(context, checks)
        accounts = context["accounts"]
        project = context["project"]
        lead = context["lead"]
        client = context["client"]
        estimate = context["estimate"]
        payment = context["progress_payment"]
        financials = context["documented_financials"]
        metrics = context["metrics"]
        users = {
            role: str(accounts[key].pk)
            for role, key in {
                "owner": "owner",
                "office": "office",
                "manager": "manager",
                "sales": "sales",
                "field": "field",
                "subcontractor": "subcontractor_user",
                "client": "client_user",
                "unauthorized": "unauthorized",
            }.items()
        }
        result = {
            "scenario": "full-lifecycle",
            "passed": True,
            "checks": checks,
            "pilot": {"project_id": str(project.pk), "user_ids": users},
            "records": {
                "lead_id": str(lead.pk),
                "client_id": str(client.pk),
                "site_visit_id": str(context["visit"].pk),
                "estimate_id": str(estimate.pk),
                "agreement_id": str(context["agreement"].pk),
                "project_id": str(project.pk),
                "daily_report_id": str(context["daily_report"].pk),
                "material_request_id": str(context["material_request"].pk),
                "problem_report_id": str(context["problem"].pk),
                "inspection_id": str(context["inspection"].pk),
                "selection_id": str(context["selection"].pk),
                "assignment_id": str(context["assignment"].pk),
                "commitment_id": str(context["commitment"].pk),
                "change_order_id": str(context["change_order"].pk),
                "payment_id": str(payment.pk),
                "document_id": str(context["document"].pk),
                "media_id": str(context["media"].pk),
                "warranty_id": str(warranty.pk),
            },
            "metrics": {key: str(value) if isinstance(value, Decimal) else value for key, value in metrics.items()},
            "financials": {key: str(value) if isinstance(value, Decimal) else value for key, value in financials.items()},
            "review": context["review"],
            "counts": {
                "simulation_users": get_user_model().objects.filter(username__startswith=SIMULATION_PREFIX).count(),
                "simulation_clients": Client.objects.filter(email__endswith=f"@{SIMULATION_DOMAIN}").count(),
                "projects": Project.objects.filter(pk=project.pk).count(),
                "estimates": Estimate.objects.filter(pk=estimate.pk).count(),
                "payments": PaymentRecord.objects.filter(project=project).count(),
                "change_orders": ChangeOrder.objects.filter(project=project).count(),
                "workflow_events": WorkflowEvent.objects.filter(project=project).count(),
            },
            "credentials": {
                role: {"username": f"{SIMULATION_PREFIX}{role}", "password": password}
                for role, password in accounts["passwords"].items()
            },
        }
    return result
