import importlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client as Browser, TestCase, override_settings
from django.urls import reverse

from .construction_policies import can_view_media
from .forms import ProjectForm
from .models import Client, MediaAsset, MediaPublication, Project, Service, SiteSettings
from .publishing import client_is_live, media_presentation, publish, unpublish


@override_settings(GCC_TURNSTILE_ENABLED=False, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "contact_submissions": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
})
class PublishingWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.owner = users.objects.create_user("publisher", is_staff=True, is_superuser=True)
        cls.client_user = users.objects.create_user("client-publishing")
        cls.other_user = users.objects.create_user("other-publishing")
        cls.client_record = Client.objects.create(name="PRIVATE CLIENT", email="client@example.test", user=cls.client_user)
        cls.other_client = Client.objects.create(name="Other", user=cls.other_user)
        cls.project = Project.objects.create(
            title="PRIVATE PROJECT 123 Secret Street", location="123 Secret Street", summary="PRIVATE SUMMARY",
            public_title="Coastal renovation", public_location="Ventura County", public_summary="An open and bright living space.",
            is_published=True, client=cls.client_record,
        )
        cls.asset = MediaAsset.objects.create(project=cls.project, title="private-original", caption="private note", fallback_image="operations/images/project-bathroom.png")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="gcc-publishing-tests-")
        self.media_override = override_settings(MEDIA_ROOT=self.directory.name)
        self.media_override.enable()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.media_override.disable)
        self.browser = Browser()

    def login_owner(self):
        self.browser.force_login(self.owner)
        self.browser.post(reverse("admin:access"), {"identifier": self.owner.username})

    def publication(self, destination, title="Reviewed title", **kwargs):
        return MediaPublication.objects.create(asset=self.asset, destination=destination, title=title, **kwargs)

    def edit(self, destination="website", action="publish", **values):
        data = {"destination": destination, "action": action,
                f"media-{self.asset.pk}-title": "Reviewed title", f"media-{self.asset.pk}-caption": "Reviewed caption",
                f"media-{self.asset.pk}-sort_order": "0"}
        data.update({f"media-{self.asset.pk}-{name}": value for name, value in values.items()})
        return self.browser.post(reverse("operations:media-update", args=[self.asset.pk]), data)

    def test_public_project_uses_shared_header_and_safe_fields(self):
        for url in (reverse("operations:projects"), reverse("operations:project-detail", args=[self.project.pk])):
            response = self.browser.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, "operations/public_header.html")
            self.assertContains(response, "Coastal renovation")
            self.assertContains(response, "Ventura County")
            for private in (self.project.title, self.project.location, self.project.summary, self.client_record.name):
                self.assertNotContains(response, private)

    def test_legacy_project_stays_live_with_generic_title_and_no_internal_location(self):
        Project.objects.filter(pk=self.project.pk).update(public_title="", public_location="", public_summary="")
        response = self.browser.get(reverse("operations:project-detail", args=[self.project.pk]))
        self.assertContains(response, "A Grand Coast project")
        self.assertNotContains(response, self.project.title)
        self.assertNotContains(response, self.project.location)

    def test_client_name_is_explicit_opt_in(self):
        self.project.show_client_name = True
        self.project.save()
        response = self.browser.get(reverse("operations:project-detail", args=[self.project.pk]))
        self.assertContains(response, self.client_record.name)

    def test_new_projects_require_public_title_but_legacy_projects_remain_editable(self):
        project = Project(title="New internal", is_published=True)
        with self.assertRaises(ValidationError):
            project.full_clean()
        project.is_published = False
        project.full_clean()
        Project.objects.filter(pk=self.project.pk).update(public_title="")
        self.project.refresh_from_db()
        self.project.full_clean()

    def test_media_destinations_are_independent(self):
        website = self.publication("website")
        publish(website)
        self.assertTrue(can_view_media(AnonymousUser(), self.asset))
        self.assertTrue(can_view_media(self.client_user, self.asset))
        client = self.publication("client", title="Client filename", caption="Client note")
        publish(client)
        self.assertTrue(can_view_media(self.client_user, self.asset))
        self.assertTrue(can_view_media(self.other_user, self.asset))
        unpublish(website)
        self.assertFalse(can_view_media(AnonymousUser(), self.asset))
        self.assertFalse(can_view_media(self.other_user, self.asset))
        self.assertTrue(can_view_media(self.client_user, self.asset))
        self.assertEqual(media_presentation(self.asset, "client")["title"], "Client filename")

    def test_saved_drafts_do_not_change_live_data_or_order(self):
        publication = self.publication("website", title="Live title", caption="Live caption", sort_order=1)
        publish(publication)
        publication.title = "Draft title"
        publication.caption = "Draft caption"
        publication.sort_order = 7
        publication.save()
        response = self.browser.get(reverse("operations:project-detail", args=[self.project.pk]))
        self.assertContains(response, "Live title")
        self.assertNotContains(response, "Draft title")
        self.assertEqual(media_presentation(self.asset, "website")["sort_order"], 1)
        publish(publication)
        self.assertEqual(media_presentation(self.asset, "website")["title"], "Draft title")

    def test_client_shares_do_not_move_to_a_different_client(self):
        publish(self.publication("client"))
        self.project.client = self.other_client
        self.project.save()
        self.asset.refresh_from_db()
        self.assertFalse(client_is_live(self.asset))
        self.assertFalse(can_view_media(self.other_user, self.asset))
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]), {"destination": "client", "state": "published"})
        self.assertEqual(response.context["media_rows"], [])
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]), {"destination": "client"})
        self.assertContains(response, "Review this file before sharing it with the current client.")

    def test_publishing_validation(self):
        publication = self.publication("website", title="")
        with self.assertRaises(ValidationError):
            publish(publication)
        publication.title = "Reviewed"
        self.project.public_title = ""
        self.project.save()
        self.asset.refresh_from_db()
        with self.assertRaises(ValidationError):
            publish(publication)
        self.project.client = None
        self.project.save()
        self.asset.refresh_from_db()
        with self.assertRaises(ValidationError):
            publish(self.publication("client"))

    def test_owner_editor_errors_and_private_preview(self):
        self.login_owner()
        response = self.edit(title="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a title before sharing")
        response = self.edit(action="preview", title="Unpublished preview")
        self.assertEqual(response.status_code, 302)
        response = self.browser.get(response.url)
        self.assertContains(response, "Unpublished preview")
        self.assertIsNone(MediaPublication.objects.get(asset=self.asset).published_at)
        self.browser.logout()
        response = self.browser.get(reverse("operations:media-preview", args=[self.asset.pk]))
        self.assertEqual(response.status_code, 302)

    @patch("operations.views._notify_project_client")
    def test_notifications_only_on_changed_client_publish(self, notify):
        self.login_owner()
        self.assertEqual(self.edit().status_code, 302)
        notify.assert_not_called()
        self.assertEqual(self.edit(destination="client", action="save").status_code, 302)
        notify.assert_not_called()
        self.assertEqual(self.edit(destination="client").status_code, 302)
        self.assertEqual(notify.call_count, 1)
        self.edit(destination="client")
        self.assertEqual(notify.call_count, 1)
        self.project.client = self.other_client
        self.project.save()
        self.edit(destination="client")
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args.args[0].client_id, self.other_client.pk)
        self.edit(destination="client", action="unpublish")
        self.assertFalse(client_is_live(self.asset))
        self.assertEqual(notify.call_count, 2)

    @patch("operations.views._notify_project_client")
    def test_multi_file_upload_is_draft_only(self, notify):
        self.login_owner()
        response = self.browser.post(reverse("operations:media-upload"), {
            "project": self.project.pk, "destinations": ["client", "website"],
            "files": [SimpleUploadedFile("first.png", b"\x89PNG\r\n\x1a\nimage", content_type="image/png"),
                      SimpleUploadedFile("second.mp4", b"\x00\x00\x00\x18ftypmp42video", content_type="video/mp4")],
        })
        self.assertEqual(response.status_code, 302)
        uploaded = MediaAsset.objects.exclude(pk=self.asset.pk)
        self.assertEqual(uploaded.count(), 2)
        self.assertEqual(MediaPublication.objects.filter(asset__in=uploaded).count(), 4)
        self.assertFalse(MediaPublication.objects.filter(asset__in=uploaded, published_at__isnull=False).exists())
        self.assertTrue(all(not can_view_media(AnonymousUser(), asset) for asset in uploaded))
        notify.assert_not_called()

    def test_upload_errors_preserve_project_and_draft_destination(self):
        self.login_owner()
        response = self.browser.post(reverse("operations:media-upload"), {
            "project": self.project.pk, "destination": "website", "destinations": ["website"],
            "files": SimpleUploadedFile("bad.png", b"not an image", content_type="image/png"),
        })
        self.assertContains(response, "file contents do not match")
        self.assertContains(response, "bad.png:")
        self.assertEqual(response.context["media_destination"], "website")
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_delete_requires_confirmation_and_removes_file_and_both_publications(self):
        self.login_owner()
        self.asset.file.save("original.png", SimpleUploadedFile("original.png", b"image"))
        name = self.asset.file.path
        publish(self.publication("website"))
        publish(self.publication("client"))
        url = reverse("operations:media-delete", args=[self.asset.pk]) + "?destination=website"
        self.assertEqual(self.browser.get(url).status_code, 200)
        self.browser.post(url, {})
        self.assertTrue(Path(name).exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(url, {"confirm": "delete"})
            self.assertEqual(response.status_code, 302)
            self.assertIn("destination=website", response.url)
        self.assertFalse(Path(name).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=self.asset.pk).exists())
        self.assertFalse(MediaPublication.objects.exists())

    def test_client_and_employee_cannot_publish_or_delete(self):
        employee = get_user_model().objects.create_user("field-publishing", is_staff=True)
        employee.groups.add(Group.objects.get_or_create(name="Field")[0])
        self.project.assigned_staff.add(employee)
        for user in (self.client_user, employee):
            self.browser.force_login(user)
            self.assertEqual(self.edit().status_code, 403)
            self.assertEqual(self.browser.post(reverse("operations:media-delete", args=[self.asset.pk]), {"confirm": "delete"}).status_code, 403)

    def test_services_empty_state_and_crud_order(self):
        response = self.browser.get(reverse("operations:services"))
        self.assertNotContains(response, 'class="section service-detail-section"')
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["content"]))
        self.assertContains(response, "No services added yet")
        response = self.browser.post(reverse("operations:service-add"), {
            "new-service-title": "Remodeling", "new-service-description": "Careful work", "new-service-sort_order": 0, "new-service-is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        first = Service.objects.get()
        second = Service.objects.create(slug="second", title="Second", description="Second service", sort_order=2)
        self.browser.post(reverse("operations:service-move", args=[second.pk]), {"direction": "up"})
        self.assertEqual(list(Service.objects.values_list("pk", flat=True)), [second.pk, first.pk])
        response = self.browser.post(reverse("operations:service-save", args=[first.pk]), {
            f"service-{first.pk}-title": "Updated", f"service-{first.pk}-description": "Updated copy", f"service-{first.pk}-sort_order": 1,
        })
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.browser.logout()
        response = self.browser.get(reverse("operations:services"))
        self.assertContains(response, "Second service")
        self.assertNotContains(response, "Updated copy")

    def test_services_intro_updates_public_page(self):
        self.login_owner()
        response = self.browser.post(reverse("operations:content-update"), {
            "headline": "Welcome", "subheadline": "Welcome copy", "featured_title": "Featured", "featured_body": "Body",
            "services_title": "Our services", "services_intro": "Your next project starts here.", "services_note": "Ventura County",
        })
        self.assertEqual(response.status_code, 302)
        self.browser.logout()
        response = self.browser.get(reverse("operations:services"))
        self.assertContains(response, "Our services")
        self.assertContains(response, "Your next project starts here.")

    @patch("operations.views._notify_project_client")
    def test_legacy_migration_preserves_destinations_without_alerts(self, notify):
        migration = importlib.import_module("operations.migrations.0033_migrate_media_destinations")
        for visibility, destinations in (("public", {"website", "client"}), ("client", {"client"}), ("internal", set())):
            asset = MediaAsset.objects.create(project=self.project, title="Legacy " + visibility, visibility=visibility)
            migration.migrate_destinations(apps, connection.schema_editor())
            self.assertEqual(set(asset.publications.values_list("destination", flat=True)), destinations)
            self.assertTrue(all(item.published_at for item in asset.publications.all()))
        notify.assert_not_called()

    def test_client_portal_and_api_show_only_live_client_copy(self):
        publish(self.publication("website", title="Website title"))
        self.browser.force_login(self.client_user)
        response = self.browser.get(reverse("operations:portal-section", args=["photos"]), {"project": self.project.pk})
        self.assertEqual(response.context["portal_media"], [])
        api_url = reverse("operations-api:project-media", args=[self.project.pk])
        self.assertEqual(self.browser.get(api_url).json()["results"], [])
        publish(self.publication("client", title="Client title", caption="Client note"))
        response = self.browser.get(reverse("operations:portal-section", args=["photos"]), {"project": self.project.pk})
        self.assertEqual(response.context["portal_media"][0]["title"], "Client title")
        self.assertContains(response, "Client note")
        self.assertNotContains(response, "Website title")
        media = self.browser.get(api_url).json()["results"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["title"], "Client title")
        self.assertEqual(media[0]["caption"], "Client note")
