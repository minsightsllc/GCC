import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import GoogleReview, SiteSettings


logger = logging.getLogger(__name__)

GOOGLE_PLACES_ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
GOOGLE_PLACES_FIELD_MASK = "displayName,rating,userRatingCount,reviews,googleMapsUri"


class GoogleReviewsError(Exception):
    """Base error for Google review synchronization."""


class GoogleReviewsConfigurationError(GoogleReviewsError):
    """Raised when the server is not configured for Google Places."""


class GoogleReviewsFetchError(GoogleReviewsError):
    """Raised when Google Places cannot be fetched or parsed."""


def _review_text(value):
    if not isinstance(value, dict):
        return ""
    return str(value.get("text") or "").strip()


def _parse_publish_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Ignoring an invalid Google review publish timestamp.")
        return None


def _parse_rating(value):
    try:
        rating = Decimal(str(value)).quantize(Decimal("0.1"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.0")
    return max(Decimal("0.0"), min(Decimal("5.0"), rating))


def _place_display_name(payload):
    display_name = payload.get("displayName")
    return _review_text(display_name) if isinstance(display_name, dict) else str(display_name or "").strip()


def _normalize_review(review, fallback_maps_uri, display_order):
    if not isinstance(review, dict):
        return None
    provider_review_id = str(review.get("name") or "").strip()
    if not provider_review_id:
        return None
    author = review.get("authorAttribution") or {}
    if not isinstance(author, dict):
        author = {}
    author_name = str(author.get("displayName") or "Google user").strip()[:180] or "Google user"
    return {
        "provider_review_id": provider_review_id[:512],
        "author_name": author_name,
        "author_uri": str(author.get("uri") or "").strip(),
        "author_photo_uri": str(author.get("photoUri") or "").strip(),
        "rating": _parse_rating(review.get("rating")),
        "review_text": (
            _review_text(review.get("text"))
            or _review_text(review.get("originalText"))
        ),
        "publish_time": _parse_publish_time(review.get("publishTime")),
        "google_maps_uri": str(review.get("googleMapsUri") or fallback_maps_uri or "").strip(),
        "display_order": display_order,
    }


def _request_timeout(timeout):
    if timeout is None:
        timeout = getattr(settings, "GCC_GOOGLE_PLACES_TIMEOUT_SECONDS", 8)
    try:
        return max(1, float(timeout))
    except (TypeError, ValueError):
        return 8.0


def _google_error_summary(body, *, http_status=None, api_key=""):
    """Extract a safe, concise diagnostic from a Google API error response."""
    if isinstance(body, dict):
        payload = body
    else:
        try:
            payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            payload = {}

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        provider_status = " ".join(str(error.get("status") or "").split())[:80]
        message = " ".join(str(error.get("message") or "").split())
        error_code = error.get("code")
    else:
        provider_status = ""
        message = " ".join(str(error or "").split())
        error_code = None

    if api_key and message:
        message = message.replace(api_key, "[redacted]")
    message = message[:360]

    status_code = http_status or error_code
    status_code = " ".join(str(status_code or "").split())[:40]
    status = f"HTTP {status_code}" if status_code else "request rejected"
    if provider_status:
        status += f", {provider_status}"
    summary = f"Google Places rejected the request ({status})"
    if message:
        summary += f": {message}"
    return summary if summary.endswith((".", "!", "?")) else summary + "."


def fetch_google_reviews(api_key=None, place_id=None, timeout=None, opener=None):
    """Fetch and normalize the configured place's Google reviews."""
    api_key = (api_key if api_key is not None else getattr(settings, "GCC_GOOGLE_PLACES_API_KEY", "")).strip()
    place_id = (place_id if place_id is not None else getattr(settings, "GCC_GOOGLE_PLACE_ID", "")).strip()
    if not api_key or not place_id:
        raise GoogleReviewsConfigurationError(
            "Google reviews are not configured. Set GCC_GOOGLE_PLACES_API_KEY and GCC_GOOGLE_PLACE_ID."
        )

    request = Request(
        GOOGLE_PLACES_ENDPOINT.format(place_id=quote(place_id, safe="")),
        headers={
            "Accept": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": GOOGLE_PLACES_FIELD_MASK,
        },
        method="GET",
    )
    opener = opener or urlopen
    try:
        with opener(request, timeout=_request_timeout(timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_body = exc.read(16_384)
        except OSError:
            error_body = b""
        finally:
            exc.close()
        message = _google_error_summary(
            error_body,
            http_status=exc.code,
            api_key=api_key,
        )
        logger.warning("Google Places review fetch failed: %s", message)
        raise GoogleReviewsFetchError(message) from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "Google Places review fetch failed (%s): %s",
            exc.__class__.__name__,
            exc,
        )
        raise GoogleReviewsFetchError("Google reviews could not be fetched right now.") from exc

    if isinstance(payload, dict) and payload.get("error"):
        message = _google_error_summary(payload, api_key=api_key)
        logger.warning("Google Places review fetch failed: %s", message)
        raise GoogleReviewsFetchError(message)
    if not isinstance(payload, dict):
        logger.warning("Google Places review fetch returned a non-object response.")
        raise GoogleReviewsFetchError("Google reviews returned an invalid response.")

    fallback_maps_uri = str(payload.get("googleMapsUri") or "").strip()
    reviews = []
    for position, review in enumerate(payload.get("reviews") or []):
        normalized = _normalize_review(review, fallback_maps_uri, position)
        if normalized is not None:
            reviews.append(normalized)
    return {
        "place_id": place_id,
        "display_name": _place_display_name(payload)[:180],
        "rating": _parse_rating(payload.get("rating")),
        "review_count": max(0, int(payload.get("userRatingCount") or 0)),
        "google_maps_uri": fallback_maps_uri,
        "reviews": reviews[:5],
    }


def sync_google_reviews():
    """Fetch first, then atomically replace the cached review set."""
    payload = fetch_google_reviews()
    synced_at = timezone.now()
    with transaction.atomic():
        GoogleReview.objects.all().delete()
        GoogleReview.objects.bulk_create(
            [
                GoogleReview(
                    provider_review_id=review["provider_review_id"],
                    place_id=payload["place_id"],
                    author_name=review["author_name"],
                    author_uri=review["author_uri"],
                    author_photo_uri=review["author_photo_uri"],
                    rating=review["rating"],
                    review_text=review["review_text"],
                    publish_time=review["publish_time"],
                    google_maps_uri=review["google_maps_uri"],
                    display_order=review["display_order"],
                    fetched_at=synced_at,
                )
                for review in payload["reviews"]
            ]
        )
        site_settings, _ = SiteSettings.objects.get_or_create(pk=1)
        site_settings.google_places_display_name = payload["display_name"]
        site_settings.google_places_rating = payload["rating"]
        site_settings.google_places_review_count = payload["review_count"]
        site_settings.google_reviews_synced_at = synced_at
        site_settings.save(
            update_fields=[
                "google_places_display_name",
                "google_places_rating",
                "google_places_review_count",
                "google_reviews_synced_at",
                "updated_at",
            ]
        )
    return {
        **payload,
        "synced_at": synced_at,
        "review_count_cached": len(payload["reviews"]),
    }
