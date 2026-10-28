"""Media destinations, published snapshots, and audience-specific presentation."""

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from .models import MediaAsset, MediaPublication


def publication_for(asset, destination):
    return next((item for item in asset.publications.all() if item.destination == destination), None)


def website_is_live(asset):
    publication = publication_for(asset, "website")
    return bool(publication and publication.published_at and asset.project_id and asset.project.is_published)


def client_is_live(asset):
    publication = publication_for(asset, "client")
    return bool(publication and publication.published_at and asset.project_id
                and asset.project.client_id and publication.shared_client_id == asset.project.client_id)


def media_presentation(asset, destination, *, preview=False, publication=None):
    publication = publication or publication_for(asset, destination)
    data = (publication.draft_data() if preview else publication.published_data) if publication else {}
    return {
        "pk": asset.pk, "file": bool(asset.file), "media_type": asset.media_type,
        "display_url": reverse("operations:media-file", kwargs={"pk": asset.pk}),
        "title": data.get("title", ""), "caption": data.get("caption", ""),
        "label": data.get("label", ""), "location": data.get("location", ""),
        "client_name": data.get("client_name", ""), "sort_order": data.get("sort_order", 0),
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
        if not project.is_published:
            raise ValidationError("Publish the project page in Projects before publishing its media.")
        # Existing published galleries remain public with a generic project
        # title. Allow their existing media to be updated without forcing a
        # project-title change; newly published media still needs one.
        if not project.public_title.strip() and not publication.published_at:
            raise ValidationError(
                "Add a public project title in Projects before publishing new media. "
                "The title on this form only names the photo or video."
            )
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
