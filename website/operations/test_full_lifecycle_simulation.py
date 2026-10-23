import json
import tempfile

from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    Client,
    ChangeOrder,
    Estimate,
    PaymentRecord,
    PreconstructionItem,
    Project,
    ProjectUpdate,
    WarrantyItem,
)
from .simulation import run_full_lifecycle


class FullLifecycleSimulationTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory(prefix="gcc-test-media-")
        self.settings_override = override_settings(
            GCC_OPERATING_SYSTEM_ENABLED=True,
            GCC_EXECUTION_LOOP_ENABLED=True,
            GCC_EXECUTION_LOOP_PROJECT_IDS="",
            GCC_EXECUTION_LOOP_USER_IDS="",
            GCC_AI_ENABLED=False,
            GCC_EMAIL_DELIVERY_ENABLED=False,
            EXPO_PUSH_ENABLED=False,
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            MEDIA_ROOT=self.media_dir.name,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.result = run_full_lifecycle()
        self.project = Project.objects.get(pk=self.result["pilot"]["project_id"])
        self.users = {
            role: self._user_by_id(user_id)
            for role, user_id in self.result["pilot"]["user_ids"].items()
        }
        self.http = HttpClient()

    def _user_by_id(self, user_id):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.get(pk=user_id)

    def _login(self, role):
        self.http.force_login(self.users[role])

    def test_complete_lifecycle_and_duplicate_contract(self):
        self.assertTrue(self.result["passed"])
        self.assertGreaterEqual(len(self.result["checks"]), 18)
        self.assertEqual(self.project.status, Project.Status.COMPLETE)
        self.assertEqual(self.project.operational_phase, Project.OperationalPhase.WARRANTY)
        self.assertEqual(Project.objects.filter(pk=self.project.pk).count(), 1)
        self.assertEqual(Estimate.objects.filter(pk=self.result["records"]["estimate_id"]).count(), 1)
        self.assertEqual(Client.objects.filter(pk=self.result["records"]["client_id"]).count(), 1)
        self.assertEqual(ChangeOrder.objects.filter(project=self.project).count(), 1)
        self.assertEqual(PaymentRecord.objects.filter(project=self.project).count(), 2)
        self.assertEqual(WarrantyItem.objects.filter(project=self.project).count(), 1)
        self.assertEqual(WarrantyItem.objects.get(project=self.project).status, WarrantyItem.Status.RESOLVED)

    def test_role_scoped_project_workspaces_and_financial_surfaces(self):
        project_url = reverse("operations:project-operations", kwargs={"pk": self.project.pk})
        summary_url = reverse("operations-api:project-summary", kwargs={"pk": self.project.pk})
        commitments_url = reverse("operations-api:project-commitments", kwargs={"pk": self.project.pk})
        document_url = reverse("operations:document-file", kwargs={"pk": self.result["records"]["document_id"]})

        self._login("owner")
        owner_project = self.http.get(project_url)
        self.assertEqual(owner_project.status_code, 302)
        self.assertIn("/dashboard/projects/?project=", owner_project["Location"])
        owner_record = self.http.get(owner_project["Location"])
        self.assertEqual(owner_record.status_code, 200)
        self.assertContains(owner_record, "Project record")
        owner_summary = self.http.get(summary_url)
        self.assertEqual(owner_summary.status_code, 200)
        self.assertIn("financials", json.loads(owner_summary.content)["project"])
        self.assertEqual(self.http.get(commitments_url).status_code, 200)
        owner_document = self.http.get(document_url)
        self.assertEqual(owner_document.status_code, 200)
        owner_document.close()

        self._login("manager")
        manager_project = self.http.get(project_url)
        self.assertEqual(manager_project.status_code, 302)
        self.assertIn("/projects/?project=", manager_project["Location"])
        self.assertEqual(self.http.get(manager_project["Location"]).status_code, 200)
        manager_summary = json.loads(self.http.get(summary_url).content)["project"]
        self.assertIn("financials", manager_summary)

        self._login("field")
        field_response = self.http.get(project_url)
        self.assertEqual(field_response.status_code, 302)
        self.assertIn("/team/projects/?project=", field_response["Location"])
        field_project = self.http.get(field_response["Location"])
        self.assertEqual(field_project.status_code, 200)
        self.assertNotIn(b"Framing and supervision actuals", field_project.content)
        field_summary = json.loads(self.http.get(summary_url).content)["project"]
        self.assertNotIn("financials", field_summary)
        self.assertEqual(self.http.get(commitments_url).status_code, 403)

        self._login("client")
        client_summary = json.loads(self.http.get(summary_url).content)["project"]
        self.assertNotIn("financials", client_summary)
        self.assertEqual(self.http.get(project_url).status_code, 403)
        client_document = self.http.get(document_url)
        self.assertEqual(client_document.status_code, 200)
        client_document.close()

        self._login("unauthorized")
        self.assertEqual(self.http.get(summary_url).status_code, 404)
        unauthorized_document = self.http.get(document_url)
        self.assertIn(unauthorized_document.status_code, {403, 404})
        unauthorized_document.close()

    def test_overview_legacy_review_calendar_and_client_portal(self):
        self._login("owner")
        dashboard = self.http.get(reverse("operations:dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Overview", dashboard.content)

        review = self.http.get(reverse("operations:weekly-review"))
        self.assertEqual(review.status_code, 302)
        self.assertIn("/dashboard/overview/", review["Location"])

        calendar = self.http.get(reverse("operations:dashboard-section", kwargs={"section": "calendar"}))
        self.assertEqual(calendar.status_code, 200)
        self.assertGreaterEqual(calendar.context["derived_calendar_event_count"], 4)
        self.assertTrue(calendar.context["calendar_conflicts"])

        self._login("client")
        portal = self.http.get(reverse("operations:portal"))
        self.assertEqual(portal.status_code, 200)
        self.assertIn(b"White oak flooring", portal.content)

    def test_api_mutation_parsing_and_native_upload_boundary(self):
        self._login("owner")
        milestone = self.project.milestones.get(sort_order=3)
        milestone_url = reverse("operations-api:milestone-status", kwargs={"pk": milestone.pk})
        response = self.http.post(
            milestone_url,
            data=json.dumps({"is_complete": "false"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="simulation-milestone-reopen",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(json.loads(response.content)["milestone"]["is_complete"])

        self._login("field")
        material_url = reverse("operations-api:material-request-status", kwargs={"pk": self.result["records"]["material_request_id"]})
        self.assertEqual(
            self.http.post(
                material_url,
                data=json.dumps({"status": "received"}),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="simulation-material-replay",
            ).status_code,
            403,
        )

    def test_project_record_actions_are_in_context_and_retry_safe(self):
        self._login("owner")
        readiness = PreconstructionItem.objects.filter(project=self.project).first()
        readiness_url = reverse("operations:readiness-update", kwargs={"pk": readiness.pk})
        readiness_data = {
            "owner": self.users["manager"].pk,
            "due_date": "2026-09-20",
            "notes": "Confirm the final construction schedule.",
            "idempotency_key": "simulation-readiness-detail-update",
        }
        self.assertEqual(self.http.post(readiness_url, data=readiness_data).status_code, 302)
        self.assertEqual(self.http.post(readiness_url, data=readiness_data).status_code, 302)
        readiness.refresh_from_db()
        self.assertEqual(readiness.owner_id, self.users["manager"].pk)
        self.assertEqual(readiness.notes, readiness_data["notes"])

        update_url = reverse("operations:project-add-update", kwargs={"pk": self.project.pk})
        update_data = {
            "title": "Pilot project update",
            "body": "The project remains on track after the final walkthrough.",
            "visibility": "internal",
            "idempotency_key": "simulation-project-update",
        }
        self.assertEqual(self.http.post(update_url, data=update_data).status_code, 302)
        self.assertEqual(self.http.post(update_url, data=update_data).status_code, 302)
        self.assertEqual(
            ProjectUpdate.objects.filter(project=self.project, title=update_data["title"]).count(),
            1,
        )

        self._login("field")
        field_hub = self.http.get(reverse("operations:project-operations", kwargs={"pk": self.project.pk}))
        self.assertEqual(field_hub.status_code, 302)
        field_page = self.http.get(field_hub["Location"])
        self.assertEqual(field_page.status_code, 200)
        self.assertNotContains(field_page, update_data["body"])

        self._login("owner")
        hub = self.http.get(reverse("operations:project-operations", kwargs={"pk": self.project.pk}))
        self.assertEqual(hub.status_code, 302)
        project_page = self.http.get(hub["Location"])
        self.assertEqual(project_page.status_code, 200)
        self.assertNotContains(project_page, "Readiness details")
        self.assertContains(project_page, "Pilot project update")

        calendar = self.http.get(
            reverse("operations:dashboard-section", kwargs={"section": "calendar"}),
            {"new": "event", "project": str(self.project.pk)},
        )
        self.assertEqual(calendar.status_code, 200)
        self.assertEqual(str(calendar.context["event_form"].initial["project"]), str(self.project.pk))
