from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client as DjangoClient, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Activity, AdminSecurityEvent
from .test_helpers import force_login_with_admin_gate


@override_settings(RENDER_API_KEY="", RENDER_POSTGRES_ID="")
class DatabaseCapacityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="capacity-owner",
            email="capacity-owner@example.test",
            password="capacity-owner-pass",
        )

    def setUp(self):
        self.client = DjangoClient()
        force_login_with_admin_gate(self.client, self.admin)

    def make_old_audit_rows(self, *, message="old activity", attempted_identifier="old-security"):
        old_time = timezone.now() - timedelta(days=40)
        activity = Activity.objects.create(message=message, detail="retention fixture")
        Activity.objects.filter(pk=activity.pk).update(created_at=old_time)
        event = AdminSecurityEvent.objects.create(
            event_type=AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            attempted_identifier=attempted_identifier,
        )
        AdminSecurityEvent.objects.filter(pk=event.pk).update(created_at=old_time)
        return activity, event, old_time

    def test_admin_home_links_to_restricted_capacity_tool(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manage Database Capacity")
        capacity = self.client.get(reverse("admin:database-capacity"))
        self.assertEqual(capacity.status_code, 200)
        self.assertContains(capacity, "Database Disk Storage")
        self.assertContains(capacity, "Table and index sizes")
        self.assertContains(capacity, "connected to its production database")

    def test_non_superuser_cannot_open_capacity_tool_or_trash(self):
        employee = get_user_model().objects.create_user(
            username="capacity-employee",
            password="employee-pass",
            is_staff=True,
            is_superuser=False,
        )
        employee_client = DjangoClient()
        employee_client.force_login(employee)
        self.assertEqual(employee_client.get(reverse("admin:database-capacity")).status_code, 403)
        self.assertEqual(employee_client.get(reverse("admin:database-capacity-trash")).status_code, 403)

    def test_render_history_reports_unavailable_without_configuration(self):
        from .admin_usage import get_render_database_disk_history

        history = get_render_database_disk_history()
        self.assertEqual(history["status"], "not_configured")
        response = self.client.get(reverse("admin:database-capacity"))
        self.assertContains(response, "The seven-day Database Disk Storage trend is unavailable.")

    def test_render_history_samples_are_normalized_to_bytes(self):
        from .admin_usage import _render_metric_points

        points = _render_metric_points(
            {
                "series": [
                    {
                        "unit": "MB",
                        "values": [
                            ["2026-11-01T12:00:00Z", 2.5],
                            ["2026-11-01T13:00:00Z", 3.0],
                        ],
                    }
                ]
            }
        )
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["used_bytes"], 2_500_000)
        self.assertEqual(points[1]["used_bytes"], 3_000_000)

    def test_cutoff_preview_counts_matching_records_without_mutating_them(self):
        activity, event, old_time = self.make_old_audit_rows()
        recent = Activity.objects.create(message="recent activity")
        cutoff = (timezone.localdate() - timedelta(days=10)).isoformat()

        response = self.client.get(reverse("admin:database-capacity"), {"cutoff": cutoff})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview_counts"], {"activity": 1, "security": 1})
        self.assertContains(response, "old activity")
        self.assertContains(response, "old-security")
        activity.refresh_from_db()
        event.refresh_from_db()
        self.assertIsNone(activity.trashed_at)
        self.assertIsNone(event.trashed_at)
        self.assertGreaterEqual(recent.created_at, old_time)

    def test_cutoff_records_can_be_moved_to_trash_and_restored_as_a_batch(self):
        activity, event, _ = self.make_old_audit_rows()
        cutoff = (timezone.localdate() - timedelta(days=10)).isoformat()
        response = self.client.post(
            reverse("admin:database-capacity-cutoff"),
            {"cutoff_date": cutoff, "action": "trash"},
        )
        self.assertRedirects(response, reverse("admin:database-capacity-trash"))
        activity.refresh_from_db()
        event.refresh_from_db()
        self.assertIsNotNone(activity.trashed_at)
        self.assertEqual(activity.retention_batch_id, event.retention_batch_id)
        self.assertEqual(Activity.objects.filter(trashed_at__isnull=False).count(), 1)
        self.assertEqual(AdminSecurityEvent.objects.filter(trashed_at__isnull=False).count(), 1)
        trash_page = self.client.get(reverse("admin:database-capacity-trash"))
        self.assertEqual(trash_page.status_code, 200)
        self.assertContains(trash_page, "Trash batches")
        self.assertContains(trash_page, "Restore batch")

        response = self.client.post(
            reverse("admin:database-capacity-trash-action", kwargs={"batch_id": activity.retention_batch_id}),
            {"action": "restore"},
        )
        self.assertRedirects(response, reverse("admin:database-capacity-trash"))
        activity.refresh_from_db()
        event.refresh_from_db()
        self.assertIsNone(activity.trashed_at)
        self.assertIsNone(event.trashed_at)
        self.assertTrue(Activity.objects.filter(message="Database audit records restored").exists())

    def test_cutoff_permanent_deletion_requires_confirmation_and_is_audited(self):
        activity, event, _ = self.make_old_audit_rows()
        cutoff = (timezone.localdate() - timedelta(days=10)).isoformat()
        url = reverse("admin:database-capacity-cutoff")
        response = self.client.post(url, {"cutoff_date": cutoff, "action": "purge"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Permanently delete audit records?")
        self.assertTrue(Activity.objects.filter(pk=activity.pk).exists())
        self.assertTrue(AdminSecurityEvent.objects.filter(pk=event.pk).exists())

        response = self.client.post(
            url,
            {"cutoff_date": cutoff, "action": "purge", "confirmed": "1"},
        )
        self.assertRedirects(response, reverse("admin:database-capacity"))
        self.assertFalse(Activity.objects.filter(pk=activity.pk).exists())
        self.assertFalse(AdminSecurityEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(Activity.objects.filter(message="Database audit records permanently deleted").exists())

    def test_database_trash_permanent_deletion_requires_confirmation(self):
        activity, event, _ = self.make_old_audit_rows()
        batch_id = "8d48c1af-6be0-4a7a-9f8c-9c5155d202a8"
        Activity.objects.filter(pk=activity.pk).update(
            retention_batch_id=batch_id,
            trashed_at=timezone.now(),
            trashed_by=self.admin,
        )
        AdminSecurityEvent.objects.filter(pk=event.pk).update(
            retention_batch_id=batch_id,
            trashed_at=timezone.now(),
            trashed_by=self.admin,
        )
        url = reverse("admin:database-capacity-trash-action", kwargs={"batch_id": batch_id})
        response = self.client.post(url, {"action": "purge"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Permanently delete this Database Trash batch?")
        self.assertTrue(Activity.objects.filter(pk=activity.pk).exists())
        self.assertTrue(AdminSecurityEvent.objects.filter(pk=event.pk).exists())

        response = self.client.post(url, {"action": "purge", "confirmed": "1"})
        self.assertRedirects(response, reverse("admin:database-capacity-trash"))
        self.assertFalse(Activity.objects.filter(pk=activity.pk).exists())
        self.assertFalse(AdminSecurityEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(Activity.objects.filter(message="Database audit trash permanently deleted").exists())
