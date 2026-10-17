from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as HttpClient, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .construction_services import (
    create_project_from_estimate,
    record_external_estimate_status,
    record_external_invoice_status,
)
from .construction_policies import can_manage_external_estimate, external_estimate_enabled_for
from .models import (
    Agreement,
    Client,
    Estimate,
    EstimateLineItem,
    EmailOutbox,
    Lead,
    PaymentRecord,
    PaymentSchedule,
    Project,
    WorkflowEvent,
)


User = get_user_model()


class ExternalEstimateStatusTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="external-owner",
            email="external-owner@example.com",
            password="owner-password-123",
            is_staff=True,
            is_superuser=True,
        )
        self.manager = User.objects.create_user(
            username="external-manager",
            email="external-manager@example.com",
            password="manager-password-123",
            is_staff=True,
        )
        self.manager.groups.add(Group.objects.get_or_create(name="Manager")[0])
        self.office = User.objects.create_user(
            username="external-office",
            email="external-office@example.com",
            password="office-password-123",
            is_staff=True,
        )
        self.office.groups.add(Group.objects.get_or_create(name="Office")[0])
        self.field = User.objects.create_user(
            username="external-field",
            email="external-field@example.com",
            password="field-password-123",
            is_staff=True,
        )
        self.field.groups.add(Group.objects.get_or_create(name="Field")[0])
        self.client_user = User.objects.create_user(
            username="external-client",
            email="external-client@example.com",
            password="client-password-123",
        )
        self.client_record = Client.objects.create(
            name="External Client",
            email=self.client_user.email,
            user=self.client_user,
        )
        self.lead = Lead.objects.create(
            client=self.client_record,
            assigned_to=self.manager,
            name="External Client",
            email=self.client_record.email,
            service="Renovation",
            location="Beachmont",
        )
        self.estimate = Estimate.objects.create(
            client=self.client_record,
            lead=self.lead,
            title="Beachmont customer estimate",
            deposit_amount=Decimal("10000.00"),
            created_by=self.office,
        )
        EstimateLineItem.objects.create(
            estimate=self.estimate,
            description="Internal budget line that must not appear in External mode",
            quantity=Decimal("1.00"),
            unit_price=Decimal("50000.00"),
            estimated_cost=Decimal("30000.00"),
        )

    def _approve_and_create_project(self):
        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/test-123",
            external_reference="J-123",
            external_total=Decimal("75000.00"),
            external_deposit_amount=Decimal("15000.00"),
            idempotency_key="external-send-1",
        )
        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.PENDING,
            idempotency_key="external-pending-1",
        )
        estimate, created = record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.APPROVED,
            external_client_visible=True,
            idempotency_key="external-approve-1",
        )
        self.assertTrue(created)
        project, project_created = create_project_from_estimate(estimate, actor=self.owner)
        self.assertTrue(project_created)
        project.assigned_staff.add(self.manager, self.field)
        return project

    def test_external_approval_unlocks_internal_workflow_and_uses_summary_amounts(self):
        project = self._approve_and_create_project()

        estimate = Estimate.objects.get(pk=self.estimate.pk)
        agreement = Agreement.objects.get(project=project)
        schedule = PaymentSchedule.objects.get(project=project, sequence=1)

        self.assertEqual(estimate.status, Estimate.Status.ACCEPTED)
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.APPROVED)
        self.assertEqual(agreement.contract_value, Decimal("75000.00"))
        self.assertEqual(agreement.deposit_amount, Decimal("15000.00"))
        self.assertEqual(schedule.amount, Decimal("15000.00"))
        self.assertEqual(Project.objects.filter(estimate=estimate).count(), 1)

    def test_retries_are_idempotent_and_do_not_duplicate_audit_events(self):
        first, created = record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/retry",
            idempotency_key="same-status-key",
        )
        second, retried = record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/retry",
            idempotency_key="same-status-key",
        )

        self.assertTrue(created)
        self.assertFalse(retried)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            WorkflowEvent.objects.filter(event_type="external_estimate_status_changed").count(),
            1,
        )

    def test_invalid_urls_transitions_and_stale_updates_are_rejected(self):
        with self.assertRaises(ValidationError):
            record_external_estimate_status(
                self.estimate,
                actor=self.owner,
                status=Estimate.ExternalEstimateStatus.SENT,
                external_url="http://unsafe.example/estimate",
                idempotency_key="unsafe-link",
            )
        with self.assertRaises(ValidationError):
            record_external_estimate_status(
                self.estimate,
                actor=self.owner,
                status=Estimate.ExternalEstimateStatus.SENT,
                external_url="https://user:password@app.external.com/estimate/unsafe",
                idempotency_key="credential-link",
            )

        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/stale",
            idempotency_key="stale-send",
        )
        expected = str(int(self.estimate.updated_at.timestamp()))
        Estimate.objects.filter(pk=self.estimate.pk).update(
            updated_at=timezone.now() + timedelta(seconds=20),
        )
        with self.assertRaises(ValidationError):
            record_external_estimate_status(
                self.estimate,
                actor=self.owner,
                status=Estimate.ExternalEstimateStatus.PENDING,
                expected_updated_at=expected,
                idempotency_key="stale-pending",
            )
        with self.assertRaises(ValidationError):
            record_external_estimate_status(
                self.estimate,
                actor=self.owner,
                status=Estimate.ExternalEstimateStatus.NOT_STARTED,
                idempotency_key="invalid-jump-after-stale",
            )

    def test_rejected_status_closes_estimate_without_creating_project(self):
        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/declined",
            idempotency_key="declined-send",
        )
        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.REJECTED,
            external_status_note="Client declined in External.",
            idempotency_key="declined-result",
        )
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.status, Estimate.Status.DECLINED)
        self.assertEqual(self.estimate.external_status, Estimate.ExternalEstimateStatus.REJECTED)
        with self.assertRaises(ValidationError):
            create_project_from_estimate(self.estimate, actor=self.owner)

    def test_only_owner_manager_and_office_can_mutate_status(self):
        for actor in (self.field, self.client_user):
            with self.subTest(actor=actor.username):
                with self.assertRaises(PermissionDenied):
                    record_external_estimate_status(
                        self.estimate,
                        actor=actor,
                        status=Estimate.ExternalEstimateStatus.SENT,
                        idempotency_key=f"blocked-{actor.username}",
                    )

        record_external_estimate_status(
            self.estimate,
            actor=self.office,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://app.external.com/estimate/office",
            idempotency_key="office-status",
        )
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.external_status, Estimate.ExternalEstimateStatus.SENT)

    def test_invoice_status_never_creates_payment_record_and_reaches_command_surfaces(self):
        project = self._approve_and_create_project()
        schedule = PaymentSchedule.objects.get(project=project, sequence=1)
        record_external_invoice_status(
            schedule,
            actor=self.owner,
            status=PaymentSchedule.ExternalInvoiceStatus.SENT,
            external_invoice_url="https://app.external.com/invoice/test-123",
            external_invoice_reference="INV-123",
            external_client_visible=True,
            idempotency_key="invoice-sent-1",
        )
        record_external_invoice_status(
            schedule,
            actor=self.owner,
            status=PaymentSchedule.ExternalInvoiceStatus.PENDING,
            idempotency_key="invoice-pending-1",
        )
        record_external_invoice_status(
            schedule,
            actor=self.owner,
            status=PaymentSchedule.ExternalInvoiceStatus.PAID,
            idempotency_key="invoice-paid-1",
        )

        schedule.refresh_from_db()
        self.assertEqual(schedule.external_invoice_status, PaymentSchedule.ExternalInvoiceStatus.PAID)
        self.assertEqual(PaymentRecord.objects.filter(project=project).count(), 0)

        http = HttpClient()
        http.force_login(self.client_user)
        portal = http.get(reverse("operations:portal"), {"project": project.pk})
        self.assertEqual(portal.status_code, 200)
        self.assertContains(portal, "Open estimate")
        self.assertNotContains(portal, "Internal budget line that must not appear in External mode")
        client_estimate_link = http.get(
            reverse("operations:external-estimate-link", kwargs={"pk": self.estimate.pk}),
        )
        self.assertEqual(client_estimate_link.status_code, 302)
        self.assertEqual(client_estimate_link["Location"], "https://app.external.com/estimate/test-123")
        client_invoice_link = http.get(
            reverse("operations:external-invoice-link", kwargs={"pk": schedule.pk}),
        )
        self.assertEqual(client_invoice_link.status_code, 302)
        self.assertEqual(client_invoice_link["Location"], "https://app.external.com/invoice/test-123")

        http.force_login(self.field)
        self.assertEqual(
            http.get(reverse("operations:external-estimate-link", kwargs={"pk": self.estimate.pk})).status_code,
            404,
        )

        http.force_login(self.owner)
        self.assertEqual(
            http.get(reverse("operations:external-estimate-link", kwargs={"pk": self.estimate.pk})).status_code,
            404,
        )
        self.assertEqual(
            http.get(reverse("operations:external-invoice-link", kwargs={"pk": schedule.pk})).status_code,
            404,
        )
        owner_estimates = http.get(
            reverse("operations:dashboard-section", kwargs={"section": "estimates"}),
            {"estimate": self.estimate.pk},
        )
        self.assertEqual(owner_estimates.status_code, 200)
        self.assertContains(owner_estimates, 'href="https://app.external.com/estimate/test-123"')
        self.assertNotContains(owner_estimates, 'href="/portal/')

    def test_estimate_status_surface_is_automatic_for_authorized_users(self):
        project = self._approve_and_create_project()
        self.assertTrue(external_estimate_enabled_for(self.owner))
        self.assertTrue(external_estimate_enabled_for(self.owner, project=project))
        self.assertTrue(can_manage_external_estimate(self.owner, project=project))
        self.assertTrue(can_manage_external_estimate(self.owner, estimate=self.estimate))

    def test_dashboard_external_estimate_creation_only_requires_client_and_link(self):
        http = HttpClient()
        http.force_login(self.owner)
        response = http.get(
            reverse("operations:dashboard-section", kwargs={"section": "estimates"}),
            {"new": "estimate"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Estimate link")
        self.assertNotContains(response, "Deposit amount")
        self.assertNotContains(response, "Title")
        created = http.post(
            reverse("operations:estimate-create"),
            {
                "client": str(self.client_record.pk),
                "external_url": "https://estimates.example.com/estimate/new",
            },
        )
        self.assertEqual(created.status_code, 302)
        estimate = Estimate.objects.get(external_url="https://estimates.example.com/estimate/new")
        self.assertEqual(estimate.client_id, self.client_record.pk)
        self.assertEqual(estimate.title, "Estimate for External Client")
        self.assertEqual(estimate.external_status, Estimate.ExternalEstimateStatus.NOT_STARTED)
        self.assertFalse(estimate.line_items.exists())

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_optional_estimate_email_queues_once_and_marks_sent(self):
        self.estimate.external_url = "https://estimates.example.com/estimate/email"
        self.estimate.save(update_fields=["external_url", "updated_at"])
        http = HttpClient()
        http.force_login(self.owner)
        payload = {"idempotency_key": "estimate-email-once"}
        first = http.post(
            reverse("operations:external-estimate-send", kwargs={"pk": self.estimate.pk}),
            payload,
        )
        second = http.post(
            reverse("operations:external-estimate-send", kwargs={"pk": self.estimate.pk}),
            payload,
        )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.external_status, Estimate.ExternalEstimateStatus.SENT)
        self.assertTrue(self.estimate.external_client_visible)
        self.assertEqual(
            EmailOutbox.objects.filter(idempotency_key="estimate-email-once").count(),
            1,
        )

    @override_settings(GCC_EMAIL_DELIVERY_ENABLED=False)
    def test_estimate_email_send_publishes_an_existing_status_to_the_client_portal(self):
        record_external_estimate_status(
            self.estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.SENT,
            external_url="https://estimates.example.com/estimate/already-sent",
            external_client_visible=False,
            idempotency_key="already-sent-status",
        )
        http = HttpClient()
        http.force_login(self.owner)
        response = http.post(
            reverse("operations:external-estimate-send", kwargs={"pk": self.estimate.pk}),
            {"idempotency_key": "already-sent-email"},
        )

        self.assertEqual(response.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertTrue(self.estimate.external_client_visible)
        self.assertEqual(
            EmailOutbox.objects.filter(idempotency_key="already-sent-email").count(),
            1,
        )

    def test_estimate_panel_offers_provider_free_native_text_link(self):
        self.client_record.phone = "+1 (805) 555-0100"
        self.client_record.save(update_fields=["phone", "updated_at"])
        self.estimate.external_url = "https://estimates.example.com/estimate/text"
        self.estimate.save(update_fields=["external_url", "updated_at"])

        http = HttpClient()
        http.force_login(self.owner)
        response = http.get(
            reverse("operations:dashboard-section", kwargs={"section": "estimates"}),
            {"estimate": self.estimate.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Text client")
        self.assertContains(response, "sms:")
        self.assertContains(response, "device's Messages app")
        self.assertContains(response, "emails the saved estimate link")
        self.assertContains(response, "client portal")
        self.assertNotContains(response, "Twilio")

        self.client_record.phone = ""
        self.client_record.save(update_fields=["phone", "updated_at"])
        response = http.get(
            reverse("operations:dashboard-section", kwargs={"section": "estimates"}),
            {"estimate": self.estimate.pk},
        )
        self.assertNotContains(response, "Text client")

    def test_client_portal_never_falls_back_to_internal_estimate_details(self):
        project = self._approve_and_create_project()
        http = HttpClient()
        http.force_login(self.client_user)
        portal = http.get(reverse("operations:portal"), {"project": project.pk})
        self.assertEqual(portal.status_code, 200)
        self.assertContains(portal, "Estimate")
        self.assertNotContains(portal, "Internal budget line that must not appear in External mode")
        self.assertNotContains(portal, "Select an estimate to review its line items.")
