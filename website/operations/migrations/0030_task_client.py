from django.db import migrations, models
import django.db.models.deletion


def populate_task_clients(apps, schema_editor):
    Task = apps.get_model("operations", "Task")
    tasks = list(
        Task.objects.filter(
            client__isnull=True,
            project__client__isnull=False,
        ).select_related("project")
    )
    for task in tasks:
        task.client_id = task.project.client_id
    if tasks:
        Task.objects.bulk_update(tasks, ["client"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0029_project_admin_stage_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="client",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks",
                to="operations.client",
            ),
        ),
        migrations.RunPython(populate_task_clients, migrations.RunPython.noop),
    ]
