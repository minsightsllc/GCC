from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from .models import ProcessStep, Project, Service, UserLanguagePreference, WebsiteGalleryPost
from .publishing import website_post_presentation
from .views import _translated_notice


class LanguagePreferenceTests(TestCase):
    def test_anonymous_language_choice_uses_cookie_and_keeps_page_url(self):
        target = reverse("operations:services")
        response = self.client.post(
            reverse("operations:set-language"),
            {"language": "es", "next": target},
        )

        self.assertRedirects(response, target, fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "es")

        page = self.client.get(target)
        self.assertEqual(page.context["LANGUAGE_CODE"], "es")
        self.assertContains(page, 'lang="es"')
        self.assertContains(page, "Servicios")
        self.assertNotIn("?language=es", page.request["PATH_INFO"])

    def test_public_contact_form_labels_and_required_errors_are_spanish(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "es"

        page = self.client.post(reverse("operations:contact"), {})

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Nombre")
        self.assertContains(page, "Este campo es obligatorio.")

    def test_signed_in_language_is_persisted_and_overrides_a_stale_cookie(self):
        user = get_user_model().objects.create_user(username="language-user", password="test-password")
        self.client.force_login(user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "es"

        first_page = self.client.get(reverse("operations:services"))
        self.assertEqual(first_page.context["LANGUAGE_CODE"], "en")
        preference = UserLanguagePreference.objects.get(user=user)
        self.assertEqual(preference.language, "en")

        target = reverse("operations:services")
        response = self.client.post(
            reverse("operations:set-language"),
            {"language": "es", "next": target},
        )
        self.assertRedirects(response, target, fetch_redirect_response=False)
        preference.refresh_from_db()
        self.assertEqual(preference.language, "es")

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        page = self.client.get(target)
        self.assertEqual(page.context["LANGUAGE_CODE"], "es")
        self.assertContains(page, "Servicios")

    def test_invalid_language_is_rejected(self):
        response = self.client.post(
            reverse("operations:set-language"),
            {"language": "fr", "next": reverse("operations:home")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)

    def test_public_service_and_process_copy_falls_back_to_english(self):
        service = Service.objects.create(
            slug="language-service",
            title="English service title",
            description="English service description",
            title_es="Titulo del servicio",
        )
        ProcessStep.objects.create(
            key="language-process",
            title="English process title",
            description="English process description",
            title_es="Titulo del proceso",
        )
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "es"

        page = self.client.get(reverse("operations:services"))

        localized_service = next(item for item in page.context["services"] if item.pk == service.pk)
        self.assertEqual(localized_service.display_title, "Titulo del servicio")
        self.assertEqual(localized_service.display_description, "English service description")
        process_page = self.client.get(reverse("operations:process"))
        localized_step = next(item for item in process_page.context["process_steps"] if item.key == "language-process")
        self.assertEqual(localized_step.display_title, "Titulo del proceso")
        self.assertEqual(localized_step.display_description, "English process description")

    def test_public_project_and_gallery_copy_use_spanish_with_english_fallback(self):
        project = Project.objects.create(
            title="Internal project title",
            public_title="English public title",
            public_location="Ventura, CA",
            public_summary="English public summary",
            public_title_es="Título público",
            public_location_es="Ventura, California",
            is_published=True,
        )
        post = WebsiteGalleryPost.objects.create(
            project=project,
            title="English post title",
            caption="English post caption",
            published_at=timezone.now(),
            published_data={
                "title": "English post title",
                "title_es": "Título de la publicación",
                "caption": "English post caption",
                "caption_es": "",
                "label": "Project update",
                "label_es": "",
                "slides": [],
            },
        )
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "es"

        page = self.client.get(reverse("operations:projects"))
        localized_project = next(item for item in page.context["public_projects"] if item.pk == project.pk)
        self.assertEqual(localized_project.display_public_title, "Título público")
        self.assertEqual(localized_project.display_public_location, "Ventura, California")
        self.assertEqual(localized_project.display_public_summary, "English public summary")

        presentation = website_post_presentation(post, language="es")
        self.assertEqual(presentation["title"], "Título de la publicación")
        self.assertEqual(presentation["caption"], "English post caption")
        self.assertEqual(presentation["label"], "Project update")

    def test_dynamic_system_notice_is_translated_but_keeps_record_name(self):
        with override("es"):
            notice = _translated_notice(
                "%(lead)s was added to the lead pipeline.",
                lead="Morgan Rivera",
            )
        self.assertEqual(notice, "Morgan Rivera se agregó al proceso de clientes potenciales.")
