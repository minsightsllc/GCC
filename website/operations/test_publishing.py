import importlib
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client as Browser, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .construction_policies import can_view_media
from .forms import MediaUploadForm
from .models import (
    Client,
    MediaAsset,
    MediaPublication,
    Project,
    Service,
    SiteSettings,
    WebsiteGalleryPost,
    WebsiteGalleryPostItem,
)
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

    def published_post(self, *, asset=None, title="Published post", caption="", **values):
        asset = asset or self.asset
        if not asset.file:
            asset.file.save(
                f"{asset.pk}.png",
                SimpleUploadedFile(f"{asset.pk}.png", b"project image", content_type="image/png"),
                save=True,
            )
        post = WebsiteGalleryPost.objects.create(
            project=asset.project,
            title=title,
            caption=caption,
            **values,
        )
        WebsiteGalleryPostItem.objects.create(post=post, asset=asset, sort_order=0)
        publication, _ = MediaPublication.objects.get_or_create(
            asset=asset,
            destination=MediaPublication.Destination.WEBSITE,
            defaults={"title": asset.title or title},
        )
        publish(publication)
        post.published_data = post.draft_data()
        post.published_at = timezone.now()
        post.save(update_fields=["published_data", "published_at", "updated_at"])
        return post, publication

    def edit(self, destination="client", action="publish", **values):
        if destination != "client":
            raise ValueError("Website publishing uses project post forms.")
        prefix = "media"
        MediaPublication.objects.get_or_create(
            asset=self.asset,
            destination=MediaPublication.Destination.CLIENT,
            defaults={"title": self.asset.title},
        )
        data = {"action": action,
                f"{prefix}-{self.asset.pk}-title": "Reviewed title", f"{prefix}-{self.asset.pk}-caption": "Reviewed caption",
                f"{prefix}-{self.asset.pk}-sort_order": "0"}
        data.update({f"{prefix}-{self.asset.pk}-{name}": value for name, value in values.items()})
        return self.browser.post(reverse("operations:media-update", args=[self.asset.pk]), data)

    def test_public_project_uses_shared_header_and_safe_fields(self):
        self.published_post()
        response = self.browser.get(reverse("operations:projects"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "operations/public_header.html")
        self.assertContains(response, "Coastal renovation")
        for private in (self.project.title, self.project.location, self.project.summary, self.client_record.name):
            self.assertNotContains(response, private)
        detail_response = self.browser.get(reverse("operations:project-detail", args=[self.project.pk]))
        self.assertEqual(detail_response.status_code, 302)
        self.assertIn(f"#public-project-{self.project.pk}", detail_response.url)

    def test_client_only_share_never_appears_in_public_project_gallery(self):
        publication = self.publication(
            "client",
            title="Private client filename",
            caption="Private client note",
        )
        publish(publication)

        response = self.browser.get(reverse("operations:projects"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private client filename")
        self.assertNotContains(response, "Private client note")
        self.assertNotContains(response, reverse("operations:media-file", args=[self.asset.pk]))
        self.assertEqual(
            self.browser.get(reverse("operations:project-detail", args=[self.project.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.browser.get(reverse("operations:media-file", args=[self.asset.pk])).status_code,
            302,
        )

    def test_legacy_project_stays_live_with_generic_title_and_no_internal_location(self):
        Project.objects.filter(pk=self.project.pk).update(public_title="", public_location="", public_summary="")
        self.published_post()
        response = self.browser.get(reverse("operations:projects"))
        self.assertContains(response, "A Grand Coast project")
        self.assertNotContains(response, self.project.title)
        self.assertNotContains(response, self.project.location)

    def test_client_name_is_explicit_opt_in(self):
        self.published_post(show_client_name=True)
        response = self.browser.get(reverse("operations:projects"))
        self.assertContains(response, self.client_record.name)

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
        post, _ = self.published_post(title="Live title", caption="Live caption")
        self.login_owner()
        prefix = f"post-{post.pk}"
        response = self.browser.post(
            reverse("operations:website-post-update", args=[post.pk]),
            {
                "website_project": str(self.project.pk),
                "action": "save",
                f"{prefix}-title": "Draft title",
                f"{prefix}-caption": "Draft caption",
                f"{prefix}-label": "",
                f"{prefix}-layout": WebsiteGalleryPost.Layout.WIDE,
                f"{prefix}-accent": WebsiteGalleryPost.Accent.NAVY,
                f"{prefix}-sort_order": "7",
                f"{prefix}-items_to_keep": str(self.asset.pk),
                f"{prefix}-slide_order_{self.asset.pk}": "7",
            },
        )
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertEqual(post.published_data["title"], "Live title")
        self.assertEqual(post.published_data["slides"][0]["sort_order"], 0)
        self.assertEqual(post.draft_data()["title"], "Draft title")
        self.assertEqual(post.draft_data()["slides"][0]["sort_order"], 7)
        response = self.browser.get(reverse("operations:projects"))
        self.assertContains(response, "Live title")
        self.assertNotContains(response, "Draft title")

    def test_client_shares_do_not_move_to_a_different_client(self):
        publish(self.publication("client"))
        self.project.client = self.other_client
        self.project.save()
        self.asset.refresh_from_db()
        self.assertFalse(client_is_live(self.asset))
        self.assertFalse(can_view_media(self.other_user, self.asset))
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]), {"state": "published"})
        self.assertEqual(response.context["media_rows"], [])
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]))
        self.assertContains(response, "no longer shared with the project's current client")

    def test_publishing_validation(self):
        publication = self.publication("website", title="")
        with self.assertRaises(ValidationError):
            publish(publication)
        publication.title = "Reviewed"
        self.asset.project = None
        self.asset.save(update_fields=["project"])
        self.asset.refresh_from_db()
        with self.assertRaises(ValidationError):
            publish(publication)
        self.asset.project = self.project
        self.asset.save(update_fields=["project"])
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
        self.assertEqual(self.edit(action="save").status_code, 302)
        notify.assert_not_called()
        self.assertEqual(self.edit(action="publish").status_code, 302)
        self.assertEqual(notify.call_count, 1)
        self.edit(action="publish")
        self.assertEqual(notify.call_count, 1)
        self.project.client = self.other_client
        self.project.save()
        self.edit(action="publish")
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args.args[0].client_id, self.other_client.pk)
        self.edit(action="unpublish")
        self.assertFalse(client_is_live(self.asset))
        self.assertEqual(notify.call_count, 2)

    @patch("operations.views._notify_project_client")
    def test_multi_file_upload_is_draft_only(self, notify):
        self.login_owner()
        response = self.browser.post(reverse("operations:media-upload"), {
            "project": self.project.pk,
            "files": [SimpleUploadedFile("first.png", b"\x89PNG\r\n\x1a\nimage", content_type="image/png"),
                      SimpleUploadedFile("second.mp4", b"\x00\x00\x00\x18ftypmp42video", content_type="video/mp4")],
        })
        self.assertEqual(response.status_code, 302)
        uploaded = MediaAsset.objects.exclude(pk=self.asset.pk)
        self.assertEqual(uploaded.count(), 2)
        self.assertEqual(MediaPublication.objects.filter(asset__in=uploaded).count(), 2)
        self.assertFalse(MediaPublication.objects.filter(asset__in=uploaded, destination="website").exists())
        self.assertEqual(set(MediaPublication.objects.filter(asset__in=uploaded).values_list("destination", flat=True)), {"client"})
        self.assertFalse(MediaPublication.objects.filter(asset__in=uploaded, published_at__isnull=False).exists())
        self.assertTrue(all(not can_view_media(AnonymousUser(), asset) for asset in uploaded))
        notify.assert_not_called()

    def test_media_upload_rejects_documents_for_the_documents_area(self):
        upload = SimpleUploadedFile("project-plan.pdf", b"%PDF-1.7 project plan", content_type="application/pdf")
        form = MediaUploadForm(
            {"project": self.project.pk},
            {"files": [upload]},
            project_queryset=Project.objects.all(),
            require_client=True,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("files", form.errors)

    def test_upload_errors_preserve_project_and_client_sharing_surface(self):
        self.login_owner()
        response = self.browser.post(reverse("operations:media-upload"), {
            "project": self.project.pk,
            "files": SimpleUploadedFile("bad.png", b"not an image", content_type="image/png"),
        })
        self.assertContains(response, "file contents do not match")
        self.assertContains(response, "bad.png:")
        self.assertEqual(response.context["media_destination"], "client")
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_client_sharing_lists_only_client_publications_and_has_no_website_or_delete_controls(self):
        client_publication = self.publication("client", title="Client file")
        website_only_asset = MediaAsset.objects.create(project=self.project, title="Website-only original")
        MediaPublication.objects.create(asset=website_only_asset, destination="website", title="Website title")
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]))
        self.assertEqual([row["asset"].pk for row in response.context["media_rows"]], [self.asset.pk])
        self.assertContains(response, "Client file")
        self.assertNotContains(response, "Website-only original")
        self.assertNotContains(response, "name=\"website-")
        self.assertNotContains(response, "Delete original file")
        self.assertNotContains(response, 'aria-label="Media destination"')
        self.assertIsNotNone(client_publication.pk)

    def test_existing_project_media_can_be_prepared_as_client_draft_without_alert(self):
        self.asset.file.save("existing.jpg", SimpleUploadedFile("existing.jpg", b"image bytes"))
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["media"]))
        self.assertTrue(response.context["client_media_prepare_available"])
        self.assertContains(response, "Prepare an existing file for a client")
        with patch("operations.views._notify_project_client") as notify:
            response = self.browser.post(reverse("operations:client-media-prepare"), {"asset": self.asset.pk})
        self.assertEqual(response.status_code, 302)
        publication = MediaPublication.objects.get(asset=self.asset, destination="client")
        self.assertEqual(publication.title, self.asset.title)
        self.assertIsNone(publication.published_at)
        self.assertFalse(MediaPublication.objects.filter(asset=self.asset, destination="website").exists())
        notify.assert_not_called()

    @patch("operations.views._notify_project_client")
    def test_project_post_reuses_client_shared_file_without_changing_client_share(self, notify):
        self.asset.file.save(
            "shared-original.png",
            SimpleUploadedFile("shared-original.png", b"image bytes", content_type="image/png"),
            save=True,
        )
        client_publication = self.publication(
            "client",
            title="Client-facing file",
            caption="Private client note",
        )
        publish(client_publication)
        website_publication = self.publication("website", title="Website draft file")
        self.login_owner()

        response = self.browser.post(
            reverse("operations:website-post-create"),
            {
                "website_project": str(self.project.pk),
                "action": "save",
                "new-post-title": "Kitchen progress",
                "new-post-caption": "The new layout is taking shape.",
                "new-post-label": "In progress",
                "new-post-layout": WebsiteGalleryPost.Layout.WIDE,
                "new-post-accent": WebsiteGalleryPost.Accent.NAVY,
                "new-post-sort_order": "2",
                "new-post-existing_assets": str(self.asset.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        post = WebsiteGalleryPost.objects.get(project=self.project)
        self.assertIsNone(post.published_at)
        self.assertEqual(post.items.count(), 1)
        website_publication.refresh_from_db()
        self.assertIsNone(website_publication.published_at)

        prefix = f"post-{post.pk}"
        response = self.browser.post(
            reverse("operations:website-post-update", args=[post.pk]),
            {
                "website_project": str(self.project.pk),
                "action": "publish",
                f"{prefix}-title": "Kitchen progress",
                f"{prefix}-caption": "The new layout is taking shape.",
                f"{prefix}-label": "In progress",
                f"{prefix}-layout": WebsiteGalleryPost.Layout.WIDE,
                f"{prefix}-accent": WebsiteGalleryPost.Accent.NAVY,
                f"{prefix}-sort_order": "2",
                f"{prefix}-items_to_keep": str(self.asset.pk),
                f"{prefix}-slide_order_{self.asset.pk}": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        website_publication.refresh_from_db()
        client_publication.refresh_from_db()
        self.assertIsNotNone(post.published_at)
        self.assertEqual(post.published_data["title"], "Kitchen progress")
        self.assertIsNotNone(website_publication.published_at)
        self.assertEqual(client_publication.title, "Client-facing file")
        self.assertEqual(client_publication.caption, "Private client note")
        self.assertIsNotNone(client_publication.published_at)
        notify.assert_not_called()

    def test_publish_content_uses_project_post_workflow(self):
        self.login_owner()
        response = self.browser.get(reverse("operations:dashboard-section", args=["content"]))
        self.assertContains(response, 'data-website-post-create')
        self.assertContains(response, 'data-website-post-save')
        self.assertNotContains(response, 'name="publication_details"')
        self.assertNotContains(response, 'name="website-project-public_title"')
        self.assertNotContains(response, 'name="website-project-public_location"')

    def test_public_projects_page_shows_published_post_and_hides_private_metadata(self):
        self.published_post(
            title="Kitchen progress",
            caption="The new layout is taking shape.",
            label="In progress",
        )
        response = self.browser.get(reverse("operations:projects"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coastal renovation")
        self.assertContains(response, "Kitchen progress")
        self.assertContains(response, "The new layout is taking shape.")
        self.assertContains(response, "In progress")
        self.assertNotContains(response, self.client_record.name)
        self.assertNotContains(response, self.project.title)
        self.assertNotContains(response, self.project.location)

    def test_unpublished_project_gallery_and_media_remain_private(self):
        self.published_post(title="Private project post")
        Project.objects.filter(pk=self.project.pk).update(is_published=False)
        self.project.refresh_from_db()

        self.assertFalse(can_view_media(AnonymousUser(), self.asset))
        response = self.browser.get(reverse("operations:projects"))
        self.assertNotContains(response, "Private project post")
        self.assertEqual(
            self.browser.get(reverse("operations:media-file", args=[self.asset.pk])).status_code,
            302,
        )

    def test_legacy_website_media_on_unpublished_project_migrates_as_draft(self):
        migration = importlib.import_module("operations.migrations.0035_website_gallery_posts")
        Project.objects.filter(pk=self.project.pk).update(is_published=False)
        publication = self.publication("website", title="Legacy website item")
        publication.published_at = timezone.now()
        publication.published_data = publication.draft_data()
        publication.save()

        migration.migrate_website_publications(apps, connection.schema_editor())

        publication.refresh_from_db()
        post = WebsiteGalleryPost.objects.get(project=self.project)
        self.assertIsNone(publication.published_at)
        self.assertEqual(publication.published_data, {})
        self.assertIsNone(post.published_at)
        self.assertEqual(post.published_data, {})

    def test_private_media_repair_migration_clears_legacy_live_snapshots(self):
        migration = importlib.import_module("operations.migrations.0036_unpublish_private_project_media")
        Project.objects.filter(pk=self.project.pk).update(is_published=False)
        publication = self.publication("website", title="Legacy website item")
        publication.published_at = timezone.now()
        publication.published_data = publication.draft_data()
        publication.save()
        post = WebsiteGalleryPost.objects.create(
            project=self.project,
            title="Legacy live post",
            published_at=timezone.now(),
            published_data={"title": "Private snapshot"},
        )

        migration.unpublish_private_project_media(apps, connection.schema_editor())

        publication.refresh_from_db()
        post.refresh_from_db()
        self.assertIsNone(publication.published_at)
        self.assertEqual(publication.published_data, {})
        self.assertIsNone(post.published_at)
        self.assertEqual(post.published_data, {})

    def test_slide_keep_checkboxes_are_bound_by_asset_id(self):
        second_asset = MediaAsset.objects.create(
            project=self.project,
            title="Second asset",
            fallback_image="operations/images/project-bathroom.png",
        )
        now = timezone.now()
        MediaAsset.objects.filter(pk=self.asset.pk).update(created_at=now - timedelta(days=1))
        MediaAsset.objects.filter(pk=second_asset.pk).update(created_at=now)
        post = WebsiteGalleryPost.objects.create(project=self.project, title="Two slides")
        second_item = WebsiteGalleryPostItem.objects.create(post=post, asset=second_asset, sort_order=0)
        first_item = WebsiteGalleryPostItem.objects.create(post=post, asset=self.asset, sort_order=0)
        WebsiteGalleryPostItem.objects.filter(pk=second_item.pk).update(created_at=now - timedelta(days=1))
        WebsiteGalleryPostItem.objects.filter(pk=first_item.pk).update(created_at=now)
        self.login_owner()

        response = self.browser.get(
            reverse("operations:dashboard-section", args=["content"]),
            {"website_project": str(self.project.pk)},
        )

        row = response.context["website_post_rows"][0]
        self.assertEqual(
            [str(item.asset_id) for item in row.items_for_editor],
            [str(second_asset.pk), str(self.asset.pk)],
        )
        for item in row.items_for_editor:
            self.assertEqual(item.keep_checkbox.data["value"], str(item.asset_id))

    def test_deleting_website_draft_keeps_original_and_client_share(self):
        self.asset.file.save(
            "original.png",
            SimpleUploadedFile("original.png", b"image bytes", content_type="image/png"),
            save=True,
        )
        client_publication = self.publication("client", title="Client copy")
        publish(client_publication)
        self.publication("website", title="Website draft")
        post = WebsiteGalleryPost.objects.create(project=self.project, title="Draft post")
        WebsiteGalleryPostItem.objects.create(post=post, asset=self.asset)
        self.login_owner()

        url = reverse("operations:website-post-delete", args=[post.pk])
        self.assertEqual(self.browser.get(url).status_code, 200)
        response = self.browser.post(url, {"action": "delete"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WebsiteGalleryPost.objects.filter(pk=post.pk).exists())
        self.assertTrue(MediaAsset.objects.filter(pk=self.asset.pk).exists())
        self.assertTrue(self.asset.file.storage.exists(self.asset.file.name))
        client_publication.refresh_from_db()
        self.assertIsNotNone(client_publication.published_at)
        self.assertEqual(client_publication.title, "Client copy")
        self.assertFalse(
            MediaPublication.objects.filter(
                asset=self.asset,
                destination=MediaPublication.Destination.WEBSITE,
            ).exists()
        )

    def test_client_and_employee_cannot_publish_or_delete_website_posts(self):
        employee = get_user_model().objects.create_user("field-publishing", is_staff=True)
        employee.groups.add(Group.objects.get_or_create(name="Field")[0])
        self.project.assigned_staff.add(employee)
        post = WebsiteGalleryPost.objects.create(project=self.project, title="Draft post")
        for user in (self.client_user, employee):
            self.browser.force_login(user)
            self.assertEqual(self.edit().status_code, 403)
            self.assertEqual(
                self.browser.post(
                    reverse("operations:website-post-update", args=[post.pk]),
                    {"website_project": str(self.project.pk), "action": "publish"},
                ).status_code,
                403,
            )
            self.assertEqual(
                self.browser.post(
                    reverse("operations:website-post-delete", args=[post.pk]),
                    {"action": "delete"},
                ).status_code,
                403,
            )
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
