from django.db import migrations, models
from django.utils import timezone


def archive_existing_completed_projects(apps, schema_editor):
    Project = apps.get_model("operations", "Project")
    Project.objects.filter(status="complete", archived_at__isnull=True).update(
        archived_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0044_employee_media_workspace_dismissals"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="archived_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(
            archive_existing_completed_projects,
            migrations.RunPython.noop,
        ),
    ]
