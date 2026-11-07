import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0046_storagetrash"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="retention_batch_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="activity",
            name="trashed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activity",
            name="trashed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trashed_operations_activity",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="activity",
            index=models.Index(
                fields=["trashed_at", "created_at"],
                name="ops_activity_trash_time_idx",
            ),
        ),
        migrations.AddField(
            model_name="adminsecurityevent",
            name="retention_batch_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="adminsecurityevent",
            name="trashed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="adminsecurityevent",
            name="trashed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trashed_admin_security_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="adminsecurityevent",
            index=models.Index(
                fields=["trashed_at", "created_at"],
                name="admin_sec_event_trash_time",
            ),
        ),
    ]
