import uuid

from django.db import migrations, models
import django.db.models.deletion


def migrate_website_publications(apps, schema_editor):
    database = schema_editor.connection.alias
    MediaPublication = apps.get_model("operations", "MediaPublication")
    WebsiteGalleryPost = apps.get_model("operations", "WebsiteGalleryPost")
    WebsiteGalleryPostItem = apps.get_model("operations", "WebsiteGalleryPostItem")

    layout_map = {
        "grid": "split",
        "feature": "split",
        "stack": "wide",
    }
    accent_map = {"gold": "gold", "blue": "blue", "navy": "navy"}

    publications = (
        MediaPublication.objects.using(database)
        .filter(destination="website", asset__project__isnull=False)
        .select_related("asset__project")
        .order_by("asset_id")
    )
    for publication in publications.iterator():
        asset = publication.asset
        project = asset.project
        layout = layout_map.get(project.gallery_layout, "split")
        accent = accent_map.get(project.gallery_accent, "gold")
        post = WebsiteGalleryPost.objects.using(database).create(
            id=uuid.uuid4(),
            project_id=project.pk,
            title=publication.title or asset.title,
            caption=publication.caption,
            label=publication.label,
            show_client_name=publication.show_client_name,
            layout=layout,
            accent=accent,
            sort_order=publication.sort_order,
            published_at=publication.published_at,
        )
        WebsiteGalleryPostItem.objects.using(database).create(
            post_id=post.pk,
            asset_id=asset.pk,
            sort_order=0,
            is_in_draft=True,
        )

        if publication.published_at:
            live_data = publication.published_data or {}
            show_client_name = bool(
                publication.show_client_name
                and project.show_client_name
                and project.client_id
            )
            post.published_data = {
                "title": live_data.get("title") or publication.title or asset.title,
                "caption": live_data.get("caption", publication.caption),
                "label": live_data.get("label", publication.label),
                "show_client_name": show_client_name,
                "client_name": live_data.get("client_name", "") if show_client_name else "",
                "layout": layout,
                "accent": accent,
                "sort_order": publication.sort_order,
                "slides": [{"asset_id": str(asset.pk), "sort_order": 0}],
            }
            post.save(update_fields=["published_data"])


def remove_migrated_posts(apps, schema_editor):
    database = schema_editor.connection.alias
    apps.get_model("operations", "WebsiteGalleryPost").objects.using(database).all().delete()


class Migration(migrations.Migration):
    dependencies = [("operations", "0034_rename_media_and_content_labels")]

    operations = [
        migrations.CreateModel(
            name="WebsiteGalleryPost",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=180)),
                ("caption", models.TextField(blank=True)),
                ("label", models.CharField(blank=True, max_length=60)),
                ("show_client_name", models.BooleanField(default=False)),
                (
                    "layout",
                    models.CharField(
                        choices=[("split", "Story split"), ("wide", "Wide media"), ("compact", "Compact feature")],
                        default="split",
                        max_length=16,
                    ),
                ),
                (
                    "accent",
                    models.CharField(
                        choices=[("gold", "Coast gold"), ("blue", "Brand blue"), ("navy", "Deep navy"), ("sage", "Coastal sage")],
                        default="gold",
                        max_length=16,
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("published_data", models.JSONField(blank=True, default=dict)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="website_gallery_posts",
                        to="operations.project",
                    ),
                ),
            ],
            options={"ordering": ["sort_order", "created_at", "pk"]},
        ),
        migrations.CreateModel(
            name="WebsiteGalleryPostItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("is_in_draft", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "asset",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="website_post_item",
                        to="operations.mediaasset",
                    ),
                ),
                (
                    "post",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="operations.websitegallerypost",
                    ),
                ),
            ],
            options={"ordering": ["sort_order", "created_at", "pk"]},
        ),
        migrations.RunPython(migrate_website_publications, remove_migrated_posts),
    ]
