from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0037_alter_processstep_options_processstep_image_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="is_portfolio_only",
            field=models.BooleanField(default=False),
        ),
    ]
