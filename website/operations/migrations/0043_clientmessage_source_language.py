from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0042_message_translation_cache_and_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientmessage",
            name="source_language",
            field=models.CharField(
                blank=True,
                choices=[("en", "English"), ("es", "Spanish")],
                default="",
                max_length=2,
            ),
        ),
    ]
