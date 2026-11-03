from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .google_reviews import (
    GoogleReviewsConfigurationError,
    GoogleReviewsFetchError,
    fetch_google_reviews,
    sync_google_reviews,
)
from .models import GoogleReview, SiteSettings
from .test_helpers import GatedTestClient as DjangoClient


class FakeGoogleResponse:
    def __init__(self, payload):
        import json

        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


def google_payload(review_name="places/test-place/reviews/one"):
    return {
        "displayName": {"text": "Grand Coast Construction Inc."},
        "rating": 4.8,
        "userRatingCount": 42,
        "googleMapsUri": "https://maps.google.com/?cid=grand-coast",
        "reviews": [
            {
                "name": review_name,
                "rating": 5,
                "publishTime": "2026-01-20T12:30:00Z",
                "text": {"text": "Thoughtful, organized, and excellent work."},
                "authorAttribution": {
                    "displayName": "Maya Thompson",
                    "uri": "https://www.google.com/maps/contrib/maya",
                    "photoUri": "https://lh3.googleusercontent.com/maya",
                },
                "googleMapsUri": "https://www.google.com/maps/reviews/one",
            },
            {
                "name": "places/test-place/reviews/two",
                "rating": 4,
                "publishTime": "2025-11-04T09:00:00Z",
                "originalText": {"text": "Clear communication from start to finish."},
                "authorAttribution": {"displayName": "Jordan Lee"},
            },
        ],
    }


@override_settings(
    GCC_GOOGLE_PLACES_API_KEY="server-test-key",
    GCC_GOOGLE_PLACE_ID="test-place",
    GCC_GOOGLE_PLACES_TIMEOUT_SECONDS=11,
)
class GoogleReviewServiceTests(TestCase):
    def test_fetch_parses_reviews_and_uses_google_field_mask(self):
        with patch("operations.google_reviews.urlopen") as opener:
            opener.return_value = FakeGoogleResponse(google_payload())
            result = fetch_google_reviews()

        self.assertEqual(result["display_name"], "Grand Coast Construction Inc.")
        self.assertEqual(result["rating"], Decimal("4.8"))
        self.assertEqual(result["review_count"], 42)
        self.assertEqual(len(result["reviews"]), 2)
        self.assertEqual(result["reviews"][0]["author_name"], "Maya Thompson")
        self.assertEqual(result["reviews"][0]["google_maps_uri"], "https://www.google.com/maps/reviews/one")
        self.assertEqual(
            result["reviews"][1]["review_text"],
            "Clear communication from start to finish.",
        )
        self.assertEqual(
            result["reviews"][0]["publish_time"],
            datetime(2026, 1, 20, 12, 30, tzinfo=datetime_timezone.utc),
        )

        request = opener.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, "https://places.googleapis.com/v1/places/test-place")
        self.assertEqual(headers["x-goog-api-key"], "server-test-key")
        self.assertEqual(
            headers["x-goog-fieldmask"],
            "displayName,rating,userRatingCount,reviews,googleMapsUri",
        )

    def test_sync_replaces_cached_reviews_and_updates_summary(self):
        old = GoogleReview.objects.create(
            provider_review_id="places/old/reviews/old",
            place_id="old-place",
            author_name="Old reviewer",
            rating=Decimal("3.0"),
        )
        payload = fetch_google_reviews
        with patch(
            "operations.google_reviews.fetch_google_reviews",
            return_value=payload(api_key="server-test-key", place_id="test-place", opener=lambda *args, **kwargs: FakeGoogleResponse(google_payload())),
        ):
            result = sync_google_reviews()

        self.assertEqual(result["review_count_cached"], 2)
        self.assertFalse(GoogleReview.objects.filter(pk=old.pk).exists())
        self.assertEqual(
            list(GoogleReview.objects.values_list("provider_review_id", flat=True)),
            [
                "places/test-place/reviews/one",
                "places/test-place/reviews/two",
            ],
        )
        settings = SiteSettings.objects.get(pk=1)
        self.assertEqual(settings.google_places_display_name, "Grand Coast Construction Inc.")
        self.assertEqual(settings.google_places_rating, Decimal("4.8"))
        self.assertEqual(settings.google_places_review_count, 42)
        self.assertIsNotNone(settings.google_reviews_synced_at)

    def test_fetch_failure_preserves_existing_cache(self):
        review = GoogleReview.objects.create(
            provider_review_id="places/test-place/reviews/kept",
            place_id="test-place",
            author_name="Kept reviewer",
            rating=Decimal("5.0"),
            review_text="Keep this while Google is unavailable.",
        )
        with patch(
            "operations.google_reviews.fetch_google_reviews",
            side_effect=GoogleReviewsFetchError("temporary outage"),
        ):
            with self.assertRaises(GoogleReviewsFetchError):
                sync_google_reviews()
        self.assertTrue(GoogleReview.objects.filter(pk=review.pk).exists())
        self.assertEqual(GoogleReview.objects.get(pk=review.pk).review_text, review.review_text)

    @override_settings(GCC_GOOGLE_PLACES_API_KEY="", GCC_GOOGLE_PLACE_ID="")
    def test_missing_configuration_is_safe(self):
        with self.assertRaises(GoogleReviewsConfigurationError):
            fetch_google_reviews()


class GoogleReviewViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_superuser(
            username="reviews-owner",
            email="reviews-owner@example.com",
            password="owner-password",
        )
        cls.employee = user_model.objects.create_user(
            username="reviews-employee",
            email="reviews-employee@example.com",
            password="employee-password",
            is_staff=True,
        )

    def setUp(self):
        self.browser = DjangoClient()

    @override_settings(
        GCC_GOOGLE_PLACES_API_KEY="server-test-key",
        GCC_GOOGLE_PLACE_ID="test-place",
    )
    @patch("operations.views.sync_google_reviews")
    def test_owner_can_sync_and_employee_cannot(self, sync):
        sync.return_value = {
            "display_name": "Grand Coast Construction Inc.",
            "review_count_cached": 2,
        }
        self.browser.force_login(self.owner)
        response = self.browser.post(reverse("operations:google-reviews-sync"))
        self.assertRedirects(
            response,
            reverse("operations:dashboard-section", kwargs={"section": "content"}) + "#publish-page-home",
        )
        sync.assert_called_once()

        self.browser.force_login(self.employee)
        response = self.browser.post(reverse("operations:google-reviews-sync"))
        self.assertEqual(response.status_code, 403)

    def test_homepage_renders_cached_reviews_and_empty_state_is_safe(self):
        response = self.browser.get(reverse("operations:home"))
        self.assertNotContains(response, "data-google-reviews-carousel")

        GoogleReview.objects.create(
            provider_review_id="places/test-place/reviews/home",
            place_id="test-place",
            author_name="Maya Thompson",
            rating=Decimal("5.0"),
            review_text="A great experience.",
            google_maps_uri="https://www.google.com/maps/reviews/home",
        )
        response = self.browser.get(reverse("operations:home"))
        self.assertContains(response, "data-google-reviews-carousel")
        self.assertContains(response, "Maya Thompson")
        self.assertContains(response, "Read on Google")
        self.assertContains(response, "https://www.google.com/maps/reviews/home")
        self.assertContains(response, "Reviews are displayed in Google")
        self.assertContains(response, "data-google-reviews-prev")
        self.assertContains(response, "data-google-reviews-next")
