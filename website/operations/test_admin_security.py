from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core import management
from django.core import mail
from django.test import Client as DjangoClient, RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import (
    AdminAccessBlock,
    AdminRecoveryToken,
    AdminSecurityEvent,
    AdminSecurityProfile,
    Activity,
)
from .security import client_ip, record_admin_security_event, retry_pending_admin_security_emails


@override_settings(GCC_TURNSTILE_ENABLED=False)
class PrivateAdminSecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = get_user_model().objects.create_superuser(
            username="security-owner",
            email="security-owner@gcc.example.com",
            password="security-owner-pass",
        )

    def setUp(self):
        self.browser = DjangoClient()
        self.profile, _ = AdminSecurityProfile.objects.get_or_create(user=self.admin_user)
        self.profile.pin_enabled = False
        self.profile.pin_hash = ""
        self.profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])
        TOTPDevice.objects.filter(user=self.admin_user).delete()

    def enter_password_login(self):
        gate = self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )
        self.assertRedirects(gate, reverse("admin:login"))
        login_response = self.browser.post(
            reverse("admin:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )
        self.assertRedirects(login_response, reverse("admin:index"))

    def test_legacy_admin_is_404_and_direct_login_cannot_bypass_gate(self):
        self.assertEqual(self.browser.get("/admin/").status_code, 404)
        response = self.browser.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            reverse('admin:access') + '?next=%2Fgccad%2Flogin%2F',
        )

    def test_pin_enabled_requires_pin_before_password_login(self):
        self.profile.pin_enabled = True
        self.profile.pin_hash = make_password("123456")
        self.profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])

        identifier = self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.email},
        )
        self.assertEqual(identifier.status_code, 302)
        self.assertIn(reverse("admin:access"), identifier["Location"])

        wrong_pin = self.browser.post(
            reverse("admin:access"),
            {"pin": "000000"},
        )
        self.assertEqual(wrong_pin.status_code, 200)
        self.assertContains(wrong_pin, "That PIN could not be verified.")

        correct_pin = self.browser.post(
            reverse("admin:access"),
            {"pin": "123456"},
        )
        self.assertRedirects(correct_pin, reverse("admin:login"))
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        login_response = self.browser.post(
            reverse("admin:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )
        self.assertRedirects(login_response, reverse("admin:index"))
        self.assertEqual(self.browser.session.get("_auth_user_id"), str(self.admin_user.pk))

    def test_gccad_entry_opens_branded_login_without_pin(self):
        response = self.browser.get(reverse('admin:index'), follow=True)

        self.assertContains(response, 'Welcome back.')
        self.assertNotContains(response, 'Before we begin.')
        self.assertNotContains(response, 'Enter your PIN.')

    def test_gccad_entry_starts_with_pin_when_admin_pin_is_enabled(self):
        self.profile.pin_enabled = True
        self.profile.pin_hash = make_password('123456')
        self.profile.save(update_fields=['pin_enabled', 'pin_hash', 'updated_at'])

        response = self.browser.get(reverse('admin:index'), follow=True)

        self.assertContains(response, 'Secure access')
        self.assertContains(response, 'Enter your PIN.')
        self.assertNotContains(response, 'Welcome back.')
        self.assertEqual(
            self.browser.session.get('gccad_gate_pending_pin'),
            str(self.admin_user.pk),
        )
    @override_settings(
        CLOUDFLARE_TURNSTILE_SITE_KEY="",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="",
    )
    def test_public_login_rejects_superuser_without_creating_session(self):
        response = self.browser.post(
            reverse("operations:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password.")
        self.assertNotContains(response, "%(username)s")
        self.assertIsNone(self.browser.session.get("_auth_user_id"))
        self.assertNotContains(response, "gccad")

    @staticmethod
    def mobile_headers():
        return {
            "HTTP_USER_AGENT": "GrandCoastMobile/1.0",
            "HTTP_X_GRAND_COAST_MOBILE": "1",
        }

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=False,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_login_is_disabled_by_default(self):
        response = self.browser.post(
            reverse("operations:login") + "?mobile=1",
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
            **self.mobile_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password.")
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_requires_mobile_marker_and_webview(self):
        missing_query = self.browser.post(
            reverse("operations:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
            **self.mobile_headers(),
        )
        self.assertEqual(missing_query.status_code, 200)
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        browser_request = self.browser.post(
            reverse("operations:login") + "?mobile=1",
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
        )
        self.assertEqual(browser_request.status_code, 200)
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_without_factors_lands_in_command_center(self):
        response = self.browser.post(
            reverse("operations:login") + "?mobile=1",
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
            **self.mobile_headers(),
        )
        self.assertRedirects(response, reverse("operations:dashboard"))
        self.assertEqual(
            self.browser.session.get("_auth_user_id"),
            str(self.admin_user.pk),
        )
        self.assertTrue(self.browser.session.get("gcc_mobile_owner_mode"))
        admin_response = self.browser.get(
            reverse("admin:index"),
            **self.mobile_headers(),
        )
        self.assertRedirects(
            admin_response,
            reverse("operations:dashboard"),
        )
        admin_access_response = self.browser.get(
            reverse("admin:access"),
            **self.mobile_headers(),
        )
        self.assertRedirects(
            admin_access_response,
            reverse("operations:dashboard"),
        )

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_can_log_in_again_after_mobile_logout(self):
        login_url = reverse("operations:login") + "?mobile=1"
        credentials = {
            "username": self.admin_user.username,
            "password": "security-owner-pass",
            "mobile": "1",
        }

        first_login = self.browser.post(
            login_url,
            credentials,
            **self.mobile_headers(),
        )
        self.assertRedirects(first_login, reverse("operations:dashboard"))

        logout_response = self.browser.post(
            reverse("operations:logout"),
            **self.mobile_headers(),
        )
        self.assertRedirects(logout_response, login_url)
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        second_login = self.browser.post(
            login_url,
            credentials,
            **self.mobile_headers(),
        )
        self.assertRedirects(second_login, reverse("operations:dashboard"))
        self.assertEqual(
            self.browser.session.get("_auth_user_id"),
            str(self.admin_user.pk),
        )

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_marker_survives_a_webview_post_without_mobile_headers(self):
        login_url = reverse("operations:login") + "?mobile=1"
        initial_response = self.browser.get(login_url, **self.mobile_headers())
        self.assertEqual(initial_response.status_code, 200)
        self.assertContains(initial_response, 'name="mobile" value="1"')

        response = self.browser.post(
            login_url,
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
        )

        self.assertRedirects(response, reverse("operations:dashboard"))
        self.assertEqual(
            self.browser.session.get("_auth_user_id"),
            str(self.admin_user.pk),
        )

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
        ADMIN_SECURITY_EMAIL_ALERTS_ENABLED=False,
    )
    def test_mobile_owner_pin_is_required_before_session_is_created(self):
        self.profile.pin_enabled = True
        self.profile.pin_hash = make_password("123456")
        self.profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])

        password_response = self.browser.post(
            reverse("operations:login") + "?mobile=1",
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
            **self.mobile_headers(),
        )
        self.assertRedirects(
            password_response,
            reverse("operations:mobile-owner-pin") + "?mobile=1",
        )
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        wrong = self.browser.post(
            reverse("operations:mobile-owner-pin") + "?mobile=1",
            {"pin": "000000", "mobile": "1"},
            **self.mobile_headers(),
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertContains(wrong, "could not be verified")
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        valid = self.browser.post(
            reverse("operations:mobile-owner-pin") + "?mobile=1",
            {"pin": "123456", "mobile": "1"},
            **self.mobile_headers(),
        )
        self.assertRedirects(valid, reverse("operations:dashboard"))
        self.assertEqual(
            self.browser.session.get("_auth_user_id"),
            str(self.admin_user.pk),
        )
        self.assertTrue(
            AdminSecurityEvent.objects.filter(
                user=self.admin_user,
                event_type=AdminSecurityEvent.EventType.PIN_FAILURE,
            ).exists()
        )

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
        ADMIN_SECURITY_EMAIL_ALERTS_ENABLED=False,
    )
    def test_mobile_owner_totp_is_required_before_session_is_created(self):
        device = TOTPDevice.objects.create(
            user=self.admin_user,
            name="Mobile owner test device",
            confirmed=True,
        )
        password_response = self.browser.post(
            reverse("operations:login") + "?mobile=1",
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
                "mobile": "1",
            },
            **self.mobile_headers(),
        )
        self.assertRedirects(
            password_response,
            reverse("operations:mobile-owner-otp") + "?mobile=1",
        )
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        invalid = self.browser.post(
            reverse("operations:mobile-owner-otp") + "?mobile=1",
            {"token": "000000", "mobile": "1"},
            **self.mobile_headers(),
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "could not be verified")
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        # django-otp intentionally throttles a device after a failed token.
        # Replace the disposable device before testing the successful factor.
        device.delete()
        device = TOTPDevice.objects.create(
            user=self.admin_user,
            name="Mobile owner valid test device",
            confirmed=True,
        )
        valid = self.browser.post(
            reverse("operations:mobile-owner-otp") + "?mobile=1",
            {"token": f"{totp(device.bin_key):06d}", "mobile": "1"},
            **self.mobile_headers(),
        )
        self.assertRedirects(valid, reverse("operations:dashboard"))
        self.assertEqual(
            self.browser.session.get("_auth_user_id"),
            str(self.admin_user.pk),
        )

    @override_settings(
        GCC_MOBILE_OWNER_ACCESS_ENABLED=True,
        GCC_TURNSTILE_ENABLED=False,
    )
    def test_mobile_owner_challenge_without_pending_password_redirects_to_login(self):
        response = self.browser.get(
            reverse("operations:mobile-owner-pin") + "?mobile=1",
            **self.mobile_headers(),
        )
        self.assertRedirects(
            response,
            reverse("operations:login") + "?mobile=1",
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_recovery_email_locks_after_five_unknown_emails_and_is_one_time(self):
        self.profile.pin_enabled = True
        self.profile.pin_hash = make_password("111111")
        self.profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])
        device = TOTPDevice.objects.create(
            user=self.admin_user,
            name="Recovery test device",
            confirmed=True,
        )

        for attempt in range(4):
            response = self.browser.post(
                reverse("admin:recovery"),
                {"email": f"wrong-{attempt}@example.com"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "That email does not match an active administrator account.")
            self.assertContains(response, f"You have {4 - attempt} attempt")

        locked = self.browser.post(
            reverse("admin:recovery"),
            {"email": "wrong-final@example.com"},
        )
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Too many attempts.", locked.content.decode())

        self.browser = DjangoClient()
        sent = self.browser.post(
            reverse("admin:recovery"),
            {"email": self.admin_user.email},
        )
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"/gccad/recover/([A-Za-z0-9_-]+)/", mail.outbox[0].body)
        self.assertIsNotNone(match)
        raw_token = match.group(1)
        token = AdminRecoveryToken.objects.get(user=self.admin_user)

        confirm = self.browser.get(
            reverse("admin:recovery-confirm", kwargs={"token": raw_token})
        )
        self.assertEqual(confirm.status_code, 200)
        complete = self.browser.post(
            reverse("admin:recovery-confirm", kwargs={"token": raw_token}),
            {"new_pin": "246810", "new_pin_confirmation": "246810"},
        )
        self.assertEqual(complete.status_code, 200)
        self.assertContains(complete, "Security reset")
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.pin_enabled)
        self.assertTrue(check_password("246810", self.profile.pin_hash))
        self.assertFalse(TOTPDevice.objects.filter(user=self.admin_user).exists())
        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)
        self.assertIsNone(self.browser.session.get("_auth_user_id"))
        self.assertEqual(
            self.browser.get(
                reverse("admin:recovery-confirm", kwargs={"token": raw_token})
            ).status_code,
            410,
        )
        device.delete()

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_recovery_email_accepts_correct_email_before_lockout(self):
        for attempt in range(4):
            response = self.browser.post(
                reverse("admin:recovery"),
                {"email": f"wrong-before-match-{attempt}@example.com"},
            )
            self.assertEqual(response.status_code, 200)

        sent = self.browser.post(
            reverse("admin:recovery"),
            {"email": self.admin_user.email},
        )
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNone(self.browser.session.get("gccad_recovery_attempts"))
        self.assertIsNone(self.browser.session.get("gccad_recovery_locked_until"))

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        CLOUDFLARE_TURNSTILE_SITE_KEY="",
        CLOUDFLARE_TURNSTILE_SECRET_KEY="",
    )
    def test_admin_password_recovery_resets_password_without_changing_security_factors(self):
        page = self.browser.get(reverse("admin:index"), follow=True)
        self.assertContains(page, "Forgot your admin password?")
        reset_page = self.browser.get(reverse("admin:password-reset"))
        self.assertContains(reset_page, "/static/operations/css/unfold.css")
        self.assertContains(reset_page, "gcc-login-form-wrap")

        response = self.browser.post(
            reverse("admin:password-reset"),
            {"email": self.admin_user.email},
        )
        self.assertRedirects(response, reverse("admin:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        reset_url = next(
            line for line in mail.outbox[0].body.splitlines()
            if "/gccad/password-reset/confirm/" in line
        )
        confirm = self.browser.get(reset_url, follow=True)
        self.assertContains(confirm, "Choose a new password.")
        confirm_url = reset_url.replace(reset_url.rstrip("/").rsplit("/", 1)[-1], "set-password")
        reset = self.browser.post(
            confirm_url,
            {
                "new_password1": "admin-new-password-123",
                "new_password2": "admin-new-password-123",
            },
        )
        self.assertRedirects(reset, reverse("admin:password-reset-complete"))
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.check_password("admin-new-password-123"))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.pin_enabled)
        self.assertEqual(self.browser.get(reset_url).status_code, 200)
        self.assertContains(self.browser.get(reset_url), "That link has expired.")

        mail.outbox.clear()
        public_request = self.browser.post(
            reverse("operations:password-reset"),
            {"email": self.admin_user.email},
        )
        self.assertRedirects(public_request, reverse("operations:password-reset-done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_password_recovery_locks_after_five_unknown_emails(self):
        reset_url = reverse("admin:password-reset")

        for attempt in range(4):
            response = self.browser.post(
                reset_url,
                {"email": f"unknown-admin-{attempt}@example.com"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "That email does not match an active administrator account.")
            remaining = 4 - attempt
            self.assertContains(response, f"You have {remaining} attempt")
        self.assertEqual(len(mail.outbox), 0)

        locked = self.browser.post(
            reset_url,
            {"email": "unknown-admin-final@example.com"},
        )
        self.assertEqual(locked.status_code, 429)
        self.assertContains(locked, "Too many password reset attempts.", status_code=429)
        self.assertEqual(self.browser.get(reset_url).status_code, 429)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_password_recovery_accepts_correct_email_before_lockout(self):
        reset_url = reverse("admin:password-reset")
        for attempt in range(4):
            response = self.browser.post(
                reset_url,
                {"email": f"unknown-admin-before-match-{attempt}@example.com"},
            )
            self.assertEqual(response.status_code, 200)

        response = self.browser.post(reset_url, {"email": self.admin_user.email})
        self.assertRedirects(response, reverse("admin:password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNone(self.browser.session.get("password_reset_attempts"))

    def test_totp_is_required_for_each_new_admin_session(self):
        device = TOTPDevice.objects.create(
            user=self.admin_user,
            name="Login test device",
            confirmed=True,
        )
        self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )
        password_response = self.browser.post(
            reverse("admin:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )
        self.assertRedirects(password_response, reverse("admin:otp"))
        otp_response = self.browser.get(reverse('admin:otp'))
        self.assertContains(otp_response, 'Authenticator verification')

        self.assertIsNone(self.browser.session.get("_auth_user_id"))

        valid = self.browser.post(
            reverse("admin:otp"),
            {"token": f"{totp(device.bin_key):06d}"},
        )
        self.assertRedirects(valid, reverse("admin:index"))
        self.assertEqual(self.browser.session.get("_auth_user_id"), str(self.admin_user.pk))
        self.assertEqual(self.browser.get(reverse("admin:index")).status_code, 200)

    def test_invalid_totp_code_does_not_authenticate(self):
        TOTPDevice.objects.create(
            user=self.admin_user,
            name="Invalid-code test device",
            confirmed=True,
        )
        self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )
        self.browser.post(
            reverse("admin:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )
        response = self.browser.post(reverse("admin:otp"), {"token": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be verified")
        self.assertIsNone(self.browser.session.get("_auth_user_id"))

    def test_security_settings_use_simple_pin_controls(self):
        self.enter_password_login()
        page = self.browser.get(reverse("admin:security"))
        self.assertNotContains(page, "Current password")
        self.assertNotContains(page, "Current admin PIN")
        self.assertNotContains(page, "Current authenticator code")

        enable_pin = self.browser.post(
            reverse("admin:security"),
            {
                "action": "save-pin",
                "pin_enabled": "on",
                "new_pin": "123456",
                "new_pin_confirmation": "123456",
            },
        )
        self.assertRedirects(enable_pin, reverse("admin:security"))
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.pin_enabled)
        self.assertTrue(check_password("123456", self.profile.pin_hash))

        disable_pin = self.browser.post(
            reverse("admin:security"),
            {"action": "save-pin"},
        )
        self.assertRedirects(disable_pin, reverse("admin:security"))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.pin_enabled)
        self.assertEqual(self.profile.pin_hash, "")

    def test_security_settings_can_enroll_totp_with_qr_scan(self):
        self.enter_password_login()
        page = self.browser.get(reverse("admin:security"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Authenticator app")
        self.assertContains(page, "No authenticator is connected")
        self.assertNotContains(page, "Current password")
        self.assertNotContains(page, "Current admin PIN")
        self.assertNotContains(page, "Current authenticator code")

        begin = self.browser.post(
            reverse("admin:security"),
            {
                "action": "begin-2fa",
            },
        )
        self.assertRedirects(begin, reverse("admin:security"))
        pending = TOTPDevice.objects.get(user=self.admin_user, confirmed=False)
        enrollment = self.browser.get(reverse("admin:security"))
        self.assertContains(enrollment, "I have scanned the QR code")
        self.assertContains(enrollment, "No code entry is needed here")
        self.assertNotContains(enrollment, "Manual setup key")
        self.assertNotContains(enrollment, 'name="token"')
        self.assertContains(enrollment, "data:image/svg+xml;base64")

        confirm = self.browser.post(
            reverse("admin:security"),
            {
                "action": "confirm-2fa",
            },
        )
        self.assertRedirects(confirm, reverse("admin:security"))
        pending.refresh_from_db()
        self.assertTrue(pending.confirmed)

        disable = self.browser.post(
            reverse("admin:security"),
            {"action": "disable-2fa"},
        )
        self.assertRedirects(disable, reverse("admin:security"))
        self.assertFalse(TOTPDevice.objects.filter(user=self.admin_user).exists())

    def test_logout_clears_admin_gate_and_otp_state(self):
        self.enter_password_login()
        session = self.browser.session
        session["gccad_gate_pending_pin"] = "pending"
        session["gccad_otp_pending_user"] = str(self.admin_user.pk)
        session["gccad_otp_verified_user"] = str(self.admin_user.pk)
        session.save()
        response = self.browser.get(reverse("admin:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('admin:access'))
        session = self.browser.session
        self.assertIsNone(session.get("_auth_user_id"))
        self.assertIsNone(session.get("gccad_gate_pending_pin"))
        self.assertIsNone(session.get("gccad_otp_pending_user"))
        self.assertIsNone(session.get("gccad_otp_verified_user"))

    def test_pwa_manifest_worker_and_public_offline_fallback(self):
        manifest = self.browser.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["start_url"], "/")
        self.assertEqual(manifest.json()["scope"], "/")
        self.assertEqual(
            {icon["sizes"] for icon in manifest.json()["icons"]},
            {"192x192", "512x512"},
        )

        worker = self.browser.get("/service-worker.js")
        self.assertEqual(worker.status_code, 200)
        self.assertContains(worker, '"/gccad/"')
        self.assertContains(worker, '"/portal/"')
        self.assertNotContains(worker, "localStorage")

        public = self.browser.get(reverse("operations:home"))
        self.assertContains(public, "/manifest.webmanifest")
        self.assertContains(public, "pwa-register.js")
        self.assertEqual(self.browser.get("/offline/").status_code, 200)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_invalid_admin_identifier_creates_event_and_email(self):
        get_user_model().objects.create(
            username="duplicate-email-admin",
            email=self.admin_user.email.upper(),
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        get_user_model().objects.create(
            username="inactive-email-admin",
            email="inactive@gcc.example.com",
            is_active=False,
            is_staff=True,
            is_superuser=True,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(
                reverse("admin:access"),
                {"identifier": "intruder@example.com"},
                REMOTE_ADDR="198.51.100.40",
                HTTP_USER_AGENT="Security test browser",
            )

        self.assertEqual(response.status_code, 200)
        event = AdminSecurityEvent.objects.order_by("-created_at").first()
        self.assertEqual(event.event_type, AdminSecurityEvent.EventType.IDENTIFIER_FAILURE)
        self.assertEqual(event.outcome, AdminSecurityEvent.Outcome.FAILURE)
        self.assertEqual(event.attempted_identifier, "intruder@example.com")
        self.assertEqual(event.ip_address, "198.51.100.40")
        self.assertEqual(event.path, "/gccad/access/")
        self.assertIsNone(event.user)
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.admin_user.email])
        self.assertIn("Invalid admin identifier", mail.outbox[0].body)
        self.assertIn("198.51.100.40", mail.outbox[0].body)
        self.assertIn("/gccad/security/", mail.outbox[0].body)
        self.assertNotIn("security-owner-pass", mail.outbox[0].body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_invalid_admin_password_creates_event_and_email_without_password(self):
        self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(
                reverse("admin:login"),
                {
                    "username": self.admin_user.username,
                    "password": "wrong-admin-password",
                },
                REMOTE_ADDR="198.51.100.41",
                HTTP_USER_AGENT="Password attack test",
            )

        self.assertEqual(response.status_code, 200)
        event = AdminSecurityEvent.objects.order_by("-created_at").first()
        self.assertEqual(event.event_type, AdminSecurityEvent.EventType.PASSWORD_FAILURE)
        self.assertEqual(event.user, self.admin_user)
        self.assertEqual(event.path, "/gccad/login/")
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("wrong-admin-password", mail.outbox[0].body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_invalid_pin_creates_event_and_email(self):
        self.profile.pin_enabled = True
        self.profile.pin_hash = make_password("111111")
        self.profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])
        self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(
                reverse("admin:access"),
                {"pin": "000000"},
                REMOTE_ADDR="198.51.100.42",
            )

        self.assertEqual(response.status_code, 200)
        event = AdminSecurityEvent.objects.order_by("-created_at").first()
        self.assertEqual(event.event_type, AdminSecurityEvent.EventType.PIN_FAILURE)
        self.assertEqual(event.user, self.admin_user)
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("000000", mail.outbox[0].body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_invalid_otp_creates_event_and_email(self):
        device = TOTPDevice.objects.create(
            user=self.admin_user,
            name="Email alert test device",
            confirmed=True,
        )
        self.browser.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
        )
        self.browser.post(
            reverse("admin:login"),
            {
                "username": self.admin_user.username,
                "password": "security-owner-pass",
            },
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(
                reverse("admin:otp"),
                {"token": "000000"},
                REMOTE_ADDR="198.51.100.43",
            )

        self.assertEqual(response.status_code, 200)
        event = AdminSecurityEvent.objects.order_by("-created_at").first()
        self.assertEqual(event.event_type, AdminSecurityEvent.EventType.OTP_FAILURE)
        self.assertEqual(event.user, self.admin_user)
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("000000", mail.outbox[0].body)
        device.delete()

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_failed_recovery_and_password_reset_verification_are_emailed(self):
        with self.captureOnCommitCallbacks(execute=True):
            recovery = self.browser.post(
                reverse("admin:recovery"),
                {"email": "unknown-recovery@example.com"},
                REMOTE_ADDR="198.51.100.44",
            )
            password_reset = self.browser.post(
                reverse("admin:password-reset"),
                {"email": "unknown-password-reset@example.com"},
                REMOTE_ADDR="198.51.100.45",
            )

        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(password_reset.status_code, 200)
        events = list(
            AdminSecurityEvent.objects.order_by("created_at").values_list(
                "event_type", "email_status", "attempted_identifier"
            )
        )
        self.assertEqual(
            events,
            [
                (
                    AdminSecurityEvent.EventType.RECOVERY_FAILURE,
                    AdminSecurityEvent.EmailStatus.SENT,
                    "unknown-recovery@example.com",
                ),
                (
                    AdminSecurityEvent.EventType.PASSWORD_RESET_FAILURE,
                    AdminSecurityEvent.EmailStatus.SENT,
                    "unknown-password-reset@example.com",
                ),
            ],
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("unknown-recovery@example.com", mail.outbox[0].body)
        self.assertIn("unknown-password-reset@example.com", mail.outbox[1].body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_successful_admin_sign_in_is_recorded_without_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.enter_password_login()

        event = AdminSecurityEvent.objects.filter(
            event_type=AdminSecurityEvent.EventType.LOGIN_SUCCESS
        ).order_by("-created_at").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, AdminSecurityEvent.Outcome.SUCCESS)
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.NOT_REQUIRED)
        self.assertEqual(len(mail.outbox), 0)

    def test_ip_block_is_admin_only_reversible_and_csrf_protected(self):
        csrf_client = DjangoClient(enforce_csrf_checks=True)
        csrf_response = csrf_client.post(
            reverse("admin:security-block-ip"),
            {"ip_address": "198.51.100.60"},
        )
        self.assertEqual(csrf_response.status_code, 403)

        self.enter_password_login()
        ip_address = "198.51.100.60"
        response = self.browser.post(
            reverse("admin:security-block-ip"),
            {"ip_address": ip_address, "reason": "Security test"},
        )
        self.assertRedirects(response, reverse("admin:security"))
        block = AdminAccessBlock.objects.get(
            scope=AdminAccessBlock.Scope.IP,
            ip_address=ip_address,
            is_active=True,
        )

        blocked_client = DjangoClient()
        blocked_post = blocked_client.post(
            reverse("admin:access"),
            {"identifier": self.admin_user.username},
            REMOTE_ADDR=ip_address,
        )
        self.assertEqual(blocked_post.status_code, 403)
        self.assertEqual(
            blocked_client.get(reverse("operations:home"), REMOTE_ADDR=ip_address).status_code,
            200,
        )

        unblock = self.browser.post(
            reverse("admin:security-block-unblock", kwargs={"pk": block.pk})
        )
        self.assertRedirects(unblock, reverse("admin:security"))
        block.refresh_from_db()
        self.assertFalse(block.is_active)
        self.assertEqual(
            blocked_client.get(reverse("admin:access"), REMOTE_ADDR=ip_address).status_code,
            302,
        )

    def test_ip_block_cannot_target_current_connection(self):
        self.enter_password_login()
        current_ip = "198.51.100.61"
        AdminSecurityEvent.objects.create(
            event_type=AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            attempted_identifier="blocked-test",
            ip_address=current_ip,
            path="/gccad/access/",
        )

        page = self.browser.get(
            reverse("admin:security"),
            REMOTE_ADDR=current_ip,
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Current connection IP")
        self.assertContains(page, current_ip)
        self.assertContains(page, f'data-current-ip="{current_ip}"')
        self.assertContains(page, "disabled aria-disabled=\"true\"")

        response = self.browser.post(
            reverse("admin:security-block-ip"),
            {"ip_address": current_ip, "reason": "Accidental self-block test"},
            REMOTE_ADDR=current_ip,
        )
        self.assertRedirects(response, reverse("admin:security"))
        self.assertFalse(
            AdminAccessBlock.objects.filter(
                scope=AdminAccessBlock.Scope.IP,
                ip_address=current_ip,
                is_active=True,
            ).exists()
        )

    def test_admin_user_lock_is_reversible_and_cannot_self_lock(self):
        other_admin = get_user_model().objects.create_superuser(
            username="second-security-owner",
            email="second-security-owner@gcc.example.com",
            password="second-security-pass",
        )
        self.enter_password_login()
        response = self.browser.post(
            reverse("admin:security-block-user"),
            {"user": str(other_admin.pk), "reason": "Security test"},
        )
        self.assertRedirects(response, reverse("admin:security"))
        lock = AdminAccessBlock.objects.get(
            scope=AdminAccessBlock.Scope.USER,
            user=other_admin,
            is_active=True,
        )

        self_lock = self.browser.post(
            reverse("admin:security-block-user"),
            {"user": str(self.admin_user.pk), "reason": "Should be rejected"},
        )
        self.assertRedirects(self_lock, reverse("admin:security"))
        self.assertFalse(
            AdminAccessBlock.objects.filter(
                scope=AdminAccessBlock.Scope.USER,
                user=self.admin_user,
                is_active=True,
            ).exists()
        )

        locked_client = DjangoClient()
        locked = locked_client.post(
            reverse("admin:access"),
            {"identifier": other_admin.username},
        )
        self.assertEqual(locked.status_code, 200)
        self.assertContains(locked, "currently locked")
        direct_login = locked_client.post(
            reverse("admin:login"),
            {"username": other_admin.username, "password": "second-security-pass"},
        )
        self.assertEqual(direct_login.status_code, 302)
        self.assertTrue(direct_login["Location"].startswith(reverse("admin:access")))
        self.assertIsNone(locked_client.session.get("_auth_user_id"))

        unblock = self.browser.post(
            reverse("admin:security-block-unblock", kwargs={"pk": lock.pk})
        )
        self.assertRedirects(unblock, reverse("admin:security"))
        self.assertRedirects(
            locked_client.post(
                reverse("admin:access"),
                {"identifier": other_admin.username},
            ),
            reverse("admin:login"),
        )

    def test_security_event_delete_is_owner_only_and_audited(self):
        event = AdminSecurityEvent.objects.create(
            event_type=AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            attempted_identifier="delete-me",
            ip_address="198.51.100.90",
        )
        csrf_client = DjangoClient(enforce_csrf_checks=True)
        self.assertEqual(
            csrf_client.post(
                reverse("admin:security-event-delete", kwargs={"pk": event.pk}),
                {"return_to": reverse("admin:security")},
            ).status_code,
            403,
        )

        self.enter_password_login()
        response = self.browser.get(reverse("admin:security"))
        self.assertContains(response, "Clear matching events")
        self.assertContains(response, "Delete")

        response = self.browser.post(
            reverse("admin:security-event-delete", kwargs={"pk": event.pk}),
            {"return_to": reverse("admin:security")},
        )
        self.assertRedirects(response, reverse("admin:security"))
        self.assertFalse(AdminSecurityEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(
            Activity.objects.filter(message="Admin security event deleted").exists()
        )

    def test_security_events_clear_removes_only_matching_rows_and_is_audited(self):
        matching = AdminSecurityEvent.objects.create(
            event_type=AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            attempted_identifier="clear-me",
            ip_address="198.51.100.91",
        )
        retained = AdminSecurityEvent.objects.create(
            event_type=AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            attempted_identifier="keep-me",
            ip_address="198.51.100.92",
        )
        self.enter_password_login()
        return_to = f'{reverse("admin:security")}?q=clear-me'
        response = self.browser.post(
            reverse("admin:security-events-clear"),
            {
                "q": "clear-me",
                "return_to": return_to,
            },
        )
        self.assertRedirects(response, return_to)
        self.assertFalse(AdminSecurityEvent.objects.filter(pk=matching.pk).exists())
        self.assertTrue(AdminSecurityEvent.objects.filter(pk=retained.pk).exists())
        self.assertTrue(
            Activity.objects.filter(message="Admin security events cleared").exists()
        )

    def test_only_active_superusers_can_view_security_dashboard(self):
        employee = get_user_model().objects.create_user(
            username="security-employee",
            password="security-employee-pass",
            is_active=True,
            is_staff=True,
            is_superuser=False,
        )
        employee_client = DjangoClient()
        employee_client.force_login(employee)
        self.assertEqual(employee_client.get(reverse("admin:security")).status_code, 403)

    def test_forwarded_headers_require_trusted_proxy_configuration(self):
        factory = RequestFactory()
        spoofed = factory.get(
            "/gccad/access/",
            REMOTE_ADDR="198.51.100.70",
            HTTP_X_FORWARDED_FOR="203.0.113.70",
        )
        with self.settings(ADMIN_TRUSTED_PROXY_IPS=()):
            self.assertEqual(client_ip(spoofed), "198.51.100.70")

        trusted = factory.get(
            "/gccad/access/",
            REMOTE_ADDR="198.51.100.71",
            HTTP_X_FORWARDED_FOR="198.51.100.72, 198.51.100.71",
        )
        with self.settings(ADMIN_TRUSTED_PROXY_IPS=("198.51.100.71",)):
            self.assertEqual(client_ip(trusted), "198.51.100.72")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_failed_security_email_is_recorded_and_retryable(self):
        request = RequestFactory().post(
            "/gccad/login/",
            REMOTE_ADDR="198.51.100.80",
            HTTP_USER_AGENT="Delivery failure test",
        )
        with patch("operations.security.send_mail", side_effect=RuntimeError("SMTP unavailable")):
            with self.captureOnCommitCallbacks(execute=True):
                event = record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.PASSWORD_FAILURE,
                    attempted_identifier="security-owner",
                    detail="The password could not be verified.",
                )
        event.refresh_from_db()
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.FAILED)
        self.assertEqual(event.email_attempt_count, 1)
        self.assertIn("SMTP unavailable", event.email_error)

        with patch("operations.security.send_mail", return_value=1):
            self.assertEqual(retry_pending_admin_security_emails(), 1)
        event.refresh_from_db()
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(event.email_attempt_count, 2)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
        ADMIN_SECURITY_EMAIL_ALERTS_ENABLED=False,
    )
    def test_security_email_switch_preserves_event_without_delivery(self):
        request = RequestFactory().post("/gccad/access/", REMOTE_ADDR="198.51.100.81")
        with self.captureOnCommitCallbacks(execute=True):
            event = record_admin_security_event(
                request,
                AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
                attempted_identifier="disabled-alert-test",
            )
        event.refresh_from_db()
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.DISABLED)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="security@gcc.example.com",
    )
    def test_invalid_admin_password_reset_link_creates_event_and_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.browser.post(
                reverse(
                    "admin:password-reset-confirm",
                    kwargs={"uidb64": "invalid", "token": "invalid"},
                ),
                {
                    "new_password1": "not-a-real-password",
                    "new_password2": "not-a-real-password",
                },
                REMOTE_ADDR="198.51.100.82",
            )

        self.assertEqual(response.status_code, 200)
        event = AdminSecurityEvent.objects.order_by("-created_at").first()
        self.assertEqual(event.event_type, AdminSecurityEvent.EventType.PASSWORD_RESET_FAILURE)
        self.assertEqual(event.email_status, AdminSecurityEvent.EmailStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("not-a-real-password", mail.outbox[0].body)
