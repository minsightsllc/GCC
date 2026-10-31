from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import logging
import secrets
from datetime import timedelta

import segno
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import (
    AdminAccessBlock,
    AdminRecoveryToken,
    AdminSecurityEvent,
    AdminSecurityProfile,
    APIRateLimitBucket,
    CALENDAR_TIME_ZONE,
)


logger = logging.getLogger(__name__)


ADMIN_GATE_USER_ID = "gccad_gate_user_id"
ADMIN_GATE_EXPIRES_AT = "gccad_gate_expires_at"
ADMIN_GATE_NEXT = "gccad_gate_next"
ADMIN_GATE_PENDING_PIN = "gccad_gate_pending_pin"
ADMIN_GATE_PIN_ATTEMPTS = "gccad_gate_pin_attempts"
ADMIN_GATE_LOCKED_UNTIL = "gccad_gate_locked_until"
ADMIN_OTP_PENDING_USER = "gccad_otp_pending_user"
ADMIN_OTP_PENDING_NEXT = "gccad_otp_pending_next"
ADMIN_OTP_ATTEMPTS = "gccad_otp_attempts"
ADMIN_OTP_VERIFIED_USER = "gccad_otp_verified_user"
ADMIN_OTP_ENROLLMENT_DEVICE = "gccad_otp_enrollment_device"
MOBILE_OWNER_MODE = "gcc_mobile_owner_mode"
MOBILE_OWNER_USER_ID = "gcc_mobile_owner_user_id"
MOBILE_OWNER_PENDING_USER = "gcc_mobile_owner_pending_user"
MOBILE_OWNER_PENDING_STEP = "gcc_mobile_owner_pending_step"
MOBILE_OWNER_PENDING_EXPIRES_AT = "gcc_mobile_owner_pending_expires_at"
MOBILE_OWNER_PIN_ATTEMPTS = "gcc_mobile_owner_pin_attempts"
MOBILE_OWNER_PIN_LOCKED_UNTIL = "gcc_mobile_owner_pin_locked_until"
MOBILE_OWNER_OTP_ATTEMPTS = "gcc_mobile_owner_otp_attempts"
MOBILE_OWNER_OTP_LOCKED_UNTIL = "gcc_mobile_owner_otp_locked_until"

ADMIN_GATE_TTL = timedelta(minutes=10)
ADMIN_RECOVERY_TTL = timedelta(minutes=30)
ADMIN_RECOVERY_LOCKOUT = timedelta(minutes=15)
ADMIN_PASSWORD_ATTEMPT_LIMIT = 10
ADMIN_PASSWORD_ATTEMPT_WINDOW = timedelta(minutes=15)
ADMIN_RECOVERY_ACCOUNT_LIMIT = 3
ADMIN_RECOVERY_SOURCE_LIMIT = 10
ADMIN_RECOVERY_RATE_WINDOW = timedelta(hours=1)
ADMIN_PIN_ATTEMPT_LIMIT = 5
ADMIN_OTP_ATTEMPT_LIMIT = 5
MOBILE_OWNER_CHALLENGE_TTL = timedelta(minutes=10)
MOBILE_OWNER_ATTEMPT_LIMIT = 5
MOBILE_OWNER_LOCKOUT = timedelta(minutes=15)


def _normalize_ip_address(value):
    try:
        address = ipaddress.ip_address((value or "").strip())
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return None


def _trusted_proxy_ips():
    configured = getattr(settings, "ADMIN_TRUSTED_PROXY_IPS", ())
    if isinstance(configured, str):
        configured = configured.split(",")
    return {
        normalized
        for normalized in (_normalize_ip_address(value) for value in configured)
        if normalized
    }


def _forwarded_ip_value(value):
    value = (value or "").strip().strip('"')
    if not value or value.lower() == "unknown" or value.startswith("_"):
        return None
    if value.startswith("["):
        closing = value.find("]")
        if closing > 0:
            value = value[1:closing]
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host
    return _normalize_ip_address(value)


def _forwarded_client_ips(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if x_forwarded_for:
        return [
            ip
            for ip in (_forwarded_ip_value(value) for value in x_forwarded_for.split(","))
            if ip
        ]

    forwarded = request.META.get("HTTP_FORWARDED", "")
    values = []
    for forwarded_entry in forwarded.split(","):
        for parameter in forwarded_entry.split(";"):
            key, separator, value = parameter.partition("=")
            if separator and key.strip().lower() == "for":
                ip = _forwarded_ip_value(value)
                if ip:
                    values.append(ip)
                break
    return values


def client_ip(request):
    """Return the normalized client address without trusting spoofable headers."""
    remote_ip = _normalize_ip_address(request.META.get("REMOTE_ADDR"))
    if not remote_ip or remote_ip not in _trusted_proxy_ips():
        return remote_ip

    forwarded_ips = _forwarded_client_ips(request)
    if not forwarded_ips:
        return remote_ip

    for candidate in [remote_ip, *reversed(forwarded_ips)]:
        if candidate not in _trusted_proxy_ips():
            return candidate
    return forwarded_ips[0]


def _admin_rate_bucket_identity(namespace, subject):
    digest = salted_hmac(
        "operations.admin_rate_limit",
        f"{namespace}:{subject}",
    ).hexdigest()
    return f"gcc-admin:{namespace}:{digest}"


def _locked_admin_rate_bucket(identity, now):
    try:
        return APIRateLimitBucket.objects.select_for_update().get(identity=identity)
    except APIRateLimitBucket.DoesNotExist:
        try:
            with transaction.atomic():
                return APIRateLimitBucket.objects.create(
                    identity=identity,
                    window_started_at=now,
                )
        except IntegrityError:
            return APIRateLimitBucket.objects.select_for_update().get(identity=identity)


def _consume_admin_rate_buckets(specs):
    """Consume fixed-window counters for admin auth, storing only HMAC identities."""
    buckets = {}
    for namespace, subject, limit, window in specs:
        identity = _admin_rate_bucket_identity(namespace, subject)
        buckets[identity] = (max(1, int(limit)), window)

    now = timezone.now()
    try:
        with transaction.atomic():
            locked = {
                identity: _locked_admin_rate_bucket(identity, now)
                for identity in sorted(buckets)
            }
            allowed = True
            for identity, bucket in locked.items():
                limit, window = buckets[identity]
                if now >= bucket.window_started_at + window:
                    bucket.window_started_at = now
                    bucket.request_count = 0
                if bucket.request_count >= limit:
                    allowed = False

            for identity, bucket in locked.items():
                limit, _window = buckets[identity]
                if bucket.request_count < limit:
                    bucket.request_count += 1
                bucket.save(update_fields=["window_started_at", "request_count", "updated_at"])
        return allowed
    except DatabaseError:
        logger.exception("Admin authentication rate-limit storage is unavailable.")
        return False


def admin_password_attempt_allowed(request, identifier):
    """Allow at most ten admin password submissions per account and source per 15 minutes."""
    normalized_identifier = " ".join(str(identifier or "").split()).casefold() or "<empty>"
    admin_user = resolve_admin_identifier(identifier)
    account_subject = (
        f"user:{admin_user.pk}"
        if admin_user is not None
        else f"identifier:{normalized_identifier}"
    )
    source_subject = client_ip(request) or "unknown"
    return _consume_admin_rate_buckets(
        (
            ("admin-login-account", account_subject, ADMIN_PASSWORD_ATTEMPT_LIMIT, ADMIN_PASSWORD_ATTEMPT_WINDOW),
            ("admin-login-source", source_subject, ADMIN_PASSWORD_ATTEMPT_LIMIT, ADMIN_PASSWORD_ATTEMPT_WINDOW),
        )
    )


def admin_recovery_request_allowed(request, email):
    """Limit syntactically valid admin recovery requests by email and source IP."""
    normalized_email = (email or "").strip().casefold() or "<empty>"
    source_subject = client_ip(request) or "unknown"
    return _consume_admin_rate_buckets(
        (
            ("admin-recovery-account", normalized_email, ADMIN_RECOVERY_ACCOUNT_LIMIT, ADMIN_RECOVERY_RATE_WINDOW),
            ("admin-recovery-source", source_subject, ADMIN_RECOVERY_SOURCE_LIMIT, ADMIN_RECOVERY_RATE_WINDOW),
        )
    )


def public_password_reset_request_allowed(request, email):
    """Limit public account-reset emails by address and source without exposing matches."""
    normalized_email = (email or "").strip().casefold() or "<empty>"
    source_subject = client_ip(request) or "unknown"
    return _consume_admin_rate_buckets(
        (
            ("public-password-reset-account", normalized_email, 3, ADMIN_RECOVERY_RATE_WINDOW),
            ("public-password-reset-source", source_subject, 10, ADMIN_RECOVERY_RATE_WINDOW),
        )
    )


def admin_ip_block(request):
    address = client_ip(request)
    if not address:
        return None
    return (
        AdminAccessBlock.objects.filter(
            scope=AdminAccessBlock.Scope.IP,
            ip_address=address,
            is_active=True,
        )
        .select_related("created_by")
        .first()
    )


def admin_user_block(user):
    if not is_active_admin(user):
        return None
    return (
        AdminAccessBlock.objects.filter(
            scope=AdminAccessBlock.Scope.USER,
            user=user,
            is_active=True,
        )
        .select_related("created_by")
        .first()
    )


def admin_access_block(request, user=None):
    return admin_ip_block(request) or admin_user_block(user)


def _security_identifier(value):
    return " ".join(str(value or "").split())[:254]


def _admin_email_recipients():
    recipients = []
    seen = set()
    users = get_user_model().objects.filter(
        is_active=True,
        is_staff=True,
        is_superuser=True,
    ).exclude(email="")
    for email in users.values_list("email", flat=True):
        email = (email or "").strip()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            continue
        key = email.casefold()
        if key not in seen:
            seen.add(key)
            recipients.append(email)
    return recipients


def _security_event_local_time(event):
    local_time = timezone.localtime(event.created_at, CALENDAR_TIME_ZONE)
    hour = local_time.strftime("%I").lstrip("0") or "0"
    return (
        f"{local_time.strftime('%B')} {local_time.day}, {local_time.year} "
        f"at {hour}:{local_time.strftime('%M:%S %p %Z')}"
    )


def _security_event_email_body(event):
    lines = [
        "A Grand Coast administration security event was recorded.",
        "",
        f"Event: {event.get_event_type_display()}",
        f"Outcome: {event.get_outcome_display()}",
        f"Time: {_security_event_local_time(event)}",
        f"IP address: {event.ip_address or 'Unavailable'}",
        f"Route: {event.path or 'Unavailable'}",
    ]
    if event.attempted_identifier:
        lines.append(f"Attempted identifier: {event.attempted_identifier}")
    if event.user_id:
        lines.append(f"Resolved administrator: {event.user.get_username()}")
    if event.user_agent:
        lines.append(f"User agent: {event.user_agent}")
    if event.detail:
        lines.append(f"Details: {event.detail}")
    lines.extend(
        [
            "",
            "Review the event in the private administration security dashboard:",
            "/gccad/security/",
        ]
    )
    return "\n".join(lines)


def dispatch_admin_security_email(event_id):
    if not getattr(settings, "ADMIN_SECURITY_EMAIL_ALERTS_ENABLED", True):
        AdminSecurityEvent.objects.filter(pk=event_id).update(
            email_status=AdminSecurityEvent.EmailStatus.DISABLED,
            email_error="Email alerts are disabled by configuration.",
        )
        return False

    now = timezone.now()
    with transaction.atomic():
        event = (
            AdminSecurityEvent.objects.select_for_update()
            .select_related("user")
            .filter(pk=event_id, outcome=AdminSecurityEvent.Outcome.FAILURE)
            .first()
        )
        if event is None:
            return False
        if event.email_status in {
            AdminSecurityEvent.EmailStatus.SENT,
            AdminSecurityEvent.EmailStatus.DISABLED,
        }:
            return event.email_status == AdminSecurityEvent.EmailStatus.SENT
        if (
            event.email_status == AdminSecurityEvent.EmailStatus.PENDING
            and event.email_last_attempt_at
            and now - event.email_last_attempt_at < timedelta(minutes=5)
        ):
            return False
        event.email_status = AdminSecurityEvent.EmailStatus.PENDING
        event.email_attempt_count += 1
        event.email_last_attempt_at = now
        event.email_error = ""
        event.save(
            update_fields=[
                "email_status",
                "email_attempt_count",
                "email_last_attempt_at",
                "email_error",
            ]
        )

    recipients = _admin_email_recipients()
    if not recipients:
        AdminSecurityEvent.objects.filter(pk=event_id).update(
            email_status=AdminSecurityEvent.EmailStatus.NO_RECIPIENT,
            email_error="No active administrator has a valid email address.",
        )
        return False

    from_email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
    if not from_email:
        error = "DEFAULT_FROM_EMAIL is not configured."
        AdminSecurityEvent.objects.filter(pk=event_id).update(
            email_status=AdminSecurityEvent.EmailStatus.FAILED,
            email_error=error,
        )
        logger.error(error)
        return False

    try:
        sent_count = send_mail(
            f"Grand Coast admin security alert: {event.get_event_type_display()}",
            _security_event_email_body(event),
            from_email,
            recipients,
            fail_silently=False,
        )
        if not sent_count:
            raise RuntimeError("The configured email backend accepted no recipients.")
    except Exception as exc:
        error = str(exc)[:2000] or exc.__class__.__name__
        AdminSecurityEvent.objects.filter(pk=event_id).update(
            email_status=AdminSecurityEvent.EmailStatus.FAILED,
            email_error=error,
        )
        logger.exception("Unable to send admin security alert for event %s.", event_id)
        return False

    AdminSecurityEvent.objects.filter(pk=event_id).update(
        email_status=AdminSecurityEvent.EmailStatus.SENT,
        email_sent_at=timezone.now(),
        email_error="",
    )
    return True


def retry_pending_admin_security_emails(limit=100):
    event_ids = list(
        AdminSecurityEvent.objects.filter(
            outcome=AdminSecurityEvent.Outcome.FAILURE,
            email_status__in=[
                AdminSecurityEvent.EmailStatus.PENDING,
                AdminSecurityEvent.EmailStatus.FAILED,
                AdminSecurityEvent.EmailStatus.NO_RECIPIENT,
            ],
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[: max(1, limit)]
    )
    for event_id in event_ids:
        dispatch_admin_security_email(event_id)
    return len(event_ids)


def record_admin_security_event(
    request,
    event_type,
    *,
    outcome=AdminSecurityEvent.Outcome.FAILURE,
    user=None,
    attempted_identifier="",
    detail="",
):
    should_email = outcome == AdminSecurityEvent.Outcome.FAILURE
    if should_email:
        email_status = (
            AdminSecurityEvent.EmailStatus.PENDING
            if getattr(settings, "ADMIN_SECURITY_EMAIL_ALERTS_ENABLED", True)
            else AdminSecurityEvent.EmailStatus.DISABLED
        )
    else:
        email_status = AdminSecurityEvent.EmailStatus.NOT_REQUIRED

    event = AdminSecurityEvent.objects.create(
        event_type=event_type,
        outcome=outcome,
        attempted_identifier=_security_identifier(attempted_identifier),
        user=user if is_active_admin(user) else None,
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:500],
        path=(request.path or "")[:255],
        detail=" ".join(str(detail or "").split())[:255],
        email_status=email_status,
    )
    if email_status == AdminSecurityEvent.EmailStatus.PENDING:
        transaction.on_commit(
            lambda event_id=event.pk: dispatch_admin_security_email(event_id)
        )
    return event


def is_active_admin(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and user.is_staff
        and user.is_superuser
    )


def admin_security_profile(user):
    profile, _ = AdminSecurityProfile.objects.get_or_create(user=user)
    return profile


def confirmed_totp_device(user):
    return (
        TOTPDevice.objects.filter(user=user, confirmed=True)
        .order_by("-last_used_at", "-created_at")
        .first()
    )


def has_totp_enabled(user):
    return confirmed_totp_device(user) is not None


def pin_is_enabled(user):
    profile = admin_security_profile(user)
    return bool(profile.pin_enabled and profile.pin_hash)


def verify_admin_pin(user, pin):
    profile = admin_security_profile(user)
    return bool(profile.pin_enabled and profile.pin_hash and check_password(pin or "", profile.pin_hash))


def resolve_admin_identifier(identifier):
    value = (identifier or "").strip()
    if not value:
        return None
    user_model = get_user_model()
    admins = user_model.objects.filter(is_active=True, is_staff=True, is_superuser=True)
    username_match = admins.filter(username__iexact=value)
    if username_match.count() == 1:
        return username_match.first()
    email_match = admins.filter(email__iexact=value)
    if email_match.count() == 1:
        return email_match.first()
    return None


def _safe_admin_next(value):
    candidate = (value or "").strip()
    if candidate and candidate.startswith(settings.ADMIN_URL_PREFIX) and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={},
        require_https=False,
    ):
        return candidate
    return reverse("admin:index")


def gate_is_valid(request, user=None):
    stored_user = request.session.get(ADMIN_GATE_USER_ID)
    expires_at = request.session.get(ADMIN_GATE_EXPIRES_AT)
    if not stored_user or not expires_at:
        return False
    now = timezone.now()
    try:
        active = float(expires_at) > now.timestamp()
    except (TypeError, ValueError):
        active = False
    if not active:
        clear_gate(request)
        return False
    matches_user = user is None or str(stored_user) == str(user.pk)
    if matches_user:
        request.session[ADMIN_GATE_EXPIRES_AT] = (now + ADMIN_GATE_TTL).timestamp()
        request.session.modified = True
    return matches_user


def gate_next(request):
    return _safe_admin_next(request.session.get(ADMIN_GATE_NEXT))


def set_gate(request, user, next_url=None):
    request.session[ADMIN_GATE_USER_ID] = str(user.pk)
    request.session[ADMIN_GATE_EXPIRES_AT] = (timezone.now() + ADMIN_GATE_TTL).timestamp()
    request.session[ADMIN_GATE_NEXT] = _safe_admin_next(next_url)
    request.session.pop(ADMIN_GATE_PENDING_PIN, None)
    request.session.pop(ADMIN_GATE_PIN_ATTEMPTS, None)
    request.session.modified = True


def clear_gate(request):
    for key in (
        ADMIN_GATE_USER_ID,
        ADMIN_GATE_EXPIRES_AT,
        ADMIN_GATE_NEXT,
        ADMIN_GATE_PENDING_PIN,
        ADMIN_GATE_PIN_ATTEMPTS,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def admin_gate_locked(request):
    locked_until = request.session.get(ADMIN_GATE_LOCKED_UNTIL)
    if not locked_until:
        return False
    try:
        locked = float(locked_until) > timezone.now().timestamp()
    except (TypeError, ValueError):
        locked = False
    if not locked:
        request.session.pop(ADMIN_GATE_LOCKED_UNTIL, None)
        request.session.pop(ADMIN_GATE_PIN_ATTEMPTS, None)
        request.session.modified = True
    return locked


def register_gate_failure(request):
    attempts = int(request.session.get(ADMIN_GATE_PIN_ATTEMPTS, 0)) + 1
    request.session[ADMIN_GATE_PIN_ATTEMPTS] = attempts
    if attempts >= ADMIN_PIN_ATTEMPT_LIMIT:
        request.session[ADMIN_GATE_LOCKED_UNTIL] = (
            timezone.now() + ADMIN_RECOVERY_LOCKOUT
        ).timestamp()
    request.session.modified = True


def begin_otp_challenge(request, user, next_url=None):
    request.session[ADMIN_OTP_PENDING_USER] = str(user.pk)
    request.session[ADMIN_OTP_PENDING_NEXT] = _safe_admin_next(next_url)
    request.session[ADMIN_OTP_ATTEMPTS] = 0
    request.session.modified = True


def pending_otp_user(request):
    user_id = request.session.get(ADMIN_OTP_PENDING_USER)
    if not user_id:
        return None
    user = get_user_model().objects.filter(
        pk=user_id,
        is_active=True,
        is_staff=True,
        is_superuser=True,
    ).first()
    if user is None:
        clear_otp_challenge(request)
    return user


def pending_otp_next(request):
    return _safe_admin_next(request.session.get(ADMIN_OTP_PENDING_NEXT))


def clear_otp_challenge(request):
    for key in (
        ADMIN_OTP_PENDING_USER,
        ADMIN_OTP_PENDING_NEXT,
        ADMIN_OTP_ATTEMPTS,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def clear_admin_challenges(request):
    clear_gate(request)
    clear_otp_challenge(request)
    for key in (
        ADMIN_GATE_LOCKED_UNTIL,
        ADMIN_OTP_VERIFIED_USER,
        ADMIN_OTP_ENROLLMENT_DEVICE,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def mark_otp_verified(request, user, device):
    request.session[ADMIN_OTP_VERIFIED_USER] = str(user.pk)
    request.session.modified = True
    otp_login(request, device)


def otp_is_verified(request, user):
    return str(request.session.get(ADMIN_OTP_VERIFIED_USER, "")) == str(user.pk)


def register_otp_failure(request):
    attempts = int(request.session.get(ADMIN_OTP_ATTEMPTS, 0)) + 1
    request.session[ADMIN_OTP_ATTEMPTS] = attempts
    request.session.modified = True
    return attempts >= ADMIN_OTP_ATTEMPT_LIMIT


def begin_totp_enrollment(user):
    TOTPDevice.objects.filter(user=user, confirmed=False).delete()
    return TOTPDevice.objects.create(
        user=user,
        name="Grand Coast authenticator",
        confirmed=False,
        digits=6,
    )


def remove_totp_devices(user):
    TOTPDevice.objects.filter(user=user).delete()


def totp_qr_data_uri(device):
    qr = segno.make(device.config_url, micro=False)
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=5)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def totp_manual_key(device):
    return base64.b32encode(device.bin_key).decode("ascii").rstrip("=")


@transaction.atomic
def create_admin_recovery_token(request, user):
    now = timezone.now()
    raw_token = secrets.token_urlsafe(32)
    token = AdminRecoveryToken.objects.create(
        user=user,
        purpose=AdminRecoveryToken.Purpose.SECURITY_RESET,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        expires_at=now + ADMIN_RECOVERY_TTL,
    )
    recovery_url = request.build_absolute_uri(
        reverse("admin:recovery-confirm", kwargs={"token": raw_token})
    )
    send_mail(
        "Grand Coast administration security recovery",
        (
            f"Use this one-time link to reset your Grand Coast admin PIN and disable "
            f"authenticator 2FA:\n\n{recovery_url}\n\n"
            f"This link expires in 30 minutes and will not sign you in automatically."
        ),
        getattr(settings, "DEFAULT_FROM_EMAIL", "webmaster@localhost"),
        [user.email],
    )
    return token


def find_admin_recovery_token(raw_token):
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    token = AdminRecoveryToken.objects.select_related("user").filter(
        token_hash=token_hash,
        purpose=AdminRecoveryToken.Purpose.SECURITY_RESET,
    ).first()
    return token if token and token.is_usable and is_active_admin(token.user) else None


@transaction.atomic
def consume_admin_recovery_token(raw_token):
    token_hash = hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()
    candidate = AdminRecoveryToken.objects.filter(
        token_hash=token_hash,
        purpose=AdminRecoveryToken.Purpose.SECURITY_RESET,
    ).first()
    if candidate is None:
        return None
    # Serialize consumption across separate links for the same account. This
    # keeps two concurrent valid links from both resetting security settings.
    user = get_user_model().objects.select_for_update().filter(pk=candidate.user_id).first()
    if user is None:
        return None
    token = (
        AdminRecoveryToken.objects.select_for_update()
        .filter(pk=candidate.pk)
        .first()
    )
    if token is None or not token.is_usable or not is_active_admin(user):
        return None
    now = timezone.now()
    token.used_at = now
    token.save(update_fields=["used_at"])
    AdminRecoveryToken.objects.filter(
        user=token.user,
        purpose=AdminRecoveryToken.Purpose.SECURITY_RESET,
        used_at__isnull=True,
    ).exclude(pk=token.pk).update(used_at=now)
    return token


def _mobile_owner_factor_keys(factor):
    if factor == "pin":
        return MOBILE_OWNER_PIN_ATTEMPTS, MOBILE_OWNER_PIN_LOCKED_UNTIL
    if factor == "otp":
        return MOBILE_OWNER_OTP_ATTEMPTS, MOBILE_OWNER_OTP_LOCKED_UNTIL
    raise ValueError("Unsupported mobile owner factor.")


def set_mobile_owner_challenge(request, user, step):
    """Start a short-lived, unauthenticated mobile-owner factor challenge."""
    if step not in {"pin", "otp"}:
        raise ValueError("Unsupported mobile owner challenge step.")
    attempts_key, _ = _mobile_owner_factor_keys(step)
    request.session[MOBILE_OWNER_PENDING_USER] = str(user.pk)
    request.session[MOBILE_OWNER_PENDING_STEP] = step
    request.session[MOBILE_OWNER_PENDING_EXPIRES_AT] = (
        timezone.now() + MOBILE_OWNER_CHALLENGE_TTL
    ).timestamp()
    if not mobile_owner_factor_locked(request, step):
        request.session[attempts_key] = 0
    request.session.modified = True


def pending_mobile_owner_user(request, step=None):
    user_id = request.session.get(MOBILE_OWNER_PENDING_USER)
    stored_step = request.session.get(MOBILE_OWNER_PENDING_STEP)
    expires_at = request.session.get(MOBILE_OWNER_PENDING_EXPIRES_AT)
    if not user_id or stored_step not in {"pin", "otp"} or not expires_at:
        clear_mobile_owner_challenge(request)
        return None
    if step is not None and stored_step != step:
        return None
    try:
        active = float(expires_at) > timezone.now().timestamp()
    except (TypeError, ValueError):
        active = False
    if not active:
        clear_mobile_owner_challenge(request)
        return None
    user = get_user_model().objects.filter(
        pk=user_id,
        is_active=True,
        is_staff=True,
        is_superuser=True,
    ).first()
    if user is None:
        clear_mobile_owner_challenge(request)
    return user


def mobile_owner_challenge_step(request):
    return request.session.get(MOBILE_OWNER_PENDING_STEP)


def mobile_owner_factor_locked(request, factor):
    _, locked_key = _mobile_owner_factor_keys(factor)
    locked_until = request.session.get(locked_key)
    if not locked_until:
        return False
    try:
        locked = float(locked_until) > timezone.now().timestamp()
    except (TypeError, ValueError):
        locked = False
    if not locked:
        attempts_key, _ = _mobile_owner_factor_keys(factor)
        request.session.pop(locked_key, None)
        request.session.pop(attempts_key, None)
        request.session.modified = True
    return locked


def register_mobile_owner_failure(request, factor):
    attempts_key, locked_key = _mobile_owner_factor_keys(factor)
    attempts = int(request.session.get(attempts_key, 0)) + 1
    request.session[attempts_key] = attempts
    if attempts >= MOBILE_OWNER_ATTEMPT_LIMIT:
        request.session[locked_key] = (
            timezone.now() + MOBILE_OWNER_LOCKOUT
        ).timestamp()
    request.session.modified = True
    return attempts >= MOBILE_OWNER_ATTEMPT_LIMIT


def clear_mobile_owner_challenge(request):
    for key in (
        MOBILE_OWNER_PENDING_USER,
        MOBILE_OWNER_PENDING_STEP,
        MOBILE_OWNER_PENDING_EXPIRES_AT,
        MOBILE_OWNER_PIN_ATTEMPTS,
        MOBILE_OWNER_PIN_LOCKED_UNTIL,
        MOBILE_OWNER_OTP_ATTEMPTS,
        MOBILE_OWNER_OTP_LOCKED_UNTIL,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def mark_mobile_owner_session(request, user):
    clear_mobile_owner_challenge(request)
    request.session[MOBILE_OWNER_MODE] = True
    request.session[MOBILE_OWNER_USER_ID] = str(user.pk)
    request.session.modified = True


def clear_mobile_owner_session(request):
    request.session.pop(MOBILE_OWNER_MODE, None)
    request.session.pop(MOBILE_OWNER_USER_ID, None)
    request.session.modified = True


def is_mobile_owner_session(request):
    return bool(
        request
        and request.session.get(MOBILE_OWNER_MODE)
        and str(request.session.get(MOBILE_OWNER_USER_ID, ""))
        == str(getattr(getattr(request, "user", None), "pk", ""))
        and is_active_admin(getattr(request, "user", None))
    )


class PasswordResetThrottleMixin:
    def form_valid(self, form):
        allowed = public_password_reset_request_allowed(
            self.request,
            form.cleaned_data["email"],
        )
        if allowed:
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
                logger.exception("Public password reset email could not be sent.")
        return HttpResponseRedirect(self.get_success_url())


def revoke_user_sessions(user):
    for session in Session.objects.all():
        if str(session.get_decoded().get("_auth_user_id", "")) == str(user.pk):
            session.delete()


def reset_admin_security(user, new_pin):
    profile = admin_security_profile(user)
    profile.pin_enabled = True
    profile.pin_hash = make_password(new_pin)
    profile.save(update_fields=["pin_enabled", "pin_hash", "updated_at"])
    remove_totp_devices(user)
