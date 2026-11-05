from django.utils import translation


class AccountLanguageMiddleware:
    """Apply the signed-in user language across web and mobile sessions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from .models import UserLanguagePreference

            language = UserLanguagePreference.objects.filter(user_id=user.pk).values_list("language", flat=True).first()
            if language is None:
                language = "en"
                UserLanguagePreference.objects.get_or_create(user_id=user.pk, defaults={"language": language})
            if language not in {"en", "es"}:
                language = "en"
            translation.activate(language)
            request.LANGUAGE_CODE = language
        return self.get_response(request)
