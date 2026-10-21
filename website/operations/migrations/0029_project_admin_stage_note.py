from django.db import migrations, models
from django.db.models import F


def copy_project_status_to_admin_note(apps, schema_editor):
    Project = apps.get_model("operations", "Project")
    Project.objects.update(admin_stage_note=F("status"))


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0028_projectupdatedraft"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="admin_stage_note",
            field=models.CharField(
                blank=True,
                choices=[
                    ("planning", "Planning"),
                    ("selections", "Selections"),
                    ("construction", "Construction"),
                    ("final", "Final walkthrough"),
                    ("complete", "Complete"),
                    ("on_hold", "On hold"),
                ],
                default="",
                max_length=30,
            ),
        ),
        migrations.RunPython(copy_project_status_to_admin_note, migrations.RunPython.noop),
    ]
