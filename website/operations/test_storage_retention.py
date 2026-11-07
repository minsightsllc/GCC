from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from .admin import LeadAdmin, grand_coast_admin_site
from .models import Activity, Agreement, CostEntry, Lead, LeadAttachment, MediaAsset, MediaPublication, Project, ProjectDocument, StorageTrash
from .storage_retention import (
    StorageRetentionError,
    move_file_to_trash,
    permanently_delete_storage_item,
    reattach_recovered_file,
    restore_storage_item,
    storage_file_rows,
)
from .project_archives import permanently_delete_project, project_export_file


TEST_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="gcc-storage-retention-tests-"))
TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "contact_submissions": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
}


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT, STORAGES=TEST_STORAGES, USE_SUPABASE_STORAGE=False)
class StorageRetentionTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def make_project(self, title="Retention test"):
        return Project.objects.create(title=title, status=Project.Status.CONSTRUCTION)

    def make_trash_item(self, storage_name="stored/test-file.pdf", *, status=StorageTrash.Status.TRASHED):
        return StorageTrash.objects.create(
            storage_name=storage_name,
            original_name=storage_name.rsplit("/", 1)[-1],
            source_model="operations.projectdocument",
            source_id="missing-source",
            source_field="file",
            record_title="Test document",
            size_bytes=128,
            content_type="application/pdf",
            status=status,
        )

    def test_trash_unpublishes_asset_and_restore_reattaches_without_sharing(self):
        project = self.make_project()
        asset = MediaAsset.objects.create(
            project=project,
            title="Before photo",
            file="Client Media/project/photo.jpg",
            visibility=MediaAsset.Visibility.PUBLIC,
        )
        publication = MediaPublication.objects.create(
            asset=asset,
            destination=MediaPublication.Destination.WEBSITE,
            published_at=timezone.now(),
            published_data={"title": "Before photo"},
        )

        item = move_file_to_trash("operations.mediaasset", asset.pk, "file")

        asset.refresh_from_db()
        publication.refresh_from_db()
        self.assertEqual(asset.file.name, "")
        self.assertEqual(asset.visibility, MediaAsset.Visibility.INTERNAL)
        self.assertIsNone(publication.published_at)
        self.assertEqual(item.storage_name, "Client Media/project/photo.jpg")
        self.assertTrue(StorageTrash.objects.filter(pk=item.pk).exists())

        restore_storage_item(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.status, StorageTrash.Status.RECOVERED)
        self.assertEqual(asset.file.name, "")

        reattach_recovered_file(item.pk)
        asset.refresh_from_db()
        self.assertEqual(asset.file.name, item.storage_name)
        self.assertEqual(asset.visibility, MediaAsset.Visibility.INTERNAL)
        self.assertIsNone(asset.publications.get().published_at)
        self.assertFalse(StorageTrash.objects.filter(pk=item.pk).exists())
        self.assertTrue(Activity.objects.filter(message="File restored to Recovered files").exists())

    def test_trash_hides_client_document_from_portal_queryset_and_download_policy(self):
        project = self.make_project()
        document = ProjectDocument.objects.create(
            project=project,
            title="Client guide",
            file="Documents and Files/client-guide.pdf",
            visibility=ProjectDocument.Visibility.CLIENT,
        )

        move_file_to_trash("operations.projectdocument", document.pk, "file")

        self.assertFalse(
            project.documents.filter(
                pk=document.pk,
                visibility=ProjectDocument.Visibility.CLIENT,
                file__gt="",
            ).exists()
        )
        document.refresh_from_db()
        self.assertEqual(document.visibility, ProjectDocument.Visibility.INTERNAL)
        self.assertEqual(document.file.name, "")

    def test_signed_agreement_and_cost_receipt_are_protected_from_individual_trash(self):
        project = self.make_project()
        agreement = Agreement.objects.create(
            project=project,
            signed_pdf="signed/agreement.pdf",
        )
        cost = CostEntry.objects.create(
            project=project,
            description="Materials",
            amount="25.00",
            receipt="receipts/materials.pdf",
        )

        for model, record, field in (
            ("operations.agreement", agreement, "signed_pdf"),
            ("operations.costentry", cost, "receipt"),
        ):
            with self.subTest(model=model), self.assertRaises(StorageRetentionError):
                move_file_to_trash(model, record.pk, field)

        self.assertEqual(agreement.signed_pdf.name, "signed/agreement.pdf")
        self.assertEqual(cost.receipt.name, "receipts/materials.pdf")

    def test_inventory_includes_and_protects_signed_agreements_and_receipts(self):
        project = self.make_project()
        Agreement.objects.create(project=project, signed_pdf="signed/agreement.pdf")
        CostEntry.objects.create(
            project=project,
            description="Materials",
            amount="25.00",
            receipt="receipts/materials.pdf",
        )

        rows = {row["original_name"]: row for row in storage_file_rows()}

        self.assertTrue(rows["agreement.pdf"]["protected"])
        self.assertEqual(rows["agreement.pdf"]["sharing_status"], "Signed agreement")
        self.assertTrue(rows["materials.pdf"]["protected"])
        self.assertEqual(rows["materials.pdf"]["sharing_status"], "Financial receipt")

    def test_admin_lead_deletion_keeps_uploaded_attachment_in_storage_trash(self):
        owner = get_user_model().objects.create_user(
            username="storage-owner",
            email="storage-owner@example.test",
            password="owner-pass-123",
            is_staff=True,
            is_superuser=True,
        )
        lead = Lead.objects.create(
            name="Lead with attachment",
            email="lead@example.test",
            service="Renovation",
            location="Ventura",
        )
        attachment = LeadAttachment.objects.create(
            lead=lead,
            original_name="project-photo.jpg",
            file=ContentFile(b"lead attachment", name="project-photo.jpg"),
        )
        storage_name = attachment.file.name
        storage = attachment.file.storage
        model_admin = LeadAdmin(Lead, grand_coast_admin_site)

        model_admin.delete_model(SimpleNamespace(user=owner), lead)

        self.assertFalse(Lead.objects.filter(pk=lead.pk).exists())
        retained = StorageTrash.objects.get(storage_name=storage_name)
        self.assertEqual(retained.status, StorageTrash.Status.TRASHED)
        self.assertEqual(retained.storage_alias, StorageTrash.StorageAlias.CONTACT)
        self.assertTrue(storage.exists(storage_name))

    def test_project_export_includes_original_file_after_it_was_moved_to_trash(self):
        project = self.make_project()
        document = ProjectDocument.objects.create(
            project=project,
            title="Warranty guide",
            file=ContentFile(b"original project document", name="warranty-guide.pdf"),
        )
        move_file_to_trash("operations.projectdocument", document.pk, "file")

        archive_file = project_export_file(project)
        with ZipFile(archive_file) as archive:
            retained_paths = [name for name in archive.namelist() if name.startswith("files/recovered/")]
            self.assertEqual(len(retained_paths), 1)
            self.assertEqual(archive.read(retained_paths[0]), b"original project document")

    def test_archived_project_deletion_purges_a_previously_recovered_project_file(self):
        project = self.make_project()
        project.status = Project.Status.COMPLETE
        project.save(update_fields=["status", "updated_at"])
        document = ProjectDocument.objects.create(
            project=project,
            title="Recovered project file",
            file=ContentFile(b"remove with archived project", name="recovered-project.pdf"),
        )
        stored_name = document.file.name
        storage = document.file.storage
        item = move_file_to_trash("operations.projectdocument", document.pk, "file")
        restore_storage_item(item.pk)

        with self.captureOnCommitCallbacks(execute=True):
            permanently_delete_project(project)

        self.assertFalse(Project.objects.filter(pk=project.pk).exists())
        self.assertFalse(StorageTrash.objects.filter(pk=item.pk).exists())
        self.assertFalse(storage.exists(stored_name))

    def test_purge_deletes_unreferenced_object_and_records_activity(self):
        item = self.make_trash_item()
        storage = Mock()
        storage.exists.return_value = True

        with patch("operations.storage_retention.storage_for_alias", return_value=storage):
            result = permanently_delete_storage_item(item.pk)

        self.assertEqual(result, "deleted")
        storage.delete.assert_called_once_with(item.storage_name)
        self.assertFalse(StorageTrash.objects.filter(pk=item.pk).exists())
        self.assertTrue(Activity.objects.filter(message="File permanently deleted from Storage Trash").exists())

    def test_purge_preserves_object_still_referenced_by_an_active_record(self):
        project = self.make_project()
        document = ProjectDocument.objects.create(
            project=project,
            title="Still in use",
            file="stored/shared.pdf",
        )
        item = self.make_trash_item("stored/shared.pdf")
        storage = Mock()

        with patch("operations.storage_retention.storage_for_alias", return_value=storage):
            result = permanently_delete_storage_item(item.pk)

        self.assertEqual(result, "retained")
        storage.delete.assert_not_called()
        self.assertTrue(ProjectDocument.objects.filter(pk=document.pk, file="stored/shared.pdf").exists())
        self.assertFalse(StorageTrash.objects.filter(pk=item.pk).exists())

    def test_failed_storage_delete_remains_in_trash_for_retry(self):
        item = self.make_trash_item()
        storage = Mock()
        storage.exists.return_value = True
        storage.delete.side_effect = OSError("storage unavailable")

        with patch("operations.storage_retention.storage_for_alias", return_value=storage):
            result = permanently_delete_storage_item(item.pk)

        item.refresh_from_db()
        self.assertEqual(result, "failed")
        self.assertEqual(item.status, StorageTrash.Status.PURGE_FAILED)
        self.assertIn("retry", item.last_error.casefold())
