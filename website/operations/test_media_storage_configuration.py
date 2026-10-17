from django.conf import settings
from django.test import SimpleTestCase

from backend.settings import _should_use_supabase_upload_storage


class UploadStorageModeTests(SimpleTestCase):
    def test_regular_development_uses_local_storage_even_with_cloud_opt_in(self):
        self.assertFalse(
            _should_use_supabase_upload_storage(
                debug=True,
                simulation_mode=False,
                storage_smoke_enabled=False,
                explicit_request=True,
            )
        )

    def test_production_uses_supabase_without_a_separate_opt_in(self):
        self.assertTrue(
            _should_use_supabase_upload_storage(
                debug=False,
                simulation_mode=False,
                storage_smoke_enabled=False,
                explicit_request=False,
            )
        )

    def test_only_isolated_storage_certification_can_opt_in_during_development(self):
        self.assertTrue(
            _should_use_supabase_upload_storage(
                debug=True,
                simulation_mode=True,
                storage_smoke_enabled=True,
                explicit_request=True,
            )
        )
        self.assertFalse(
            _should_use_supabase_upload_storage(
                debug=True,
                simulation_mode=True,
                storage_smoke_enabled=False,
                explicit_request=True,
            )
        )

    def test_default_and_contact_uploads_share_the_selected_backend(self):
        expected_backend = (
            "storages.backends.s3.S3Storage"
            if settings.USE_SUPABASE_STORAGE
            else "django.core.files.storage.FileSystemStorage"
        )
        self.assertEqual(settings.STORAGES["default"]["BACKEND"], expected_backend)
        self.assertEqual(
            settings.STORAGES["contact_submissions"]["BACKEND"],
            expected_backend,
        )
        self.assertEqual(
            settings.USE_SUPABASE_CONTACT_STORAGE,
            settings.USE_SUPABASE_STORAGE,
        )
