from django.db import migrations


def unpublish_private_project_media(apps, schema_editor):
    database = schema_editor.connection.alias
    MediaPublication = apps.get_model("operations", "MediaPublication")
    WebsiteGalleryPost = apps.get_model("operations", "WebsiteGalleryPost")

    MediaPublication.objects.using(database).filter(
        destination="website",
        published_at__isnull=False,
        asset__project__is_published=False,
    ).update(published_at=None, published_data={})

    WebsiteGalleryPost.objects.using(database).filter(
        published_at__isnull=False,
        project__is_published=False,
    ).update(published_at=None, published_data={})


class Migration(migrations.Migration):
    dependencies = [("operations", "0035_website_gallery_posts")]

    operations = [
        migrations.RunPython(unpublish_private_project_media, migrations.RunPython.noop),
    ]
