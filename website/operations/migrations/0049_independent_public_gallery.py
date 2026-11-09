import uuid
from pathlib import PurePosixPath

from django.db import migrations, models


def detach_public_gallery_from_projects(apps, schema_editor):
    """Give existing public posts independent media records before removing project links."""
    database = schema_editor.connection.alias
    Project = apps.get_model("operations", "Project")
    MediaAsset = apps.get_model("operations", "MediaAsset")
    MediaPublication = apps.get_model("operations", "MediaPublication")
    StorageTrash = apps.get_model("operations", "StorageTrash")
    WebsiteGalleryPost = apps.get_model("operations", "WebsiteGalleryPost")
    WebsiteGalleryPostItem = apps.get_model("operations", "WebsiteGalleryPostItem")

    posts = WebsiteGalleryPost.objects.using(database).all().order_by("created_at", "pk")
    for post in posts.iterator():
        project = Project.objects.using(database).filter(pk=post.project_id).first()
        snapshot = dict(post.published_data or {})
        old_live_slides = {
            str(slide.get("asset_id"))
            for slide in snapshot.get("slides", [])
            if isinstance(slide, dict) and slide.get("asset_id")
        }

        # Preserve city/region copy, never a private street address. The per-post
        # value takes precedence when an earlier snapshot already had one.
        if not post.location:
            post.location = snapshot.get("location", "") or (project.public_location if project else "")
        if not post.location_es:
            post.location_es = snapshot.get("location_es", "") or (project.public_location_es if project else "")
        post.title_es = post.title_es or snapshot.get("title_es", "")
        post.caption_es = post.caption_es or snapshot.get("caption_es", "")
        post.label_es = post.label_es or snapshot.get("label_es", "")
        if not post.client_name and post.show_client_name:
            post.client_name = snapshot.get("client_name", "")

        project_was_public = bool(project and project.is_published)
        public_snapshot_is_live = bool(post.published_at and project_was_public)
        asset_id_map = {}

        items = list(
            WebsiteGalleryPostItem.objects.using(database)
            .filter(post_id=post.pk)
            .order_by("sort_order", "created_at", "pk")
        )
        for item in items:
            source_asset = MediaAsset.objects.using(database).filter(pk=item.asset_id).first()
            if source_asset is None:
                continue

            website_publication = MediaPublication.objects.using(database).filter(
                asset_id=source_asset.pk,
                destination="website",
            ).first()
            cloned_asset_id = uuid.uuid4()
            file_name = getattr(source_asset.file, "name", source_asset.file) or ""
            file_is_restricted = bool(
                file_name
                and StorageTrash.objects.using(database).filter(
                    storage_alias="default",
                    storage_name=file_name,
                ).exists()
            )
            slide_is_live = str(source_asset.pk) in old_live_slides
            cloned_publication_is_live = bool(
                public_snapshot_is_live
                and slide_is_live
                and website_publication
                and website_publication.published_at
                and file_name
                and not file_is_restricted
            )

            # Copy legacy project-owned bytes into the Public Content storage
            # area. Keeping the same storage key would make trashing or purging
            # the operational project file hide or delete the public post too.
            public_file_name = ""
            if file_name and not file_is_restricted:
                storage = MediaAsset._meta.get_field("file").storage
                suffix = PurePosixPath(file_name).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}:
                    suffix = ""
                target_name = f"Public Content/gallery/media/legacy/{post.pk}/{source_asset.pk}{suffix}"
                if storage.exists(target_name):
                    public_file_name = target_name
                else:
                    with storage.open(file_name, "rb") as source:
                        public_file_name = storage.save(target_name, source)

            clone = MediaAsset.objects.using(database).create(
                id=cloned_asset_id,
                project_id=None,
                site_visit_id=None,
                title=source_asset.title,
                file=public_file_name,
                media_type=source_asset.media_type,
                context=source_asset.context,
                visibility="public" if cloned_publication_is_live else "internal",
                caption=source_asset.caption,
                fallback_image=source_asset.fallback_image,
                uploaded_by_id=source_asset.uploaded_by_id,
                created_at=source_asset.created_at,
                updated_at=source_asset.updated_at,
            )
            asset_id_map[str(source_asset.pk)] = str(clone.pk)

            if website_publication:
                publication_values = {
                    "asset_id": clone.pk,
                    "destination": "website",
                    "title": website_publication.title,
                    "caption": website_publication.caption,
                    "label": website_publication.label,
                    "location": website_publication.location,
                    "show_client_name": False,
                    "sort_order": website_publication.sort_order,
                    "published_data": website_publication.published_data if cloned_publication_is_live else {},
                    "published_at": website_publication.published_at if cloned_publication_is_live else None,
                    "shared_client_id": None,
                }
                for field_name in ("title_es", "caption_es", "label_es"):
                    if hasattr(website_publication, field_name):
                        publication_values[field_name] = getattr(website_publication, field_name)
                MediaPublication.objects.using(database).create(**publication_values)
                website_publication.delete(using=database)
                remaining_client_publication = MediaPublication.objects.using(database).filter(
                    asset_id=source_asset.pk,
                    destination="client",
                    published_at__isnull=False,
                ).exists()
                source_asset.visibility = "client" if remaining_client_publication else "internal"
                source_asset.save(using=database, update_fields=["visibility"])

            item.asset_id = clone.pk
            item.save(using=database, update_fields=["asset"])

        for slide in snapshot.get("slides", []):
            if isinstance(slide, dict) and str(slide.get("asset_id", "")) in asset_id_map:
                slide["asset_id"] = asset_id_map[str(slide["asset_id"])]

        if not public_snapshot_is_live:
            post.published_at = None
            post.published_data = {}
        else:
            snapshot["location"] = post.location
            snapshot["location_es"] = post.location_es
            snapshot["client_name"] = post.client_name
            post.published_data = snapshot
        post.project_id = None
        post.save(
            using=database,
            update_fields=[
                "location", "location_es", "title_es", "caption_es", "label_es",
                "client_name", "published_at", "published_data", "project",
            ],
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0048_archive_projects_with_all_milestones_complete")]

    operations = [
        migrations.AddField(
            model_name="websitegallerypost",
            name="location",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="websitegallerypost",
            name="location_es",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="websitegallerypost",
            name="client_name",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AlterField(
            model_name="websitegallerypost",
            name="project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="website_gallery_posts",
                to="operations.project",
            ),
        ),
        migrations.RunPython(detach_public_gallery_from_projects, migrations.RunPython.noop),
        migrations.RemoveField(model_name="websitegallerypost", name="show_client_name"),
        migrations.RemoveField(model_name="websitegallerypost", name="project"),
    ]
