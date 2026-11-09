from django.db import migrations
from django.db.models import Count, Q
from django.utils import timezone


def archive_projects_with_all_milestones_complete(apps, schema_editor):
    Project = apps.get_model("operations", "Project")
    database = schema_editor.connection.alias
    now = timezone.now()

    projects = (
        Project.objects.using(database)
        .filter(is_portfolio_only=False)
        .exclude(status__in=("complete", "on_hold"))
        .annotate(
            milestone_count=Count("milestones"),
            incomplete_milestone_count=Count(
                "milestones",
                filter=Q(milestones__is_complete=False),
            ),
        )
        .filter(milestone_count__gt=0, incomplete_milestone_count=0)
    )

    for project_id in projects.values_list("pk", flat=True).iterator():
        Project.objects.using(database).filter(pk=project_id).update(
            status="complete",
            operational_phase="closeout",
            next_step="Project archived",
            archived_at=now,
            updated_at=now,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0047_databaseauditretention"),
    ]

    operations = [
        migrations.RunPython(
            archive_projects_with_all_milestones_complete,
            migrations.RunPython.noop,
        ),
    ]
