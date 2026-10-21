from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_legacy_assignees(apps, schema_editor):
    Task = apps.get_model("operations", "Task")
    through = Task.assigned_to.through
    database = schema_editor.connection.alias
    task_fk_name = next(
        field.attname
        for field in through._meta.fields
        if getattr(field.remote_field, "model", None) is Task
    )
    user_fk_name = next(
        field.attname
        for field in through._meta.fields
        if getattr(field.remote_field, "model", None) is not Task
        and field.remote_field is not None
    )
    links = [
        through(**{task_fk_name: task.pk, user_fk_name: task.legacy_assigned_to_id})
        for task in Task.objects.using(database).exclude(legacy_assigned_to_id=None).iterator()
    ]
    if links:
        through.objects.using(database).bulk_create(links, ignore_conflicts=True, batch_size=500)


def restore_legacy_assignees(apps, schema_editor):
    """Restore the old singular FK with the first assignee when rolling back."""
    Task = apps.get_model("operations", "Task")
    database = schema_editor.connection.alias
    through = Task.assigned_to.through
    task_fk_name = next(
        field.attname
        for field in through._meta.fields
        if getattr(field.remote_field, "model", None) is Task
    )
    user_fk_name = next(
        field.attname
        for field in through._meta.fields
        if getattr(field.remote_field, "model", None) is not Task
        and field.remote_field is not None
    )
    first_assignee_by_task = {}
    for link in through.objects.using(database).order_by(task_fk_name, user_fk_name).iterator():
        first_assignee_by_task.setdefault(
            getattr(link, task_fk_name),
            getattr(link, user_fk_name),
        )
    for task_id, user_id in first_assignee_by_task.items():
        Task.objects.using(database).filter(pk=task_id).update(legacy_assigned_to_id=user_id)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("operations", "0030_task_client"),
    ]

    operations = [
        migrations.RenameField(
            model_name="task",
            old_name="assigned_to",
            new_name="legacy_assigned_to",
        ),
        migrations.AlterField(
            model_name="task",
            name="legacy_assigned_to",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="legacy_assigned_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="assigned_to",
            field=models.ManyToManyField(
                blank=True,
                related_name="assigned_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(copy_legacy_assignees, restore_legacy_assignees),
        migrations.RemoveField(
            model_name="task",
            name="legacy_assigned_to",
        ),
        migrations.AlterField(
            model_name="task",
            name="watchers",
            field=models.ManyToManyField(
                blank=True,
                related_name="watched_tasks",
                to=settings.AUTH_USER_MODEL,
                verbose_name="supervisors",
            ),
        ),
    ]
