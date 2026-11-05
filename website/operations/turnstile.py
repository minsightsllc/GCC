from __future__ import annotations

import json
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils.translation import gettext_lazy as _


TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_ERROR_MESSAGE = _("Complete the security check and try again.")
MOBILE_WEBVIEW_USER_AGENT_MARKER = "GrandCoastMobile/"
MOBILE_WEBVIEW_HEADER = "HTTP_X_GRAND_COAST_MOBILE"
MOBILE_WEBVIEW_SESSION_KEY = "gcc_mobile_webview"


def is_mobile_webview(request):
    if request is None:
        return False
    marked_request = request.META.get(MOBILE_WEBVIEW_HEADER) == "1"
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    marked_request = marked_request or MOBILE_WEBVIEW_USER_AGENT_MARKER in user_agent
    session = getattr(request, "session", None)
    if marked_request:
        mobile_owner_route = (
            request.path.rstrip("/") == "/accounts/login"
            or request.path.startswith("/accounts/mobile-owner/")
        )
        mobile_owner_marker = str(
            request.GET.get("mobile") or request.POST.get("mobile") or ""
        ).strip() == "1"
        if (
            session is not None
            and mobile_owner_route
            and mobile_owner_marker
            and getattr(settings, "GCC_MOBILE_OWNER_ACCESS_ENABLED", False)
        ):
            session[MOBILE_WEBVIEW_SESSION_KEY] = True
        return True
    return bool(session is not None and session.get(MOBILE_WEBVIEW_SESSION_KEY))


def is_mobile_owner_login(request):
    """Return whether this is the explicitly marked mobile owner login flow.

    The marker selects the authentication flow; it is not an authorization
    boundary.  Project, financial, document, and administration access still
    use Django's normal server-side authorization checks.
    """
    if request is None or not is_mobile_webview(request):
        return False
    marker = str(
        request.GET.get("mobile") or request.POST.get("mobile") or ""
    ).strip()
    return bool(
        marker == "1"
        and getattr(settings, "GCC_MOBILE_OWNER_ACCESS_ENABLED", False)
    )


def get_turnstile_site_key(request=None):
    if not getattr(settings, "GCC_TURNSTILE_ENABLED", True):
        return ""
    if is_mobile_webview(request) and getattr(
        settings,
        "GCC_MOBILE_TURNSTILE_BYPASS_ENABLED",
        False,
    ):
        return ""
    site_key = getattr(settings, "CLOUDFLARE_TURNSTILE_SITE_KEY", "") or ""
    secret_key = getattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "") or ""
    return site_key if site_key and secret_key else ""


def verify_turnstile_request(request, expected_action=None):
    """
    Validate the single-use Turnstile token submitted by a website form.

    The legacy native WebView exemption is development-only.  Production
    requests must provide a valid Turnstile token even when a caller spoofs
    the mobile user-agent or header.
    """
    if request is None:
        return True
    if not getattr(settings, "GCC_TURNSTILE_ENABLED", True):
        return bool(getattr(settings, "GCC_TURNSTILE_ALLOW_MISSING", False))
    if (
        is_mobile_webview(request)
        and getattr(settings, "GCC_MOBILE_TURNSTILE_BYPASS_ENABLED", False)
    ):
        return True

    site_key = get_turnstile_site_key(request)
    secret_key = getattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "") or ""
    if not site_key or not secret_key:
        return bool(getattr(settings, "GCC_TURNSTILE_ALLOW_MISSING", False))

    token = request.POST.get("cf-turnstile-response", "").strip()
    if not token:
        return False

    payload = {
        "secret": secret_key,
        "response": token,
    }
    remote_ip = request.META.get("REMOTE_ADDR")
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        verification_request = Request(
            TURNSTILE_VERIFY_URL,
            data=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(verification_request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, TypeError, ValueError, URLError):
        return False

    if not isinstance(result, dict) or not result.get("success"):
        return False

    returned_action = result.get("action")
    if expected_action and returned_action and returned_action != expected_action:
        return False

    return True
