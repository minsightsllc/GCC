from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import Client as DjangoClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .admin import ProjectAdmin, grand_coast_admin_site
from .construction_services import complete_closeout_item
from .models import (
    Activity,
    Agreement,
    Client,
    ClientMessage,
    ClientNotification,
    CloseoutItem,
    EmployeeNotification,
    EmployeeProfile,
    Estimate,
    EstimateLineItem,
    Project,
    ProjectDocument,
    Task,
    WarrantyItem,
    WorkflowEvent,
    WebsiteGalleryPost,
)
from .project_archives import (
    estimate_history_delete_blocked,
    permanently_delete_project,
    project_export_file,
)
from .test_helpers import force_login_with_admin_gate


TEST_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="gcc-project-archive-tests-"))
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
    GCC_TURNSTILE_ALLOW_MISSING=True,
    CLOUDFLARE_TURNSTILE_SITE_KEY="",
    CLOUDFLARE_TURNSTILE_SECRET_KEY="",
)
class ProjectArchiveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(
            username="archive-owner",
            email="archive-owner@example.test",
            password="owner-pass-123",
            is_staff=True,
            is_superuser=True,
        )
        cls.employee = User.objects.create_user(
            username="archive-field",
            email="archive-field@example.test",
            password="field-pass-123",
            is_staff=True,
        )
        field_group, _ = Group.objects.get_or_create(name="Field")
        cls.employee.groups.add(field_group)
        EmployeeProfile.objects.create(user=cls.employee, job_title="Field employee")
        cls.manager = User.objects.create_user(
            username="archive-manager",
            email="archive-manager@example.test",
            password="manager-pass-123",
            is_staff=True,
        )
        manager_group, _ = Group.objects.get_or_create(name="Manager")
        cls.manager.groups.add(manager_group)
        EmployeeProfile.objects.create(user=cls.manager, job_title="Project manager")
        cls.client_user = User.objects.create_user(
            username="archive-client",
            email="archive-client@example.test",
            password="client-pass-123",
        )
        cls.client_record = Client.objects.create(
            name="Archive Client",
            email=cls.client_user.email,
            user=cls.client_user,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def make_project(self, *, title="Completed kitchen", status=Project.Status.COMPLETE, estimate=None):
        project = Project.objects.create(
            title=title,
            location="Ventura, CA",
            client=self.client_record,
            estimate=estimate,
            project_manager=self.employee,
            status=status,
            operational_phase=(
                Project.OperationalPhase.WARRANTY
                if status == Project.Status.COMPLETE
                else Project.OperationalPhase.CONSTRUCTION
            ),
            is_published=True,
            public_title=title,
        )
        project.assigned_staff.add(self.employee)
        return project

    def test_project_completion_archives_creates_warranty_and_keeps_employee_active(self):
        project = self.make_project(
            title="Ready to close",
            status=Project.Status.FINAL,
        )
        project.operational_phase = Project.OperationalPhase.CLOSEOUT
        project.save(update_fields=["operational_phase", "updated_at"])
        closeout = CloseoutItem.objects.create(
            project=project,
            key="final-walkthrough",
            label="Final walkthrough",
            required=True,
        )

        complete_closeout_item(closeout, actor=self.owner)

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.COMPLETE)
        self.assertEqual(project.operational_phase, Project.OperationalPhase.WARRANTY)
        self.assertIsNotNone(project.archived_at)
        self.assertTrue(project.warranty_items.filter(title="Post-closeout warranty coverage").exists())
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)
        self.assertTrue(self.employee.employee_profile.is_active)

    def test_archived_project_keeps_complete_status(self):
        project = self.make_project(title="Status stays complete")
        project.status = Project.Status.CONSTRUCTION
        with self.assertRaises(ValidationError):
            project.save(update_fields=["status", "updated_at"])

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.COMPLETE)
        self.assertIsNotNone(project.archived_at)

    def test_admin_project_save_archives_and_initializes_warranty(self):
        project = self.make_project(title="Admin completed", status=Project.Status.FINAL)
        project.status = Project.Status.COMPLETE
        admin = ProjectAdmin(Project, grand_coast_admin_site)

        admin.save_model(
            SimpleNamespace(user=self.owner),
            project,
            form=None,
            change=True,
        )

        project.refresh_from_db()
        self.assertIsNotNone(project.archived_at)
        self.assertEqual(project.operational_phase, Project.OperationalPhase.WARRANTY)
        self.assertEqual(project.warranty_items.count(), 1)
        self.assertFalse(admin.has_delete_permission(SimpleNamespace(user=self.owner), project))
        self.assertIn("status", admin.get_readonly_fields(SimpleNamespace(user=self.owner), project))

    def test_client_can_view_archive_but_project_actions_and_messages_are_read_only(self):
        project = self.make_project()
        Agreement.objects.create(
            project=project,
            status=Agreement.Status.ISSUED,
            contract_value="1200.00",
        )
        ClientMessage.objects.create(
            client=self.client_record,
            project=project,
            body="Archive project history",
            sent_by=self.owner,
        )
        browser = DjangoClient()
        browser.force_login(self.client_user)

        overview = browser.get(reverse("operations:portal"))
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, "available to view")
        messages_page = browser.get(
            reverse("operations:portal-section", kwargs={"section": "messages"})
            + f"?project={project.pk}"
        )
        self.assertContains(messages_page, "messages are available to view")
        self.assertNotContains(messages_page, 'name="project" value=')

        denied_message = browser.post(
            reverse("operations:portal-message"),
            {"project": str(project.pk), "body": "New message"},
        )
        self.assertEqual(denied_message.status_code, 403)
        denied_agreement = browser.post(
            reverse("operations:portal-accept-agreement", kwargs={"pk": project.agreement.pk})
        )
        self.assertEqual(denied_agreement.status_code, 403)
        self.assertEqual(ClientMessage.objects.filter(project=project).count(), 1)

    def test_client_keeps_other_active_projects_and_account_after_project_delete(self):
        archived = self.make_project(title="Archived kitchen")
        active = self.make_project(
            title="Active bathroom",
            status=Project.Status.CONSTRUCTION,
        )
        browser = DjangoClient()
        browser.force_login(self.client_user)

        portal_response = browser.get(reverse("operations:portal"))
        self.assertEqual(portal_response.status_code, 200)
        self.assertEqual(
            {project.pk for project in portal_response.context["portal_projects"]},
            {archived.pk, active.pk},
        )
        active_message = browser.post(
            reverse("operations:portal-message"),
            {"project": str(active.pk), "body": "Question about the active project"},
        )
        self.assertEqual(active_message.status_code, 302)
        self.assertTrue(
            ClientMessage.objects.filter(
                project=active,
                body="Question about the active project",
            ).exists()
        )

        permanently_delete_project(archived)

        self.assertFalse(Project.objects.filter(pk=archived.pk).exists())
        self.assertTrue(Project.objects.filter(pk=active.pk).exists())
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.is_active)
        self.assertTrue(Client.objects.filter(pk=self.client_record.pk, user=self.client_user).exists())
        after_delete = browser.get(reverse("operations:portal"))
        self.assertEqual(after_delete.status_code, 200)
        self.assertEqual(
            {project.pk for project in after_delete.context["portal_projects"]},
            {active.pk},
        )
        self.assertNotContains(after_delete, archived.title)

    def test_employee_sees_only_assigned_archived_warranty_work_and_can_resolve_it(self):
        archived = self.make_project(title="Archive with warranty")
        active = self.make_project(
            title="Current project",
            status=Project.Status.CONSTRUCTION,
        )
        warranty = WarrantyItem.objects.create(
            project=archived,
            title="Repair cabinet hinge",
            description="Replace and adjust the hinge.",
            assigned_to=self.employee,
            status=WarrantyItem.Status.OPEN,
        )
        unassigned = WarrantyItem.objects.create(
            project=archived,
            title="Unassigned warranty work",
            status=WarrantyItem.Status.OPEN,
        )
        browser = DjangoClient()
        browser.force_login(self.employee)

        projects_page = browser.get(reverse("operations:team-section", kwargs={"section": "projects"}))
        self.assertContains(projects_page, active.title)
        self.assertNotContains(projects_page, archived.title)
        tasks_page = browser.get(reverse("operations:team-section", kwargs={"section": "tasks"}))
        self.assertContains(tasks_page, warranty.title)
        self.assertNotContains(tasks_page, unassigned.title)
        self.assertNotIn(archived.pk, [item.pk for item in tasks_page.context["projects"]])

        response = browser.post(
            reverse("operations:project-warranty-resolution", kwargs={"pk": warranty.pk}),
            {
                "status": WarrantyItem.Status.RESOLVED,
                "resolution": "Replaced and tested the hinge.",
            },
        )
        self.assertEqual(response.status_code, 302)
        warranty.refresh_from_db()
        self.assertEqual(warranty.status, WarrantyItem.Status.RESOLVED)
        self.assertTrue(self.employee.is_active)

    def test_export_contains_project_graph_files_but_omits_general_client_messages(self):
        project = self.make_project(title="Export records")
        linked_message = ClientMessage.objects.create(
            client=self.client_record,
            project=project,
            body="Only this project conversation belongs in the export.",
            sent_by=self.owner,
        )
        general_message = ClientMessage.objects.create(
            client=self.client_record,
            body="Keep general client conversation out.",
            sent_by=self.client_user,
        )
        activity = Activity.objects.create(
            message="Project history",
            detail="Closeout audit",
            project=project,
            actor=self.owner,
        )
        workflow = WorkflowEvent.objects.create(
            event_type="archive-export-test",
            project=project,
            actor=self.owner,
        )
        document = ProjectDocument.objects.create(
            project=project,
            title="Warranty guide",
            category="Manual",
            file=ContentFile(b"local original file", name="warranty-guide.pdf"),
        )
        estimate = Estimate.objects.create(
            number=77801,
            client=self.client_record,
            title="Stored estimate record",
        )
        EstimateLineItem.objects.create(
            estimate=estimate,
            description="Local estimate line item",
            quantity=1,
            unit_price="125.00",
        )
        project.estimate = estimate
        project.save(update_fields=["estimate", "updated_at"])

        output = project_export_file(project)
        with ZipFile(output) as archive:
            records = json.loads(archive.read("records.json"))
            manifest = json.loads(archive.read("manifest.json"))
            serialized = json.dumps(records)
            self.assertIn(str(linked_message.pk), serialized)
            self.assertIn(str(activity.pk), serialized)
            self.assertIn(str(workflow.pk), serialized)
            self.assertIn(str(document.pk), serialized)
            self.assertIn(str(estimate.pk), serialized)
            self.assertIn("Local estimate line item", serialized)
            self.assertNotIn(str(general_message.pk), serialized)
            self.assertNotIn("provider-document.pdf", archive.namelist())
            self.assertIn(document.file.name, serialized)
            self.assertTrue(any(name.startswith("files/") for name in archive.namelist()))
            self.assertEqual(manifest["project_id"], str(project.pk))
            self.assertEqual(manifest["file_count"], 1)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())
        self.assertTrue(ClientMessage.objects.filter(pk=general_message.pk).exists())

    def test_delete_removes_linked_records_but_keeps_general_client_data_and_account(self):
        project = self.make_project(title="Delete linked records")
        active_project = self.make_project(
            title="Keep active project",
            status=Project.Status.CONSTRUCTION,
        )
        linked_message = ClientMessage.objects.create(
            client=self.client_record,
            project=project,
            body="Project-linked message",
            sent_by=self.owner,
        )
        general_message = ClientMessage.objects.create(
            client=self.client_record,
            body="General conversation",
            sent_by=self.client_user,
        )
        employee_notification = EmployeeNotification.objects.create(
            employee=self.employee,
            kind="project-test",
            title="Project notification",
            body="This belongs to the project.",
            project=project,
        )
        client_notification = ClientNotification.objects.create(
            client=self.client_record,
            kind="project-test",
            title="Client project notification",
            body="This belongs to the project.",
            project=project,
        )
        Task.objects.create(title="Project task", project=project, client=self.client_record)
        project_file = ProjectDocument.objects.create(
            project=project,
            title="Project file",
            file=ContentFile(b"file bytes", name="project-delete.pdf"),
        )
        file_name = project_file.file.name
        file_storage = project_file.file.storage

        with self.captureOnCommitCallbacks(execute=True):
            permanently_delete_project(project)

        self.assertFalse(Project.objects.filter(pk=project.pk).exists())
        self.assertTrue(Project.objects.filter(pk=active_project.pk).exists())
        self.assertFalse(ClientMessage.objects.filter(pk=linked_message.pk).exists())
        self.assertTrue(ClientMessage.objects.filter(pk=general_message.pk).exists())
        self.assertFalse(EmployeeNotification.objects.filter(pk=employee_notification.pk).exists())
        self.assertFalse(ClientNotification.objects.filter(pk=client_notification.pk).exists())
        self.assertFalse(ProjectDocument.objects.filter(pk=project_file.pk).exists())
        self.assertFalse(file_storage.exists(file_name))
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.is_active)
        self.assertTrue(Client.objects.filter(pk=self.client_record.pk).exists())

    def test_shared_file_is_retained_when_an_unrelated_record_still_references_it(self):
        project = self.make_project(title="Shared asset source")
        other_project = self.make_project(
            title="Another active project",
            status=Project.Status.CONSTRUCTION,
        )
        first = ProjectDocument.objects.create(
            project=project,
            title="Shared source",
            file=ContentFile(b"shared bytes", name="shared.pdf"),
        )
        second = ProjectDocument.objects.create(
            project=other_project,
            title="Shared reference",
        )
        second.file.name = first.file.name
        second.save(update_fields=["file"])
        storage = first.file.storage
        name = first.file.name

        with self.captureOnCommitCallbacks(execute=True):
            permanently_delete_project(project)

        self.assertTrue(storage.exists(name))
        self.assertTrue(ProjectDocument.objects.filter(pk=second.pk).exists())

    def test_estimate_history_is_kept_by_default_and_can_be_deleted_when_selected(self):
        estimate = Estimate.objects.create(
            number=77701,
            client=self.client_record,
            title="Linked estimate history",
            status=Estimate.Status.ACCEPTED,
        )
        line = EstimateLineItem.objects.create(
            estimate=estimate,
            description="Cabinets",
            quantity=1,
            unit_price="100.00",
        )
        estimate.locked_at = timezone.now()
        estimate.save(update_fields=["locked_at", "updated_at"])
        keep_project = self.make_project(title="Keep estimate reference", estimate=estimate)
        permanently_delete_project(keep_project)
        self.assertTrue(Estimate.objects.filter(pk=estimate.pk).exists())
        self.assertTrue(EstimateLineItem.objects.filter(pk=line.pk).exists())

        second_estimate = Estimate.objects.create(
            number=77702,
            client=self.client_record,
            title="Delete selected estimate history",
            status=Estimate.Status.ACCEPTED,
        )
        second_line = EstimateLineItem.objects.create(
            estimate=second_estimate,
            description="Tile",
            quantity=1,
            unit_price="80.00",
        )
        second_estimate.locked_at = timezone.now()
        second_estimate.save(update_fields=["locked_at", "updated_at"])
        delete_project = self.make_project(
            title="Delete estimate with project",
            estimate=second_estimate,
        )
        self.assertFalse(estimate_history_delete_blocked(delete_project, second_estimate))
        permanently_delete_project(delete_project, delete_estimate_history=True)
        self.assertFalse(Estimate.objects.filter(pk=second_estimate.pk).exists())
        self.assertFalse(EstimateLineItem.objects.filter(pk=second_line.pk).exists())

    def test_estimate_history_used_by_another_project_is_protected(self):
        estimate = Estimate.objects.create(
            number=77703,
            client=self.client_record,
            title="Shared estimate history",
        )
        target = self.make_project(title="Archive that uses estimate", estimate=estimate)
        self.make_project(
            title="Another project sharing estimate",
            status=Project.Status.CONSTRUCTION,
            estimate=estimate,
        )

        self.assertTrue(estimate_history_delete_blocked(target, estimate))
        with self.assertRaises(ValueError):
            permanently_delete_project(target, delete_estimate_history=True)
        self.assertTrue(Project.objects.filter(pk=target.pk).exists())
        self.assertTrue(Estimate.objects.filter(pk=estimate.pk).exists())

    def test_archive_ui_requires_owner_gate_and_confirms_deletion(self):
        project = self.make_project(title="Confirm archive deletion")
        browser = DjangoClient()
        force_login_with_admin_gate(browser, self.owner)

        archive_response = browser.get(reverse("operations:project-archive"))
        self.assertEqual(archive_response.status_code, 200)
        self.assertContains(archive_response, project.title)
        self.assertContains(archive_response, "Export project ZIP")
        confirm_url = reverse("operations:project-archive-delete", kwargs={"pk": project.pk})
        confirm_response = browser.get(confirm_url)
        self.assertContains(confirm_response, "Data included in deletion")
        self.assertContains(confirm_response, "client account and other projects stay active")

        invalid_post = browser.post(confirm_url, {"confirm_delete": ""})
        self.assertEqual(invalid_post.status_code, 400)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

        delete_response = browser.post(confirm_url, {"confirm_delete": "yes"})
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_archive_includes_portfolio_projects_and_keeps_published_gallery_live_until_delete(self):
        project = self.make_project(title="Published archive gallery")
        project.is_portfolio_only = True
        project.save(update_fields=["is_portfolio_only", "updated_at"])
        post = WebsiteGalleryPost.objects.create(
            project=project,
            title="Published room gallery",
            published_data={"title": "Published room gallery", "slides": []},
            published_at=timezone.now(),
        )
        browser = DjangoClient()
        force_login_with_admin_gate(browser, self.owner)

        archive_response = browser.get(reverse("operations:project-archive"))
        self.assertContains(archive_response, project.title)
        public_response = browser.get(reverse("operations:project-detail", kwargs={"pk": project.pk}))
        self.assertEqual(public_response.status_code, 302)

        with self.captureOnCommitCallbacks(execute=True):
            permanently_delete_project(project)

        self.assertFalse(WebsiteGalleryPost.objects.filter(pk=post.pk).exists())
        self.assertEqual(
            browser.get(reverse("operations:project-detail", kwargs={"pk": project.pk})).status_code,
            404,
        )

    def test_archive_endpoints_are_not_available_to_an_employee(self):
        project = self.make_project(title="Restricted archive")
        browser = DjangoClient()
        browser.force_login(self.employee)

        self.assertEqual(browser.get(reverse("operations:project-archive")).status_code, 403)
        self.assertEqual(
            browser.get(reverse("operations:project-archive-export", kwargs={"pk": project.pk})).status_code,
            403,
        )
