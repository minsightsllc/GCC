from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0026_lead_internal_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientnotification",
            name="dismissed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="clientnotification",
            index=models.Index(
                fields=["client", "dismissed_at", "created_at"],
                name="ops_clientnotif_dismissed_idx",
            ),
        ),
    ]
