from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .construction_services import (
    approve_change_order,
    advance_selection,
    complete_closeout_item,
    complete_readiness_item,
    create_inspection,
    create_lead,
    create_change_order,
    create_site_visit,
    create_selection,
    create_subcontractor_assignment,
    create_project_from_estimate,
    project_financial_summary,
    record_deposit,
    record_payment,
    resolve_warranty_item,
    set_milestone_status,
    send_estimate,
    submit_problem_report,
)
from .construction_forms import InspectionForm
from .models import (
    Agreement,
    BudgetLine,
    ChangeOrder,
    Client,
    CloseoutItem,
    Commitment,
    CostEntry,
    DailyReport,
    Estimate,
    EstimateLineItem,
    Inspection,
    Lead,
    MaterialRequest,
    MediaAsset,
    Milestone,
    NativeUploadGrant,
    PaymentRecord,
    PaymentSchedule,
    Permit,
    PreconstructionItem,
    ProblemReport,
    Project,
    ProjectDocument,
    Selection,
    Subcontractor,
    SubcontractorAssignment,
    Task,
    WarrantyItem,
    WorkflowEvent,
)


User = get_user_model()


class ConstructionOperatingSystemTests(TestCase):
    def setUp(self):
        self.http = HttpClient()
        self.owner = User.objects.create_user(
            username="owner",
            password="owner-password-123",
            email="owner@example.com",
            is_staff=True,
            is_superuser=True,
        )
        self.manager = User.objects.create_user(
            username="manager",
            password="manager-password-123",
            email="manager@example.com",
            is_staff=True,
        )
        self.manager.groups.add(Group.objects.get_or_create(name="Manager")[0])
        self.field = User.objects.create_user(
            username="field",
            password="field-password-123",
            email="field@example.com",
            is_staff=True,
        )
        self.field.groups.add(Group.objects.get_or_create(name="Field")[0])
        self.client_user = User.objects.create_user(
            username="homeowner",
            password="client-password-123",
            email="homeowner@example.com",
        )
        self.other_field = User.objects.create_user(
            username="other-field",
            password="other-password-123",
            email="other-field@example.com",
            is_staff=True,
        )
        self.other_field.groups.add(Group.objects.get_or_create(name="Field")[0])
        self.client_record = Client.objects.create(
            name="Homeowner",
            email=self.client_user.email,
            user=self.client_user,
        )
        self.lead = Lead.objects.create(
            client=self.client_record,
            assigned_to=self.manager,
            name="Homeowner",
            email=self.client_user.email,
            service="Kitchen renovation",
            location="Beachmont",
            budget_amount=Decimal("150000.00"),
        )
        self.estimate = Estimate.objects.create(
            client=self.client_record,
            lead=self.lead,
            title="Beachmont kitchen renovation",
            status=Estimate.Status.ACCEPTED,
            deposit_amount=Decimal("30000.00"),
            external_url="https://estimates.example.com/beachmont-kitchen",
            external_status=Estimate.ExternalEstimateStatus.APPROVED,
            external_status_at=timezone.now(),
            external_status_by=self.owner,
            external_client_visible=True,
        )
        EstimateLineItem.objects.create(
            estimate=self.estimate,
            description="Construction scope",
            quantity=Decimal("1.00"),
            unit_price=Decimal("100000.00"),
            estimated_cost=Decimal("65000.00"),
        )

    def _create_project(self):
        project, created = create_project_from_estimate(self.estimate, actor=self.owner)
        self.assertTrue(created)
        project.assigned_staff.add(self.manager, self.field)
        return project

    def test_estimate_conversion_is_idempotent_and_seeds_operational_records(self):
        project = self._create_project()
        same_project, created = create_project_from_estimate(self.estimate, actor=self.owner)

        self.assertFalse(created)
        self.assertEqual(project.pk, same_project.pk)
        self.assertEqual(Project.objects.filter(estimate=self.estimate).count(), 1)
        self.assertEqual(PreconstructionItem.objects.filter(project=project).count(), 9)
        self.assertEqual(PaymentSchedule.objects.filter(project=project).count(), 1)
        self.assertTrue(WorkflowEvent.objects.filter(event_type="project_created_from_estimate").exists())
        self.assertFalse(project.is_published)

    def test_role_and_object_scope_hides_unassigned_project(self):
        project = self._create_project()
        self.http.force_login(self.other_field)

        response = self.http.get(reverse("operations-api:project-summary", kwargs={"pk": project.pk}))

        self.assertEqual(response.status_code, 404)

    def test_office_and_sales_scopes_do_not_cross_assignment_boundaries(self):
        project = self._create_project()
        office = User.objects.create_user(
            username="office",
            password="office-password-123",
            email="office@example.com",
            is_staff=True,
        )
        office.groups.add(Group.objects.get_or_create(name="Office")[0])
        self.http.force_login(office)
        project_response = self.http.get(
            reverse("operations-api:project-summary", kwargs={"pk": project.pk})
        )
        self.assertEqual(project_response.status_code, 404)

        sales = User.objects.create_user(
            username="sales",
            password="sales-password-123",
            email="sales@example.com",
            is_staff=True,
        )
        sales.groups.add(Group.objects.get_or_create(name="Sales")[0])
        self.http.force_login(sales)
        self.assertEqual(self.http.get(reverse("operations-api:estimates")).json()["results"], [])
        with self.assertRaises(PermissionDenied):
            send_estimate(self.estimate, actor=sales)

    def test_client_can_view_project_without_internal_financials(self):
        project = self._create_project()
        self.http.force_login(self.client_user)

        response = self.http.get(reverse("operations-api:project-summary", kwargs={"pk": project.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("financials", response.json()["project"])
        payments = self.http.get(reverse("operations-api:payments", kwargs={"pk": project.pk}))
        self.assertEqual(payments.status_code, 403)

    def test_client_project_serializer_hides_internal_notes_and_field_hides_co_price(self):
        project = self._create_project()
        project.health_note = "Internal budget risk."
        project.save(update_fields=["health_note", "updated_at"])
        readiness = project.readiness_items.first()
        readiness.notes = "Internal readiness note."
        readiness.save(update_fields=["notes", "updated_at"])
        Selection.objects.create(
            project=project,
            category="cabinets",
            item_name="Walnut cabinets",
            description="Internal vendor quote details",
            vendor="Private supplier",
            status=Selection.Status.SUBMITTED,
        )
        Inspection.objects.create(
            project=project,
            inspection_type="Rough framing",
            status=Inspection.Status.FAILED,
            result_notes="Internal inspector notes",
            corrective_action="Internal correction plan",
        )
        change_order, created = create_change_order(
            project,
            actor=self.owner,
            title="Add pantry wall",
            description="Frame and finish the pantry wall.",
            price_impact=Decimal("2500.00"),
            status=ChangeOrder.Status.SENT,
            idempotency_key="co-serializer-1",
        )
        self.assertTrue(created)

        self.http.force_login(self.client_user)
        client_payload = self.http.get(
            reverse("operations-api:project-summary", kwargs={"pk": project.pk})
        ).json()["project"]
        self.assertEqual(client_payload["health_note"], "")
        self.assertEqual(client_payload["readiness"][0]["notes"], "")
        self.assertEqual(client_payload["selections"][0]["vendor"], "")
        self.assertEqual(client_payload["inspections"][0]["corrective_action"], "")
        self.assertEqual(client_payload["change_orders"][0]["price_impact"], "2500.00")

        self.http.force_login(self.field)
        field_payload = self.http.get(
            reverse("operations-api:change-orders", kwargs={"pk": project.pk})
        ).json()["results"]
        self.assertEqual(field_payload[0]["price_impact"], None)
        self.assertEqual(change_order.status, ChangeOrder.Status.SENT)

    def test_field_cannot_approve_or_advance_client_selections(self):
        project = self._create_project()
        selection = Selection.objects.create(
            project=project,
            category="windows",
            item_name="Windows",
            status=Selection.Status.SUBMITTED,
        )
        with self.assertRaises(PermissionDenied):
            advance_selection(
                selection,
                actor=self.field,
                status=Selection.Status.APPROVED,
                idempotency_key="selection-field-1",
            )
        selection.refresh_from_db()
        self.assertEqual(selection.status, Selection.Status.SUBMITTED)

    def test_change_order_approval_updates_contract_and_cannot_be_edited(self):
        project = self._create_project()
        change_order, created = create_change_order(
            project,
            actor=self.owner,
            title="Add pantry wall",
            description="Frame and finish the pantry wall.",
            price_impact=Decimal("2500.00"),
            status=ChangeOrder.Status.SENT,
            idempotency_key="co-beachmont-1",
        )
        self.assertTrue(created)
        self.http.force_login(self.client_user)

        response = self.http.post(
            reverse("operations-api:change-order-approve", kwargs={"pk": change_order.pk}),
            data="{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="approve-beachmont-1",
        )

        self.assertEqual(response.status_code, 200)
        change_order.refresh_from_db()
        self.assertEqual(change_order.status, ChangeOrder.Status.APPROVED)
        self.assertEqual(project_financial_summary(project)["current_contract"], Decimal("102500.00"))
        change_order.title = "Changed after approval"
        with self.assertRaises(ValidationError):
            change_order.full_clean()

    def test_payment_retry_is_idempotent_and_decimal_safe(self):
        project = self._create_project()
        self.http.force_login(self.owner)
        schedule = project.payment_schedules.get(sequence=1)

        first, created = record_payment(
            project,
            actor=self.owner,
            amount=Decimal("10000.005"),
            schedule=schedule,
            idempotency_key="payment-beachmont-1",
        )
        second, retried = record_payment(
            project,
            actor=self.owner,
            amount=Decimal("999.99"),
            schedule=schedule,
            idempotency_key="payment-beachmont-1",
        )

        self.assertTrue(created)
        self.assertFalse(retried)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PaymentRecord.objects.filter(project=project).count(), 1)
        self.assertEqual(project_financial_summary(project)["payments_received"], Decimal("10000.01"))

    def test_command_center_and_project_operations_are_additive_views(self):
        project = self._create_project()
        self.http.force_login(self.owner)

        dashboard = self.http.get(reverse("operations:dashboard"))
        command_center = self.http.get(reverse("operations:command-center"))
        legacy_overview = self.http.get(
            reverse("operations:dashboard-section", kwargs={"section": "overview"})
        )
        project_view = self.http.get(reverse("operations:project-operations", kwargs={"pk": project.pk}))

        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "What needs my attention today?")
        self.assertContains(dashboard, "Upcoming draws")
        self.assertContains(dashboard, "Active contract value")
        self.assertRedirects(command_center, reverse("operations:dashboard"))
        self.assertEqual(legacy_overview.status_code, 200)
        self.assertContains(legacy_overview, "Here’s the shape of the work today.")
        self.assertEqual(project_view.status_code, 200)
        self.assertContains(project_view, "Ready for construction")
        self.assertContains(project_view, "Current contract")
        self.assertContains(project_view, "Gross margin")
        self.assertNotContains(project_view, "Restricted financial view")

        self.http.force_login(self.field)
        field_project_view = self.http.get(
            reverse("operations:project-operations", kwargs={"pk": project.pk})
        )
        self.assertEqual(field_project_view.status_code, 200)
        self.assertContains(field_project_view, "Owner / assigned manager")
        self.assertNotContains(field_project_view, "Current contract")
        self.assertContains(field_project_view, 'href="/team/"')
        self.assertNotContains(field_project_view, 'href="/dashboard/"')
        self.assertNotContains(field_project_view, "Legacy project view")

    @override_settings(
        GCC_OWNER_COMMAND_CENTER_ENABLED=False,
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_dashboard_feature_flag_falls_back_to_existing_overview(self):
        self.http.force_login(self.owner)

        response = self.http.get(reverse("operations:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Here’s the shape of the work today.")
        self.assertNotContains(response, "What needs my attention today?")

    @override_settings(
        GCC_NATIVE_MEDIA_ENABLED=True,
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_native_upload_grants_are_scoped_validated_and_idempotent(self):
        project = self._create_project()
        self.http.force_login(self.field)
        grant_url = reverse("operations-api:native-upload-grant")
        complete_url = reverse("operations-api:native-upload-complete")
        grant_response = self.http.post(
            grant_url,
            data=(
                '{"target":"project_media","project_id":"%s","file_name":"progress.jpg",'
                '"file_size":3,"content_type":"image/jpeg"}'
            ) % project.pk,
            content_type="application/json",
        )
        self.assertEqual(grant_response.status_code, 201)
        grant = grant_response.json()
        upload = SimpleUploadedFile(
            "progress.jpg",
            b"\xff\xd8\xff",
            content_type="image/jpeg",
        )
        uploaded = self.http.post(
            complete_url,
            data={"file": upload},
            HTTP_X_GRAND_COAST_UPLOAD_TOKEN=grant["grant_token"],
            HTTP_IDEMPOTENCY_KEY=grant["idempotency_key"],
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertTrue(uploaded.json()["uploaded"])
        self.assertEqual(MediaAsset.objects.filter(project=project).count(), 1)
        self.assertEqual(
            str(NativeUploadGrant.objects.get(pk=grant["grant_id"]).media_asset_id),
            uploaded.json()["id"],
        )

        replay = self.http.post(
            complete_url,
            data={"file": SimpleUploadedFile("progress.jpg", b"\xff\xd8\xff", content_type="image/jpeg")},
            HTTP_X_GRAND_COAST_UPLOAD_TOKEN=grant["grant_token"],
            HTTP_IDEMPOTENCY_KEY=grant["idempotency_key"],
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(MediaAsset.objects.filter(project=project).count(), 1)

        self.http.force_login(self.other_field)
        denied = self.http.post(
            grant_url,
            data=(
                '{"target":"project_media","project_id":"%s","file_name":"other.jpg",'
                '"file_size":3,"content_type":"image/jpeg"}'
            ) % project.pk,
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 404)

    @override_settings(
        GCC_NATIVE_MEDIA_ENABLED=True,
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_native_upload_rejects_bad_signature_and_expired_grants(self):
        project = self._create_project()
        self.http.force_login(self.field)
        grant_response = self.http.post(
            reverse("operations-api:native-upload-grant"),
            data=(
                '{"target":"project_media","project_id":"%s","file_name":"progress.jpg",'
                '"file_size":8,"content_type":"image/jpeg"}'
            ) % project.pk,
            content_type="application/json",
        )
        self.assertEqual(grant_response.status_code, 201)
        grant = grant_response.json()
        rejected = self.http.post(
            reverse("operations-api:native-upload-complete"),
            data={"file": SimpleUploadedFile("progress.jpg", b"not-jpeg", content_type="image/jpeg")},
            HTTP_X_GRAND_COAST_UPLOAD_TOKEN=grant["grant_token"],
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(MediaAsset.objects.filter(project=project).count(), 0)

        expired_response = self.http.post(
            reverse("operations-api:native-upload-grant"),
            data=(
                '{"target":"project_media","project_id":"%s","file_name":"expired.jpg",'
                '"file_size":3,"content_type":"image/jpeg"}'
            ) % project.pk,
            content_type="application/json",
        )
        expired = expired_response.json()
        NativeUploadGrant.objects.filter(pk=expired["grant_id"]).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        expired_upload = self.http.post(
            reverse("operations-api:native-upload-complete"),
            data={"file": SimpleUploadedFile("expired.jpg", b"\xff\xd8\xff", content_type="image/jpeg")},
            HTTP_X_GRAND_COAST_UPLOAD_TOKEN=expired["grant_token"],
        )
        self.assertEqual(expired_upload.status_code, 410)

    def test_api_lead_creation_and_conversion_are_idempotent(self):
        self.http.force_login(self.owner)
        lead_response = self.http.post(
            reverse("operations-api:leads"),
            data='{"name":"New Homeowner","email":"new@example.com","service":"Addition","location":"Irvine","budget_amount":"210000.00"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="lead-create-1",
        )
        self.assertEqual(lead_response.status_code, 201)
        lead_id = lead_response.json()["lead"]["id"]
        retry = self.http.post(
            reverse("operations-api:leads"),
            data='{"name":"Different Payload","email":"different@example.com","service":"Addition","location":"Irvine"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="lead-create-1",
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["lead"]["id"], lead_id)
        convert_url = reverse("operations-api:lead-convert-client", kwargs={"pk": lead_id})
        converted = self.http.post(
            convert_url,
            data="{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="lead-convert-1",
        )
        self.assertEqual(converted.status_code, 200)
        converted_retry = self.http.post(
            convert_url,
            data="{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="lead-convert-1",
        )
        self.assertEqual(converted_retry.status_code, 200)
        self.assertEqual(converted_retry.json()["client"]["id"], converted.json()["client"]["id"])
        self.assertEqual(Client.objects.filter(email="new@example.com").count(), 1)

    def test_deposit_and_approved_records_are_immutable(self):
        project = self._create_project()
        payment, created = record_deposit(
            project,
            actor=self.owner,
            amount=Decimal("30000.00"),
            idempotency_key="deposit-beachmont-1",
        )
        self.assertTrue(created)
        self.assertEqual(payment.schedule.sequence, 1)
        payment.created_by = self.manager
        with self.assertRaises(ValidationError):
            payment.save()
        agreement = Agreement.objects.get(project=project)
        agreement.status = Agreement.Status.ACCEPTED
        agreement.locked_at = agreement.created_at
        agreement.save()
        agreement.status = Agreement.Status.ISSUED
        with self.assertRaises(ValidationError):
            agreement.save()
        event = WorkflowEvent.objects.order_by("created_at").first()
        event.event_type = "tampered"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_cost_and_commitment_history_is_revision_or_void_only(self):
        project = self._create_project()
        budget_line = BudgetLine.objects.create(
            project=project,
            description="Materials",
            category=BudgetLine.Category.MATERIALS,
        )
        cost = CostEntry.objects.create(
            project=project,
            budget_line=budget_line,
            description="Tile deposit",
            amount=Decimal("125.00"),
            created_by=self.owner,
        )
        cost.amount = Decimal("250.00")
        with self.assertRaises(ValidationError):
            cost.save()
        cost.refresh_from_db()
        cost.is_void = True
        cost.voided_at = timezone.now()
        cost.voided_by = self.owner
        cost.save()
        cost.description = "Tampered cost"
        with self.assertRaises(ValidationError):
            cost.save()
        commitment = Commitment.objects.create(
            project=project,
            budget_line=budget_line,
            description="Cabinet subcontract",
            amount=Decimal("5000.00"),
            created_by=self.owner,
        )
        commitment.amount = Decimal("6000.00")
        with self.assertRaises(ValidationError):
            commitment.save()
        commitment.refresh_from_db()
        commitment.status = Commitment.Status.COMMITTED
        commitment.save()
        commitment.status = Commitment.Status.PLANNED
        with self.assertRaises(ValidationError):
            commitment.save()
        with self.assertRaises(ValidationError):
            commitment.delete()

    def test_cost_api_is_idempotent_and_voidable(self):
        project = self._create_project()
        self.http.force_login(self.owner)
        costs_url = reverse("operations-api:project-costs", kwargs={"pk": project.pk})
        created = self.http.post(
            costs_url,
            data='{"description":"Tile deposit","amount":"125.00","source":"invoice"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cost-api-1",
        )
        self.assertEqual(created.status_code, 201)
        cost_id = created.json()["cost"]["id"]
        retry = self.http.post(
            costs_url,
            data='{"description":"Different text","amount":"999.00"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cost-api-1",
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["cost"]["id"], cost_id)
        voided = self.http.post(
            reverse("operations-api:cost-void", kwargs={"pk": cost_id}),
            data="{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="cost-void-1",
        )
        self.assertEqual(voided.status_code, 200)
        self.assertTrue(voided.json()["cost"]["is_void"])
        self.http.force_login(self.field)
        hidden = self.http.get(costs_url)
        self.assertEqual(hidden.status_code, 403)

    def test_permits_problems_and_closeout_are_scoped_and_idempotent(self):
        project = self._create_project()
        permit_url = reverse("operations-api:project-permits", kwargs={"pk": project.pk})
        self.http.force_login(self.owner)
        missing_key = self.http.post(
            permit_url,
            data='{"permit_type":"Building"}',
            content_type="application/json",
        )
        self.assertEqual(missing_key.status_code, 400)
        permit_response = self.http.post(
            permit_url,
            data='{"permit_type":"Building","status":"submitted"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="permit-beachmont-1",
        )
        self.assertEqual(permit_response.status_code, 201)
        retry = self.http.post(
            permit_url,
            data='{"permit_type":"Changed"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="permit-beachmont-1",
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Permit.objects.filter(project=project).count(), 1)
        self.http.force_login(self.field)
        problem_url = reverse("operations-api:project-problems", kwargs={"pk": project.pk})
        problem_response = self.http.post(
            problem_url,
            data='{"title":"Missing material","description":"The specified tile has not arrived.","severity":"high"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="12345678-1234-4234-8234-123456789012",
        )
        self.assertEqual(problem_response.status_code, 201)
        self.assertEqual(ProblemReport.objects.filter(project=project).count(), 1)
        closeout_response = self.http.get(
            reverse("operations-api:project-closeout", kwargs={"pk": project.pk})
        )
        self.assertEqual(closeout_response.status_code, 200)
        self.assertEqual(len(closeout_response.json()["results"]), 6)

    def test_subcontractor_sees_only_assigned_work(self):
        project = self._create_project()
        sub_user = User.objects.create_user(
            username="subcontractor",
            password="sub-password-123",
            email="sub@example.com",
        )
        subcontractor = Subcontractor.objects.create(
            company="Trusted Trade",
            contact_name="Trade Contact",
            email="sub@example.com",
            portal_user=sub_user,
        )
        assignment = SubcontractorAssignment.objects.create(
            project=project,
            subcontractor=subcontractor,
            work_package="Cabinet installation",
            scope="Install cabinets per approved plans.",
            status=SubcontractorAssignment.Status.ASSIGNED,
        )
        self.http.force_login(sub_user)
        response = self.http.get(
            reverse("operations-api:project-summary", kwargs={"pk": project.pk})
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()["project"]
        self.assertNotIn("financials", payload)
        self.assertEqual(payload["assignments"][0]["id"], str(assignment.pk))
        self.assertEqual(payload["readiness"], [])
        self.assertEqual(payload["selections"], [])

    def test_execution_loop_flag_controls_project_hub_and_weekly_review(self):
        project = self._create_project()
        self.http.force_login(self.owner)
        with self.settings(
            GCC_EXECUTION_LOOP_ENABLED=False,
            GCC_OPERATING_SYSTEM_ENABLED=True,
        ):
            fallback = self.http.get(
                reverse("operations:project-operations", kwargs={"pk": project.pk})
            )
            self.assertEqual(fallback.status_code, 200)
            self.assertNotContains(fallback, "Pilot execution loop")
            self.assertEqual(
                self.http.get(reverse("operations:weekly-review")).status_code,
                404,
            )
        with self.settings(
            GCC_EXECUTION_LOOP_ENABLED=True,
            GCC_EXECUTION_LOOP_PROJECT_IDS=str(project.pk),
            GCC_EXECUTION_LOOP_USER_IDS=str(self.owner.pk),
            GCC_OPERATING_SYSTEM_ENABLED=True,
        ):
            enabled = self.http.get(
                reverse("operations:project-operations", kwargs={"pk": project.pk})
            )
            self.assertEqual(enabled.status_code, 200)
            self.assertContains(enabled, "Pilot execution loop")
            review = self.http.get(reverse("operations:weekly-review"))
            self.assertEqual(review.status_code, 200)
            self.assertContains(review, "Weekly action list")
            self.assertContains(review, project.title)

        self.http.force_login(self.field)
        with self.settings(
            GCC_EXECUTION_LOOP_ENABLED=True,
            GCC_EXECUTION_LOOP_PROJECT_IDS="",
            GCC_EXECUTION_LOOP_USER_IDS="",
            GCC_OPERATING_SYSTEM_ENABLED=True,
        ):
            employee_review = self.http.get(reverse("operations:weekly-review"))
            self.assertEqual(employee_review.status_code, 200)
            self.assertContains(employee_review, 'href="/team/"')
            self.assertNotContains(employee_review, 'href="/dashboard/"')

    @override_settings(
        GCC_EXECUTION_LOOP_ENABLED=True,
        GCC_EXECUTION_LOOP_PROJECT_IDS="",
        GCC_EXECUTION_LOOP_USER_IDS="",
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_project_hub_execution_actions_are_transactional_and_scoped(self):
        project = self._create_project()
        self.http.force_login(self.owner)
        project_url = reverse("operations:project-operations", kwargs={"pk": project.pk})

        response = self.http.post(
            reverse("operations:project-site-visit-create", kwargs={"pk": project.pk}),
            data={
                "assigned_to": self.owner.pk,
                "scheduled_at": "2030-01-03T10:00",
                "address": "12 Beachmont Way",
                "scope": "Walk the kitchen and rear addition.",
                "measurements": "Kitchen 14 by 18.",
                "client_requests": "Keep the existing island.",
                "existing_conditions": "Water damage at the sink wall.",
                "potential_additional_work": "Review electrical service.",
                "notes": "Bring laser measure.",
            },
        )
        self.assertEqual(response.status_code, 302)
        visit = project.site_visits.get()
        self.assertEqual(visit.scope, "Walk the kitchen and rear addition.")

        response = self.http.post(
            reverse("operations:project-permit-create", kwargs={"pk": project.pk}),
            data={
                "permit_type": "Building",
                "jurisdiction": "City of Beachmont",
                "status": Permit.Status.PENDING,
                "expires_at": "",
                "notes": "Submit after engineering review.",
            },
        )
        self.assertEqual(response.status_code, 302)
        permit = project.permits.get()

        inspection_form = InspectionForm(
            {
                "inspection_type": "Rough framing",
                "permit": str(permit.pk),
                "scheduled_at": "2030-01-10T09:00",
            },
            project=project,
            permit_queryset=project.permits.all(),
        )
        self.assertTrue(inspection_form.is_valid(), inspection_form.errors)
        response = self.http.post(
            reverse("operations:project-inspection-create", kwargs={"pk": project.pk}),
            data={
                "inspection_type": "Rough framing",
                "permit": permit.pk,
                "scheduled_at": "2030-01-10T09:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        inspection = project.inspections.get()
        response = self.http.post(
            reverse("operations:project-inspection-result", kwargs={"pk": inspection.pk}),
            data={
                "status": Inspection.Status.FAILED,
                "result_notes": "Blocking fastener spacing is incorrect.",
                "corrective_action": "Correct framing before reinspection.",
                "rescheduled_at": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(project.blockers.filter(category="inspection").exists())

        response = self.http.post(
            reverse("operations:project-selection-create", kwargs={"pk": project.pk}),
            data={
                "category": "Cabinets",
                "item_name": "Walnut cabinets",
                "description": "Full-height pantry and island.",
                "vendor": "Trusted Millwork",
                "allowance": "18000.00",
                "client_choice": "",
                "due_date": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        selection = project.selections.get()
        response = self.http.post(
            reverse("operations:project-selection-advance", kwargs={"pk": selection.pk}),
            data={
                "status": Selection.Status.APPROVED,
                "client_choice": "Walnut slab fronts.",
            },
        )
        self.assertEqual(response.status_code, 302)
        selection.refresh_from_db()
        self.assertEqual(selection.status, Selection.Status.APPROVED)
        self.assertTrue(Task.objects.filter(project=project, title__startswith="Order selection:").exists())

        self.assertEqual(
            self.http.post(
                reverse("operations:project-daily-report", kwargs={"pk": project.pk}),
                data={
                    "report_date": "2030-01-03",
                    "summary": "Framing inspection preparation completed.",
                    "work_completed": "Reviewed framing corrections.",
                    "labor_count": "2",
                    "hours_worked": "8.00",
                    "weather": "Clear",
                    "equipment": "Laser measure",
                    "notes": "",
                },
            ).status_code,
            302,
        )
        self.assertTrue(DailyReport.objects.filter(project=project).exists())

        self.assertEqual(
            self.http.post(
                reverse("operations:project-material-request", kwargs={"pk": project.pk}),
                data={
                    "description": "Replacement framing hardware",
                    "quantity": "2 boxes",
                    "needed_by": "2030-01-08",
                    "vendor": "Local supplier",
                    "notes": "Needed for correction.",
                },
            ).status_code,
            302,
        )
        self.assertTrue(MaterialRequest.objects.filter(project=project).exists())

        self.assertEqual(
            self.http.post(
                reverse("operations:project-problem-report", kwargs={"pk": project.pk}),
                data={
                    "title": "Framing correction",
                    "description": "Inspection found a framing issue.",
                    "severity": "high",
                },
            ).status_code,
            302,
        )
        self.assertTrue(ProblemReport.objects.filter(project=project).exists())

        subcontractor = Subcontractor.objects.create(company="Beachmont Electric", created_by=self.owner)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-assignment-create", kwargs={"pk": project.pk}),
                data={
                    "subcontractor": subcontractor.pk,
                    "task": "",
                    "work_package": "Electrical rough-in",
                    "scope": "Complete rough-in per approved plans.",
                    "start_date": "2030-01-11",
                    "end_date": "2030-01-12",
                    "status": SubcontractorAssignment.Status.ASSIGNED,
                    "notes": "",
                },
            ).status_code,
            302,
        )
        self.assertTrue(SubcontractorAssignment.objects.filter(project=project).exists())

        schedule = project.payment_schedules.get(sequence=1)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-payment-record", kwargs={"pk": project.pk}),
                data={
                    "schedule": schedule.pk,
                    "amount": "1000.00",
                    "received_on": "2030-01-03",
                    "method": PaymentRecord.Method.CHECK,
                    "reference": "CHK-100",
                    "notes": "",
                },
            ).status_code,
            302,
        )
        self.assertTrue(PaymentRecord.objects.filter(project=project).exists())
        self.assertEqual(
            self.http.post(
                reverse("operations:project-cost-record", kwargs={"pk": project.pk}),
                data={
                    "budget_line": "",
                    "description": "Inspection correction materials",
                    "vendor": "Local supplier",
                    "amount": "125.00",
                    "incurred_on": "2030-01-03",
                    "source": "invoice",
                },
            ).status_code,
            302,
        )
        self.assertTrue(CostEntry.objects.filter(project=project).exists())

        self.http.force_login(self.field)
        self.assertEqual(
            self.http.get(project_url).status_code,
            200,
        )
        self.assertNotContains(self.http.get(project_url), "Financial ledger")
        self.assertEqual(
            self.http.post(
                reverse("operations:project-cost-record", kwargs={"pk": project.pk}),
                data={"description": "Hidden", "amount": "10.00", "incurred_on": "2030-01-03", "source": "manual"},
            ).status_code,
            403,
        )
        self.http.force_login(self.other_field)
        self.assertEqual(self.http.get(project_url).status_code, 404)
        self.http.force_login(self.client_user)
        self.assertEqual(self.http.get(project_url).status_code, 403)

    @override_settings(
        GCC_EXECUTION_LOOP_ENABLED=True,
        GCC_EXECUTION_LOOP_PROJECT_IDS="",
        GCC_EXECUTION_LOOP_USER_IDS="",
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_project_hub_can_create_tasks_and_protected_documents(self):
        project = self._create_project()
        self.http.force_login(self.owner)

        response = self.http.post(
            reverse("operations:project-task-create", kwargs={"pk": project.pk}),
            data={
                "title": "Confirm cabinet delivery",
                "description": "Confirm the delivery window with the millwork vendor.",
                "milestone": "",
                "assigned_to": self.manager.pk,
                "status": Task.Status.OPEN,
                "priority": Task.Priority.HIGH,
                "due_date": "2030-01-15",
            },
            HTTP_IDEMPOTENCY_KEY="project-task-create-1",
        )
        self.assertEqual(response.status_code, 302)
        task = project.tasks.get(title="Confirm cabinet delivery")
        self.assertEqual(task.assigned_to_id, self.manager.pk)

        response = self.http.post(
            reverse("operations:project-task-status", kwargs={"pk": task.pk}),
            data={"status": Task.Status.COMPLETE},
            HTTP_IDEMPOTENCY_KEY="project-task-status-1",
        )
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETE)

        field_task = Task.objects.create(
            project=project,
            title="Photograph cabinet delivery",
            assigned_to=self.field,
            created_by=self.owner,
        )
        self.http.force_login(self.field)
        response = self.http.post(
            reverse("operations:project-task-status", kwargs={"pk": field_task.pk}),
            data={"status": Task.Status.IN_PROGRESS},
            HTTP_IDEMPOTENCY_KEY="field-project-task-status-1",
        )
        self.assertEqual(response.status_code, 302)
        field_task.refresh_from_db()
        self.assertEqual(field_task.status, Task.Status.IN_PROGRESS)
        self.http.force_login(self.owner)

        upload = SimpleUploadedFile("cabinet-scope.txt", b"Approved cabinet scope", content_type="text/plain")
        response = self.http.post(
            reverse("operations:project-document-upload", kwargs={"pk": project.pk}),
            data={
                "project": project.pk,
                "title": "Cabinet scope",
                "category": "Plans",
                "description": "Approved cabinet scope for the job file.",
                "visibility": ProjectDocument.Visibility.INTERNAL,
                "file": upload,
            },
            HTTP_IDEMPOTENCY_KEY="project-document-upload-1",
        )
        self.assertEqual(response.status_code, 302)
        document = project.documents.get(title="Cabinet scope")
        self.assertEqual(document.visibility, ProjectDocument.Visibility.INTERNAL)

        replay = self.http.post(
            reverse("operations:project-document-upload", kwargs={"pk": project.pk}),
            data={
                "project": project.pk,
                "title": "Cabinet scope",
                "category": "Plans",
                "description": "Different retry payload must not duplicate the record.",
                "visibility": ProjectDocument.Visibility.INTERNAL,
                "file": SimpleUploadedFile("cabinet-scope.txt", b"different retry", content_type="text/plain"),
            },
            HTTP_IDEMPOTENCY_KEY="project-document-upload-1",
        )
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(project.documents.filter(title="Cabinet scope").count(), 1)

        self.http.force_login(self.other_field)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-task-create", kwargs={"pk": project.pk}),
                data={"title": "Unauthorized task"},
            ).status_code,
            404,
        )
        self.http.force_login(self.client_user)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-document-upload", kwargs={"pk": project.pk}),
                data={"project": project.pk, "title": "Client upload"},
            ).status_code,
            403,
        )

    @override_settings(
        GCC_EXECUTION_LOOP_ENABLED=True,
        GCC_EXECUTION_LOOP_PROJECT_IDS="",
        GCC_EXECUTION_LOOP_USER_IDS="",
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_execution_transitions_release_draw_and_finish_warranty(self):
        project = self._create_project()
        milestone = project.milestones.get(sort_order=3)
        draw = PaymentSchedule.objects.create(
            project=project,
            milestone=milestone,
            sequence=2,
            description="Selections draw",
            amount=Decimal("15000.00"),
        )
        set_milestone_status(milestone, actor=self.owner, is_complete=True, idempotency_key="milestone-draw-1")
        draw.refresh_from_db()
        self.assertEqual(draw.status, PaymentSchedule.Status.READY)
        for index, item in enumerate(project.readiness_items.all(), start=1):
            complete_readiness_item(item, actor=self.owner, idempotency_key=f"readiness-{index}")
        project.refresh_from_db()
        self.assertIsNotNone(project.construction_ready_at)
        for index, item in enumerate(project.closeout_items.all(), start=1):
            complete_closeout_item(
                item,
                actor=self.owner,
                idempotency_key=f"closeout-{index}",
            )
        project.refresh_from_db()
        self.assertEqual(project.operational_phase, Project.OperationalPhase.WARRANTY)
        self.assertEqual(project.status, Project.Status.COMPLETE)
        warranty = WarrantyItem.objects.create(project=project, title="Touch-up warranty item")
        resolve_warranty_item(
            warranty,
            actor=self.owner,
            resolution="Touch-up completed and documented.",
            idempotency_key="warranty-1",
        )
        warranty.refresh_from_db()
        self.assertEqual(warranty.status, WarrantyItem.Status.RESOLVED)

    def test_execution_creation_commands_are_idempotent_on_retries(self):
        project = self._create_project()
        selection_one, created = create_selection(
            project,
            actor=self.owner,
            category="Flooring",
            item_name="White oak",
            idempotency_key="selection-create-retry",
        )
        selection_two, retried = create_selection(
            project,
            actor=self.owner,
            category="Different category",
            item_name="Different item",
            idempotency_key="selection-create-retry",
        )
        self.assertTrue(created)
        self.assertFalse(retried)
        self.assertEqual(selection_one.pk, selection_two.pk)
        self.assertEqual(Selection.objects.filter(project=project).count(), 1)

        inspection_one, created = create_inspection(
            project,
            actor=self.owner,
            inspection_type="Electrical rough-in",
            idempotency_key="inspection-create-retry",
        )
        inspection_two, retried = create_inspection(
            project,
            actor=self.owner,
            inspection_type="Different inspection",
            idempotency_key="inspection-create-retry",
        )
        self.assertTrue(created)
        self.assertFalse(retried)
        self.assertEqual(inspection_one.pk, inspection_two.pk)
        self.assertEqual(Inspection.objects.filter(project=project).count(), 1)

        subcontractor = Subcontractor.objects.create(company="Retry-safe trade", created_by=self.owner)
        assignment_one, created = create_subcontractor_assignment(
            project,
            actor=self.owner,
            subcontractor=subcontractor,
            work_package="Plumbing",
            idempotency_key="assignment-create-retry",
        )
        assignment_two, retried = create_subcontractor_assignment(
            project,
            actor=self.owner,
            subcontractor=subcontractor,
            work_package="Different package",
            idempotency_key="assignment-create-retry",
        )
        self.assertTrue(created)
        self.assertFalse(retried)
        self.assertEqual(assignment_one.pk, assignment_two.pk)
        self.assertEqual(SubcontractorAssignment.objects.filter(project=project).count(), 1)

    @override_settings(
        GCC_EXECUTION_LOOP_ENABLED=True,
        GCC_EXECUTION_LOOP_PROJECT_IDS="",
        GCC_EXECUTION_LOOP_USER_IDS="",
        GCC_OPERATING_SYSTEM_ENABLED=True,
    )
    def test_execution_calendar_aggregates_project_signals_and_conflicts(self):
        project = self._create_project()
        scheduled_at = timezone.now() + timedelta(hours=2)
        create_site_visit(
            project.lead,
            actor=self.owner,
            project=project,
            assigned_to=self.owner,
            scheduled_at=scheduled_at,
            address=project.location,
            scope="Calendar conflict test",
            idempotency_key="calendar-visit-1",
        )
        Inspection.objects.create(
            project=project,
            inspection_type="Same-time inspection",
            scheduled_at=scheduled_at,
            created_by=self.owner,
        )
        self.http.force_login(self.owner)
        month = timezone.localtime(scheduled_at).strftime("%Y-%m")
        response = self.http.get(
            reverse("operations:dashboard-section", kwargs={"section": "calendar"}),
            data={"month": month},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.context["derived_calendar_event_count"], 2)
        self.assertContains(response, "Site visit")
        self.assertTrue(response.context["calendar_conflicts"])

    @override_settings(GCC_AI_ENABLED=True)
    def test_ask_grand_coast_is_read_only_and_permission_filtered(self):
        project = self._create_project()
        self.http.force_login(self.owner)
        response = self.http.post(
            reverse("operations-api:ask-grand-coast"),
            data='{"question":"What needs my attention today?"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["read_only"])
        self.assertEqual(response.json()["available_actions"], [])
        self.http.force_login(self.client_user)
        restricted = self.http.post(
            reverse("operations-api:ask-grand-coast"),
            data='{"question":"What is our cash flow?"}',
            content_type="application/json",
        )
        self.assertEqual(restricted.status_code, 200)
        self.assertEqual(restricted.json()["kind"], "restricted")
