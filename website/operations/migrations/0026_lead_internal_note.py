from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0025_apiratelimitbucket"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="internal_note",
            field=models.TextField(blank=True, default="", verbose_name="Internal admin note"),
        ),
    ]
