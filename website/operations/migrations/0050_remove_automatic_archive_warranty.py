from django.db import migrations


def remove_automatic_archive_warranty(apps, schema_editor):
    database = schema_editor.connection.alias
    Project = apps.get_model("operations", "Project")
    WarrantyItem = apps.get_model("operations", "WarrantyItem")

    generated_projects = Project.objects.using(database).filter(
        status="complete",
        archived_at__isnull=False,
        operational_phase="warranty",
        next_step="Warranty support",
    )
    WarrantyItem.objects.using(database).filter(
        project_id__in=generated_projects.values("pk"),
        title="Post-closeout warranty coverage",
        description="Track warranty requests and resolutions after closeout.",
        status="open",
        resolved_at__isnull=True,
    ).delete()
    generated_projects.update(
        operational_phase="closeout",
        next_step="Project archived",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0049_independent_public_gallery"),
    ]

    operations = [
        migrations.RunPython(
            remove_automatic_archive_warranty,
            migrations.RunPython.noop,
        ),
    ]
