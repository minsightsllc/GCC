from __future__ import annotations

import re
from html import unescape

import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import EstimateForm, ProjectForm
from .models import (
    Client as ClientRecord,
    ClientMessage,
    ClientNotification,
    EmployeeNotification,
    EmployeeProfile,
    Estimate,
    Lead,
    MediaAsset,
    Project,
    ProjectDocument,
    ProjectUpdate,
    Task,
)
from .services import ensure_role_groups
from .views import _notify_task_participants
from django.core.files.uploadedfile import SimpleUploadedFile
from .construction_services import record_external_estimate_status


TEST_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="gcc-workflow-contract-"))


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class WorkflowContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(
            username="contract-owner",
            password="owner-pass-123",
            first_name="Contract",
            last_name="Owner",
            email="contract-owner@gcc.example.com",
            is_staff=True,
            is_superuser=True,
        )
        cls.manager = user_model.objects.create_user(
            username="contract-manager",
            password="manager-pass-123",
            first_name="Contract",
            last_name="Manager",
            email="contract-manager@gcc.example.com",
            is_staff=True,
        )
        cls.office = user_model.objects.create_user(
            username="contract-office",
            password="office-pass-123",
            first_name="Contract",
            last_name="Office",
            email="contract-office@gcc.example.com",
            is_staff=True,
        )
        cls.field = user_model.objects.create_user(
            username="contract-field",
            password="field-pass-123",
            first_name="Contract",
            last_name="Field",
            email="contract-field@gcc.example.com",
            is_staff=True,
        )
        roles = ensure_role_groups()
        cls.owner.groups.add(roles["Owner"])
        cls.manager.groups.add(roles["Manager"])
        cls.office.groups.add(roles["Office"])
        cls.field.groups.add(roles["Field"])
        for user, title in (
            (cls.owner, "Owner"),
            (cls.manager, "Manager"),
            (cls.office, "Office"),
            (cls.field, "Field"),
        ):
            EmployeeProfile.objects.create(user=user, job_title=title)

        cls.client_user = user_model.objects.create_user(
            username="contract-client",
            password="client-pass-123",
            first_name="Contract",
            last_name="Client",
            email="contract-client@example.com",
        )
        cls.other_client_user = user_model.objects.create_user(
            username="contract-other-client",
            password="client-pass-123",
            first_name="Other",
            last_name="Client",
            email="other-contract-client@example.com",
        )
        cls.client_record = ClientRecord.objects.create(
            name="Contract Client",
            email=cls.client_user.email,
            user=cls.client_user,
        )
        cls.other_client_record = ClientRecord.objects.create(
            name="Other Client",
            email=cls.other_client_user.email,
            user=cls.other_client_user,
        )
        cls.lead = Lead.objects.create(
            client=cls.client_record,
            name="Contract Client",
            email=cls.client_user.email,
            service="Renovation",
            location="Ventura, CA",
            assigned_to=cls.manager,
            created_by=cls.owner,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.http = DjangoClient()
        self.next_number = 12000

    def login_as(self, user):
        self.http.logout()
        self.http.force_login(user)
        if user.is_superuser:
            self.http.post(reverse("admin:access"), {"identifier": user.username})

    def make_estimate(self, *, status=Estimate.Status.SENT):
        self.next_number += 1
        external_status = {
            Estimate.Status.DRAFT: Estimate.ExternalEstimateStatus.NOT_STARTED,
            Estimate.Status.SENT: Estimate.ExternalEstimateStatus.SENT,
            Estimate.Status.ACCEPTED: Estimate.ExternalEstimateStatus.APPROVED,
            Estimate.Status.DECLINED: Estimate.ExternalEstimateStatus.REJECTED,
        }[status]
        timestamp = timezone.now() if status != Estimate.Status.DRAFT else None
        estimate = Estimate.objects.create(
            number=self.next_number,
            lead=self.lead,
            client=self.client_record,
            title="Contract scope",
            status=status,
            deposit_amount=Decimal("100.00"),
            created_by=self.owner,
            external_url=f"https://estimates.example.com/{self.next_number}",
            external_status=external_status,
            external_status_at=timestamp,
            external_status_by=self.owner if timestamp else None,
            external_client_visible=status != Estimate.Status.DRAFT,
            sent_at=timestamp if status == Estimate.Status.SENT else None,
            accepted_at=timestamp if status == Estimate.Status.ACCEPTED else None,
            declined_at=timestamp if status == Estimate.Status.DECLINED else None,
        )
        return estimate

    def make_project(self, *, estimate=None, assigned=None, client=None):
        project = Project.objects.create(
            estimate=estimate,
            lead=self.lead if client is None or client == self.client_record else None,
            client=client or self.client_record,
            title="Contract project",
            location="Ventura, CA",
            status=Project.Status.PLANNING,
            next_step="Assign project staff",
            fallback_image="operations/images/project-adu.png",
            created_by=self.owner,
        )
        if assigned:
            project.assigned_staff.add(*assigned)
        return project

    @staticmethod
    def project_payload(project, *, status=None, staff=None):
        return {
            "title": project.title,
            "client": str(project.client_id or ""),
            "lead": str(project.lead_id or ""),
            "assigned_staff": [str(user.pk) for user in (staff or [])],
            "location": project.location,
            "project_type": project.project_type,
            "status": status or project.status,
            "next_step": project.next_step,
            "summary": project.summary,
            "is_published": "on" if project.is_published else "",
            "start_date": project.start_date.isoformat() if project.start_date else "",
            "target_date": project.target_date.isoformat() if project.target_date else "",
        }

    @staticmethod
    def sidebar_markup(response):
        markup = response.content.decode()
        start = markup.index('<nav class="admin-nav grouped-admin-nav"')
        return markup[start : markup.index("</nav>", start)]

    @classmethod
    def sidebar_structure(cls, response):
        sidebar = cls.sidebar_markup(response)
        groups = tuple(
            unescape(re.sub(r"<[^>]+>", "", value)).strip()
            for value in re.findall(r'<h2 class="sidebar-group-label"[^>]*>(.*?)</h2>', sidebar, re.DOTALL)
        )
        links = []
        for href, body in re.findall(r'<a class="[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', sidebar, re.DOTALL):
            label = re.search(r'<span class="nav-icon">.*?</span>\s*<span>([^<]+)</span>', body, re.DOTALL)
            if label:
                links.append((href, unescape(label.group(1)).strip()))
        return groups, tuple(links)

    def test_sidebar_structure_is_stable_across_every_workspace_route(self):
        self.login_as(self.owner)
        admin_sections = (
            "overview", "leads", "clients", "estimates", "projects", "tasks",
            "calendar", "time", "documents", "media", "team", "notifications", "content",
        )
        expected = None
        for section in admin_sections:
            url = reverse("operations:dashboard") if section == "overview" else reverse(
                "operations:dashboard-section", kwargs={"section": section}
            )
            response = self.http.get(url)
            self.assertEqual(response.status_code, 200, section)
            structure = self.sidebar_structure(response)
            expected = structure if expected is None else expected
            self.assertEqual(structure, expected, section)
        self.assertEqual(
            self.sidebar_structure(self.http.get(
                reverse("operations:dashboard-section", kwargs={"section": "clients"}) + "?messages=1"
            )),
            expected,
        )

        self.login_as(self.office)
        employee_sections = ("overview", "projects", "tasks", "calendar", "time", "media", "notifications", "profile")
        expected = None
        for section in employee_sections:
            url = reverse("operations:team") if section == "overview" else reverse(
                "operations:team-section", kwargs={"section": section}
            )
            response = self.http.get(url)
            self.assertEqual(response.status_code, 200, section)
            structure = self.sidebar_structure(response)
            expected = structure if expected is None else expected
            self.assertEqual(structure, expected, section)
    def test_sidebar_order_is_canonical_and_mobile_matches_it(self):
        self.login_as(self.owner)
        owner_response = self.http.get(reverse("operations:dashboard"))
        owner_sidebar = self.sidebar_markup(owner_response)
        owner_tokens = (
            ">Overview<",
            "Client &amp; Job Operations",
            ">Leads<",
            ">Clients<",
            ">Estimates<",
            ">Projects<",
            ">Tasks<",
            ">Calendar<",
            ">Time<",
            ">Documents<",
            ">Media<",
            "Miscellaneous",
            ">Team<",
            ">Messages<",
            ">Notifications<",
            ">Content<",
        )
        self.assertEqual([owner_sidebar.index(token) for token in owner_tokens], sorted(owner_sidebar.index(token) for token in owner_tokens))

        self.login_as(self.office)
        employee_response = self.http.get(reverse("operations:team"))
        employee_sidebar = self.sidebar_markup(employee_response)
        employee_tokens = (
            ">Overview<",
            "Client &amp; Job Operations",
            ">Projects<",
            ">Tasks<",
            ">Calendar<",
            ">Time<",
            ">Media<",
            "Miscellaneous",
            ">Notifications<",
            ">Profile<",
        )
        self.assertEqual([employee_sidebar.index(token) for token in employee_tokens], sorted(employee_sidebar.index(token) for token in employee_tokens))
        self.assertNotIn(">Leads<", employee_sidebar)
        self.assertNotIn(">Clients<", employee_sidebar)
        self.assertNotIn(">Estimates<", employee_sidebar)
        self.assertNotIn(">Documents<", employee_sidebar)
        self.assertNotIn(">Team<", employee_sidebar)
        self.assertNotIn(">Messages<", employee_sidebar)
        mobile_source = (Path(__file__).resolve().parents[2] / "mobile" / "App.js").read_text(encoding="utf-8")
        employee_block = mobile_source[mobile_source.index("const employeeDrawerPages") : mobile_source.index("const adminDrawerPages")]
        admin_block = mobile_source[mobile_source.index("const adminDrawerPages") : mobile_source.index("const clientTabIcons")]
        employee_labels = ("Overview", "Projects", "Tasks", "Calendar", "Time", "Media", "Notifications", "Profile")
        admin_labels = ("Overview", "Leads", "Clients", "Estimates", "Projects", "Tasks", "Calendar", "Time", "Documents", "Media", "Team", "Messages", "Notifications", "Content")
        for block, labels in ((employee_block, employee_labels), (admin_block, admin_labels)):
            positions = [block.index(f"label: '{label}'") for label in labels]
            self.assertEqual(positions, sorted(positions))
        self.assertIn("path: '/dashboard/clients/?messages=1'", admin_block)
        self.assertIn("group: 'Client & Job Operations'", admin_block)
        self.assertIn("group: 'Miscellaneous'", admin_block)
        self.assertNotIn("Workspace overview", admin_block)
        self.assertNotIn("Command center", admin_block)
        self.assertNotIn("Today's work", employee_block)
        client_block = mobile_source[mobile_source.index("const clientDrawerPages") : mobile_source.index("const employeeDrawerPages")]
        for route in (
            "/portal/overview/",
            "/portal/updates/",
            "/portal/photos/",
            "/portal/messages/",
            "/portal/estimate-files/",
            "/portal/notifications/",
        ):
            self.assertIn(f"path: '{route}'", client_block)
        self.assertIn("path: CLIENT_PROFILE_PATH", client_block)
        self.assertNotIn("/portal/#", client_block)
        client_tabs = mobile_source[
            mobile_source.index("const clientBottomTabs") : mobile_source.index("const employeeMorePages")
        ]
        for tab_name in ("name: 'Overview'", "name: 'Notifications'", "name: 'More'"):
            self.assertIn(tab_name, client_tabs)
        self.assertIn("CLIENT_OVERVIEW_PATH", client_tabs)
        self.assertIn("CLIENT_NOTIFICATIONS_PATH", client_tabs)
        self.assertIn("function ClientMoreScreen", mobile_source)
        self.assertIn("workspaceKind === 'employee' || workspaceKind === 'client' ? 'Open profile'", mobile_source)
        self.assertIn("key={`${page.group || 'overview'}:${page.label}`}", mobile_source)
        self.assertIn("if (numericCount === 0)", mobile_source)
        self.assertIn(".admin-sidebar, .staging-bar", mobile_source)
        self.assertIn(".admin-main { margin-left: 0 !important; }", mobile_source)
        self.assertIn("const CLIENT_PUSH_REGISTER_PATH = '/portal/notifications/devices/'", mobile_source)
        self.assertIn("const CLIENT_PUSH_DEACTIVATE_PATH = '/portal/notifications/devices/deactivate/'", mobile_source)
        self.assertIn("fetch(registerPath", mobile_source)
        self.assertIn("isPushWorkspace", mobile_source)
        self.assertIn("requestedPath.startsWith('/portal/')", mobile_source)

    def test_external_estimate_status_is_staff_confirmed_and_project_setup_is_explicit(self):
        estimate = self.make_estimate()
        owner_form = EstimateForm(
            data={"title": estimate.title, "status": Estimate.Status.ACCEPTED, "deposit_amount": "100", "notes": ""},
            instance=estimate,
        )
        self.assertFalse(owner_form.is_valid())
        self.assertNotIn(Estimate.Status.ACCEPTED, dict(owner_form.fields["status"].choices))
        self.login_as(self.owner)
        self.assertEqual(
            self.http.post(reverse("operations:portal-accept-estimate", kwargs={"pk": estimate.pk})).status_code,
            403,
        )
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, Estimate.Status.SENT)

        self.login_as(self.client_user)
        self.assertEqual(
            self.http.post(reverse("operations:portal-accept-estimate", kwargs={"pk": estimate.pk})).status_code,
            302,
        )
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, Estimate.Status.SENT)
        self.assertIsNone(estimate.accepted_by_id)
        self.assertFalse(ClientNotification.objects.filter(kind="estimate-accepted", estimate=estimate).exists())
        self.assertFalse(EmployeeNotification.objects.filter(kind="estimate-accepted", estimate=estimate).exists())

        owner_peer = get_user_model().objects.create_user(
            username="contract-owner-peer",
            password="owner-pass-123",
            email="contract-owner-peer@gcc.example.com",
            is_staff=True,
            is_superuser=True,
        )
        self.login_as(self.owner)
        estimate, changed = record_external_estimate_status(
            estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.APPROVED,
            external_client_visible=True,
            idempotency_key="contract-confirm-external-approval",
        )
        self.assertTrue(changed)
        self.assertEqual(estimate.status, Estimate.Status.ACCEPTED)
        self.assertIsNone(estimate.accepted_by_id)
        self.assertEqual(estimate.external_status_by_id, self.owner.pk)
        self.assertTrue(ClientNotification.objects.filter(kind="estimate-accepted", estimate=estimate).exists())
        owner_notice = EmployeeNotification.objects.get(
            kind="estimate-accepted",
            estimate=estimate,
            employee=owner_peer,
        )
        self.assertEqual(
            owner_notice.destination_url,
            reverse("operations:dashboard-section", kwargs={"section": "estimates"})
            + f"?estimate={estimate.pk}",
        )
        self.assertFalse(EmployeeNotification.objects.filter(kind="estimate-accepted", estimate=estimate, employee=self.manager).exists())
        self.assertFalse(EmployeeNotification.objects.filter(kind="estimate-accepted", estimate=estimate, employee=self.owner).exists())

        response = self.http.post(reverse("operations:estimate-project", kwargs={"pk": estimate.pk}))
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(estimate=estimate)
        self.assertEqual(project.next_step, "Assign project staff")
        self.assertTrue(project.needs_staff_assignment)
        project_notice = ClientNotification.objects.get(kind="project-created", project=project)
        self.assertIn("has not started yet", project_notice.body)
        invalid_execution_form = ProjectForm(
            data=self.project_payload(project, status=Project.Status.CONSTRUCTION, staff=[]),
            instance=project,
        )
        self.assertFalse(invalid_execution_form.is_valid())
        self.assertIn("execution", invalid_execution_form.errors["assigned_staff"][0])

        response = self.http.post(
            reverse("operations:project-update", kwargs={"pk": project.pk}),
            self.project_payload(project, staff=[self.manager]),
        )
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertFalse(project.needs_staff_assignment)
        self.assertTrue(EmployeeNotification.objects.filter(kind="project-assignment", project=project, employee=self.manager).exists())
        self.assertTrue(ClientNotification.objects.filter(kind="project-staff-assigned", project=project).exists())

    def test_external_estimate_decline_notifies_owners_but_not_lead_employee(self):
        estimate = self.make_estimate()
        owner_peer = get_user_model().objects.create_user(
            username="contract-decline-owner-peer",
            password="owner-pass-123",
            email="contract-decline-owner-peer@gcc.example.com",
            is_staff=True,
            is_superuser=True,
        )

        estimate, changed = record_external_estimate_status(
            estimate,
            actor=self.owner,
            status=Estimate.ExternalEstimateStatus.REJECTED,
            idempotency_key="contract-confirm-external-decline",
        )

        self.assertTrue(changed)
        owner_notice = EmployeeNotification.objects.get(
            kind="estimate-declined",
            estimate=estimate,
            employee=owner_peer,
        )
        self.assertEqual(
            owner_notice.destination_url,
            reverse("operations:dashboard-section", kwargs={"section": "estimates"})
            + f"?estimate={estimate.pk}",
        )
        self.assertFalse(EmployeeNotification.objects.filter(kind="estimate-declined", estimate=estimate, employee=self.manager).exists())
        self.assertFalse(EmployeeNotification.objects.filter(kind="estimate-declined", estimate=estimate, employee=self.owner).exists())

    def test_project_and_task_notifications_use_recipient_workspace_links(self):
        owner_peer = get_user_model().objects.create_user(
            username="contract-routing-owner-peer",
            password="owner-pass-123",
            email="contract-routing-owner-peer@gcc.example.com",
            is_staff=True,
            is_superuser=True,
        )
        project = self.make_project(
            estimate=self.make_estimate(status=Estimate.Status.ACCEPTED),
            assigned=[self.manager],
        )
        self.login_as(self.owner)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-update", kwargs={"pk": project.pk}),
                self.project_payload(project, staff=[self.manager, owner_peer]),
            ).status_code,
            302,
        )

        owner_project_notice = EmployeeNotification.objects.get(
            kind="project-assignment",
            project=project,
            employee=owner_peer,
        )
        employee_project_notice = EmployeeNotification.objects.get(
            kind="project-assignment",
            project=project,
            employee=self.manager,
        )
        owner_project_url = (
            reverse("operations:dashboard-section", kwargs={"section": "projects"})
            + f"?project={project.pk}"
        )
        employee_project_url = (
            reverse("operations:team-section", kwargs={"section": "projects"})
            + f"?project={project.pk}"
        )
        self.assertEqual(owner_project_notice.destination_url, owner_project_url)
        self.assertEqual(employee_project_notice.destination_url, employee_project_url)
        self.assertEqual(
            EmployeeNotification.objects.filter(
                kind="project-assignment",
                project=project,
                employee=owner_peer,
            ).count(),
            1,
        )

        task = Task.objects.create(
            title="Route-specific task",
            project=project,
            lead=self.lead,
            created_by=self.owner,
        )
        task.assigned_to.add(self.manager)
        _notify_task_participants(
            task,
            kind="task-assignment",
            title="New task assigned",
            body=f"{task.title} was added to your work queue.",
            created_by=self.owner,
        )
        owner_task_notice = EmployeeNotification.objects.get(
            kind="task-assignment",
            task=task,
            employee=owner_peer,
        )
        employee_task_notice = EmployeeNotification.objects.get(
            kind="task-assignment",
            task=task,
            employee=self.manager,
        )
        owner_task_url = (
            reverse("operations:dashboard-section", kwargs={"section": "tasks"})
            + f"?task={task.pk}"
        )
        employee_task_url = (
            reverse("operations:team-section", kwargs={"section": "tasks"})
            + f"?task={task.pk}"
        )
        self.assertEqual(owner_task_notice.destination_url, owner_task_url)
        self.assertEqual(employee_task_notice.destination_url, employee_task_url)
        self.assertEqual(
            EmployeeNotification.objects.filter(
                kind="task-assignment",
                task=task,
                employee=owner_peer,
            ).count(),
            1,
        )

        self.login_as(owner_peer)
        owner_inbox = self.http.get(
            reverse("operations:dashboard-section", kwargs={"section": "notifications"})
        )
        self.assertContains(owner_inbox, f'href="{owner_task_url}"')
        self.assertEqual(self.http.get(owner_task_url).status_code, 200)
        self.assertNotContains(owner_inbox, employee_task_url)

        self.login_as(self.manager)
        employee_inbox = self.http.get(
            reverse("operations:team-section", kwargs={"section": "notifications"})
        )
        self.assertContains(employee_inbox, f'href="{employee_task_url}"')
        self.assertEqual(self.http.get(employee_task_url).status_code, 200)
        self.assertNotContains(employee_inbox, owner_task_url)

    def test_required_publication_alerts_and_read_states_are_durable(self):
        estimate = self.make_estimate(status=Estimate.Status.ACCEPTED)
        project = self.make_project(estimate=estimate, assigned=[self.manager])
        self.login_as(self.owner)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-add-update", kwargs={"pk": project.pk}),
                {"title": "Progress update", "body": "The site is ready for the next phase.", "visibility": "client"},
            ).status_code,
            302,
        )
        self.assertTrue(ClientNotification.objects.filter(kind="update-published", project=project).exists())

        media_file = SimpleUploadedFile("progress.jpg", b"not-empty", content_type="image/jpeg")
        self.assertEqual(
            self.http.post(
                reverse("operations:media-upload"),
                {"project": project.pk, "visibility": MediaAsset.Visibility.CLIENT, "files": media_file},
            ).status_code,
            302,
        )
        self.assertTrue(ClientNotification.objects.filter(kind="media-published", project=project).exists())

        document_file = SimpleUploadedFile("scope.pdf", b"%PDF-1.4 contract", content_type="application/pdf")
        self.assertEqual(
            self.http.post(
                reverse("operations:document-upload"),
                {"project": project.pk, "title": "Scope PDF", "category": "Scope", "description": "", "visibility": ProjectDocument.Visibility.CLIENT, "file": document_file},
            ).status_code,
            302,
        )
        self.assertTrue(ClientNotification.objects.filter(kind="document-published", project=project).exists())

        task = Task.objects.create(
            title="Complete field work",
            project=project,
            lead=self.lead,
            created_by=self.owner,
        )
        task.assigned_to.add(self.manager)
        self.assertEqual(
            self.http.post(reverse("operations:task-status", kwargs={"pk": task.pk}), {"status": Task.Status.IN_PROGRESS}).status_code,
            302,
        )
        self.assertTrue(EmployeeNotification.objects.filter(kind="task-status", task=task, employee=self.manager).exists())

        client_alert = ClientNotification.objects.filter(project=project).order_by("-created_at").first()
        self.login_as(self.client_user)
        portal_response = self.http.get(
            reverse("operations:portal-section", kwargs={"section": "notifications"}),
        )
        self.assertContains(portal_response, "Notifications")
        self.assertContains(portal_response, client_alert.title)
        mark_read_response = self.http.post(
            reverse("operations:client-notification-mark-read", kwargs={"pk": client_alert.pk}),
        )
        self.assertEqual(mark_read_response.status_code, 302)
        self.assertEqual(mark_read_response.url, reverse("operations:portal-section", kwargs={"section": "notifications"}))
        client_alert.refresh_from_db()
        self.assertIsNotNone(client_alert.read_at)
        mark_all_read_response = self.http.post(reverse("operations:client-notifications-mark-all-read"))
        self.assertEqual(mark_all_read_response.status_code, 302)
        self.assertEqual(mark_all_read_response.url, reverse("operations:portal-section", kwargs={"section": "notifications"}))

        self.login_as(self.manager)
        employee_alert = EmployeeNotification.objects.filter(employee=self.manager, task=task).order_by("-created_at").first()
        self.assertContains(self.http.get(reverse("operations:team-section", kwargs={"section": "notifications"})), employee_alert.title)

    def test_document_library_upload_is_owner_only(self):
        project = self.make_project()
        upload_url = reverse("operations:document-upload")

        for user in (self.manager, self.office, self.field, self.client_user):
            with self.subTest(user=user.username):
                self.login_as(user)
                response = self.http.post(
                    upload_url,
                    {
                        "project": str(project.pk),
                        "title": "Restricted document",
                        "category": "Scope",
                        "description": "",
                        "visibility": ProjectDocument.Visibility.CLIENT,
                        "file": SimpleUploadedFile(
                            "restricted.pdf",
                            b"%PDF-1.4 document",
                            content_type="application/pdf",
                        ),
                    },
                )
                self.assertEqual(response.status_code, 403)

        self.assertFalse(ProjectDocument.objects.filter(title="Restricted document").exists())

    def test_client_can_clear_own_notification_without_deleting_estimate(self):
        estimate = self.make_estimate(status=Estimate.Status.SENT)
        notification = ClientNotification.objects.create(
            client=self.client_record,
            estimate=estimate,
            kind="estimate-sent",
            title="Estimate ready to review",
            body="Grand Coast shared an estimate link.",
        )
        other_notification = ClientNotification.objects.create(
            client=self.other_client_record,
            kind="estimate-sent",
            title="Another client estimate",
            body="This notification belongs to another client.",
        )

        self.login_as(self.client_user)
        inbox_url = reverse("operations:portal-section", kwargs={"section": "notifications"})
        response = self.http.get(inbox_url)
        self.assertContains(response, notification.title)
        self.assertContains(response, f'aria-label="Clear notification: {notification.title}"')

        # A client cannot dismiss another client's notification by guessing its ID.
        cross_client_response = self.http.post(
            reverse("operations:client-notification-clear", kwargs={"pk": other_notification.pk}),
        )
        self.assertEqual(cross_client_response.status_code, 404)
        other_notification.refresh_from_db()
        self.assertIsNone(other_notification.dismissed_at)

        clear_response = self.http.post(
            reverse("operations:client-notification-clear", kwargs={"pk": notification.pk}),
        )
        self.assertEqual(clear_response.status_code, 302)
        self.assertEqual(clear_response.url, inbox_url)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.dismissed_at)
        self.assertIsNotNone(notification.read_at)
        self.assertTrue(Estimate.objects.filter(pk=estimate.pk).exists())

        inbox_response = self.http.get(inbox_url)
        self.assertNotContains(inbox_response, notification.title)
        self.assertContains(inbox_response, "0 unread")

        self.login_as(self.other_client_user)
        self.assertContains(self.http.get(inbox_url), other_notification.title)

    def test_client_messages_are_project_scoped_for_employees_and_owner_visible_globally(self):
        estimate = self.make_estimate(status=Estimate.Status.ACCEPTED)
        project = self.make_project(estimate=estimate, assigned=[self.manager])
        other_project = self.make_project(client=self.other_client_record)
        other_project.title = "Other contract project"
        other_project.save(update_fields=["title", "updated_at"])
        self.login_as(self.client_user)
        message_body = "Please confirm the cabinet delivery window."
        self.assertEqual(
            self.http.post(reverse("operations:portal-message"), {"project": project.pk, "body": message_body}).status_code,
            302,
        )
        client_message = ClientMessage.objects.get(body=message_body)
        self.assertTrue(EmployeeNotification.objects.filter(kind="client-message", message=client_message, employee=self.manager).exists())
        self.assertTrue(EmployeeNotification.objects.filter(kind="client-message", message=client_message, employee=self.owner).exists())

        self.login_as(self.office)
        self.assertNotContains(self.http.get(reverse("operations:team-section", kwargs={"section": "projects"})), message_body)
        self.assertEqual(
            self.http.post(
                reverse("operations:team-staff-message-reply", kwargs={"client_pk": self.client_record.pk}),
                {"project": project.pk, "body": "Unauthorized reply"},
            ).status_code,
            403,
        )

        self.login_as(self.manager)
        employee_project_response = self.http.get(f"{reverse('operations:team-section', kwargs={'section': 'projects'})}?project={project.pk}")
        self.assertContains(employee_project_response, message_body)
        self.assertEqual(
            self.http.post(
                reverse("operations:team-staff-message-reply", kwargs={"client_pk": self.client_record.pk}),
                {"project": project.pk, "body": "Delivery is planned for Friday."},
            ).status_code,
            302,
        )
        self.assertTrue(ClientNotification.objects.filter(kind="employee-reply", project=project).exists())

        self.login_as(self.owner)
        owner_client_response = self.http.get(f"{reverse('operations:dashboard-section', kwargs={'section': 'clients'})}?client={self.client_record.pk}")
        self.assertContains(owner_client_response, message_body)
        self.assertContains(owner_client_response, "Delivery is planned for Friday.")

        self.login_as(self.other_client_user)
        other_portal_response = self.http.get(reverse("operations:portal"))
        self.assertNotContains(other_portal_response, message_body)
        self.assertNotContains(other_portal_response, project.title)
        self.assertFalse(ClientNotification.objects.filter(client=self.other_client_record, project=project).exists())

        self.login_as(self.owner)
        self.assertEqual(
            self.http.post(
                reverse("operations:project-update", kwargs={"pk": project.pk}),
                self.project_payload(project, status=Project.Status.COMPLETE, staff=[self.manager]),
            ).status_code,
            302,
        )
        self.assertTrue(ClientNotification.objects.filter(kind="project-complete", project=project).exists())
