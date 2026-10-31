from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0038_project_is_portfolio_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="websitegallerypostitem",
            name="poster_image",
            field=models.ImageField(blank=True, upload_to="website-posters/%Y/%m/"),
        ),
    ]
