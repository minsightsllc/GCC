"""Media destinations, published snapshots, and audience-specific presentation."""

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from .models import MediaAsset, MediaPublication, WebsiteGalleryPost


def publication_for(asset, destination):
    return next((item for item in asset.publications.all() if item.destination == destination), None)


def website_is_live(asset):
    publication = publication_for(asset, "website")
    return bool(publication and publication.published_at and asset.project_id)


def client_is_live(asset):
    publication = publication_for(asset, "client")
    return bool(publication and publication.published_at and asset.project_id
                and asset.project.client_id and publication.shared_client_id == asset.project.client_id)


def media_presentation(asset, destination, *, preview=False, publication=None):
    publication = publication or publication_for(asset, destination)
    data = (publication.draft_data() if preview else publication.published_data) if publication else {}
    client_name = data.get("client_name", "")
    return {
        "pk": asset.pk, "file": bool(asset.file), "media_type": asset.media_type,
        "display_url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
        "title": data.get("title", ""), "caption": data.get("caption", ""),
        "label": data.get("label", ""),
        "location": data.get("location", "") if destination != "website" else "",
        "client_name": client_name, "sort_order": data.get("sort_order", 0),
    }


def website_post_presentation(post, *, preview=False):
    """Build public-safe copy and slide URLs from one post snapshot."""
    data = post.draft_data() if preview else (post.published_data or {})
    items = {
        str(item.asset_id): item.asset
        for item in post.items.select_related("asset").prefetch_related("asset__publications")
    }
    slides = []
    for slide in data.get("slides", []):
        asset = items.get(str(slide.get("asset_id", "")))
        if not asset or not asset.file:
            continue
        publication = publication_for(asset, MediaPublication.Destination.WEBSITE)
        if not preview and not (publication and publication.published_at):
            continue
        slides.append({
            "asset_id": asset.pk,
            "media_type": asset.media_type,
            "display_url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
            "alt_text": data.get("title") or "Project photo or video",
        })
    return {
        "pk": post.pk,
        "title": data.get("title", ""),
        "caption": data.get("caption", ""),
        "label": data.get("label", ""),
        "client_name": data.get("client_name", "") if data.get("show_client_name") else "",
        "layout": data.get("layout", WebsiteGalleryPost.Layout.SPLIT),
        "accent": data.get("accent", WebsiteGalleryPost.Accent.GOLD),
        "sort_order": data.get("sort_order", 0),
        "slides": slides,
        "is_preview": preview,
    }


def publish(publication):
    """Caller locks the original asset, authorizes the actor, and sends alerts."""
    project = publication.asset.project
    previous_client_id = publication.shared_client_id
    if not publication.title.strip():
        raise ValidationError({"title": "Enter a title before sharing or publishing this file."})
    if publication.destination == "client":
        if not project or not project.client_id:
            raise ValidationError("Connect a client to this project before sharing media.")
        publication.shared_client = project.client
    elif publication.destination == "website":
        if not project:
            raise ValidationError("Assign this media to a project before publishing it to the website.")
    else:
        raise ValidationError("Choose a valid destination before publishing this media.")
    changed = (publication.published_at is None or publication.published_data != publication.draft_data()
               or (publication.destination == "client" and previous_client_id != publication.shared_client_id))
    publication.published_data = publication.draft_data()
    if changed:
        publication.published_at = timezone.now()
    publication.save()
    sync_legacy_visibility(publication.asset)
    return changed


def sync_legacy_visibility(asset):
    """Legacy displays are descriptive only; permissions use publication records."""
    live = set(asset.publications.filter(published_at__isnull=False).values_list("destination", flat=True))
    visibility = MediaAsset.Visibility.PUBLIC if "website" in live else (
        MediaAsset.Visibility.CLIENT if "client" in live else MediaAsset.Visibility.INTERNAL
    )
    MediaAsset.objects.filter(pk=asset.pk).update(visibility=visibility)


def unpublish(publication):
    publication.published_at = None
    publication.published_data = {}
    publication.shared_client = None
    publication.save()
    sync_legacy_visibility(publication.asset)
