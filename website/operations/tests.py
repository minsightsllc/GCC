from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import ContactLeadForm, MediaUploadForm
from .models import (
    Activity,
    Client,
    ClientInvite,
    Estimate,
    EstimateLineItem,
    Lead,
    LeadAttachment,
    MediaAsset,
    Milestone,
    ProcessStep,
    Project,
    ProjectUpdate,
    Service,
    SiteSettings,
)
from .services import create_client_invite


TEST_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="gcc-operations-tests-"))
VALID_PNG = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)
TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
    "contact_submissions": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    STORAGES=TEST_STORAGES,
    GCC_TURNSTILE_ENABLED=False,
    CLOUDFLARE_TURNSTILE_SITE_KEY="",
    CLOUDFLARE_TURNSTILE_SECRET_KEY="",
)
class OperationsWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.staff = user_model.objects.create_user(
            username="owner",
            password="owner-pass",
            is_staff=True,
            is_superuser=True,
        )
        cls.client_user = user_model.objects.create_user(
            username="client-a",
            password="client-pass",
            first_name="Maya",
            last_name="Thompson",
            email="maya@example.com",
        )
        cls.other_client_user = user_model.objects.create_user(
            username="client-b",
            password="client-pass",
            first_name="Jordan",
            last_name="Lee",
            email="jordan@example.com",
        )
        cls.client_record = Client.objects.create(
            name="Maya Thompson",
            email="maya@example.com",
            user=cls.client_user,
        )
        cls.other_client_record = Client.objects.create(
            name="Jordan Lee",
            email="jordan@example.com",
            user=cls.other_client_user,
        )
        cls.lead = Lead.objects.create(
            client=cls.client_record,
            name="Maya Thompson",
            email="maya@example.com",
            service="Renovations",
            location="Ventura, CA",
            status=Lead.Status.NEW,
            created_by=cls.staff,
        )
        cls.estimate = Estimate.objects.create(
            lead=cls.lead,
            client=cls.client_record,
            title="Kitchen scope",
            status=Estimate.Status.DRAFT,
            deposit_amount=Decimal("50.00"),
            created_by=cls.staff,
        )
        cls.line = EstimateLineItem.objects.create(
            estimate=cls.estimate,
            description="Cabinetry",
            quantity=Decimal("2.00"),
            unit_price=Decimal("100.00"),
            sort_order=1,
        )
        cls.project = Project.objects.create(
            estimate=cls.estimate,
            lead=cls.lead,
            client=cls.client_record,
            title="Kitchen project",
            location="Ventura, CA",
            project_type="renovation",
            status=Project.Status.PLANNING,
            fallback_image="operations/images/progress-kitchen.png",
            created_by=cls.staff,
        )
        cls.milestone = Milestone.objects.create(
            project=cls.project,
            title="Walkthrough",
            sort_order=1,
        )
        cls.other_project = Project.objects.create(
            client=cls.other_client_record,
            title="Private other project",
            location="Santa Barbara, CA",
            project_type="residential",
            fallback_image="operations/images/project-adu.png",
            created_by=cls.staff,
        )
        cls.public_media = MediaAsset.objects.create(
            project=cls.project,
            title="Public reference",
            fallback_image="operations/images/hero-kitchen.png",
            visibility=MediaAsset.Visibility.PUBLIC,
            uploaded_by=cls.staff,
        )
        cls.client_media = MediaAsset.objects.create(
            project=cls.project,
            title="Client progress",
            fallback_image="operations/images/progress-kitchen.png",
            visibility=MediaAsset.Visibility.CLIENT,
            uploaded_by=cls.staff,
        )
        cls.internal_media = MediaAsset.objects.create(
            project=cls.project,
            title="Internal reference",
            fallback_image="operations/images/project-bathroom.png",
            visibility=MediaAsset.Visibility.INTERNAL,
            uploaded_by=cls.staff,
        )
        Service.objects.create(
            slug="renovations",
            title="Renovations",
            description="Renovation work.",
            image_path="operations/images/hero-kitchen.png",
            sort_order=1,
        )
        ProcessStep.objects.create(
            key="inquire",
            title="Start together",
            description="A clear first step.",
            sort_order=1,
        )
        SiteSettings.objects.create(
            headline="Build with confidence.",
            subheadline="A clear path.",
            featured_title="Featured work",
            featured_body="Thoughtful details.",
            featured_project=cls.project,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.browser = DjangoClient()

    def login_staff(self):
        self.browser.logout()
        self.browser.force_login(self.staff)
        self.browser.post(
            reverse('admin:access'),
            {'identifier': self.staff.username},
        )

    def login_client(self):
        self.browser.logout()
        self.browser.force_login(self.client_user)

    def test_public_contact_form_creates_real_lead(self):
        response = self.browser.post(
            reverse("operations:contact"),
            {
                "first_name": "New",
                "last_name": "Homeowner",
                "email": "new.homeowner@example.com",
                "phone": "805-555-0100",
                "project_type": "Restoration",
                "location": "Ojai, CA",
                "message": "Please help us plan the first phase.",
            },
        )
        self.assertRedirects(response, reverse("operations:contact"))
        lead = Lead.objects.get(email="new.homeowner@example.com")
        self.assertEqual(lead.name, "New Homeowner")
        self.assertEqual(lead.status, Lead.Status.NEW)
        self.assertEqual(lead.note, "Please help us plan the first phase.")
        self.assertEqual(lead.internal_note, "")
        self.assertTrue(Activity.objects.filter(lead=lead, message="New lead received").exists())

    def test_contact_form_attachment_uses_contact_form_storage_prefix(self):
        response = self.browser.post(
            reverse("operations:contact"),
            {
                "first_name": "Reference",
                "last_name": "Uploader",
                "email": "reference.uploader@example.com",
                "phone": "805-555-0101",
                "project_type": "Restoration",
                "location": "Ojai, CA",
                "message": "Please review the attached reference photo.",
                "photos": SimpleUploadedFile(
                    "front-elevation.png",
                    VALID_PNG,
                    content_type="image/png",
                ),
            },
        )
        self.assertRedirects(response, reverse("operations:contact"))
        attachment = LeadAttachment.objects.get(lead__email="reference.uploader@example.com")
        self.assertTrue(attachment.file.name.startswith("contact-form/"))
        self.assertTrue((TEST_MEDIA_ROOT / Path(*attachment.file.name.split("/"))).exists())

    def test_contact_form_accepts_documents_and_admin_can_view_message_and_file(self):
        response = self.browser.post(
            reverse("operations:contact"),
            {
                "first_name": "Plans",
                "last_name": "Uploader",
                "email": "plans.uploader@example.com",
                "phone": "805-555-0102",
                "project_type": "New build",
                "location": "Ojai, CA",
                "message": "Please review these plans before we schedule a call.",
                "photos": SimpleUploadedFile(
                    "site-plans.pdf",
                    b"%PDF-1.7\ncontact plans",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertRedirects(response, reverse("operations:contact"))
        lead = Lead.objects.get(email="plans.uploader@example.com")
        attachment = lead.attachments.get()
        self.login_staff()

        dashboard = self.browser.get(
            reverse("operations:dashboard-section", kwargs={"section": "leads"}),
            {"lead": lead.pk},
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, lead.note)
        self.assertContains(dashboard, "site-plans.pdf")
        download_url = reverse("operations:lead-attachment-file", kwargs={"pk": attachment.pk})
        self.assertContains(dashboard, download_url)

        download = self.browser.get(download_url)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.7\ncontact plans")
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertIn("site-plans.pdf", download["Content-Disposition"])
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

        self.login_client()
        self.assertEqual(self.browser.get(download_url).status_code, 403)
        self.browser.logout()
        self.assertEqual(self.browser.get(download_url).status_code, 302)

    def test_admin_lead_detail_shows_contact_message_and_attachment(self):
        lead = Lead.objects.create(
            name="Admin Review Lead",
            email="admin.review@example.com",
            service="Planning",
            location="Ojai, CA",
            note="Please review the attached site plan.",
            source="Website form",
        )
        attachment = LeadAttachment(lead=lead, original_name="admin-site-plan.png")
        attachment.file.save("admin-site-plan.png", ContentFile(VALID_PNG), save=True)
        self.login_staff()

        response = self.browser.get(
            reverse("admin:operations_lead_change", kwargs={"object_id": lead.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, lead.note)
        self.assertNotContains(response, 'name="note"')
        self.assertContains(response, 'name="internal_note"')
        self.assertContains(response, "admin-site-plan.png")
        self.assertContains(
            response,
            reverse("operations:lead-attachment-file", kwargs={"pk": attachment.pk}),
        )

    def test_lead_dashboard_labels_inquiry_and_admin_note_separately(self):
        lead = Lead.objects.create(
            name="Inquiry Separation Lead",
            email="inquiry.separation@example.com",
            service="Planning",
            location="Ojai, CA",
            note="Please call me about the original project inquiry.",
            internal_note="PRIVATE: confirm preferred call time.",
        )
        self.login_staff()

        response = self.browser.get(
            reverse("operations:dashboard-section", kwargs={"section": "leads"}),
            {"lead": lead.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Original client inquiry (read-only)")
        self.assertContains(response, lead.note)
        self.assertContains(response, "Admin note (internal)")
        self.assertContains(
            response,
            "Private to Grand Coast staff; not shown in the client portal.",
        )
        self.assertContains(response, lead.internal_note)

    def test_contact_upload_rejects_spoofed_file_content(self):
        form = ContactLeadForm(
            data={
                "first_name": "Spoof",
                "last_name": "Uploader",
                "email": "spoof@example.com",
                "phone": "",
                "project_type": "Testing",
                "location": "Ojai, CA",
                "message": "This should not be saved.",
            },
            files={
                "photos": SimpleUploadedFile(
                    "not-really-an-image.jpg",
                    b"plain text pretending to be an image",
                    content_type="image/jpeg",
                ),
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("contents do not match", str(form.errors["photos"]))

    def test_contact_upload_rejects_more_than_eight_files(self):
        uploads = [
            SimpleUploadedFile(f"reference-{index}.txt", b"safe text", content_type="text/plain")
            for index in range(9)
        ]
        form = ContactLeadForm(
            data={
                "first_name": "Many",
                "last_name": "Files",
                "email": "many.files@example.com",
                "phone": "",
                "project_type": "Testing",
                "location": "Ojai, CA",
                "message": "This should not be saved.",
            },
            files={"photos": uploads},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("up to 8 files", str(form.errors["photos"]))

    def test_contact_picker_advertises_media_and_document_extensions(self):
        response = self.browser.get(reverse("operations:contact"))
        self.assertEqual(response.status_code, 200)
        for extension in (".png", ".mp4", ".pdf", ".docx", ".xlsx", ".txt"):
            self.assertContains(response, extension)

    def test_staff_dashboard_is_protected_from_clients_and_anonymous_users(self):
        response = self.browser.get(reverse("operations:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("operations:login"), response["Location"])
        self.login_client()
        self.assertEqual(self.browser.get(reverse("operations:dashboard")).status_code, 403)
        self.login_staff()
        response = self.browser.get(reverse("operations:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'href="/portal/"')
        self.assertContains(response, reverse("admin:index"))

    def test_admin_search_routes_operations_records_to_dashboard(self):
        self.login_staff()

        lead_response = self.browser.get(reverse('admin:search'), {'s': 'Maya'})
        self.assertEqual(lead_response.status_code, 200)
        leads_url = reverse('operations:dashboard-section', kwargs={'section': 'leads'})
        estimates_url = reverse('operations:dashboard-section', kwargs={'section': 'estimates'})
        projects_url = reverse('operations:dashboard-section', kwargs={'section': 'projects'})
        self.assertContains(lead_response, 'Maya Thompson')
        self.assertContains(lead_response, f'{leads_url}?lead={self.lead.pk}')
        self.assertContains(lead_response, f'{estimates_url}?estimate={self.estimate.pk}')
        self.assertContains(lead_response, f'{projects_url}?project={self.project.pk}')
        self.assertNotContains(lead_response, '/admin/operations/lead/')

        media_response = self.browser.get(reverse('admin:search'), {'s': 'Public reference'})
        self.assertContains(media_response, 'Public reference')
        media_url = reverse('operations:dashboard-section', kwargs={'section': 'media'})
        self.assertContains(
            media_response,
            f'{media_url}?media_project={self.project.pk}',
        )

        content_response = self.browser.get(reverse('admin:search'), {'s': 'Renovation work'})
        self.assertContains(content_response, 'Renovations')
        self.assertContains(
            content_response,
            reverse('operations:dashboard-section', kwargs={'section': 'content'}),
        )

    def test_admin_search_is_staff_only(self):
        anonymous_response = self.browser.get(reverse('admin:search'), {'s': 'Maya'})
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn(reverse('admin:access'), anonymous_response['Location'])

        self.login_client()
        client_response = self.browser.get(reverse('admin:search'), {'s': 'Maya'})
        self.assertEqual(client_response.status_code, 403)

    def test_dashboard_lead_search_matches_client_fields(self):
        self.login_staff()
        response = self.browser.get(
            reverse('operations:dashboard-section', kwargs={'section': 'leads'}),
            {'q': 'maya@example.com'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Maya Thompson')

    def test_lead_status_and_priority_are_persistent(self):
        self.login_staff()
        status_response = self.browser.post(
            reverse("operations:lead-status", kwargs={"pk": self.lead.pk}),
            {"status": Lead.Status.QUALIFIED},
        )
        self.assertEqual(status_response.status_code, 302)
        priority_response = self.browser.post(
            reverse("operations:lead-priority", kwargs={"pk": self.lead.pk}),
        )
        self.assertEqual(priority_response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.QUALIFIED)
        self.assertTrue(self.lead.priority)

    def test_legacy_estimate_update_is_read_only(self):
        original_title = self.estimate.title
        original_total = self.estimate.total
        self.login_staff()
        response = self.browser.post(
            reverse("operations:estimate-update", kwargs={"pk": self.estimate.pk}),
            {
                "title": "Updated kitchen scope",
                "status": Estimate.Status.DRAFT,
                "deposit_amount": "75.00",
                "notes": "Updated notes",
                "lines-TOTAL_FORMS": "2",
                "lines-INITIAL_FORMS": "1",
                "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "50",
                "lines-0-id": str(self.line.pk),
                "lines-0-description": "Cabinetry revised",
                "lines-0-quantity": "2.00",
                "lines-0-unit_price": "125.00",
                "lines-0-sort_order": "1",
                "lines-1-id": "",
                "lines-1-description": "Hardware",
                "lines-1-quantity": "1.00",
                "lines-1-unit_price": "25.00",
                "lines-1-sort_order": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.title, original_title)
        self.assertEqual(self.estimate.total, original_total)
        self.assertEqual(self.estimate.deposit_amount, Decimal("50.00"))
        self.assertEqual(self.estimate.line_items.count(), 1)
        self.line.refresh_from_db()
        self.assertEqual(self.line.description, "Cabinetry")

    def test_legacy_estimate_line_items_cannot_be_removed(self):
        self.login_staff()
        response = self.browser.post(
            reverse("operations:estimate-update", kwargs={"pk": self.estimate.pk}),
            {
                "title": "Kitchen scope",
                "status": Estimate.Status.DRAFT,
                "deposit_amount": "0.00",
                "notes": "",
                "lines-TOTAL_FORMS": "2",
                "lines-INITIAL_FORMS": "1",
                "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "50",
                "lines-0-id": str(self.line.pk),
                "lines-0-description": "Cabinetry",
                "lines-0-quantity": "2.00",
                "lines-0-unit_price": "100.00",
                "lines-0-sort_order": "1",
                "lines-0-DELETE": "on",
                "lines-1-id": "",
                "lines-1-description": "",
                "lines-1-quantity": "1.00",
                "lines-1-unit_price": "0.00",
                "lines-1-sort_order": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EstimateLineItem.objects.filter(pk=self.line.pk).exists())
        self.assertEqual(self.estimate.line_items.count(), 1)

    def test_legacy_estimate_cannot_be_sent_or_mutated(self):
        self.login_staff()
        response = self.browser.post(reverse("operations:estimate-send", kwargs={"pk": self.estimate.pk}))
        self.assertEqual(response.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.status, Estimate.Status.DRAFT)
        self.assertIsNone(self.estimate.sent_at)
        old_title = self.estimate.title
        locked = self.browser.post(
            reverse("operations:estimate-update", kwargs={"pk": self.estimate.pk}),
            {"title": "Should not change"},
        )
        self.assertEqual(locked.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.title, old_title)

    def test_client_cannot_accept_legacy_estimate_inside_gcc(self):
        self.estimate.status = Estimate.Status.SENT
        self.estimate.sent_at = timezone.now()
        self.estimate.save(update_fields=["status", "sent_at", "updated_at"])
        self.login_client()
        response = self.browser.post(
            reverse("operations:portal-accept-estimate", kwargs={"pk": self.estimate.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.estimate.refresh_from_db()
        self.assertEqual(self.estimate.status, Estimate.Status.SENT)
        self.assertIsNone(self.estimate.accepted_by_id)
        self.assertIsNone(self.estimate.accepted_at)

    def test_internal_accepted_estimate_cannot_create_project(self):
        estimate = Estimate.objects.create(
            lead=self.lead,
            client=self.client_record,
            title="Accepted addition",
            status=Estimate.Status.ACCEPTED,
            created_by=self.staff,
        )
        EstimateLineItem.objects.create(
            estimate=estimate,
            description="Planning",
            quantity=1,
            unit_price=100,
        )
        self.login_staff()
        response = self.browser.post(reverse("operations:estimate-project", kwargs={"pk": estimate.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.filter(estimate=estimate).exists())

    def test_api_estimate_mutations_are_retired(self):
        external_estimate = Estimate.objects.create(
            client=self.client_record,
            title="External estimate",
            status=Estimate.Status.SENT,
            external_url="https://estimates.example.com/kitchen",
            external_status=Estimate.ExternalEstimateStatus.SENT,
            external_client_visible=True,
            created_by=self.staff,
        )
        unpublished_estimate = Estimate.objects.create(
            client=self.client_record,
            title="Unpublished external estimate",
            status=Estimate.Status.SENT,
            external_url="https://estimates.example.com/unpublished",
            external_status=Estimate.ExternalEstimateStatus.SENT,
            external_client_visible=False,
            created_by=self.staff,
        )
        self.login_staff()
        send_response = self.browser.post(
            reverse("operations-api:estimate-send", kwargs={"pk": external_estimate.pk}),
        )
        self.assertEqual(send_response.status_code, 410)
        self.assertIn("external estimate link", send_response.json()["error"].lower())
        external_estimate.refresh_from_db()
        self.assertEqual(external_estimate.status, Estimate.Status.SENT)

        self.login_client()
        accept_response = self.browser.post(
            reverse("operations-api:estimate-accept", kwargs={"pk": external_estimate.pk}),
        )
        self.assertEqual(accept_response.status_code, 410)
        self.assertIn("external estimate service", accept_response.json()["error"].lower())
        external_estimate.refresh_from_db()
        self.assertEqual(external_estimate.status, Estimate.Status.SENT)
        self.assertIsNone(external_estimate.accepted_by_id)
        list_response = self.browser.get(reverse("operations-api:estimates"))
        self.assertEqual(list_response.status_code, 200)
        listed_ids = {item["id"] for item in list_response.json()["results"]}
        self.assertIn(str(external_estimate.pk), listed_ids)
        self.assertNotIn(str(unpublished_estimate.pk), listed_ids)

    def test_project_milestones_and_updates_persist_with_visibility(self):
        self.login_staff()
        milestone_response = self.browser.post(
            reverse("operations:milestone-toggle", kwargs={"pk": self.milestone.pk}),
            {"is_complete": ["off", "on"]},
        )
        self.assertEqual(milestone_response.status_code, 302)
        update_response = self.browser.post(
            reverse("operations:project-add-update", kwargs={"pk": self.project.pk}),
            {"title": "Selections are ready", "body": "Please review the finish direction.", "visibility": ProjectUpdate.Visibility.CLIENT},
        )
        self.assertEqual(update_response.status_code, 302)
        self.milestone.refresh_from_db()
        self.assertTrue(self.milestone.is_complete)
        project_response = self.browser.get(
            reverse("operations:dashboard-section", kwargs={"section": "projects"}),
            {"project": self.project.pk},
        )
        self.assertContains(project_response, 'checked data-project-sequence-checkbox')
        self.assertContains(project_response, 'data-project-sequence-confirmation')
        self.assertContains(project_response, '100% complete')
        self.assertContains(project_response, 'Walkthrough marked complete.')
        update = ProjectUpdate.objects.get(project=self.project, title="Selections are ready")
        self.assertEqual(update.visibility, ProjectUpdate.Visibility.CLIENT)

    def test_milestone_can_be_completed_again_after_reopening(self):
        self.milestone.title = "Selections"
        self.milestone.sort_order = 3
        self.milestone.save(update_fields=["title", "sort_order"])
        for title, sort_order, is_complete in (
            ("Walkthrough", 1, True),
            ("Estimate approved", 2, True),
            ("Construction", 4, False),
            ("Final walkthrough", 5, False),
        ):
            Milestone.objects.create(
                project=self.project,
                title=title,
                sort_order=sort_order,
                is_complete=is_complete,
                completed_at=timezone.now() if is_complete else None,
            )

        self.login_staff()
        milestone_url = reverse("operations:milestone-toggle", kwargs={"pk": self.milestone.pk})
        self.browser.post(milestone_url, {"is_complete": ["off", "on"]})
        self.milestone.refresh_from_db()
        self.assertTrue(self.milestone.is_complete)
        self.assertEqual(self.project.progress_percent, 60)

        self.browser.post(milestone_url, {"is_complete": "off"})
        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_complete)
        self.assertEqual(self.project.progress_percent, 40)

        self.browser.post(milestone_url, {"is_complete": ["off", "on"]})
        self.milestone.refresh_from_db()
        self.assertTrue(self.milestone.is_complete)
        self.assertEqual(self.project.progress_percent, 60)

        admin_response = self.browser.get(
            reverse("operations:dashboard-section", kwargs={"section": "projects"}),
            {"project": self.project.pk},
        )
        self.assertContains(admin_response, "60% complete")
        self.assertContains(admin_response, "Selections marked complete.")

        self.login_client()
        client_response = self.browser.get(reverse("operations:portal"), {"project": self.project.pk})
        self.assertContains(client_response, "60% complete")

    def test_replayed_milestone_request_does_not_claim_success_for_stale_state(self):
        self.login_staff()
        milestone_url = reverse("operations:milestone-toggle", kwargs={"pk": self.milestone.pk})
        repeated_request_key = "same-milestone-completion"
        self.browser.post(
            milestone_url,
            {"is_complete": ["off", "on"], "idempotency_key": repeated_request_key},
            follow=True,
        )
        self.browser.post(milestone_url, {"is_complete": "off"}, follow=True)

        response = self.browser.post(
            milestone_url,
            {"is_complete": ["off", "on"], "idempotency_key": repeated_request_key},
            follow=True,
        )

        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_complete)
        self.assertContains(response, "The milestone could not be updated.")
        self.assertNotContains(response, "Walkthrough marked complete.")

    def test_client_portal_only_shows_assigned_project(self):
        self.lead.internal_note = "PRIVATE admin-only follow-up detail"
        self.lead.save(update_fields=["internal_note", "updated_at"])
        self.login_client()
        response = self.browser.get(
            reverse("operations:portal"),
            {"project": self.other_project.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.project.title)
        self.assertNotContains(response, self.other_project.title)
        self.assertNotContains(response, self.lead.internal_note)

    def test_edit_client_control_opens_cancels_and_saves_client_form(self):
        self.login_staff()
        clients_url = reverse("operations:dashboard-section", kwargs={"section": "clients"})

        view_response = self.browser.get(clients_url, {"client": self.client_record.pk})
        self.assertEqual(view_response.status_code, 200)
        self.assertContains(view_response, "Edit client")
        self.assertNotContains(view_response, 'id="client-edit-form"')

        edit_response = self.browser.get(
            clients_url,
            {"client": self.client_record.pk, "edit": "client"},
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "Cancel edit")
        self.assertContains(edit_response, 'id="client-edit-form"')

        update_response = self.browser.post(
            reverse("operations:client-update", kwargs={"pk": self.client_record.pk}),
            {
                "name": "Maya Thompson Updated",
                "company": "Coastal Home",
                "email": "maya.updated@example.com",
                "phone": "805-555-0111",
            },
        )
        self.assertEqual(update_response.status_code, 302)
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.name, "Maya Thompson Updated")
        self.assertEqual(self.client_record.company, "Coastal Home")
        self.assertEqual(self.client_record.email, "maya.updated@example.com")
        self.assertEqual(self.client_record.phone, "805-555-0111")

    def test_private_media_requires_correct_authenticated_client(self):
        self.public_media.file.save("public-reference.jpg", ContentFile(b"public bytes"), save=True)
        self.client_media.file.save("client-progress.jpg", ContentFile(b"client bytes"), save=True)
        self.internal_media.file.save("internal-reference.jpg", ContentFile(b"internal bytes"), save=True)
        public_url = reverse("operations:media-file", kwargs={"pk": self.public_media.pk})
        client_url = reverse("operations:media-file", kwargs={"pk": self.client_media.pk})
        internal_url = reverse("operations:media-file", kwargs={"pk": self.internal_media.pk})
        direct_storage_url = f"{settings.MEDIA_URL}{self.client_media.file.name}"
        self.assertEqual(self.browser.get(public_url).status_code, 200)
        self.assertEqual(self.browser.get(direct_storage_url).status_code, 404)
        self.login_client()
        self.assertEqual(self.browser.get(client_url, follow=True).status_code, 200)
        self.assertEqual(self.browser.get(internal_url).status_code, 403)
        self.browser.force_login(self.other_client_user)
        self.assertEqual(self.browser.get(client_url).status_code, 403)
        self.login_staff()
        self.assertEqual(self.browser.get(internal_url).status_code, 200)

    def test_media_upload_form_rejects_unsupported_extension(self):
        upload = SimpleUploadedFile("malware.exe", b"not an image", content_type="application/octet-stream")
        form = MediaUploadForm(
            {"project": str(self.project.pk), "visibility": MediaAsset.Visibility.CLIENT},
            {"files": [upload]},
            project_queryset=Project.objects.all(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Upload a JPG", str(form.errors))

    def test_valid_media_upload_is_persisted(self):
        self.login_staff()
        upload = SimpleUploadedFile("progress.jpg", b"fake image bytes", content_type="image/jpeg")
        response = self.browser.post(
            reverse("operations:media-upload"),
            {"project": str(self.project.pk), "visibility": MediaAsset.Visibility.CLIENT, "files": [upload]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MediaAsset.objects.filter(project=self.project, title="progress").exists())

    def test_content_updates_appear_on_public_site(self):
        self.login_staff()
        response = self.browser.post(
            reverse("operations:content-update"),
            {
                "headline": "Build with more clarity.",
                "subheadline": "Updated supporting copy.",
                "featured_title": "Updated featured work",
                "featured_body": "Updated featured description.",
                "service_renovations_title": "Renovation planning",
                "service_renovations_copy": "Updated service copy.",
                "step_inquire": "Begin with a walkthrough",
            },
        )
        self.assertEqual(response.status_code, 302)
        public_response = self.browser.get(reverse("operations:home"))
        self.assertContains(public_response, "Build with more clarity.")
        self.assertContains(public_response, "Renovation planning")

    def test_invite_is_one_time_and_expires(self):
        invite_client = Client.objects.create(name="Invite Client", email="invite@example.com")
        invite, raw_token = create_client_invite(invite_client, actor=self.staff)
        invite_response = self.browser.get(reverse("operations:client-invite", kwargs={"token": raw_token}))
        self.assertEqual(invite_response.status_code, 200)
        invite_form = invite_response.context["form"]
        self.assertEqual(invite_form["email"].value(), "invite@example.com")
        self.assertTrue(invite_form["email"].field.disabled)
        self.assertFalse(invite_form["username"].value())
        self.assertEqual(
            invite_form["username"].field.widget.attrs["placeholder"],
            "Create a unique username",
        )
        self.assertEqual(invite_form["password1"].label, "Password")
        self.assertEqual(invite_form["password2"].label, "Confirm Password")
        accept_response = self.browser.post(
            reverse("operations:client-invite", kwargs={"token": raw_token}),
            {
                "username": "jordan.portal",
                "first_name": "Jordan",
                "last_name": "Lee",
                "password1": "strong-client-password",
                "password2": "strong-client-password",
            },
        )
        self.assertEqual(accept_response.status_code, 302)
        invite_client.refresh_from_db()
        self.assertEqual(invite_client.user.username, "jordan.portal")
        self.assertEqual(invite_client.user.email, "invite@example.com")
        invite.refresh_from_db()
        self.assertIsNotNone(invite.accepted_at)
        self.assertEqual(self.browser.get(reverse("operations:client-invite", kwargs={"token": raw_token})).status_code, 410)

        expired_raw = "expired-token"
        expired = ClientInvite.objects.create(
            client=self.client_record,
            token_hash=hashlib.sha256(expired_raw.encode()).hexdigest(),
            expires_at=timezone.now() - timedelta(days=1),
            created_by=self.staff,
        )
        self.assertFalse(expired.is_usable)
        self.assertEqual(self.browser.get(reverse("operations:client-invite", kwargs={"token": expired_raw})).status_code, 410)
