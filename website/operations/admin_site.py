from __future__ import annotations

import logging
from functools import update_wrapper
from urllib.parse import urlencode
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.hashers import make_password
from django.core.paginator import Paginator
from django.contrib.auth.views import (
    LoginView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django_otp.plugins.otp_totp.models import TOTPDevice
from unfold.sites import UnfoldAdminSite

from .forms import (
    AdminGateIdentifierForm,
    AdminIPBlockForm,
    AdminOtpForm,
    AdminPasswordResetForm,
    AdminPinForm,
    AdminPinSettingsForm,
    AdminRecoveryRequestForm,
    AdminRecoveryResetForm,
    AdminSecurityEventFilterForm,
    AdminTwoFactorStartForm,
    AdminUserBlockForm,
    GrandCoastAdminAuthenticationForm,
)
from .models import AdminAccessBlock, AdminSecurityEvent
from .services import record_activity
from .security import (
    ADMIN_GATE_PENDING_PIN,
    ADMIN_GATE_NEXT,
    ADMIN_OTP_ENROLLMENT_DEVICE,
    admin_gate_locked,
    admin_ip_block,
    admin_user_block,
    admin_security_profile,
    begin_otp_challenge,
    begin_totp_enrollment,
    clear_admin_challenges,
    clear_mobile_owner_session,
    clear_gate,
    clear_otp_challenge,
    consume_admin_recovery_token,
    confirmed_totp_device,
    create_admin_recovery_token,
    admin_password_attempt_allowed,
    admin_recovery_request_allowed,
    client_ip,
    find_admin_recovery_token,
    gate_is_valid,
    gate_next,
    has_totp_enabled,
    is_active_admin,
    is_mobile_owner_session,
    mark_otp_verified,
    otp_is_verified,
    pending_otp_next,
    pending_otp_user,
    pin_is_enabled,
    register_gate_failure,
    register_otp_failure,
    record_admin_security_event,
    remove_totp_devices,
    reset_admin_security,
    revoke_user_sessions,
    resolve_admin_identifier,
    set_gate,
    totp_qr_data_uri,
    verify_admin_pin,
)
from .turnstile import is_mobile_webview


logger = logging.getLogger(__name__)


class GrandCoastAdminLoginView(LoginView):
    admin_site = None

    def dispatch(self, request, *args, **kwargs):
        if not is_active_admin(request.user) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    attempted_identifier=request.POST.get("username", ""),
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        if request.method == "POST" and not admin_password_attempt_allowed(
            request,
            request.POST.get("username", ""),
        ):
            form = self.get_form()
            form.add_error(None, "Too many sign-in attempts. Try again in 15 minutes.")
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        if (
            self.request.method == "POST"
            and form.errors.get("__all__")
            and form.cleaned_data.get("username")
            and form.cleaned_data.get("password")
        ):
            attempted_identifier = form.cleaned_data["username"]
            resolved_admin = resolve_admin_identifier(attempted_identifier)
            record_admin_security_event(
                self.request,
                (
                    AdminSecurityEvent.EventType.PASSWORD_FAILURE
                    if resolved_admin is not None
                    else AdminSecurityEvent.EventType.IDENTIFIER_FAILURE
                ),
                attempted_identifier=attempted_identifier,
                user=resolved_admin,
                detail=(
                    "The administrator password could not be verified."
                    if resolved_admin is not None
                    else "The identifier did not match an active administrator."
                ),
            )
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        if not is_active_admin(user):
            record_admin_security_event(
                self.request,
                AdminSecurityEvent.EventType.PASSWORD_FAILURE,
                attempted_identifier=form.cleaned_data.get("username", ""),
                detail="The credentials do not belong to an active administrator.",
            )
            form.add_error(None, form.error_messages["invalid_login"])
            return LoginView.form_invalid(self, form)
        if not gate_is_valid(self.request, user):
            record_admin_security_event(
                self.request,
                AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                outcome=AdminSecurityEvent.Outcome.BLOCKED,
                user=user,
                attempted_identifier=form.cleaned_data.get("username", ""),
                detail="The administration access gate was not completed.",
            )
            clear_gate(self.request)
            form.add_error(None, form.error_messages["invalid_login"])
            return LoginView.form_invalid(self, form)
        if admin_user_block(user) is not None:
            record_admin_security_event(
                self.request,
                AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                outcome=AdminSecurityEvent.Outcome.BLOCKED,
                user=user,
                attempted_identifier=form.cleaned_data.get("username", ""),
                detail="The administrator account is locked from new administration sign-ins.",
            )
            form.add_error(None, "This administrator account is currently locked.")
            return LoginView.form_invalid(self, form)

        next_url = gate_next(self.request)
        device = confirmed_totp_device(user)
        if device is not None:
            begin_otp_challenge(self.request, user, next_url)
            return redirect(reverse("admin:otp"))

        login(self.request, user, backend="django.contrib.auth.backends.ModelBackend")
        self.request.session.pop("gccad_otp_verified_user", None)
        record_admin_security_event(
            self.request,
            AdminSecurityEvent.EventType.LOGIN_SUCCESS,
            outcome=AdminSecurityEvent.Outcome.SUCCESS,
            user=user,
            attempted_identifier=form.cleaned_data.get("username", ""),
        )
        return HttpResponseRedirect(next_url)

    def get_success_url(self):
        return gate_next(self.request)


class GrandCoastAdminPasswordResetView(PasswordResetView):
    form_class = AdminPasswordResetForm
    template_name = "admin/password_reset_form.html"
    email_template_name = "admin/password_reset_email.txt"
    subject_template_name = "admin/password_reset_subject.txt"
    success_url = reverse_lazy("admin:password-reset-done")

    def dispatch(self, request, *args, **kwargs):
        if not is_active_admin(request.user) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    attempted_identifier=request.POST.get("email", ""),
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"].strip()
        allowed = admin_recovery_request_allowed(self.request, email)
        users = tuple(form.get_users(email))
        if not users:
            record_admin_security_event(
                self.request,
                AdminSecurityEvent.EventType.PASSWORD_RESET_FAILURE,
                attempted_identifier=form.cleaned_data.get("email", ""),
                detail="The email does not match an active administrator.",
            )
        if users and allowed:
            try:
                form.save(
                    use_https=self.request.is_secure(),
                    token_generator=self.token_generator,
                    from_email=self.from_email,
                    email_template_name=self.email_template_name,
                    subject_template_name=self.subject_template_name,
                    request=self.request,
                    html_email_template_name=self.html_email_template_name,
                    extra_email_context=self.extra_email_context,
                )
            except Exception:
                logger.exception("Admin password reset email could not be sent.")
        return HttpResponseRedirect(self.get_success_url())


class GrandCoastAdminPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "admin/password_reset_confirm.html"
    success_url = reverse_lazy("admin:password-reset-complete")

    def dispatch(self, request, *args, **kwargs):
        if not is_active_admin(request.user) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        response = super().dispatch(request, *args, **kwargs)
        if request.method == "POST" and not getattr(self, "validlink", False):
            reset_user = getattr(self, "user", None)
            resolved_admin = reset_user if is_active_admin(reset_user) else None
            record_admin_security_event(
                request,
                AdminSecurityEvent.EventType.PASSWORD_RESET_FAILURE,
                user=resolved_admin,
                attempted_identifier=(
                    resolved_admin.get_username() if resolved_admin is not None else ""
                ),
                detail="The administration password-reset link could not be verified.",
            )
        return response


class GrandCoastAdminSite(UnfoldAdminSite):
    site_header = "Grand Coast Administration"
    site_title = "Grand Coast Administration"
    index_title = "Administration"

    def has_permission(self, request):
        return bool(
            request.user.is_authenticated
            and request.user.is_active
            and request.user.is_staff
            and request.user.is_superuser
        )

    def _access_url(self, next_url=None):
        url = reverse("admin:access")
        if next_url:
            url = f"{url}?{urlencode({REDIRECT_FIELD_NAME: next_url})}"
        return url

    def _single_admin_user(self):
        admins = list(get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
            is_superuser=True,
        ))
        return admins[0] if len(admins) == 1 else None


    def _render_admin_page(self, request, template_name, context=None, **kwargs):
        page_context = self.each_context(request)
        page_context.update(context or {})
        return render(request, template_name, page_context, **kwargs)

    def admin_view(self, view, cacheable=False):
        def inner(request, *args, **kwargs):
            if is_mobile_webview(request) and is_mobile_owner_session(request):
                return redirect("operations:dashboard")
            if not self.has_permission(request):
                if request.user.is_authenticated:
                    return HttpResponseForbidden("Administration access is restricted.")
                if admin_ip_block(request) is not None:
                    return HttpResponseForbidden("Administration access is restricted.")
                return redirect(self._access_url(request.get_full_path()))
            if not gate_is_valid(request, request.user):
                return redirect(self._access_url(request.get_full_path()))
            if has_totp_enabled(request.user) and not otp_is_verified(request, request.user):
                logout(request)
                clear_admin_challenges(request)
                return redirect(self._access_url(request.get_full_path()))
            return view(request, *args, **kwargs)

        if not cacheable:
            inner = never_cache(inner)
        if not getattr(view, "csrf_exempt", False):
            inner = csrf_protect(inner)
        return update_wrapper(inner, view)

    def search(self, request, extra_context=None):
        # Unfold 0.66 only invokes the configured callback and system-model
        # searchers for extended requests. The navigation search is a normal
        # request, so promote it here to keep Operations and User results
        # available from the existing search box.
        if request.GET.get("s") and "extended" not in request.GET:
            request.GET = request.GET.copy()
            request.GET["extended"] = "1"
        return super().search(request, extra_context=extra_context)

    def login(self, request, extra_context=None):
        if is_mobile_webview(request) and is_mobile_owner_session(request):
            return redirect("operations:dashboard")
        if not self.has_permission(request) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    attempted_identifier=request.POST.get("username", ""),
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        requested_next = request.GET.get(REDIRECT_FIELD_NAME) or request.POST.get(REDIRECT_FIELD_NAME)
        if not gate_is_valid(request):
            return redirect(self._access_url(requested_next or request.get_full_path()))
        if request.method == "GET" and self.has_permission(request):
            if has_totp_enabled(request.user) and not otp_is_verified(request, request.user):
                logout(request)
                clear_admin_challenges(request)
            else:
                return HttpResponseRedirect(gate_next(request))

        redirect_url = gate_next(request)
        context = {
            **self.each_context(request),
            "title": "Log in",
            "subtitle": None,
            "app_path": request.get_full_path(),
            "username": request.user.get_username(),
            REDIRECT_FIELD_NAME: redirect_url,
        }
        context.update(extra_context or {})
        defaults = {
            "extra_context": context,
            "authentication_form": GrandCoastAdminAuthenticationForm,
            "template_name": self.login_template or "admin/login.html",
            "admin_site": self,
        }
        request.current_app = self.name
        return GrandCoastAdminLoginView.as_view(**defaults)(request)

    def logout(self, request, extra_context=None):
        clear_mobile_owner_session(request)
        logout(request)
        clear_admin_challenges(request)
        return redirect(self._access_url())

    def records(self, request):
        return self._render_admin_page(
            request,
            "admin/records.html",
            {
                "title": "Advanced records",
                "subtitle": "Low-level administration",
                "app_list": self.get_app_list(request),
            },
        )

    def get_urls(self):
        custom_urls = [
            path("access/", self.access, name="access"),
            path("otp/", self.otp, name="otp"),
            path("recover/", self.recovery, name="recovery"),
            path(
                "recover/<str:token>/",
                self.recovery_confirm,
                name="recovery-confirm",
            ),
            path("password-reset/", GrandCoastAdminPasswordResetView.as_view(), name="password-reset"),
            path(
                "password-reset/done/",
                PasswordResetDoneView.as_view(template_name="admin/password_reset_done.html"),
                name="password-reset-done",
            ),
            path(
                "password-reset/confirm/<uidb64>/<token>/",
                GrandCoastAdminPasswordResetConfirmView.as_view(),
                name="password-reset-confirm",
            ),
            path(
                "password-reset/complete/",
                PasswordResetCompleteView.as_view(template_name="admin/password_reset_complete.html"),
                name="password-reset-complete",
            ),
            path(
                "records/",
                self.admin_view(self.records),
                name="records",
            ),
            path(
                "security/",
                self.admin_view(self.security_settings),
                name="security",
            ),
            path(
                "security/blocks/ip/",
                self.admin_view(self.security_block_ip),
                name="security-block-ip",
            ),
            path(
                "security/blocks/user/",
                self.admin_view(self.security_block_user),
                name="security-block-user",
            ),
            path(
                "security/blocks/<uuid:pk>/unblock/",
                self.admin_view(self.security_block_unblock),
                name="security-block-unblock",
            ),
            path(
                "security/events/<uuid:pk>/review/",
                self.admin_view(self.security_event_review),
                name="security-event-review",
            ),
            path(
                "security/events/<uuid:pk>/delete/",
                self.admin_view(self.security_event_delete),
                name="security-event-delete",
            ),
            path(
                "security/events/clear/",
                self.admin_view(self.security_events_clear),
                name="security-events-clear",
            ),
        ]
        return custom_urls + super().get_urls()

    def access(self, request):
        if is_mobile_webview(request) and is_mobile_owner_session(request):
            return redirect("operations:dashboard")
        if request.user.is_authenticated and not self.has_permission(request):
            return HttpResponseForbidden("Administration access is restricted.")
        if not self.has_permission(request) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    attempted_identifier=request.POST.get("identifier", ""),
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")

        next_url = request.GET.get(REDIRECT_FIELD_NAME) or request.POST.get(REDIRECT_FIELD_NAME)
        next_url = next_url or reverse("admin:index")
        if request.method == "GET" and request.session.get(ADMIN_GATE_PENDING_PIN) is None:
            default_admin = self._single_admin_user()
            if default_admin is not None:
                if pin_is_enabled(default_admin):
                    request.session[ADMIN_GATE_PENDING_PIN] = str(default_admin.pk)
                    request.session[ADMIN_GATE_NEXT] = next_url
                    request.session.modified = True
                else:
                    set_gate(request, default_admin, next_url)
                    return redirect("admin:login")

        if gate_is_valid(request):
            return redirect("admin:login")

        if admin_gate_locked(request):
            return self._render_admin_page(
                request,
                "admin/access.html",
                {
                    "title": "Administration access",
                    "site_title": self.site_title,
                    "locked": True,
                    "next": next_url,
                },
                status=429,
            )

        pending_user_id = request.session.get(ADMIN_GATE_PENDING_PIN)
        pending_user = (
            get_user_model()
            .objects.filter(
                pk=pending_user_id,
                is_active=True,
                is_staff=True,
                is_superuser=True,
            )
            .first()
            if pending_user_id
            else None
        )
        if pending_user is not None:
            form = AdminPinForm(request.POST or None)
            if admin_user_block(pending_user) is not None:
                if request.method == "POST":
                    record_admin_security_event(
                        request,
                        AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                        outcome=AdminSecurityEvent.Outcome.BLOCKED,
                        user=pending_user,
                        attempted_identifier=pending_user.username,
                        detail="The administrator account is locked from new administration sign-ins.",
                    )
                form.add_error(None, "This administrator account is currently locked.")
                return self._render_admin_page(
                    request,
                    "admin/access.html",
                    {
                        "title": "Administration access",
                        "site_title": self.site_title,
                        "form": form,
                        "pin_step": True,
                        "next": next_url,
                    },
                )
            if request.method == "POST":
                if form.is_valid() and verify_admin_pin(pending_user, form.cleaned_data["pin"]):
                    set_gate(request, pending_user, next_url)
                    return redirect("admin:login")
                if form.is_valid():
                    register_gate_failure(request)
                    record_admin_security_event(
                        request,
                        AdminSecurityEvent.EventType.PIN_FAILURE,
                        user=pending_user,
                        attempted_identifier=pending_user.username,
                        detail="The administrator PIN could not be verified.",
                    )
                    form.add_error(None, "That PIN could not be verified.")
                if admin_gate_locked(request):
                    form.add_error(None, "Too many attempts. Try again in 15 minutes.")
            return self._render_admin_page(
                request,
                "admin/access.html",
                {
                    "title": "Administration access",
                    "site_title": self.site_title,
                    "form": form,
                    "pin_step": True,
                    "next": next_url,
                },
            )

        form = AdminGateIdentifierForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            admin_user = resolve_admin_identifier(form.cleaned_data["identifier"])
            if admin_user is None:
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.IDENTIFIER_FAILURE,
                    attempted_identifier=form.cleaned_data["identifier"],
                    detail="The identifier did not match an active administrator.",
                )
                form.add_error(None, "We could not verify that administration account.")
            elif admin_user_block(admin_user) is not None:
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    user=admin_user,
                    attempted_identifier=form.cleaned_data["identifier"],
                    detail="The administrator account is locked from new administration sign-ins.",
                )
                form.add_error(None, "This administrator account is currently locked.")
            elif pin_is_enabled(admin_user):
                request.session[ADMIN_GATE_PENDING_PIN] = str(admin_user.pk)
                request.session["gccad_gate_next"] = next_url
                request.session.modified = True
                return redirect(self._access_url(next_url))
            else:
                set_gate(request, admin_user, next_url)
                return redirect("admin:login")

        return self._render_admin_page(
            request,
            "admin/access.html",
            {
                "title": "Administration access",
                "site_title": self.site_title,
                "form": form,
                "next": next_url,
            },
        )

    def otp(self, request):
        if is_mobile_webview(request) and is_mobile_owner_session(request):
            return redirect("operations:dashboard")
        if not is_active_admin(request.user) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        user = pending_otp_user(request)
        if user is None or not gate_is_valid(request, user):
            clear_otp_challenge(request)
            return redirect(self._access_url(request.get_full_path()))
        device = confirmed_totp_device(user)
        if device is None:
            clear_otp_challenge(request)
            return redirect("admin:login")
        if admin_user_block(user) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    user=user,
                    attempted_identifier=user.username,
                    detail="The administrator account is locked from new administration sign-ins.",
                )
            clear_otp_challenge(request)
            clear_gate(request)
            return HttpResponseForbidden("Administration access is restricted.")

        form = AdminOtpForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            if device.verify_token(form.cleaned_data["token"]):
                next_url = pending_otp_next(request)
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                mark_otp_verified(request, user, device)
                clear_otp_challenge(request)
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.LOGIN_SUCCESS,
                    outcome=AdminSecurityEvent.Outcome.SUCCESS,
                    user=user,
                    attempted_identifier=user.username,
                )
                return redirect(next_url)
            locked = register_otp_failure(request)
            record_admin_security_event(
                request,
                AdminSecurityEvent.EventType.OTP_FAILURE,
                user=user,
                attempted_identifier=user.username,
                detail="The authenticator code could not be verified.",
            )
            form.add_error(None, "That authenticator code could not be verified.")
            if locked:
                clear_otp_challenge(request)
                clear_gate(request)
                form.add_error(None, "Too many attempts. Start again from the private admin URL.")

        return self._render_admin_page(
            request,
            "admin/otp.html",
            {
                "title": "Authenticator verification",
                "site_title": self.site_title,
                "form": form,
                "username": user.get_username(),
            },
        )

    def recovery(self, request):
        if is_mobile_webview(request) and is_mobile_owner_session(request):
            return redirect("operations:dashboard")
        if request.user.is_authenticated and not self.has_permission(request):
            return HttpResponseForbidden("Administration access is restricted.")
        if not self.has_permission(request) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    attempted_identifier=request.POST.get("email", ""),
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        form = AdminRecoveryRequestForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            email = form.cleaned_data["email"].strip()
            allowed = admin_recovery_request_allowed(request, email)
            admin_user = get_user_model().objects.filter(
                email__iexact=email,
                is_active=True,
                is_staff=True,
                is_superuser=True,
            ).first()
            if admin_user is not None and admin_user.email and allowed:
                try:
                    create_admin_recovery_token(request, admin_user)
                except Exception:
                    logger.exception("Admin security recovery email could not be sent.")
            elif admin_user is None:
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.RECOVERY_FAILURE,
                    attempted_identifier=form.cleaned_data.get("email", ""),
                    detail="The email does not match an active administrator.",
                )
            return self._render_admin_page(
                request,
                "admin/recovery_sent.html",
                {"title": "Check your email", "site_title": self.site_title},
            )
        return self._render_admin_page(
            request,
            "admin/recovery.html",
            {
                "title": "Recover administration access",
                "site_title": self.site_title,
                "form": form,
            },
        )

    def recovery_confirm(self, request, token):
        if is_mobile_webview(request) and is_mobile_owner_session(request):
            return redirect("operations:dashboard")
        if not is_active_admin(request.user) and admin_ip_block(request) is not None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    detail="The source IP is blocked from administration access.",
                )
            return HttpResponseForbidden("Administration access is restricted.")
        recovery_token = find_admin_recovery_token(token)
        if recovery_token is None:
            if request.method == "POST":
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.RECOVERY_FAILURE,
                    detail="The administration recovery token could not be verified.",
                )
            return self._render_admin_page(
                request,
                "admin/recovery_invalid.html",
                {"title": "Recovery link unavailable", "site_title": self.site_title},
                status=410,
            )
        form = AdminRecoveryResetForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            recovery_token = consume_admin_recovery_token(token)
            if recovery_token is None:
                record_admin_security_event(
                    request,
                    AdminSecurityEvent.EventType.RECOVERY_FAILURE,
                    detail="The administration recovery token could not be consumed.",
                )
                return self._render_admin_page(
                    request,
                    "admin/recovery_invalid.html",
                    {"title": "Recovery link unavailable", "site_title": self.site_title},
                    status=410,
                )
            reset_admin_security(recovery_token.user, form.cleaned_data["new_pin"])
            revoke_user_sessions(recovery_token.user)
            clear_admin_challenges(request)
            return self._render_admin_page(
                request,
                "admin/recovery_complete.html",
                {"title": "Security reset complete", "site_title": self.site_title},
            )
        return self._render_admin_page(
            request,
            "admin/recovery_confirm.html",
            {
                "title": "Reset administration security",
                "site_title": self.site_title,
                "form": form,
            },
        )

    @staticmethod
    def _security_form_errors(form):
        return "; ".join(
            str(error)
            for errors in form.errors.values()
            for error in errors
        )

    def security_block_ip(self, request):
        if request.method != "POST":
            return redirect("admin:security")
        form = AdminIPBlockForm(request.POST)
        if not form.is_valid():
            messages.error(request, self._security_form_errors(form))
            return redirect("admin:security")

        ip_address = form.cleaned_data["ip_address"]
        reason = form.cleaned_data["reason"].strip()
        current_client_ip = client_ip(request)
        if current_client_ip and ip_address == current_client_ip:
            messages.error(
                request,
                "For safety, you cannot block the IP address used by this administrator session.",
            )
            return redirect("admin:security")
        with transaction.atomic():
            existing = (
                AdminAccessBlock.objects.select_for_update()
                .filter(
                    scope=AdminAccessBlock.Scope.IP,
                    ip_address=ip_address,
                    is_active=True,
                )
                .first()
            )
            if existing is not None:
                messages.info(request, f"{ip_address} is already blocked from administration access.")
            else:
                block = AdminAccessBlock(
                    scope=AdminAccessBlock.Scope.IP,
                    ip_address=ip_address,
                    reason=reason,
                    created_by=request.user,
                )
                block.full_clean()
                block.save()
                record_activity(
                    "Admin IP block created",
                    f"{ip_address}{' · ' + reason if reason else ''}",
                    actor=request.user,
                )
                messages.success(request, f"{ip_address} is now blocked from administration access.")
        return redirect("admin:security")

    def security_block_user(self, request):
        if request.method != "POST":
            return redirect("admin:security")
        form = AdminUserBlockForm(request.POST, current_user=request.user)
        if not form.is_valid():
            messages.error(request, self._security_form_errors(form))
            return redirect("admin:security")

        user = form.cleaned_data["user"]
        reason = form.cleaned_data["reason"].strip()
        with transaction.atomic():
            existing = (
                AdminAccessBlock.objects.select_for_update()
                .filter(
                    scope=AdminAccessBlock.Scope.USER,
                    user=user,
                    is_active=True,
                )
                .first()
            )
            if existing is not None:
                messages.info(request, f"{user.get_username()} is already locked from administration sign-in.")
            else:
                block = AdminAccessBlock(
                    scope=AdminAccessBlock.Scope.USER,
                    user=user,
                    reason=reason,
                    created_by=request.user,
                )
                block.full_clean()
                block.save()
                record_activity(
                    "Admin login lock created",
                    f"{user.get_username()}{' · ' + reason if reason else ''}",
                    actor=request.user,
                )
                messages.success(request, f"{user.get_username()} is locked from new administration sign-ins.")
        return redirect("admin:security")

    def security_block_unblock(self, request, pk):
        if request.method != "POST":
            return redirect("admin:security")
        with transaction.atomic():
            block = get_object_or_404(
                AdminAccessBlock.objects.select_for_update(),
                pk=pk,
            )
            if block.is_active:
                block.is_active = False
                block.revoked_by = request.user
                block.revoked_at = timezone.now()
                block.save(update_fields=["is_active", "revoked_by", "revoked_at"])
                record_activity(
                    "Admin access block removed",
                    str(block),
                    actor=request.user,
                )
                messages.success(request, f"{block} is no longer blocked.")
            else:
                messages.info(request, f"{block} is already unblocked.")
        return redirect("admin:security")

    @staticmethod
    def _filtered_security_events(params):
        security_filter_form = AdminSecurityEventFilterForm(params or None)
        security_events_queryset = AdminSecurityEvent.objects.all()
        if security_filter_form.is_valid():
            cleaned = security_filter_form.cleaned_data
            query = cleaned.get("q")
            if query:
                security_events_queryset = security_events_queryset.filter(
                    Q(ip_address__icontains=query)
                    | Q(attempted_identifier__icontains=query)
                    | Q(user__username__icontains=query)
                    | Q(user__email__icontains=query)
                    | Q(user_agent__icontains=query)
                    | Q(path__icontains=query)
                    | Q(detail__icontains=query)
                )
            if cleaned.get("event_type"):
                security_events_queryset = security_events_queryset.filter(
                    event_type=cleaned["event_type"]
                )
            if cleaned.get("outcome"):
                security_events_queryset = security_events_queryset.filter(
                    outcome=cleaned["outcome"]
                )
            if cleaned.get("review") == "unreviewed":
                security_events_queryset = security_events_queryset.filter(
                    reviewed_at__isnull=True
                )
            elif cleaned.get("review") == "reviewed":
                security_events_queryset = security_events_queryset.filter(
                    reviewed_at__isnull=False
                )
            if cleaned.get("date_from"):
                security_events_queryset = security_events_queryset.filter(
                    created_at__date__gte=cleaned["date_from"]
                )
            if cleaned.get("date_to"):
                security_events_queryset = security_events_queryset.filter(
                    created_at__date__lte=cleaned["date_to"]
                )
        return security_filter_form, security_events_queryset

    @staticmethod
    def _security_events_return_url(request):
        security_url = reverse("admin:security")
        return_to = request.POST.get("return_to", "")
        return return_to if return_to.startswith(security_url) else security_url

    def security_event_delete(self, request, pk):
        if request.method != "POST":
            return redirect("admin:security")
        with transaction.atomic():
            event = get_object_or_404(
                AdminSecurityEvent.objects.select_for_update(),
                pk=pk,
            )
            event_detail = f"{event.get_event_type_display()}"
            if event.ip_address:
                event_detail += f"; source IP {event.ip_address}"
            event.delete()
            record_activity(
                "Admin security event deleted",
                event_detail,
                actor=request.user,
            )
        messages.success(request, "Security event deleted.")
        return redirect(self._security_events_return_url(request))

    def security_events_clear(self, request):
        if request.method != "POST":
            return redirect("admin:security")
        security_filter_form, security_events_queryset = self._filtered_security_events(
            request.POST
        )
        if not security_filter_form.is_valid():
            messages.error(request, "The security event filters could not be applied.")
            return redirect(self._security_events_return_url(request))

        with transaction.atomic():
            deleted_count, _ = security_events_queryset.delete()
            if deleted_count:
                record_activity(
                    "Admin security events cleared",
                    f"{deleted_count} event(s) matching the current filters",
                    actor=request.user,
                )
        if deleted_count:
            messages.success(request, f"Cleared {deleted_count} security event(s).")
        else:
            messages.info(request, "No security events matched the current filters.")
        return redirect(self._security_events_return_url(request))

    def security_event_review(self, request, pk):
        if request.method != "POST":
            return redirect("admin:security")
        with transaction.atomic():
            event = get_object_or_404(
                AdminSecurityEvent.objects.select_for_update(),
                pk=pk,
            )
            if event.reviewed_at is None:
                event.reviewed_at = timezone.now()
                event.reviewed_by = request.user
                event.save(update_fields=["reviewed_at", "reviewed_by"])
                record_activity(
                    "Admin security event reviewed",
                    str(event),
                    actor=request.user,
                )
                messages.success(request, "Security event marked as reviewed.")
            else:
                messages.info(request, "Security event was already reviewed.")
        return redirect("admin:security")

    def security_settings(self, request):
        profile = admin_security_profile(request.user)
        confirmed_device = confirmed_totp_device(request.user)
        pending_device_id = request.session.get(ADMIN_OTP_ENROLLMENT_DEVICE)
        pending_device = (
            TOTPDevice.objects.filter(
                user=request.user,
                pk=pending_device_id,
                confirmed=False,
            ).first()
            if pending_device_id
            else None
        )
        pin_form = AdminPinSettingsForm(
            request.POST if request.POST.get("action") == "save-pin" else None,
            profile=profile,
        )
        two_factor_form = AdminTwoFactorStartForm(
            request.POST if request.POST.get("action") in {"begin-2fa", "disable-2fa"} else None,
        )

        if request.method == "POST":
            action = request.POST.get("action")
            if action == "save-pin":
                if pin_form.is_valid():
                    profile.pin_enabled = pin_form.cleaned_data["pin_enabled"]
                    profile.pin_hash = (
                        make_password(pin_form.cleaned_data["new_pin"])
                        if profile.pin_enabled and pin_form.cleaned_data["new_pin"]
                        else profile.pin_hash if profile.pin_enabled else ""
                    )
                    profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])
                    messages.success(request, "Admin PIN settings saved.")
                    return redirect("admin:security")
            elif action in {"begin-2fa", "disable-2fa"}:
                if two_factor_form.is_valid():
                    if action == "disable-2fa":
                        remove_totp_devices(request.user)
                        request.session.pop(ADMIN_OTP_ENROLLMENT_DEVICE, None)
                        request.session.pop("gccad_otp_verified_user", None)
                        messages.success(request, "Authenticator 2FA has been disabled.")
                        return redirect("admin:security")
                    else:
                        pending_device = begin_totp_enrollment(request.user)
                        request.session[ADMIN_OTP_ENROLLMENT_DEVICE] = pending_device.pk
                        request.session.modified = True
                        messages.info(request, "Scan the QR code, then choose the enable button to finish authenticator setup.")
                        return redirect("admin:security")
            elif action == "confirm-2fa" and pending_device is not None:
                pending_device.confirmed = True
                pending_device.save(update_fields=["confirmed"])
                mark_otp_verified(request, request.user, pending_device)
                request.session.pop(ADMIN_OTP_ENROLLMENT_DEVICE, None)
                request.session.modified = True
                messages.success(request, "Authenticator 2FA is now enabled.")
                return redirect("admin:security")
            elif action == "cancel-2fa":
                if pending_device is not None:
                    pending_device.delete()
                request.session.pop(ADMIN_OTP_ENROLLMENT_DEVICE, None)
                request.session.modified = True
                return redirect("admin:security")

        security_filter_form, security_events_queryset = self._filtered_security_events(
            request.GET
        )
        security_events_queryset = security_events_queryset.select_related(
            "user", "reviewed_by"
        )

        security_event_page = Paginator(security_events_queryset, 30).get_page(
            request.GET.get("page", 1)
        )
        recent_since = timezone.now() - timedelta(hours=24)
        recent_failures = AdminSecurityEvent.objects.filter(
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            created_at__gte=recent_since,
        )
        security_metrics = {
            "recent_failures": recent_failures.count(),
            "recent_ips": recent_failures.exclude(ip_address__isnull=True)
            .values("ip_address")
            .distinct()
            .count(),
            "unreviewed": AdminSecurityEvent.objects.filter(
                reviewed_at__isnull=True
            ).count(),
            "email_failures": AdminSecurityEvent.objects.filter(
                outcome=AdminSecurityEvent.Outcome.FAILURE,
                email_status__in=[
                    AdminSecurityEvent.EmailStatus.FAILED,
                    AdminSecurityEvent.EmailStatus.NO_RECIPIENT,
                ],
            ).count(),
        }
        active_access_blocks = (
            AdminAccessBlock.objects.filter(is_active=True)
            .select_related("user", "created_by")
        )
        active_ip_addresses = set(
            active_access_blocks
            .filter(scope=AdminAccessBlock.Scope.IP)
            .values_list("ip_address", flat=True)
        )
        active_user_block_ids = set(
            active_access_blocks
            .filter(scope=AdminAccessBlock.Scope.USER)
            .values_list("user_id", flat=True)
        )
        ip_block_form = AdminIPBlockForm()
        user_block_form = AdminUserBlockForm(current_user=request.user)

        context = {
            **self.each_context(request),
            "title": "Security settings",
            "subtitle": "Private administration",
            "profile": profile,
            "two_factor_enabled": confirmed_device is not None,
            "pending_device": pending_device,
            "pending_qr": totp_qr_data_uri(pending_device) if pending_device else "",
            "pin_form": pin_form,
            "two_factor_form": two_factor_form,
            "security_filter_form": security_filter_form,
            "security_event_page": security_event_page,
            "security_events": security_event_page.object_list,
            "security_metrics": security_metrics,
            "active_access_blocks": active_access_blocks,
            "active_ip_addresses": active_ip_addresses,
            "active_user_block_ids": active_user_block_ids,
            "current_client_ip": client_ip(request),
            "ip_block_form": ip_block_form,
            "user_block_form": user_block_form,
            "security_email_alerts_enabled": getattr(
                settings,
                "ADMIN_SECURITY_EMAIL_ALERTS_ENABLED",
                True,
            ),
            "security_email_sender_configured": bool(
                getattr(settings, "DEFAULT_FROM_EMAIL", "")
            ),
        }
        request.current_app = self.name
        return self._render_admin_page(request, "admin/security.html", context)
