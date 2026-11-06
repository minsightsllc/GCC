from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0041_language_preferences_and_spanish_public_copy"),
    ]

    operations = [
        migrations.CreateModel(
            name="MessageTranslationCache",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "target_language",
                    models.CharField(
                        choices=[("en", "English"), ("es", "Spanish")],
                        max_length=2,
                    ),
                ),
                ("source_hash", models.CharField(max_length=64)),
                ("source_language", models.CharField(blank=True, max_length=35)),
                ("translated_body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="translation_cache",
                        to="operations.clientmessage",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="messagetranslationcache",
            constraint=models.UniqueConstraint(
                fields=("message", "target_language", "source_hash"),
                name="ops_msg_translation_unique",
            ),
        ),
        migrations.CreateModel(
            name="MessageTranslationUsage",
            fields=[
                ("month_start", models.DateField(primary_key=True, serialize=False)),
                ("characters_reserved", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
