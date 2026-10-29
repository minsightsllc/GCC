from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0033_migrate_media_destinations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mediapublication",
            name="destination",
            field=models.CharField(
                choices=[("client", "Client Media"), ("website", "Website publishing")],
                max_length=12,
            ),
        ),
    ]
