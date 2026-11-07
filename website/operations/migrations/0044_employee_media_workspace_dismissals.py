from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("operations", "0043_clientmessage_source_language"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediaasset",
            name="hidden_from_employee_workspace_by",
            field=models.ManyToManyField(
                blank=True,
                related_name="hidden_employee_workspace_media",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
