from __future__ import annotations

import calendar as calendar_module
import json
import mimetypes
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login, logout
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import (
    ClientInviteAcceptForm,
    ClientMessageForm,
    ContentStudioForm,
    ContactLeadForm,
    ClientForm,
    ExternalEstimateCreateForm,
    EmployeeInviteForm,
    EmployeeInviteAcceptForm,
    EmployeeProfileForm,
    AccountDeleteForm,
    AdminOtpForm,
    AdminPinForm,
    CalendarDayOverrideForm,
    EmployeeScheduleOverrideForm,
    EmployeeWeeklyScheduleForm,
    LeadForm,
    LeadAssignmentForm,
    LeadNoteForm,
    LeadStatusForm,
    MediaEditForm,
    MediaUploadForm,
    MediaPublicationForm,
    ServiceForm,
    PublicPasswordResetForm,
    ProjectCreateForm,
    ProjectDocumentForm,
    ProjectForm,
    ProjectUpdateForm,
    ProjectUpdateDraftForm,
    PublicAuthenticationForm,
    QuickTaskForm,
    ScheduleEventForm,
    StaffMessageForm,
    TeamTaskUpdateForm,
    TaskForm,
    TimeEntryForm,
)
from .models import (
    Activity,
    AdminRecoveryToken,
    CALENDAR_TIME_ZONE,
    AdminSecurityProfile,
    AdminSecurityEvent,
    Agreement,
    ChangeOrder,
    CalendarDayOverride,
    Client,
    ClientInvite,
    ClientNotification,
    ClientMessage,
    Estimate,
    GoogleReview,
    EmployeeInvite,
    EmployeeProfile,
    EmployeeNotification,
    EmployeeScheduleOverride,
    EmployeeWeeklySchedule,
    EmailOutbox,
    Lead,
    LeadAttachment,
    MediaAsset,
    MediaPublication,
    Milestone,
    ProcessStep,
    Project,
    ProjectDocument,
    ProjectUpdate,
    ProjectUpdateDraft,
    PaymentSchedule,
    ScheduleEvent,
    Selection,
    Service,
    SiteSettings,
    Inspection,
    SiteVisit,
    SubcontractorAssignment,
    Task,
    TimeEntry,
    MobilePushDevice,
    WorkflowEvent,
    effective_employee_schedule,
    schedule_event_local_dates,
    sanitize_uploaded_name,
)
from .turnstile import get_turnstile_site_key, is_mobile_owner_login, is_mobile_webview
from .publishing import client_is_live, media_presentation, publication_for, publish, unpublish, website_is_live
from .security import (
    admin_ip_block,
    admin_user_block,
    confirmed_totp_device,
    is_active_admin,
    gate_is_valid,
    mark_mobile_owner_session,
    mobile_owner_factor_locked,
    pending_mobile_owner_user,
    pin_is_enabled,
    PasswordResetThrottleMixin,
    record_admin_security_event,
    register_mobile_owner_failure,
    resolve_admin_identifier,
    set_mobile_owner_challenge,
    verify_admin_pin,
)
from .services import (
    complete_client_invite,
    complete_employee_invite,
    create_client_invite,
    create_employee_invite,
    find_invite,
    find_employee_invite,
    get_or_create_client_for_lead,
    record_activity,
)
from .google_reviews import (
    GoogleReviewsConfigurationError,
    GoogleReviewsFetchError,
    sync_google_reviews,
)
from .construction_services import (
    accept_agreement as accept_agreement_command,
    advance_selection as advance_selection_command,
    approve_change_order as approve_change_order_command,
    convert_lead as convert_lead_command,
    create_project_from_estimate,
    record_external_estimate_status,
    record_external_invoice_status,
    set_milestone_status,
)
from .construction_policies import (
    can_manage_external_estimate,
    can_view_agreement,
    can_view_lead,
    can_view_media,
    can_view_project_document,
    can_view_external_estimate,
    execution_loop_enabled_for,
    is_client,
    external_estimate_enabled_for,
    visible_estimates,
)
from .construction_forms import ExternalEstimateStatusForm, ExternalInvoiceStatusForm
from .notifications import queue_client_notifications, queue_employee_notifications
from .email_delivery import (
    deliver_email_outbox,
    queue_client_invite_email,
    queue_email,
    queue_employee_invite_email,
)


PUBLIC_PAGES = {"home", "services", "projects", "process", "contact"}
DASHBOARD_SECTIONS = {"overview", "clients", "leads", "estimates", "projects", "tasks", "calendar", "time", "media", "documents", "team", "notifications", "content"}
TEAM_SECTIONS = {"overview", "projects", "tasks", "calendar", "time", "media", "profile", "notifications"}
EMPLOYEE_GROUPS = {"Manager", "Office", "Field", "Sales"}
LEADERSHIP_GROUPS = {"Owner", "Manager"}


def _prepare_client_invite_delivery(request, *, client, invite, raw_token, actor):
    '''Queue and, when enabled, immediately deliver a client portal invite.'''
    invite_url = request.build_absolute_uri(
        reverse('operations:client-invite', kwargs={'token': raw_token})
    )
    outbox = queue_client_invite_email(invite, invite_url, actor=actor)
    outbox = deliver_email_outbox(outbox)
    request.session['last_invite_url'] = invite_url
    request.session['last_invite_email_status'] = outbox.status if outbox else ''
    request.session['last_invite_recipient'] = client.email
    if outbox and outbox.status == EmailOutbox.Status.SENT:
        messages.success(
            request,
            f'A one-time client portal invite was emailed to {client.email}. '
            'The copy link remains available below.',
        )
    elif outbox and outbox.status == EmailOutbox.Status.FAILED:
        messages.warning(
            request,
            'The invite was created, but email delivery failed. '
            'Use the one-time copy link below.',
        )
    elif outbox:
        messages.info(
            request,
            'The invite was created. Email delivery is pending or disabled; '
            'use the one-time copy link below.',
        )
    else:
        messages.warning(
            request,
            'The invite was created, but this client has no deliverable email address. '
            'Use the one-time copy link below.',
        )
    return invite_url, outbox


def pwa_manifest(request):
    return JsonResponse(
        {
            "name": "Grand Coast Construction",
            "short_name": "Grand Coast",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f5f7fa",
            "theme_color": "#31518c",
            "description": "Grand Coast Construction Inc. project and client workspace.",
            "icons": [
                {
                    "src": static("operations/images/gcc-logo.png"),
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": static("operations/images/gcc-logo.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        content_type="application/manifest+json",
    )


def pwa_service_worker(request):
    response = HttpResponse(
        render_to_string("operations/service-worker.js"),
        content_type="application/javascript",
    )
    response["Cache-Control"] = "no-cache"
    return response


def pwa_offline(request):
    return render(request, "operations/offline.html")


def _staff_users():
    return get_user_model().objects.filter(
        is_staff=True,
        is_active=True,
    ).filter(
        Q(is_superuser=True) | Q(groups__name__in=EMPLOYEE_GROUPS)
    ).filter(
        Q(employee_profile__isnull=True) | Q(employee_profile__is_active=True)
    ).order_by("first_name", "last_name", "username").distinct()


def _group_names(user):
    if not user.is_authenticated:
        return set()
    return set(user.groups.values_list("name", flat=True))


def _is_owner(user):
    return bool(user.is_superuser or "Owner" in _group_names(user))


def _is_active_staff(user):
    if not user.is_authenticated or not user.is_staff or not user.is_active:
        return False
    return not EmployeeProfile.objects.filter(user=user, is_active=False).exists()


def _is_active_client(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and not user.is_staff
        and not user.is_superuser
        and Client.objects.filter(user=user).exists()
    )


def _can_access_dashboard(user):
    return bool(_is_active_staff(user) and user.is_superuser)


def _can_manage_team(user):
    return _can_access_dashboard(user)


def _can_manage_schedule(user):
    return _can_access_dashboard(user)


def _can_manage_content(user):
    return _can_access_dashboard(user)


def _is_field_employee(user):
    return bool(user.is_staff and "Field" in _group_names(user) and not _can_access_dashboard(user))


def _can_access_team(user):
    roles = _group_names(user)
    if not _is_active_staff(user):
        return False
    return bool(not user.is_superuser and roles & EMPLOYEE_GROUPS)


def _can_delete_employee_account(user):
    roles = _group_names(user)
    return bool(
        _is_active_staff(user)
        and not _is_owner(user)
        and roles & EMPLOYEE_GROUPS
    )


def _can_delete_account(user):
    return bool(_can_delete_employee_account(user) or _is_active_client(user))


def _deleted_account_username(user_id):
    user_model = get_user_model()
    base = f"deleted-account-{user_id}"
    candidate = base[:150]
    suffix = 1
    while user_model.objects.filter(username=candidate).exclude(pk=user_id).exists():
        candidate = f"{base}-{suffix}"[:150]
        suffix += 1
    return candidate


def _can_view_clients(user):
    return _can_access_dashboard(user)


def _can_manage_tasks(user):
    return _can_access_dashboard(user)


def _can_manage_documents(user):
    return _can_access_dashboard(user)


def _can_manage_messages(user):
    return _can_access_dashboard(user)


def _visible_projects_for_user(user):
    if _can_access_dashboard(user):
        return Project.objects.all()
    return Project.objects.filter(Q(assigned_staff=user) | Q(project_manager=user)).distinct()


def _visible_tasks_for_user(user):
    visible_leads = Q(lead__isnull=True) | Q(lead__deleted_at__isnull=True)
    if _can_access_dashboard(user):
        return Task.objects.filter(visible_leads)
    return Task.objects.filter(visible_leads).filter(
        Q(assigned_to=user)
        | Q(watchers=user)
        | Q(project__assigned_staff=user)
        | Q(project__project_manager=user)
        | Q(lead__assigned_to=user)
    ).distinct()


def _visible_schedule_for_user(user):
    if _can_access_dashboard(user):
        return ScheduleEvent.objects.all()
    return ScheduleEvent.objects.filter(assignees=user).distinct()


def _visible_team_schedule_for_user(user):
    if _can_access_dashboard(user) or _group_names(user) & LEADERSHIP_GROUPS:
        return ScheduleEvent.objects.all()
    return ScheduleEvent.objects.filter(assignees=user).distinct()


def _active_employee_users():
    return _staff_users().filter(
        employee_profile__is_active=True,
        employee_profile__user__is_active=True,
    )


def _visible_time_for_user(user):
    if _can_access_dashboard(user):
        return TimeEntry.objects.all()
    return TimeEntry.objects.filter(employee=user)


def _visible_clients_for_user(user):
    if _can_access_dashboard(user):
        return Client.objects.all()
    return Client.objects.filter(
        Q(projects__assigned_staff=user) | Q(projects__project_manager=user)
    ).distinct()


def _operations_navigation_counts(user, *, team_mode=False):
    """Return the counts shown beside operations navigation categories.

    Counts are calculated from the same visibility querysets used by the
    workspace itself so employee badges cannot disclose records outside their
    assignments.  ``team_mode`` controls whether a superuser is rendering the
    employee-style workspace or the full admin operations workspace.
    """
    now = timezone.now()
    is_admin_workspace = _can_access_dashboard(user) and not team_mode
    projects_qs = _visible_projects_for_user(user)
    tasks_qs = _visible_tasks_for_user(user)
    schedule_qs = (
        _visible_team_schedule_for_user(user)
        if team_mode
        else _visible_schedule_for_user(user)
    )

    open_tasks_count = tasks_qs.filter(
        status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED]
    ).count()
    upcoming_events_count = schedule_qs.filter(start_at__gte=now).count()
    active_time_count = _visible_time_for_user(user).filter(clock_out__isnull=True).count()
    active_projects_count = projects_qs.exclude(status=Project.Status.COMPLETE).count()
    visible_documents_count = ProjectDocument.objects.filter(project__in=projects_qs).count()
    visible_media_count = MediaAsset.objects.filter(project__in=projects_qs).count()
    unread_notifications_count = EmployeeNotification.objects.filter(
        employee=user,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).count()

    if is_admin_workspace:
        unread_messages_count = ClientMessage.objects.filter(
            is_read=False,
            sent_by__is_staff=False,
        ).count()
        open_leads_count = Lead.objects.filter(deleted_at__isnull=True).exclude(
            status__in=[Lead.Status.WON, Lead.Status.LOST]
        ).count()
        pending_estimates_count = Estimate.objects.filter(
            status__in=[Estimate.Status.DRAFT, Estimate.Status.SENT]
        ).count()
        content_count = (
            Service.objects.filter(is_active=True).count()
            + ProcessStep.objects.count()
        )
        team_count = EmployeeProfile.objects.filter(
            is_active=True,
            user__is_active=True,
            user__is_staff=True,
        ).count()
        clients_count = Client.objects.count()
        overview_count = open_tasks_count + upcoming_events_count + unread_messages_count + unread_notifications_count
    else:
        unread_messages_count = 0
        open_leads_count = 0
        pending_estimates_count = 0
        content_count = 0
        team_count = 0
        clients_count = 0
        overview_count = open_tasks_count + upcoming_events_count + unread_notifications_count

    return {
        "overview": overview_count,
        "clients": clients_count,
        "tasks": open_tasks_count,
        "calendar": upcoming_events_count,
        "time": active_time_count,
        "documents": visible_documents_count,
        "leads": open_leads_count,
        "estimates": pending_estimates_count,
        "projects": active_projects_count,
        "media": visible_media_count,
        "content": content_count,
        "team": team_count,
        "messages": unread_messages_count,
        "notifications": unread_notifications_count,
        "profile": 0,
    }


def _site_settings():
    site_settings, _ = SiteSettings.objects.get_or_create(pk=1)
    return site_settings


def _asset(path):
    if not path:
        return ""
    if path.startswith(("http://", "https://", "/")):
        return path
    return static(path)


def _project_image(project):
    if project and project.cover:
        return reverse("operations:project-cover", kwargs={"pk": project.pk})
    return _asset(project.fallback_image) if project else ""


def _public_context():
    site_settings = _site_settings()
    google_reviews = list(GoogleReview.objects.all()[:5])
    services = list(Service.objects.filter(is_active=True))
    process_steps = list(ProcessStep.objects.all())
    for service in services:
        service.display_image = reverse("operations:service-image", kwargs={"pk": service.pk}) if service.image else _asset(service.image_path)
    projects = list(
        Project.objects.filter(is_published=True)
        .select_related("client")
        .prefetch_related("media_assets")
    )
    for project in projects:
        project.display_image = _project_image(project)
    featured_project = site_settings.featured_project
    public_featured_project = featured_project if featured_project and featured_project.is_published else None
    return {
        "site_settings": site_settings,
        "google_reviews": google_reviews,
        "services": services,
        "process_steps": process_steps,
        "public_projects": projects,
        "featured_project": public_featured_project,
        "featured_project_image": (
            _project_image(public_featured_project)
            if public_featured_project
            else static("operations/images/project-bathroom.png")
        ),
    }


def _public_dashboard_url(user):
    if _can_access_dashboard(user):
        return reverse("operations:dashboard")
    if _can_access_team(user) and not user.is_superuser:
        return reverse("operations:team")
    if _is_active_client(user):
        return reverse("operations:portal")
    return ""


def public_page(request, page="home"):
    if page not in PUBLIC_PAGES:
        page = "home"
    context = _public_context()
    context["page"] = page
    context["public_dashboard_url"] = _public_dashboard_url(request.user)
    context["contact_form"] = ContactLeadForm(request=request)
    context["turnstile_site_key"] = get_turnstile_site_key(request)
    if page == "contact" and request.method == "POST":
        form = ContactLeadForm(request.POST, request.FILES, request=request)
        context["contact_form"] = form
        if form.is_valid():
            full_name = f"{form.cleaned_data['first_name'].strip()} {form.cleaned_data['last_name'].strip()}".strip()
            with transaction.atomic():
                lead = Lead.objects.create(
                    name=full_name,
                    email=form.cleaned_data["email"],
                    phone=form.cleaned_data["phone"],
                    service=form.cleaned_data["project_type"],
                    location=form.cleaned_data["location"],
                    note=form.cleaned_data["message"],
                    source="Website form",
                )
                for upload in form.cleaned_data.get("photos", []):
                    original_name = sanitize_uploaded_name(upload.name)
                    upload.name = original_name
                    LeadAttachment.objects.create(lead=lead, file=upload, original_name=original_name)
                record_activity("New lead received", f"{lead.name} · Website form", lead=lead)
            messages.success(request, "Thanks for sharing your project. We will be in touch soon.")
            return redirect("operations:contact")
    return render(request, "operations/public.html", context)


LEGAL_PAGES = {
    "privacy": "Privacy Policy",
    "terms": "Terms of Service",
}


@require_GET
def legal_page(request, page):
    if page not in LEGAL_PAGES:
        raise Http404
    return render(
        request,
        "operations/legal.html",
        {"page": page, "page_title": LEGAL_PAGES[page]},
    )


def staff_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _can_access_dashboard(request.user):
            raise PermissionDenied
        gate_is_valid(request, request.user)
        return view_func(request, *args, **kwargs)

    return wrapped


def team_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _can_access_team(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def workspace_staff_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        # These handlers are shared by the Owner dashboard and employee
        # workspace. Authorize according to the URL's workspace.
        allowed = (
            _can_access_team(request.user)
            if request.path.startswith("/team/")
            else _can_access_dashboard(request.user)
        )
        if not allowed:
            raise PermissionDenied
        if not request.path.startswith("/team/"):
            gate_is_valid(request, request.user)
        return view_func(request, *args, **kwargs)

    return wrapped


def client_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.user.is_staff or not _is_active_client(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def _mobile_owner_login_url():
    return f"{reverse('operations:login')}?mobile=1"


def _mobile_owner_challenge_url(name):
    return f"{reverse(f'operations:{name}')}?mobile=1"


def _mobile_owner_request_allowed(request):
    if not is_mobile_webview(request) or not getattr(
        settings, "GCC_MOBILE_OWNER_ACCESS_ENABLED", False
    ):
        return False
    return is_mobile_owner_login(request) or bool(
        request.session.get("gcc_mobile_owner_pending_user")
    )


def _mobile_owner_security_block(request, user):
    return admin_ip_block(request) or admin_user_block(user)


def _complete_mobile_owner_login(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    mark_mobile_owner_session(request, user)
    record_admin_security_event(
        request,
        AdminSecurityEvent.EventType.LOGIN_SUCCESS,
        outcome=AdminSecurityEvent.Outcome.SUCCESS,
        user=user,
        attempted_identifier=user.get_username(),
        detail="Mobile Operations owner sign-in completed.",
    )
    return redirect("operations:dashboard")


def _mobile_owner_factor_failure(request, user, factor):
    locked = register_mobile_owner_failure(request, factor)
    record_admin_security_event(
        request,
        (
            AdminSecurityEvent.EventType.PIN_FAILURE
            if factor == "pin"
            else AdminSecurityEvent.EventType.OTP_FAILURE
        ),
        user=user,
        attempted_identifier=user.get_username(),
        detail=(
            "The mobile Operations admin PIN could not be verified."
            if factor == "pin"
            else "The mobile Operations authenticator code could not be verified."
        ),
    )
    return locked


def _render_mobile_owner_challenge(request, *, form, step, status=200):
    return render(
        request,
        "operations/mobile_owner_challenge.html",
        {
            "form": form,
            "step": step,
            "mobile_owner_locked": mobile_owner_factor_locked(request, step),
        },
        status=status,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def mobile_owner_pin(request):
    if not _mobile_owner_request_allowed(request):
        return redirect(_mobile_owner_login_url())
    user = pending_mobile_owner_user(request, "pin")
    if user is None:
        return redirect(_mobile_owner_login_url())

    form = AdminPinForm(request.POST or None)
    blocked = _mobile_owner_security_block(request, user)
    if blocked is not None:
        if request.method == "POST":
            record_admin_security_event(
                request,
                AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                outcome=AdminSecurityEvent.Outcome.BLOCKED,
                user=user,
                attempted_identifier=user.get_username(),
                detail="The administrator account or source IP is blocked from mobile Operations access.",
            )
        form.add_error(None, "Unable to verify administrator access.")
        return _render_mobile_owner_challenge(request, form=form, step="pin", status=403)

    locked = mobile_owner_factor_locked(request, "pin")
    if request.method == "POST" and not locked and form.is_valid():
        if verify_admin_pin(user, form.cleaned_data["pin"]):
            if confirmed_totp_device(user) is not None:
                set_mobile_owner_challenge(request, user, "otp")
                return redirect(_mobile_owner_challenge_url("mobile-owner-otp"))
            return _complete_mobile_owner_login(request, user)
        locked = _mobile_owner_factor_failure(request, user, "pin")
        form.add_error(None, "That verification code could not be verified.")

    if locked:
        form.add_error(None, "Too many attempts. Try again in 15 minutes.")
    return _render_mobile_owner_challenge(
        request,
        form=form,
        step="pin",
        status=429 if locked else 200,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def mobile_owner_otp(request):
    if not _mobile_owner_request_allowed(request):
        return redirect(_mobile_owner_login_url())
    user = pending_mobile_owner_user(request, "otp")
    if user is None:
        return redirect(_mobile_owner_login_url())

    form = AdminOtpForm(request.POST or None)
    blocked = _mobile_owner_security_block(request, user)
    if blocked is not None:
        if request.method == "POST":
            record_admin_security_event(
                request,
                AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                outcome=AdminSecurityEvent.Outcome.BLOCKED,
                user=user,
                attempted_identifier=user.get_username(),
                detail="The administrator account or source IP is blocked from mobile Operations access.",
            )
        form.add_error(None, "Unable to verify administrator access.")
        return _render_mobile_owner_challenge(request, form=form, step="otp", status=403)

    locked = mobile_owner_factor_locked(request, "otp")
    device = confirmed_totp_device(user)
    if request.method == "POST" and not locked and form.is_valid():
        valid = False
        if device is not None:
            try:
                valid = bool(device.verify_token(form.cleaned_data["token"]))
            except (TypeError, ValueError):
                valid = False
        if valid:
            return _complete_mobile_owner_login(request, user)
        locked = _mobile_owner_factor_failure(request, user, "otp")
        form.add_error(None, "That verification code could not be verified.")

    if locked:
        form.add_error(None, "Too many attempts. Try again in 15 minutes.")
    return _render_mobile_owner_challenge(
        request,
        form=form,
        step="otp",
        status=429 if locked else 200,
    )


class GrandCoastLoginView(LoginView):
    template_name = "operations/login.html"
    authentication_form = PublicAuthenticationForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["turnstile_site_key"] = get_turnstile_site_key(self.request)
        context["mobile_owner_mode"] = is_mobile_owner_login(self.request)
        return context

    def form_invalid(self, form):
        if (
            self.request.method == "POST"
            and is_mobile_owner_login(self.request)
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
                    "The mobile Operations administrator password could not be verified."
                    if resolved_admin is not None
                    else "The identifier did not match an active administrator for mobile Operations."
                ),
            )
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        if user.is_superuser and is_mobile_owner_login(self.request):
            if not is_active_admin(user) or _mobile_owner_security_block(self.request, user):
                record_admin_security_event(
                    self.request,
                    AdminSecurityEvent.EventType.ACCESS_BLOCKED,
                    outcome=AdminSecurityEvent.Outcome.BLOCKED,
                    user=user,
                    attempted_identifier=user.get_username(),
                    detail="The administrator account or source IP is blocked from mobile Operations access.",
                )
                form.add_error(None, "Please enter a correct username and password.")
                return self.form_invalid(form)
            if pin_is_enabled(user):
                set_mobile_owner_challenge(self.request, user, "pin")
                return redirect(_mobile_owner_challenge_url("mobile-owner-pin"))
            if confirmed_totp_device(user) is not None:
                set_mobile_owner_challenge(self.request, user, "otp")
                return redirect(_mobile_owner_challenge_url("mobile-owner-otp"))
            return _complete_mobile_owner_login(self.request, user)
        return super().form_valid(form)

    def get_success_url(self):
        if self.request.user.is_staff:
            return reverse("operations:dashboard") if _can_access_dashboard(self.request.user) else reverse("operations:team")
        return reverse("operations:portal")


class GrandCoastLogoutView(LogoutView):
    next_page = reverse_lazy("operations:login")

    def get_success_url(self):
        # Django clears the session during logout, including the marker that
        # selects the mobile owner login flow. Re-seed that flow on the next
        # login page for marked WebView requests so an owner can switch
        # accounts and authenticate again without restarting the app.
        if is_mobile_webview(self.request):
            return _mobile_owner_login_url()
        return super().get_success_url()


class GrandCoastPasswordChangeView(PasswordChangeView):
    template_name = "operations/password_change.html"

    def get_success_url(self):
        if self.request.user.is_staff:
            return reverse("operations:dashboard") if _can_access_dashboard(self.request.user) else reverse("operations:team")
        return reverse("operations:portal")


class PublicPasswordResetView(PasswordResetThrottleMixin, PasswordResetView):
    form_class = PublicPasswordResetForm
    template_name = "operations/password_reset_form.html"
    email_template_name = "operations/password_reset_email.txt"
    subject_template_name = "operations/password_reset_subject.txt"
    success_url = reverse_lazy("operations:password-reset-done")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["turnstile_site_key"] = get_turnstile_site_key(self.request)
        return context


class PublicPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "operations/password_reset_confirm.html"
    success_url = reverse_lazy("operations:password-reset-complete")


class PublicPasswordResetDoneView(PasswordResetDoneView):
    template_name = "operations/password_reset_done.html"


class PublicPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "operations/password_reset_complete.html"


def _dashboard_redirect(section, **params):
    url = reverse("operations:dashboard-section", kwargs={"section": section})
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    return redirect(f"{url}?{urlencode(clean_params)}" if clean_params else url)


def _active_lead_or_404(pk):
    return get_object_or_404(Lead, pk=pk, deleted_at__isnull=True)


def _content_form(instance, services, process_steps, data=None):
    initial = {
        "headline": instance.headline,
        "subheadline": instance.subheadline,
        "featured_title": instance.featured_title,
        "featured_body": instance.featured_body,
        "google_review_url": instance.google_review_url,
        "services_title": instance.services_title,
        "services_intro": instance.services_intro,
        "services_note": instance.services_note,
    }
    for step in process_steps:
        initial[f"step_{step.key}"] = step.title
    if data is not None:
        data = data.copy()
        for field in ("services_title", "services_intro", "services_note"):
            if field not in data:
                data[field] = initial[field]
    return ContentStudioForm(data=data, initial=initial, services=services, process_steps=process_steps)


def _dashboard_context(request, section, form_overrides=None):
    form_overrides = form_overrides or {}
    site_settings = _site_settings()
    google_review_cache = list(GoogleReview.objects.all()[:5])
    services = list(Service.objects.filter(is_active=True))
    process_steps = list(ProcessStep.objects.all())

    leads_query = request.GET.get("q", "").strip()
    leads_status = request.GET.get("status", "all")
    leads_qs = Lead.objects.filter(deleted_at__isnull=True).prefetch_related("tasks", "estimates", "attachments")
    if leads_query:
        from django.db.models import Q

        leads_qs = leads_qs.filter(
            Q(name__icontains=leads_query)
            | Q(service__icontains=leads_query)
            | Q(location__icontains=leads_query)
            | Q(email__icontains=leads_query)
            | Q(phone__icontains=leads_query)
            | Q(client__name__icontains=leads_query)
            | Q(client__email__icontains=leads_query)
            | Q(client__company__icontains=leads_query)
        )
    if leads_status != "all" and leads_status in dict(Lead.Status.choices):
        leads_qs = leads_qs.filter(status=leads_status)
    leads = list(leads_qs)
    active_leads_count = Lead.objects.filter(deleted_at__isnull=True).count()
    show_deleted_leads = request.GET.get("trash") == "1"
    deleted_leads = (
        list(
            Lead.objects.filter(deleted_at__isnull=False)
            .select_related("deleted_by", "assigned_to", "client")
            .order_by("-deleted_at")
        )
        if show_deleted_leads
        else []
    )
    deleted_leads_count = Lead.objects.filter(deleted_at__isnull=False).count()
    selected_lead = None
    if request.GET.get("lead"):
        selected_lead = Lead.objects.filter(deleted_at__isnull=True).prefetch_related("tasks", "estimates", "attachments").filter(pk=request.GET["lead"]).first()
    if selected_lead is None and leads:
        selected_lead = leads[0]

    external_estimate_enabled = external_estimate_enabled_for(request.user)
    estimate_queryset = visible_estimates(request.user).select_related(
        "lead", "client", "external_status_by"
    ).prefetch_related("projects", "line_items")
    estimates = list(estimate_queryset)
    selected_estimate = None
    if request.GET.get("estimate"):
        selected_estimate = next((item for item in estimates if str(item.pk) == request.GET["estimate"]), None)
    if selected_estimate is None and estimates:
        selected_estimate = estimates[0]

    projects = list(
        Project.objects.select_related("estimate", "lead", "client")
        .prefetch_related(
            "milestones",
            Prefetch(
                "updates",
                queryset=ProjectUpdate.objects.select_related("created_by").prefetch_related("client_draft"),
            ),
            "media_assets",
            "documents",
            "assigned_staff",
        )
        .all()
    )
    selected_project = None
    if request.GET.get("project"):
        selected_project = next((item for item in projects if str(item.pk) == request.GET["project"]), None)
    if selected_project is None and projects:
        selected_project = projects[0]
    if selected_project:
        selected_project.internal_updates = [
            update
            for update in selected_project.updates.all()
            if update.visibility == ProjectUpdate.Visibility.INTERNAL
        ]
    for project in projects:
        project.display_image = _project_image(project)

    media_visibility = request.GET.get("visibility", "all")
    media_project = request.GET.get("media_project", "all")
    media_qs = MediaAsset.objects.select_related("project__client").prefetch_related("publications").all()
    if media_visibility in dict(MediaAsset.Visibility.choices):
        media_qs = media_qs.filter(visibility=media_visibility)
    if media_project != "all":
        try:
            media_qs = media_qs.filter(project_id=uuid.UUID(media_project))
        except (ValueError, TypeError):
            media_project = "all"
    media_assets = [] if section == "media" else list(media_qs)
    for media in media_assets:
        media.display_url = reverse("operations:media-file", kwargs={"pk": media.pk})

    media_context = _media_library_context(request, form_overrides=form_overrides) if section == "media" else {}

    if selected_lead:
        selected_lead.estimate = selected_lead.estimates.order_by("-created_at").first()
        selected_lead.contact_attachments = list(selected_lead.attachments.all())
        for attachment in selected_lead.contact_attachments:
            attachment.download_url = reverse("operations:lead-attachment-file", kwargs={"pk": attachment.pk})
    if selected_estimate:
        selected_estimate.display_total = selected_estimate.total
        selected_estimate.project = selected_estimate.projects.order_by("-created_at").first()
    text_client_href = ""
    if (
        external_estimate_enabled
        and selected_estimate
        and selected_estimate.external_url
        and selected_estimate.client
        and selected_estimate.client.phone
        and can_manage_external_estimate(request.user, estimate=selected_estimate)
    ):
        sms_phone = "".join(
            character
            for character in str(selected_estimate.client.phone)
            if character.isdigit() or character in "+()-"
        )
        if any(character.isdigit() for character in sms_phone):
            sms_message = (
                f"Hi {selected_estimate.client.name}, your Grand Coast Construction "
                f"estimate is ready to review: {selected_estimate.external_url}"
            )
            text_client_href = (
                f"sms:{quote(sms_phone, safe='+()-')}?body={quote(sms_message, safe='')}"
            )
    if selected_project:
        selected_project.progress = selected_project.progress_percent

    pipeline = [
        ("New", [Lead.Status.NEW]),
        ("In conversation", [Lead.Status.CONTACTED, Lead.Status.QUALIFIED]),
        ("Quoted", [Lead.Status.QUOTED]),
        ("Won", [Lead.Status.WON]),
    ]
    all_leads = list(Lead.objects.filter(deleted_at__isnull=True))
    pipeline_columns = [
        {"title": title, "won": title == "Won", "leads": [lead for lead in all_leads if lead.status in statuses]}
        for title, statuses in pipeline
    ]

    new_type = request.GET.get("new", "")
    initial_lead = selected_lead if new_type == "estimate" else None
    initial_client = (
        initial_lead.client
        if initial_lead and initial_lead.client_id
        else Client.objects.filter(pk=request.GET.get("client")).first()
        if new_type == "estimate" and request.GET.get("client")
        else None
    )
    estimate_create_form = ExternalEstimateCreateForm(
        client_queryset=Client.objects.all(),
        initial={"client": initial_client.pk} if initial_client else None,
    )
    dashboard_forms = {
        "lead_form": LeadForm(staff_queryset=_staff_users()),
        "estimate_create_form": estimate_create_form,
        "project_form": (
            ProjectForm(instance=selected_project, staff_queryset=_staff_users())
            if selected_project and new_type != "project"
            else ProjectCreateForm(staff_queryset=_staff_users())
        ),
        "project_document_form": ProjectDocumentForm(
            project_queryset=Project.objects.all(),
            initial={"project": selected_project.pk} if selected_project else None,
        ),
        "can_manage_documents": _can_manage_documents(request.user),
        "project_update_form": ProjectUpdateForm(),
        "project_update_draft_form": ProjectUpdateDraftForm(prefix="project-update-draft"),
        "project_update_draft_source": None,
        "project_update_draft_auto_open": False,
        "media_upload_form": MediaUploadForm(project_queryset=Project.objects.all()),
        "content_form": _content_form(site_settings, services, process_steps),
        "lead_note_form": (
            LeadNoteForm(instance=selected_lead, prefix="admin-note")
            if selected_lead
            else LeadNoteForm(prefix="admin-note")
        ),
        "lead_assignment_form": LeadAssignmentForm(instance=selected_lead, staff_queryset=_staff_users()) if selected_lead else LeadAssignmentForm(staff_queryset=_staff_users()),
        "follow_up_form": QuickTaskForm(prefix="followup", staff_queryset=_staff_users()),
        "external_estimate_status_form": (
            ExternalEstimateStatusForm(instance=selected_estimate)
            if (
                external_estimate_enabled
                and selected_estimate
                and selected_estimate.external_url
                and can_manage_external_estimate(request.user, estimate=selected_estimate)
            )
            else None
        ),
    }
    dashboard_forms.update(form_overrides)
    content_form = dashboard_forms["content_form"]
    service_rows = [
        {"service": service, "form": ServiceForm(instance=service, prefix=f"service-{service.pk}")}
        for service in Service.objects.all()
    ]
    if form_overrides.get("service_error_id"):
        for row in service_rows:
            if row["service"].pk == form_overrides["service_error_id"]:
                row["form"] = form_overrides["service_edit_form"]
    content_step_fields = [
        {"step": step, "field": content_form[f"step_{step.key}"]}
        for step in process_steps
    ]

    active_projects = [project for project in projects if project.status != Project.Status.COMPLETE]
    pending_estimates = [estimate for estimate in estimates if estimate.status in {Estimate.Status.DRAFT, Estimate.Status.SENT}]
    open_leads = [lead for lead in all_leads if lead.status not in {Lead.Status.WON, Lead.Status.LOST}]
    open_tasks_count = Task.objects.filter(
        status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED]
    ).count()
    unread_messages_count = ClientMessage.objects.filter(
        is_read=False,
        sent_by__is_staff=False,
    ).count()
    upcoming_events_count = ScheduleEvent.objects.filter(start_at__gte=timezone.now()).count()
    activities = list(Activity.objects.select_related("actor").all()[:8])
    for activity in activities:
        activity.when = activity.created_at.strftime("%b %d · %I:%M %p") if activity.created_at else ""

    return {
        "active_section": section,
        "operations_nav_role": "admin",
        "operations_nav_counts": _operations_navigation_counts(request.user),
        "leads": leads,
        "active_leads_count": active_leads_count,
        "show_deleted_leads": show_deleted_leads,
        "deleted_leads": deleted_leads,
        "deleted_leads_count": deleted_leads_count,
        "selected_lead": selected_lead,
        "estimates": estimates,
        "selected_estimate": selected_estimate,
        "text_client_href": text_client_href,
        "estimate_delivery_prompt": request.GET.get("delivery_prompt") == "1",
        "estimate_sms_followup": request.GET.get("sms_followup") == "1",
        "estimate_sms_after_other_channels": request.GET.get("sms_after_other_channels") == "1",
        "external_estimate_enabled": external_estimate_enabled,
        "can_manage_external_estimate_status": bool(
            external_estimate_enabled
            and selected_estimate
            and can_manage_external_estimate(request.user, estimate=selected_estimate)
        ),
        "external_status_idempotency_key": str(uuid.uuid4()),
        "external_email_idempotency_key": str(uuid.uuid4()),
        "projects": projects,
        "selected_project": selected_project,
        "media_assets": media_assets,
        "media_visibility": media_visibility,
        "media_project": media_project,
        "pipeline_columns": pipeline_columns,
        "overview_projects": active_projects[:4],
        "activities": activities,
        "open_leads_count": len(open_leads),
        "pending_estimates_count": len(pending_estimates),
        "active_projects_count": len(active_projects),
        "open_tasks_count": open_tasks_count,
        "unread_messages_count": unread_messages_count,
        "upcoming_events_count": upcoming_events_count,
        "can_manage_team": _can_manage_team(request.user),
        "lead_status_choices": Lead.Status.choices,
        "estimate_status_choices": Estimate.Status.choices,
        "project_status_choices": Project.Status.choices,
        "media_visibility_choices": MediaAsset.Visibility.choices,
        "content_services": services,
        "content_process_steps": process_steps,
        "service_rows": service_rows,
        "service_create_form": form_overrides.get("service_create_form", ServiceForm(prefix="new-service")),
        "content_step_fields": content_step_fields,
        "site_settings": site_settings,
        "google_review_cache": google_review_cache,
        "google_places_configured": bool(
            getattr(settings, "GCC_GOOGLE_PLACES_API_KEY", "")
            and getattr(settings, "GCC_GOOGLE_PLACE_ID", "")
        ),
        "new_type": new_type,
        "last_invite_url": request.session.pop("last_invite_url", ""),
        "last_invite_email_status": request.session.pop("last_invite_email_status", ""),
        "last_invite_recipient": request.session.pop("last_invite_recipient", ""),
        **dashboard_forms,
        **media_context,
    }


@staff_required
def dashboard(request, section="overview"):
    if section not in DASHBOARD_SECTIONS:
        section = "overview"
    if section == "content" and not _can_manage_content(request.user):
        raise PermissionDenied
    if section in {"clients", "tasks", "calendar", "time", "documents", "team", "notifications"}:
        return render(request, "operations/workspace.html", _workspace_context(request, section))
    return render(request, "operations/dashboard.html", _dashboard_context(request, section))


def _workspace_redirect(request, section, **params):
    route = "operations:team-section" if request.path.startswith("/team/") or _is_field_employee(request.user) else "operations:dashboard-section"
    url_name = "section"
    url = reverse(route, kwargs={url_name: section})
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    return redirect(f"{url}?{urlencode(clean_params)}" if clean_params else url)


class _DerivedCalendarAssignees:
    def __init__(self, users):
        self._users = [user for user in users if user is not None]

    def all(self):
        return self._users


class _DerivedCalendarEvent:
    is_virtual = True

    def __init__(
        self,
        *,
        key,
        title,
        project,
        start_at,
        end_at,
        location="",
        notes="",
        assignees=None,
        task=None,
        calendar_event_kind="Job event",
        anchor="execution-loop",
    ):
        self.pk = f"derived:{key}"
        self.title = title
        self.project = project
        self.task = task
        self.start_at = start_at
        self.end_at = end_at
        self.location = location
        self.notes = notes
        self.created_by = None
        self.calendar_event_kind = calendar_event_kind
        self.assignees = _DerivedCalendarAssignees(assignees or [])
        if project is not None:
            self.source_url = reverse("operations:project-operations", kwargs={"pk": project.pk}) + f"#{anchor}"
        elif task is not None:
            self.source_url = reverse("operations:dashboard-section", kwargs={"section": "tasks"}) + f"?task={task.pk}"
        else:
            self.source_url = reverse("operations:dashboard")

def _calendar_at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), CALENDAR_TIME_ZONE)


def _calendar_datetime(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, CALENDAR_TIME_ZONE)
    return value


def _execution_calendar_events(projects, *, include_completed=False):
    """Project-linked, read-only calendar signals for the execution pilot."""
    project_ids = [project.pk for project in projects]
    project_by_id = {project.pk: project for project in projects}
    derived = []
    visits = SiteVisit.objects.filter(
        project_id__in=project_ids,
        scheduled_at__isnull=False,
    ).exclude(status=SiteVisit.Status.CANCELLED).select_related("project", "assigned_to")
    for visit in visits:
        project = project_by_id.get(visit.project_id)
        if project is None:
            continue
        start = visit.scheduled_at
        derived.append(_DerivedCalendarEvent(
            key=f"site-visit:{visit.pk}",
            title=f"Site visit: {project.title}",
            project=project,
            start_at=start,
            end_at=start + timedelta(hours=1),
            location=visit.address or project.location,
            notes=visit.scope,
            assignees=[visit.assigned_to or project.project_manager],
            calendar_event_kind="Site visit",
            anchor="site-visits",
        ))
    inspections = Inspection.objects.filter(
        project_id__in=project_ids,
        scheduled_at__isnull=False,
    ).exclude(status=Inspection.Status.CANCELLED).select_related("project")
    for inspection in inspections:
        project = project_by_id.get(inspection.project_id)
        if project is None:
            continue
        start = inspection.scheduled_at
        derived.append(_DerivedCalendarEvent(
            key=f"inspection:{inspection.pk}",
            title=f"Inspection: {inspection.inspection_type}",
            project=project,
            start_at=start,
            end_at=start + timedelta(hours=1),
            location=project.location,
            notes=inspection.corrective_action or inspection.result_notes,
            assignees=[project.project_manager],
            calendar_event_kind="Inspection",
            anchor="permits",
        ))
    # Completed work is represented by its completion timestamp, not as an
    # outstanding due item in the calendar.
    tasks_queryset = (
        Task.objects.filter(
            project_id__in=project_ids,
            due_date__isnull=False,
        )
        .exclude(status=Task.Status.COMPLETE)
        .select_related("project")
        .prefetch_related("assigned_to")
    )
    for task in tasks_queryset:
        project = project_by_id.get(task.project_id)
        if project is None:
            continue
        start = _calendar_at(task.due_date, 9)
        derived.append(_DerivedCalendarEvent(
            key=f"task:{task.pk}",
            title=f"Task: {task.title}",
            project=project,
            task=task,
            start_at=start,
            end_at=start + timedelta(hours=1),
            location=project.location,
            notes=task.description,
            assignees=list(task.assigned_to.all()) or [project.project_manager],
            calendar_event_kind="Task due",
            anchor="schedule",
        ))
    assignments = SubcontractorAssignment.objects.filter(
        project_id__in=project_ids,
        start_date__isnull=False,
    ).exclude(status=SubcontractorAssignment.Status.CANCELLED).select_related("project")
    for assignment in assignments:
        project = project_by_id.get(assignment.project_id)
        if project is None:
            continue
        start = _calendar_at(assignment.start_date, 8)
        finish_day = assignment.end_date or assignment.start_date
        end = _calendar_at(finish_day, 17)
        if end <= start:
            end = start + timedelta(hours=1)
        derived.append(_DerivedCalendarEvent(
            key=f"assignment:{assignment.pk}",
            title=f"Subcontractor: {assignment.work_package}",
            project=project,
            start_at=start,
            end_at=end,
            location=project.location,
            notes=assignment.scope,
            assignees=[project.project_manager],
            calendar_event_kind="Subcontractor work",
            anchor="subcontractors",
        ))
    for project in projects:
        if project.target_date and (include_completed or project.status != Project.Status.COMPLETE):
            start = _calendar_at(project.target_date, 16)
            derived.append(_DerivedCalendarEvent(
                key=f"deadline:{project.pk}",
                title=f"Project deadline: {project.title}",
                project=project,
                start_at=start,
                end_at=start + timedelta(hours=1),
                location=project.location,
                notes=project.next_step,
                assignees=[project.project_manager],
                calendar_event_kind="Project target",
                anchor="schedule",
            ))
    return derived


def _admin_calendar_events(projects, tasks):
    """Return every actionable job timestamp the Owner needs in the calendar."""
    derived = _execution_calendar_events(projects, include_completed=True)
    project_by_id = {project.pk: project for project in projects}

    # Project-linked due dates are already represented by the execution helper.
    # Add lead follow-up tasks as well, since those can exist before a project.
    project_task_ids = {
        event.task.pk
        for event in derived
        if getattr(event, "task", None) is not None
    }
    for task in tasks:
        if task.due_date and task.pk not in project_task_ids and not task.completed:
            project = project_by_id.get(task.project_id)
            start = _calendar_at(task.due_date, 9)
            derived.append(_DerivedCalendarEvent(
                key=f"task:{task.pk}",
                title=f"Task: {task.title}",
                project=project,
                task=task,
                start_at=start,
                end_at=start + timedelta(hours=1),
                location=project.location if project else "",
                notes=task.description,
                assignees=list(task.assigned_to.all()),
                calendar_event_kind="Task due",
                anchor="schedule",
            ))

        if task.completed_at:
            start = _calendar_datetime(task.completed_at)
            project = project_by_id.get(task.project_id)
            derived.append(_DerivedCalendarEvent(
                key=f"task-completed:{task.pk}",
                title=f"Task completed: {task.title}",
                project=project,
                task=task,
                start_at=start,
                end_at=start + timedelta(minutes=30),
                location=project.location if project else "",
                notes=task.description,
                assignees=list(task.assigned_to.all()),
                calendar_event_kind="Task completed",
                anchor="schedule",
            ))

    for project in projects:
        if project.start_date:
            start = _calendar_at(project.start_date, 8)
            derived.append(_DerivedCalendarEvent(
                key=f"project-start:{project.pk}",
                title=f"Project start: {project.title}",
                project=project,
                start_at=start,
                end_at=start + timedelta(hours=1),
                location=project.location,
                notes=project.next_step,
                assignees=[project.project_manager],
                calendar_event_kind="Project start",
                anchor="schedule",
            ))
        if project.construction_ready_at:
            start = _calendar_datetime(project.construction_ready_at)
            derived.append(_DerivedCalendarEvent(
                key=f"project-ready:{project.pk}",
                title=f"Construction ready: {project.title}",
                project=project,
                start_at=start,
                end_at=start + timedelta(hours=1),
                location=project.location,
                notes=project.next_step,
                assignees=[project.project_manager],
                calendar_event_kind="Construction ready",
                anchor="schedule",
            ))
        for milestone in project.milestones.all():
            if not milestone.completed_at:
                continue
            start = _calendar_datetime(milestone.completed_at)
            derived.append(_DerivedCalendarEvent(
                key=f"milestone-completed:{milestone.pk}",
                title=f"Milestone completed: {milestone.title}",
                project=project,
                start_at=start,
                end_at=start + timedelta(minutes=30),
                location=project.location,
                notes=project.next_step,
                assignees=[project.project_manager],
                calendar_event_kind="Milestone completed",
                anchor="schedule",
            ))
    return derived

def _calendar_conflicts(events, overrides):
    conflicts = []
    # Task due/completion markers are reminders, not booked calendar blocks.
    watchable_events = [
        event
        for event in events
        if getattr(event, "calendar_event_kind", None) not in {"Task due", "Task completed"}
    ]
    for index, event in enumerate(watchable_events):
        event_assignees = {str(user.pk) for user in event.assignees.all()}
        event_project_id = getattr(getattr(event, "project", None), "pk", None)
        for other in watchable_events[index + 1:]:
            if event.end_at <= other.start_at or other.end_at <= event.start_at:
                continue
            other_assignees = {str(user.pk) for user in other.assignees.all()}
            other_project_id = getattr(getattr(other, "project", None), "pk", None)
            if event_assignees & other_assignees:
                reason = "Assigned person has overlapping events."
            elif event_project_id and event_project_id == other_project_id:
                reason = "Project has overlapping scheduled work."
            else:
                continue
            conflicts.append({"event": event, "other": other, "reason": reason})
    for event in watchable_events:
        local_start = timezone.localtime(event.start_at, CALENDAR_TIME_ZONE)
        local_end = timezone.localtime(event.end_at, CALENDAR_TIME_ZONE)
        override = overrides.get(local_start.date())
        if override is None:
            continue
        if override.status == CalendarDayOverride.Status.CLOSED:
            conflicts.append({"event": event, "other": None, "reason": "This event falls on a closed company day."})
        elif override.status == CalendarDayOverride.Status.SHORT:
            if (
                override.short_start
                and local_start.time() < override.short_start
                or override.short_end
                and local_end.time() > override.short_end
            ):
                conflicts.append({"event": event, "other": None, "reason": "This event falls outside the short-day hours."})
    return conflicts


def _workspace_context(request, section, team_mode=False, form_overrides=None):
    form_overrides = form_overrides or {}
    projects_qs = _visible_projects_for_user(request.user).select_related("client", "lead", "estimate").prefetch_related("milestones", "assigned_staff")
    projects = list(projects_qs)
    for project in projects:
        project.display_image = _project_image(project)

    tasks_qs = _visible_tasks_for_user(request.user).select_related("lead", "client", "project", "milestone").prefetch_related("assigned_to", "watchers")
    task_query = request.GET.get("q", "").strip()
    task_status = request.GET.get("status", "all")
    task_priority = request.GET.get("priority", "all")
    task_project = request.GET.get("project_filter", "all")
    task_employee = request.GET.get("employee", "all")
    task_due = request.GET.get("due", "all")
    if task_query:
        tasks_qs = tasks_qs.filter(Q(title__icontains=task_query) | Q(description__icontains=task_query) | Q(project__title__icontains=task_query) | Q(lead__name__icontains=task_query) | Q(client__name__icontains=task_query))
    if task_status in dict(Task.Status.choices):
        tasks_qs = tasks_qs.filter(status=task_status)
    if task_priority in dict(Task.Priority.choices):
        tasks_qs = tasks_qs.filter(priority=task_priority)
    if task_project != "all":
        try:
            project_is_visible = projects_qs.filter(pk=task_project).exists()
        except (TypeError, ValueError):
            project_is_visible = False
        if project_is_visible:
            tasks_qs = tasks_qs.filter(project_id=task_project)
        else:
            task_project = "all"
    if task_employee != "all" and _can_access_dashboard(request.user):
        try:
            employee_is_visible = _staff_users().filter(pk=task_employee).exists()
        except (TypeError, ValueError):
            employee_is_visible = False
        if employee_is_visible:
            tasks_qs = tasks_qs.filter(assigned_to__pk=task_employee).distinct()
        else:
            task_employee = "all"
    if task_due == "overdue":
        tasks_qs = tasks_qs.filter(due_date__lt=timezone.localdate()).exclude(status=Task.Status.COMPLETE)
    elif task_due == "today":
        tasks_qs = tasks_qs.filter(due_date=timezone.localdate())
    elif task_due == "upcoming":
        tasks_qs = tasks_qs.filter(due_date__gte=timezone.localdate()).exclude(status=Task.Status.COMPLETE)
    tasks = list(tasks_qs)

    schedule_queryset = _visible_team_schedule_for_user(request.user) if team_mode else _visible_schedule_for_user(request.user)
    events = list(schedule_queryset.select_related("project", "task", "created_by").prefetch_related("assignees"))
    derived_calendar_events = []
    admin_calendar_tasks = []
    if section == "calendar":
        if _can_access_dashboard(request.user):
            admin_calendar_tasks = list(
                _visible_tasks_for_user(request.user)
                .select_related("lead", "project", "milestone")
                .prefetch_related("assigned_to", "watchers")
            )
            derived_calendar_events = _admin_calendar_events(projects, admin_calendar_tasks)
        elif execution_loop_enabled_for(request.user):
            derived_calendar_events = _execution_calendar_events(projects)
        events.extend(derived_calendar_events)
    today = timezone.localtime(timezone.now(), CALENDAR_TIME_ZONE).date()
    requested_month = request.GET.get("month", "")
    try:
        month_anchor = date.fromisoformat(f"{requested_month}-01") if len(requested_month) == 7 else today.replace(day=1)
    except ValueError:
        month_anchor = today.replace(day=1)
    month_calendar = calendar_module.Calendar(firstweekday=6).monthdatescalendar(
        month_anchor.year,
        month_anchor.month,
    )
    calendar_dates = [calendar_date for week in month_calendar for calendar_date in week]
    selected_day_value = request.GET.get("day", "")
    try:
        selected_day_date = date.fromisoformat(selected_day_value)
    except (TypeError, ValueError):
        selected_day_date = None
    default_selected_day = (
        today
        if (month_anchor.year, month_anchor.month) == (today.year, today.month)
        else month_anchor
    )
    if selected_day_date not in calendar_dates:
        selected_day_date = default_selected_day
    events_by_day = {}
    for event in events:
        for event_day in schedule_event_local_dates(event):
            if event_day in calendar_dates:
                events_by_day.setdefault(event_day, []).append(event)
    calendar_job_event_count = sum(
        1
        for event in derived_calendar_events
        if any(event_day in calendar_dates for event_day in schedule_event_local_dates(event))
    )
    overrides_by_day = {
        override.date: override
        for override in CalendarDayOverride.objects.filter(
            date__range=(calendar_dates[0], calendar_dates[-1]),
        )
    }
    calendar_conflicts = (
        _calendar_conflicts(events, overrides_by_day)
        if derived_calendar_events
        else []
    )
    calendar_weeks = []
    calendar_day_map = {}
    for week in month_calendar:
        week_days = []
        for calendar_date in week:
            day_events = sorted(
                events_by_day.get(calendar_date, []),
                key=lambda event: (event.start_at, event.pk),
            )
            override = overrides_by_day.get(calendar_date)
            day_context = {
                "date": calendar_date,
                "events": day_events,
                "preview_events": day_events[:3],
                "event_count": len(day_events),
                "remaining_event_count": max(len(day_events) - 3, 0),
                "override": override,
                "is_short": bool(override and override.status == CalendarDayOverride.Status.SHORT),
                "is_closed": bool(override and override.status == CalendarDayOverride.Status.CLOSED),
                "is_current_month": calendar_date.month == month_anchor.month,
                "is_today": calendar_date == today,
            }
            calendar_day_map[calendar_date] = day_context
            week_days.append(day_context)
        calendar_weeks.append(week_days)

    schedule_employees = []
    schedule_summaries = []
    selected_schedule_employee = None
    weekly_schedule_days = []
    schedule_day = selected_day_date
    schedule_override_form = None
    employee_calendar_days = {}
    if section == "calendar":
        if team_mode:
            selected_schedule_employee = request.user
        elif _can_manage_schedule(request.user):
            schedule_employees = list(_staff_users())
            for schedule_employee in schedule_employees:
                weekly_records = list(EmployeeWeeklySchedule.objects.filter(employee=schedule_employee))
                total_minutes = sum(
                    (
                        record.end_time.hour * 60
                        + record.end_time.minute
                        - record.start_time.hour * 60
                        - record.start_time.minute
                    )
                    for record in weekly_records
                    if record.is_working and record.start_time and record.end_time
                )
                schedule_summaries.append(
                    {
                        'employee': schedule_employee,
                        'weekly_records': weekly_records,
                        'weekly_hours': total_minutes / 60,
                    }
                )
            requested_employee = request.GET.get("schedule_employee", "")
            selected_schedule_employee = next(
                (
                    employee
                    for employee in schedule_employees
                    if str(employee.pk) == requested_employee
                ),
                schedule_employees[0] if schedule_employees else None,
            )

        requested_schedule_day = request.GET.get("schedule_day", "")
        try:
            schedule_day = date.fromisoformat(requested_schedule_day)
        except ValueError:
            schedule_day = selected_day_date or today

        if selected_schedule_employee is not None:
            employee_calendar_days = {
                calendar_date: effective_employee_schedule(
                    selected_schedule_employee,
                    calendar_date,
                )
                for calendar_date in calendar_dates
            }
            for calendar_date in calendar_dates:
                calendar_day_map[calendar_date]["effective_shift"] = employee_calendar_days[calendar_date]

            if _can_manage_schedule(request.user) and not team_mode:
                weekly_forms_override = form_overrides.get("weekly_forms", {})
                for weekday, weekday_label in EmployeeWeeklySchedule.Weekday.choices:
                    weekly_record = EmployeeWeeklySchedule.objects.filter(
                        employee=selected_schedule_employee,
                        weekday=weekday,
                    ).first()
                    weekly_form = weekly_forms_override.get(weekday)
                    if weekly_form is None:
                        weekly_form = EmployeeWeeklyScheduleForm(
                            instance=weekly_record,
                            prefix=f"weekday-{weekday}",
                        )
                    weekly_schedule_days.append(
                        {
                            "weekday": weekday,
                            "label": weekday_label,
                            "weekly_record": weekly_record,
                            "form": weekly_form,
                        }
                    )

                selected_schedule_override = EmployeeScheduleOverride.objects.filter(
                    employee=selected_schedule_employee,
                    date=schedule_day,
                ).first()
                schedule_override_form = EmployeeScheduleOverrideForm(
                    instance=selected_schedule_override,
                    employee_queryset=_staff_users(),
                    initial={
                        "employee": selected_schedule_employee.pk,
                        "date": schedule_day,
                    },
                )
        if form_overrides.get("schedule_override_form") is not None:
            schedule_override_form = form_overrides["schedule_override_form"]

    for calendar_date, shift in employee_calendar_days.items():
        calendar_day_map[calendar_date]["effective_shift"] = shift
    calendar_month_days = [
        calendar_day_map[calendar_date]
        for calendar_date in calendar_dates
        if calendar_date.month == month_anchor.month
        and (
            events_by_day.get(calendar_date)
            or overrides_by_day.get(calendar_date)
            or employee_calendar_days.get(calendar_date, {}).get("is_working")
            or employee_calendar_days.get(calendar_date, {}).get("employee_override")
        )
    ]
    calendar_preview_events = [
        event
        for day_context in calendar_month_days
        for event in day_context['preview_events']
    ]
    previous_month = month_anchor.month - 1 or 12
    previous_year = month_anchor.year - (1 if month_anchor.month == 1 else 0)
    next_month = month_anchor.month + 1 if month_anchor.month < 12 else 1
    next_year = month_anchor.year + (1 if month_anchor.month == 12 else 0)
    selected_day = calendar_day_map.get(selected_day_date)
    selected_day_events = selected_day["events"] if selected_day else []
    selected_day_override = selected_day["override"] if selected_day else None
    selected_day_employee_shifts = []
    if selected_day and _can_manage_schedule(request.user) and not team_mode:
        for schedule_employee in schedule_employees:
            if selected_schedule_employee and schedule_employee.pk == selected_schedule_employee.pk:
                shift = selected_day.get("effective_shift")
            else:
                shift = effective_employee_schedule(schedule_employee, selected_day["date"])
            if shift and (
                shift["is_working"]
                or (shift["employee_override"] and not shift["is_company_closed"])
                or (
                    selected_schedule_employee
                    and schedule_employee.pk == selected_schedule_employee.pk
                )
            ):
                selected_day_employee_shifts.append(
                    {"employee": schedule_employee, "shift": shift}
                )
    event_id = request.GET.get("event")
    selected_event = next(
        (
            event for event in events
            if str(event.pk) == event_id and not getattr(event, "is_virtual", False)
        ),
        None,
    ) if event_id else None
    selected_task_id = request.GET.get("task")
    selected_task = next((task for task in tasks if str(task.pk) == selected_task_id), None) if selected_task_id else (tasks[0] if tasks else None)

    clients_qs = _visible_clients_for_user(request.user).prefetch_related("leads", "projects")
    client_query = request.GET.get("client_q", "").strip()
    if client_query:
        clients_qs = clients_qs.filter(Q(name__icontains=client_query) | Q(company__icontains=client_query) | Q(email__icontains=client_query) | Q(phone__icontains=client_query))
    clients = list(clients_qs)
    selected_client_id = request.GET.get("client")
    selected_client = next((client for client in clients if str(client.pk) == selected_client_id), None) if selected_client_id else (clients[0] if clients else None)

    documents_qs = ProjectDocument.objects.select_related("project", "uploaded_by").filter(project__in=projects_qs)
    document_project = request.GET.get("project", "")
    if document_project:
        try:
            document_project_is_visible = projects_qs.filter(pk=document_project).exists()
        except (TypeError, ValueError):
            document_project_is_visible = False
        if document_project_is_visible:
            documents_qs = documents_qs.filter(project_id=document_project)
    if team_mode and _is_field_employee(request.user):
        documents_qs = documents_qs.filter(project__assigned_staff=request.user)
    documents = list(documents_qs)
    selected_project_id = request.GET.get("project")
    selected_project = next((project for project in projects if str(project.pk) == selected_project_id), None) if selected_project_id else (projects[0] if projects else None)

    media_visibility = request.GET.get("media_visibility", "all")
    media_qs = MediaAsset.objects.select_related("project", "uploaded_by").filter(project__in=projects_qs)
    if media_visibility in dict(MediaAsset.Visibility.choices):
        media_qs = media_qs.filter(visibility=media_visibility)
    media_assets = list(media_qs)
    for media in media_assets:
        media.display_url = reverse("operations:media-file", kwargs={"pk": media.pk})

    time_entries = list(_visible_time_for_user(request.user).select_related("employee", "project", "task")[:50])
    active_time_entry = TimeEntry.objects.filter(employee=request.user, clock_out__isnull=True).first()
    selected_time_entry_id = request.GET.get("time_entry")
    selected_time_entry = next((entry for entry in time_entries if str(entry.pk) == selected_time_entry_id), None) if selected_time_entry_id else None
    staff_users = list(_staff_users()) if _can_access_dashboard(request.user) else [request.user]
    task_filter_groups = []
    if section == "tasks":
        task_filter_definitions = [
            ("status", "Status", task_status, [("all", "All statuses"), *Task.Status.choices]),
            ("priority", "Priority", task_priority, [("all", "All priorities"), *Task.Priority.choices]),
            (
                "project_filter",
                "Project",
                task_project,
                [("all", "All projects")] + [(project.pk, project.title) for project in projects],
            ),
            ("due", "Due", task_due, [("all", "Any due date"), ("overdue", "Overdue"), ("today", "Today"), ("upcoming", "Upcoming")]),
        ]
        if not team_mode:
            task_filter_definitions.insert(
                3,
                (
                    "employee",
                    "Employee",
                    task_employee,
                    [("all", "All employees")]
                    + [(person.pk, person.get_full_name() or person.username) for person in staff_users],
                ),
            )
        for name, label, selected_value, options in task_filter_definitions:
            normalized_options = [(str(value), option_label) for value, option_label in options]
            option_labels = dict(normalized_options)
            selected_value = str(selected_value)
            if selected_value not in option_labels:
                selected_value = normalized_options[0][0]
            task_filter_groups.append(
                {
                    "name": name,
                    "label": label,
                    "value": selected_value,
                    "selected_label": option_labels[selected_value],
                    "options": normalized_options,
                }
            )
    task_clients_qs = _visible_clients_for_user(request.user)
    employee_profiles = list(EmployeeProfile.objects.select_related("user").filter(user__in=staff_users))
    pending_employee_invites = []
    if not team_mode and _can_manage_team(request.user):
        pending_employee_invites = list(EmployeeInvite.objects.filter(
            purpose=EmployeeInvite.Purpose.ONBOARDING,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).select_related("group")[:20])
    if team_mode:
        task_form = TeamTaskUpdateForm(
            instance=selected_task if request.GET.get("edit") == "task" else None,
        )
    else:
        task_form = TaskForm(
            instance=selected_task if request.GET.get("edit") == "task" else None,
            staff_queryset=_staff_users(),
            client_queryset=task_clients_qs if section == "tasks" else Client.objects.none(),
            project_queryset=projects_qs if section == "tasks" else Project.objects.none(),
        )
    event_form = ScheduleEventForm(
        instance=selected_event if request.GET.get("edit") == "event" else None,
        initial={
            "project": selected_project_id,
        } if request.GET.get("new") == "event" and selected_project_id else {},
        staff_queryset=_staff_users(),
        project_queryset=projects_qs,
        # Keep the editor's task choices independent from the currently active
        # task filters. An event can remain editable even when its task is not
        # part of the filtered task list.
        task_queryset=_visible_tasks_for_user(request.user),
    )
    day_form = (
        CalendarDayOverrideForm(
            instance=selected_day_override,
            initial={'date': selected_day['date']} if selected_day and not selected_day_override else {},
        )
        if selected_day and _can_manage_schedule(request.user)
        else None
    )
    client_form = ClientForm(instance=selected_client if request.GET.get("edit") == "client" else None)
    document_form = ProjectDocumentForm(project_queryset=projects_qs)
    media_upload_form = MediaUploadForm(project_queryset=projects_qs)
    time_form = TimeEntryForm(project_queryset=projects_qs, task_queryset=tasks_qs)
    time_edit_form = (
        TimeEntryForm(instance=selected_time_entry, project_queryset=projects_qs, task_queryset=tasks_qs)
        if selected_time_entry and _can_manage_team(request.user)
        else None
    )
    project_update_form = ProjectUpdateForm()
    profile = getattr(request.user, "employee_profile", None)
    profile_form = (
        EmployeeProfileForm(instance=profile, allow_status=False)
        if profile
        else EmployeeProfileForm(allow_status=False)
    )
    invite_form = EmployeeInviteForm(group_queryset=Group.objects.filter(name__in=["Manager", "Office", "Field", "Sales"]))
    client_form = form_overrides.get("client_form", client_form)
    task_form = form_overrides.get("task_form", task_form)
    event_form = form_overrides.get("event_form", event_form)
    day_form = form_overrides.get("day_form", day_form)
    document_form = form_overrides.get("document_form", document_form)
    media_upload_form = form_overrides.get("media_upload_form", media_upload_form)
    time_form = form_overrides.get("time_form", time_form)
    time_edit_form = form_overrides.get("time_edit_form", time_edit_form)
    project_update_form = form_overrides.get("project_update_form", project_update_form)
    profile_form = form_overrides.get("profile_form", profile_form)
    invite_form = form_overrides.get("invite_form", invite_form)
    selected_messages = []
    if selected_client:
        selected_messages = list(
            ClientMessage.objects.filter(client=selected_client)
            .select_related("project", "sent_by", "sent_by__employee_profile")
            .order_by("-created_at")[:25]
        )
        selected_messages.sort(key=lambda message: message.created_at)
    selected_project_messages = []
    if team_mode and selected_project:
        selected_project_messages = list(
            ClientMessage.objects.filter(project=selected_project)
            .select_related("client", "sent_by", "sent_by__employee_profile")[:25]
        )
    selected_client_portal_pending = bool(
        selected_client
        and not selected_client.user_id
        and selected_client.invites.filter(
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists()
    )
    selected_client_invite_email_status = ''
    if selected_client:
        latest_invite = selected_client.invites.order_by('-created_at').first()
        if latest_invite:
            latest_email = EmailOutbox.objects.filter(
                idempotency_key=f'client-portal-invite:{latest_invite.pk}'
            ).first()
            if latest_email:
                selected_client_invite_email_status = latest_email.status
    unread_messages_count = ClientMessage.objects.filter(is_read=False, sent_by__is_staff=False).count()
    if team_mode:
        unread_messages_count = 0
    upcoming_events = [event for event in events if event.start_at >= timezone.now()][:5]
    can_view_full_team_calendar = bool(
        team_mode
        and (_can_access_dashboard(request.user) or _group_names(request.user) & LEADERSHIP_GROUPS)
    )
    can_edit_selected_task = bool(
        selected_task
        and (
            (not team_mode and _can_manage_tasks(request.user))
            or (team_mode and selected_task.assigned_to.filter(pk=request.user.id).exists())
        )
    )
    employee_notifications = list(
        EmployeeNotification.objects.filter(
            employee=request.user,
            dismissed_at__isnull=True,
        )[:100]
    )
    notification_workspace = "employee" if team_mode else "admin"
    for notification in employee_notifications:
        notification.destination_url = _safe_notification_destination(
            notification.destination_url,
            workspace=notification_workspace,
        )
    return {
        "active_section": section,
        "team_mode": team_mode,
        "operations_nav_role": "employee" if team_mode else "admin",
        "operations_nav_counts": _operations_navigation_counts(request.user, team_mode=team_mode),
        "projects": projects,
        "selected_project": selected_project,
        "tasks": tasks,
        "task_filter_groups": task_filter_groups,
        "selected_task": selected_task,
        "task_status_choices": Task.Status.choices,
        "task_priority_choices": Task.Priority.choices,
        "task_status": task_status,
        "task_priority": task_priority,
        "task_project": task_project,
        "task_employee": task_employee,
        "task_due": task_due,
        "clients": clients,
        "selected_client": selected_client,
        "selected_client_portal_pending": selected_client_portal_pending,
        "selected_client_invite_email_status": selected_client_invite_email_status,
        "documents": documents,
        "media_assets": media_assets,
        "media_visibility": media_visibility,
        "media_visibility_choices": MediaAsset.Visibility.choices,
        "events": calendar_preview_events,
        "calendar_preview_events": calendar_preview_events,
        "calendar_conflicts": calendar_conflicts,
        "derived_calendar_event_count": len(derived_calendar_events),
        "calendar_job_event_count": calendar_job_event_count,
        "calendar_weeks": calendar_weeks,
        "calendar_month_days": calendar_month_days,
        "calendar_month_label": month_anchor.strftime("%B %Y"),
        "calendar_month_key": month_anchor.strftime("%Y-%m"),
        "calendar_previous": f"{previous_year:04d}-{previous_month:02d}",
        "calendar_next": f"{next_year:04d}-{next_month:02d}",
        "selected_day": selected_day,
        "selected_day_employee_shifts": selected_day_employee_shifts,
        "selected_day_events": selected_day_events,
        "selected_day_override": selected_day_override,
        "schedule_employees": schedule_employees,
        "schedule_summaries": schedule_summaries,
        "selected_schedule_employee": selected_schedule_employee,
        "schedule_day": schedule_day,
        "weekly_schedule_days": weekly_schedule_days,
        "schedule_override_form": schedule_override_form,
        "selected_event": selected_event,
        "time_entries": time_entries,
        "active_time_entry": active_time_entry,
        "selected_time_entry": selected_time_entry,
        "employee_profiles": employee_profiles,
        "pending_employee_invites": pending_employee_invites,
        "staff_users": staff_users,
        "invite_form": invite_form,
        "profile_form": profile_form,
        "profile": profile,
        "task_form": task_form,
        "event_form": event_form,
        "day_form": day_form,
        "client_form": client_form,
        "document_form": document_form,
        "media_upload_form": media_upload_form,
        "time_form": time_form,
        "time_edit_form": time_edit_form,
        "project_update_form": project_update_form,
        "selected_messages": selected_messages,
        "selected_project_messages": selected_project_messages,
        "staff_message_form": form_overrides.get("staff_message_form", StaffMessageForm()),
        "unread_messages_count": unread_messages_count,
        "upcoming_events": upcoming_events,
        "open_tasks_count": Task.objects.filter(status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED]).count() if _can_access_dashboard(request.user) else _visible_tasks_for_user(request.user).exclude(status=Task.Status.COMPLETE).count(),
        "can_manage_team": _can_manage_team(request.user),
        "can_access_dashboard": _can_access_dashboard(request.user),
        "can_manage_schedule": _can_manage_schedule(request.user),
        "can_manage_tasks": _can_manage_tasks(request.user),
        "can_manage_documents": _can_manage_documents(request.user),
        "can_manage_messages": _can_manage_messages(request.user),
        "is_field_employee": _is_field_employee(request.user),
        "can_view_full_team_calendar": can_view_full_team_calendar,
        "can_edit_selected_task": can_edit_selected_task,
        "last_employee_invite_url": request.session.pop("last_employee_invite_url", ""),
        "last_employee_invite_email_status": request.session.pop("last_employee_invite_email_status", ""),
        "last_employee_invite_recipient": request.session.pop("last_employee_invite_recipient", ""),
        "last_reset_url": request.session.pop("last_reset_url", ""),
        "last_invite_url": request.session.pop("last_invite_url", ""),
        "last_invite_email_status": request.session.pop("last_invite_email_status", ""),
        "last_invite_recipient": request.session.pop("last_invite_recipient", ""),
        "employee_notifications": employee_notifications,
    }


def _render_dashboard_form_error(request, section, form_overrides=None, *, selections=None, new=None, edit=None):
    query = request.GET.copy()
    for key, value in (selections or {}).items():
        if value not in (None, ""):
            query[key] = str(value)
    if new:
        query["new"] = new
    if edit:
        query["edit"] = edit
    request.GET = query
    context = _dashboard_context(request, section, form_overrides=form_overrides)
    context["lead_detail_tab"] = query.get("lead_tab", "")
    return render(request, "operations/dashboard.html", context)


def _render_workspace_form_error(request, section, form_overrides=None, *, selections=None, new=None, edit=None):
    query = request.GET.copy()
    for key, value in (selections or {}).items():
        if value not in (None, ""):
            query[key] = str(value)
    if new:
        query["new"] = new
    if edit:
        query["edit"] = edit
    request.GET = query
    team_mode = request.path.startswith("/team/")
    return render(request, "operations/workspace.html", _workspace_context(request, section, team_mode=team_mode, form_overrides=form_overrides))


@team_required
def team(request, section="overview"):
    if section not in TEAM_SECTIONS:
        section = "overview"
    return render(request, "operations/workspace.html", _workspace_context(request, section, team_mode=True))


@require_http_methods(["GET", "POST"])
@login_required
def account_delete(request):
    if not _can_delete_account(request.user):
        raise PermissionDenied

    form = AccountDeleteForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        user_model = get_user_model()
        with transaction.atomic():
            user = user_model.objects.select_for_update().get(pk=request.user.pk)
            if not _can_delete_account(user):
                raise PermissionDenied

            is_client_account = _is_active_client(user)
            client_record = None
            if is_client_account:
                client_record = Client.objects.select_for_update().filter(user=user).first()
                if client_record:
                    MobilePushDevice.objects.filter(
                        client=client_record,
                        is_active=True,
                    ).update(
                        is_active=False,
                        deactivated_at=timezone.now(),
                        updated_at=timezone.now(),
                    )
                    ClientInvite.objects.filter(client=client_record).delete()
                    client_record.name = "Deleted client"
                    client_record.company = ""
                    client_record.email = f"deleted-client-{user.pk}@invalid.local"
                    client_record.phone = ""
                    client_record.user = None
                    client_record.save(
                        update_fields=[
                            "name",
                            "company",
                            "email",
                            "phone",
                            "user",
                            "updated_at",
                        ]
                    )

            EmployeeProfile.objects.filter(user=user).update(
                job_title="",
                phone="",
                is_active=False,
                updated_at=timezone.now(),
            )
            EmployeeInvite.objects.filter(
                Q(employee__user=user) | Q(created_by=user),
            ).delete()
            AdminRecoveryToken.objects.filter(user=user).delete()
            AdminSecurityProfile.objects.filter(user=user).update(
                pin_enabled=False,
                pin_hash="",
                updated_at=timezone.now(),
            )

            user.groups.clear()
            user.user_permissions.clear()
            user.username = _deleted_account_username(user.pk)
            user.first_name = ""
            user.last_name = ""
            user.email = ""
            user.last_login = None
            user.is_active = False
            user.is_staff = False
            user.is_superuser = False
            user.set_unusable_password()
            user.save(
                update_fields=[
                    "username",
                    "first_name",
                    "last_name",
                    "email",
                    "last_login",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "password",
                ],
            )

        logout(request)
        if is_client_account:
            messages.success(request, "Your client portal account was deleted and personal details were anonymized.")
        else:
            messages.success(request, "Your employee account was deleted and personal details were anonymized.")
        return redirect("operations:login")

    return render(
        request,
        "operations/account_delete.html",
        {"form": form, "is_client_account": _is_active_client(request.user)},
    )


@require_POST
@staff_required
def client_create(request):
    form = ClientForm(request.POST)
    if form.is_valid():
        client = form.save()
        record_activity("Client created", client.name, actor=request.user)
        messages.success(request, f"{client.name} was added to Clients.")
        return _workspace_redirect(request, "clients", client=client.pk)
    messages.error(request, "Please correct the client details and try again.")
    return _render_workspace_form_error(request, "clients", {"client_form": form}, new="client")


@require_POST
@staff_required
def client_update(request, pk):
    client = get_object_or_404(Client, pk=pk)
    form = ClientForm(request.POST, instance=client)
    if form.is_valid():
        form.save()
        record_activity("Client details updated", client.name, actor=request.user)
        messages.success(request, "Client details saved.")
    else:
        messages.error(request, "Please correct the client details and try again.")
        return _render_workspace_form_error(request, "clients", {"client_form": form}, selections={"client": client.pk}, edit="client")
    return _workspace_redirect(request, "clients", client=client.pk)


@require_POST
@staff_required
def client_create_invite(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if client.user_id:
        messages.info(request, "This client already has portal access.")
    else:
        invite, raw_token = create_client_invite(client, actor=request.user)
        _prepare_client_invite_delivery(
            request,
            client=client,
            invite=invite,
            raw_token=raw_token,
            actor=request.user,
        )
        record_activity("Client portal invite created", client.name, actor=request.user)
    return _workspace_redirect(request, "clients", client=client.pk)


@require_POST
@staff_required
def client_revoke_access(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if client.user_id:
        client.user = None
        client.save(update_fields=["user", "updated_at"])
        client.invites.filter(accepted_at__isnull=True).update(expires_at=timezone.now())
        record_activity("Client portal access revoked", client.name, actor=request.user)
        messages.success(request, "Client portal access was revoked.")
    else:
        messages.info(request, "This client does not have active portal access.")
    return _workspace_redirect(request, "clients", client=client.pk)


@require_POST
@staff_required
def lead_convert_client(request, pk):
    lead = _active_lead_or_404(pk)
    client, _created = convert_lead_command(
        lead,
        actor=request.user,
        idempotency_key=f"lead-client:{lead.pk}",
    )
    if client is None:
        messages.error(
            request,
            "The client record could not be linked to this lead. Please try again or contact support.",
        )
        return _workspace_redirect(request, "leads", lead=lead.pk)
    if client.user_id:
        confirmation = (
            f"You have now converted {lead.name} to a client. Their dashboard access is already active; "
            "they can sign in with their existing account. Contact them if they are having trouble."
        )
    elif client.email:
        confirmation = (
            f"You have now converted {lead.name} to a client. Next, send a sign-in invitation link "
            "to their email from the client record. They must use that link to access their dashboard. "
            "Contact them if they are having trouble."
        )
    else:
        confirmation = (
            f"You have now converted {lead.name} to a client. Add their email address, then send a "
            "sign-in invitation link so they can access their dashboard. Contact them if they are "
            "having trouble."
        )
    messages.success(request, confirmation)
    return _workspace_redirect(request, "clients", client=client.pk)


@require_POST
@staff_required
def task_create(request):
    if not _can_manage_tasks(request.user):
        raise PermissionDenied
    projects_qs = _visible_projects_for_user(request.user)
    form = TaskForm(
        request.POST,
        staff_queryset=_staff_users(),
        client_queryset=_visible_clients_for_user(request.user),
        project_queryset=projects_qs,
    )
    if form.is_valid():
        task = form.save(commit=False)
        task.created_by = request.user
        if task.status == Task.Status.COMPLETE:
            task.completed_at = timezone.now()
        task.save()
        form.save_m2m()
        record_activity("Task created", task.title, actor=request.user, lead=task.lead, project=task.project)
        _notify_task_participants(
            task,
            kind="task-assignment",
            title="New task assigned",
            body=f"{task.title} was added to your work queue.",
            created_by=request.user,
        )
        messages.success(request, f"{task.title} was added to Tasks.")
        return _workspace_redirect(request, "tasks", task=task.pk)
    messages.error(request, "Please correct the task details and try again.")
    return _render_workspace_form_error(request, "tasks", {"task_form": form}, new="task")


@require_POST
@workspace_staff_required
def task_update(request, pk):
    team_mode = request.path.startswith("/team/")
    if team_mode:
        task = get_object_or_404(
            _visible_tasks_for_user(request.user).select_related("project", "lead"),
            pk=pk,
            assigned_to=request.user,
        )
        form = TeamTaskUpdateForm(request.POST, instance=task)
    else:
        if not _can_manage_tasks(request.user):
            raise PermissionDenied
        task = get_object_or_404(Task, pk=pk)
        form = TaskForm(
            request.POST,
            instance=task,
            staff_queryset=_staff_users(),
            client_queryset=_visible_clients_for_user(request.user),
            project_queryset=_visible_projects_for_user(request.user),
        )
    previous_assignee_ids = set(task.assigned_to.values_list("pk", flat=True))
    previous_status = task.status
    previous_watcher_ids = set(task.watchers.values_list("pk", flat=True))
    if form.is_valid():
        if not form.has_changed():
            messages.success(request, "No task changes were needed.")
            return _workspace_redirect(request, "tasks", task=task.pk)
        task = form.save(commit=False)
        if task.status == Task.Status.COMPLETE:
            if previous_status != Task.Status.COMPLETE or not task.completed_at:
                task.completed_at = timezone.now()
        else:
            task.completed_at = None
        task.save()
        if not team_mode:
            form.save_m2m()
        record_activity("Task updated", task.title, actor=request.user, lead=task.lead, project=task.project)
        task = Task.objects.select_related("lead", "client", "project").prefetch_related("assigned_to", "watchers").get(pk=task.pk)
        assignment_changed = (
            previous_assignee_ids != set(task.assigned_to.values_list("pk", flat=True))
            or previous_watcher_ids != set(task.watchers.values_list("pk", flat=True))
        )
        _notify_task_participants(
            task,
            kind="task-assignment" if assignment_changed else "task-update",
            title="Task assignment updated" if assignment_changed else "Task updated",
            body=f"{task.title} is now assigned or updated in your work queue.",
            created_by=request.user,
        )
        messages.success(request, "Task changes saved.")
        return _workspace_redirect(request, "tasks", task=task.pk)
    messages.error(request, "Please correct the task details and try again.")
    return _render_workspace_form_error(request, "tasks", {"task_form": form}, selections={"task": task.pk}, edit="task")


@require_POST
@workspace_staff_required
def task_set_status(request, pk):
    task = get_object_or_404(Task.objects.select_related("project", "lead"), pk=pk)
    if request.path.startswith("/team/"):
        if not task.assigned_to.filter(pk=request.user.id).exists():
            raise PermissionDenied
    elif not _can_manage_tasks(request.user):
        raise PermissionDenied
    previous_status = task.status
    status = request.POST.get("status")
    if status not in dict(Task.Status.choices):
        messages.error(request, "That task status is not valid.")
    elif status == previous_status:
        messages.info(request, "The task already has that status.")
    else:
        task.status = status
        if status == Task.Status.COMPLETE:
            task.completed_at = task.completed_at or timezone.now()
        else:
            task.completed_at = None
        task.save(update_fields=["status", "completed_at", "updated_at"])
        record_activity("Task status updated", f"{task.title} · {task.get_status_display()}", actor=request.user, lead=task.lead, project=task.project)
        _notify_task_participants(
            task,
            kind="task-status",
            title="Task status changed",
            body=f"{task.title} moved to {task.get_status_display()}.",
            created_by=request.user,
        )
        messages.success(request, "Task status updated.")
    return _workspace_redirect(request, "tasks", task=task.pk)


def _owner_notification_recipients():
    return list(_staff_users().filter(is_superuser=True))


def _project_staff_recipients(project):
    return list(
        project.assigned_staff.filter(
            is_staff=True,
            is_active=True,
        ).filter(
            Q(employee_profile__isnull=True) | Q(employee_profile__is_active=True)
        ).distinct()
    )


def _project_notification_url(project, *, team=False):
    route = "operations:team-section" if team else "operations:dashboard-section"
    return (
        reverse(route, kwargs={"section": "projects"})
        + "?"
        + urlencode({"project": project.pk})
    )


def _task_notification_url(task, *, team=False):
    route = "operations:team-section" if team else "operations:dashboard-section"
    return (
        reverse(route, kwargs={"section": "tasks"})
        + "?"
        + urlencode({"task": task.pk})
    )


def _queue_employee_notifications_by_workspace(
    recipients,
    *,
    owner_destination_url,
    employee_destination_url,
    **notification_kwargs,
):
    """Queue one notification per recipient with a link for their workspace."""
    unique_recipients = {}
    for recipient in recipients:
        recipient_id = getattr(recipient, "pk", None)
        if recipient_id is not None:
            unique_recipients.setdefault(recipient_id, recipient)

    owners = [user for user in unique_recipients.values() if user.is_superuser]
    employees = [user for user in unique_recipients.values() if not user.is_superuser]
    queued = []
    if owners:
        queued.extend(
            queue_employee_notifications(
                owners,
                destination_url=owner_destination_url,
                **notification_kwargs,
            )
        )
    if employees:
        queued.extend(
            queue_employee_notifications(
                employees,
                destination_url=employee_destination_url,
                **notification_kwargs,
            )
        )
    return queued


def _portal_notification_url(client, *, project=None, estimate=None, section=None):
    query = {"client": client.pk}
    if project is not None:
        query["project"] = project.pk
    if estimate is not None:
        query["estimate"] = estimate.pk
    if section:
        destination = reverse("operations:portal-section", kwargs={"section": section})
    else:
        destination = reverse("operations:portal")
    return f"{destination}?{urlencode(query)}"


def _client_notification_requires_message_open(notification):
    message = notification.message if notification.message_id else None
    return bool(
        message
        and message.client_id == notification.client_id
        and not message.is_read
        and (
            notification.kind == "employee-reply"
            or (message.sent_by_id and message.sent_by.is_staff)
        )
    )


def _client_message_notification_url(client, notification):
    project = notification.project
    if project is None and notification.message_id:
        project = notification.message.project
    if project is not None and project.client_id != client.pk:
        project = None
    return _portal_notification_url(client, project=project, section="messages")


def _notify_project_staff(
    project,
    *,
    kind,
    title,
    body,
    created_by=None,
    metadata=None,
    lead=None,
    estimate=None,
    task=None,
    message=None,
    notify_owners=False,
    additional_recipients=None,
):
    recipients = _project_staff_recipients(project)
    if additional_recipients:
        recipients += list(additional_recipients)
    if notify_owners:
        recipients += _owner_notification_recipients()
    return _queue_employee_notifications_by_workspace(
        recipients,
        owner_destination_url=_project_notification_url(project),
        employee_destination_url=_project_notification_url(project, team=True),
        kind=kind,
        title=title,
        body=body,
        metadata=metadata,
        created_by=created_by,
        exclude_users=[created_by] if created_by else None,
        lead=lead,
        estimate=estimate,
        project=project,
        task=task,
        message=message,
    )


def _notify_project_client(
    project,
    *,
    kind,
    title,
    body,
    created_by=None,
    metadata=None,
    estimate=None,
    task=None,
    message=None,
):
    if not project or not project.client_id:
        return []
    return queue_client_notifications(
        [project.client],
        kind=kind,
        title=title,
        body=body,
        destination_url=_portal_notification_url(
            project.client,
            project=project,
            estimate=estimate,
        ),
        metadata=metadata,
        created_by=created_by,
        project=project,
        estimate=estimate,
        task=task,
        message=message,
    )


def _task_notification_recipients(task, *, notify_owners=True):
    recipients = []
    recipients += list(task.assigned_to.all())
    recipients += list(task.watchers.all())
    if task.project_id:
        recipients += _project_staff_recipients(task.project)
    if task.lead_id and task.lead.assigned_to_id:
        recipients.append(task.lead.assigned_to)
    if notify_owners:
        recipients += _owner_notification_recipients()
    return list({recipient.pk: recipient for recipient in recipients if recipient}.values())


def _notify_task_participants(
    task,
    *,
    kind,
    title,
    body,
    created_by=None,
    notify_owners=True,
):
    return _queue_employee_notifications_by_workspace(
        _task_notification_recipients(task, notify_owners=notify_owners),
        owner_destination_url=_task_notification_url(task),
        employee_destination_url=_task_notification_url(task, team=True),
        kind=kind,
        title=title,
        body=body,
        metadata={"task_id": str(task.pk)},
        created_by=created_by,
        exclude_users=[created_by] if created_by else None,
        lead=task.lead,
        project=task.project,
        task=task,
    )


def _notify_lead_assignment(lead, *, created_by=None):
    recipients = _owner_notification_recipients()
    if lead.assigned_to_id:
        recipients.append(lead.assigned_to)
    return queue_employee_notifications(
        recipients,
        kind="lead-assignment",
        title="A lead was assigned to you",
        body=f"{lead.name} needs follow-up in the lead pipeline.",
        destination_url=reverse("operations:team-section", kwargs={"section": "tasks"}),
        metadata={"lead_id": str(lead.pk)},
        created_by=created_by,
        exclude_users=[created_by] if created_by else None,
        lead=lead,
    )

def _notify_client_message_recipients(client, project, message, *, actor):
    body = f"{client.name} sent a new message."
    if project is not None:
        return _notify_project_staff(
            project,
            kind="client-message",
            title="New client message",
            body=body,
            created_by=actor,
            message=message,
            notify_owners=True,
        )
    return queue_employee_notifications(
        _owner_notification_recipients(),
        kind="client-message",
        title="New client message",
        body=body,
        destination_url=(
            reverse("operations:dashboard-section", kwargs={"section": "clients"})
            + "?"
            + urlencode({"client": client.pk, "messages": 1})
        ),
        metadata={"client_id": str(client.pk)},
        created_by=actor,
        message=message,
    )

def _calendar_notification_url(day):
    return (
        reverse("operations:team-section", kwargs={"section": "calendar"})
        + "?"
        + urlencode({"month": day.strftime("%Y-%m"), "day": day.isoformat()})
    )


def _friendly_calendar_date(day):
    return day.strftime('%b %d, %Y').replace(' 0', ' ')


def _employee_schedule_signature(schedule):
    if schedule is None:
        return None
    return (
        schedule.employee_id,
        schedule.weekday,
        schedule.is_working,
        schedule.start_time,
        schedule.end_time,
    )


def _employee_override_signature(override):
    if override is None:
        return None
    return (
        override.employee_id,
        override.date,
        override.status,
        override.start_time,
        override.end_time,
        override.reason,
    )


@require_POST
@staff_required
def calendar_day_update(request):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied

    form = CalendarDayOverrideForm(request.POST)
    month_value = request.POST.get('month', '').strip()
    day_value = request.POST.get('date', '').strip()
    schedule_employee_value = request.POST.get('schedule_employee', '').strip()

    def render_error():
        query = request.GET.copy()
        if month_value:
            query['month'] = month_value
        if day_value:
            query['day'] = day_value
        if schedule_employee_value:
            query['schedule_employee'] = schedule_employee_value
        request.GET = query
        return render(
            request,
            'operations/workspace.html',
            _workspace_context(request, 'calendar', form_overrides={'day_form': form}),
        )

    if not form.is_valid():
        return render_error()

    override_date = form.cleaned_data['date']
    try:
        if len(month_value) != 7:
            raise ValueError
        date.fromisoformat(f'{month_value}-01')
    except ValueError:
        month_value = override_date.strftime('%Y-%m')

    status = form.cleaned_data['status']
    if status == CalendarDayOverrideForm.NORMAL:
        changed = False
        with transaction.atomic():
            override = CalendarDayOverride.objects.select_for_update().filter(date=override_date).first()
            if override:
                override.delete()
                changed = True
            if changed:
                record_activity(
                    'Calendar day reset',
                    override_date.isoformat(),
                    actor=request.user,
                )
                queue_employee_notifications(
                    _active_employee_users(),
                    kind='company-day',
                    title='Company calendar day reset',
                    body=f'{_friendly_calendar_date(override_date)} is back to its normal schedule.',
                    destination_url=_calendar_notification_url(override_date),
                    metadata={'date': override_date.isoformat(), 'status': 'normal'},
                    created_by=request.user,
                    exclude_users=[request.user],
                )
        if changed:
            messages.success(request, f'{override_date.isoformat()} returned to a normal day.')
        else:
            messages.info(request, f'{override_date.isoformat()} was already a normal day.')
        return _workspace_redirect(
            request,
            'calendar',
            month=month_value,
            day=override_date.isoformat(),
            schedule_employee=schedule_employee_value,
        )

    with transaction.atomic():
        override = CalendarDayOverride.objects.select_for_update().filter(date=override_date).first()
        was_existing = override is not None
        previous_signature = (
            (override.status, override.short_start, override.short_end, override.reason)
            if override is not None
            else None
        )
        if override is None:
            override = CalendarDayOverride(
                date=override_date,
                created_by=request.user,
            )
        override.status = status
        override.short_start = form.cleaned_data['short_start']
        override.short_end = form.cleaned_data['short_end']
        override.reason = form.cleaned_data['reason']
        try:
            override.full_clean()
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        else:
            next_signature = (
                status,
                form.cleaned_data['short_start'],
                form.cleaned_data['short_end'],
                form.cleaned_data['reason'],
            )
            changed = previous_signature != next_signature
            override.save()
            if changed:
                record_activity(
                    'Calendar day updated',
                    f'{override_date.isoformat()} · {override.get_status_display()}',
                    actor=request.user,
                )
                queue_employee_notifications(
                    _active_employee_users(),
                    kind='company-day',
                    title='Company calendar updated',
                    body=(
                        f'{_friendly_calendar_date(override_date)} is now '
                        f'{override.get_status_display().lower()}.'
                    ),
                    destination_url=_calendar_notification_url(override_date),
                    metadata={
                        'date': override_date.isoformat(),
                        'status': override.status,
                    },
                    created_by=request.user,
                    exclude_users=[request.user],
                )

    if form.errors:
        return render_error()

    action = 'updated' if was_existing else 'added'
    if changed:
        messages.success(request, f'{override_date.isoformat()} {override.get_status_display().lower()} setting {action}.')
    else:
        messages.info(request, f'{override_date.isoformat()} already has that setting.')
    return _workspace_redirect(
        request,
        'calendar',
        month=month_value,
        day=override_date.isoformat(),
        schedule_employee=schedule_employee_value,
    )


@require_POST
@staff_required
def schedule_create(request):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied
    form = ScheduleEventForm(
        request.POST,
        staff_queryset=_staff_users(),
        project_queryset=_visible_projects_for_user(request.user),
        task_queryset=_visible_tasks_for_user(request.user),
    )
    if form.is_valid():
        with transaction.atomic():
            event = form.save(commit=False)
            event.created_by = request.user
            event.save()
            form.save_m2m()
            assigned_employees = list(event.assignees.all())
            record_activity('Calendar event created', event.title, actor=request.user, project=event.project)
            if assigned_employees:
                event_day = timezone.localtime(event.start_at, CALENDAR_TIME_ZONE).date()
                queue_employee_notifications(
                    assigned_employees,
                    kind='calendar-event',
                    title='You were added to a calendar event',
                    body=f'{event.title} · {event_day.isoformat()}',
                    destination_url=_calendar_notification_url(event_day),
                    metadata={'event_id': str(event.pk), 'date': event_day.isoformat()},
                    created_by=request.user,
                    exclude_users=[request.user],
                    project=event.project,
                    task=event.task,
                )
        messages.success(request, f"{event.title} was added to the calendar.")
        return _workspace_redirect(request, "calendar", event=event.pk)
    messages.error(request, "Please correct the calendar event details.")
    return _render_workspace_form_error(request, "calendar", {"event_form": form}, new="event")


@require_POST
@staff_required
def schedule_update(request, pk):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied
    event = get_object_or_404(ScheduleEvent, pk=pk)
    previous_values = (
        event.title,
        event.project_id,
        event.task_id,
        event.start_at,
        event.end_at,
        event.location,
        event.notes,
        tuple(sorted(event.assignees.values_list('pk', flat=True))),
    )
    form = ScheduleEventForm(
        request.POST,
        instance=event,
        staff_queryset=_staff_users(),
        project_queryset=_visible_projects_for_user(request.user),
        task_queryset=_visible_tasks_for_user(request.user),
    )
    if form.is_valid():
        with transaction.atomic():
            locked_event = ScheduleEvent.objects.select_for_update().get(pk=pk)
            form.instance = locked_event
            # ModelForm validation populated the original instance before the
            # transaction began. Copy its validated model fields onto the
            # locked row before saving so a concurrent-safe update does not
            # accidentally write the old values back.
            for field_name in form.fields:
                if field_name != 'assignees' and field_name in form.cleaned_data:
                    setattr(locked_event, field_name, form.cleaned_data[field_name])
            event = form.save(commit=False)
            event.save()
            form.save_m2m()
            current_values = (
                event.title,
                event.project_id,
                event.task_id,
                event.start_at,
                event.end_at,
                event.location,
                event.notes,
                tuple(sorted(event.assignees.values_list('pk', flat=True))),
            )
            changed = previous_values != current_values
            if changed:
                record_activity('Calendar event updated', event.title, actor=request.user, project=event.project)
                affected_ids = set(previous_values[-1]) | set(current_values[-1])
                if affected_ids:
                    event_day = timezone.localtime(event.start_at, CALENDAR_TIME_ZONE).date()
                    queue_employee_notifications(
                        affected_ids,
                        kind='calendar-event',
                        title='A calendar event was updated',
                        body=f'{event.title} · {event_day.isoformat()}',
                        destination_url=_calendar_notification_url(event_day),
                        metadata={'event_id': str(event.pk), 'date': event_day.isoformat()},
                        created_by=request.user,
                        exclude_users=[request.user],
                        project=event.project,
                        task=event.task,
                    )
        messages.success(request, "Calendar event updated." if changed else "No calendar event changes were needed.")
        return _workspace_redirect(request, "calendar", event=event.pk)
    messages.error(request, "Please correct the calendar event details.")
    return _render_workspace_form_error(request, "calendar", {"event_form": form}, selections={"event": event.pk}, edit="event")


@require_POST
@staff_required
def schedule_delete(request, pk):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied
    with transaction.atomic():
        event = get_object_or_404(ScheduleEvent.objects.select_for_update(), pk=pk)
        event_title = event.title
        assigned_employees = list(event.assignees.all())
        event_day = timezone.localtime(event.start_at, CALENDAR_TIME_ZONE).date()
        record_activity(
            "Calendar event deleted",
            event_title,
            actor=request.user,
            project=event.project,
        )
        event.delete()
        if assigned_employees:
            queue_employee_notifications(
                assigned_employees,
                kind='calendar-event',
                title='A calendar event was removed',
                body=f'{event_title} · {event_day.isoformat()}',
                destination_url=_calendar_notification_url(event_day),
                metadata={'date': event_day.isoformat()},
                created_by=request.user,
                exclude_users=[request.user],
                project=event.project,
                task=event.task,
            )
    messages.success(request, f"{event_title} was removed from the calendar.")
    return _workspace_redirect(request, "calendar")


@require_POST
@staff_required
def weekly_schedule_update(request):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied

    employee = get_object_or_404(_staff_users(), pk=request.POST.get('employee'))
    weekly_forms = {
        weekday: EmployeeWeeklyScheduleForm(request.POST, prefix=f'weekday-{weekday}')
        for weekday, _ in EmployeeWeeklySchedule.Weekday.choices
    }
    requested_schedule_day = request.POST.get('schedule_day', '')
    try:
        schedule_day = date.fromisoformat(requested_schedule_day)
    except ValueError:
        schedule_day = timezone.localtime(timezone.now(), CALENDAR_TIME_ZONE).date()

    forms_valid = True
    for form in weekly_forms.values():
        if not form.is_valid():
            forms_valid = False

    if not forms_valid:
        messages.error(request, 'Please correct the weekly shift details.')
        return _render_workspace_form_error(
            request,
            'calendar',
            {'weekly_forms': weekly_forms},
            selections={
                'schedule_employee': employee.pk,
                'schedule_day': schedule_day.isoformat(),
            },
        )

    changed_weekdays = []
    with transaction.atomic():
        for weekday, form in weekly_forms.items():
            schedule = EmployeeWeeklySchedule.objects.select_for_update().filter(
                employee=employee,
                weekday=weekday,
            ).first()
            previous_signature = _employee_schedule_signature(schedule)
            is_working = form.cleaned_data['is_working']
            if schedule is None and not is_working:
                # An unscheduled day is the default; do not create empty rows.
                continue
            if schedule is None:
                schedule = EmployeeWeeklySchedule(
                    employee=employee,
                    weekday=weekday,
                )
            schedule.is_working = is_working
            schedule.start_time = form.cleaned_data['start_time'] if is_working else None
            schedule.end_time = form.cleaned_data['end_time'] if is_working else None
            schedule.full_clean()
            if previous_signature != _employee_schedule_signature(schedule):
                schedule.save()
                changed_weekdays.append(schedule.get_weekday_display())

        if changed_weekdays:
            record_activity(
                'Employee weekly schedule updated',
                f'{employee.get_username()} · {", ".join(changed_weekdays)}',
                actor=request.user,
            )
            queue_employee_notifications(
                [employee],
                kind='employee-schedule',
                title='Your weekly schedule was updated',
                body=f'Your recurring weekly hours were updated for {", ".join(changed_weekdays)}.',
                destination_url=_calendar_notification_url(schedule_day),
                metadata={
                    'employee_id': employee.pk,
                    'weekdays': changed_weekdays,
                },
                created_by=request.user,
                exclude_users=[request.user],
            )

    messages.success(request, 'Weekly schedule saved.' if changed_weekdays else 'No weekly schedule changes were needed.')
    return _workspace_redirect(
        request,
        'calendar',
        schedule_employee=employee.pk,
        schedule_day=schedule_day.isoformat(),
    )


@require_POST
@staff_required
def employee_day_schedule_update(request):
    if not _can_manage_schedule(request.user):
        raise PermissionDenied

    form = EmployeeScheduleOverrideForm(
        request.POST,
        employee_queryset=_staff_users(),
    )
    if not form.is_valid():
        messages.error(request, 'Please correct the employee day override details.')
        return _render_workspace_form_error(
            request,
            'calendar',
            {'schedule_override_form': form},
            selections={
                'schedule_employee': request.POST.get('employee'),
                'schedule_day': request.POST.get('date'),
            },
        )

    employee = form.cleaned_data['employee']
    override_date = form.cleaned_data['date']
    status = form.cleaned_data['status']
    try:
        week_anchor = override_date - timedelta(days=override_date.weekday())
    except (TypeError, AttributeError):
        week_anchor = override_date

    with transaction.atomic():
        override = EmployeeScheduleOverride.objects.select_for_update().filter(
            employee=employee,
            date=override_date,
        ).first()
        previous_signature = _employee_override_signature(override)
        if status == EmployeeScheduleOverrideForm.CLEAR:
            if override is not None:
                override.delete()
            changed = previous_signature is not None
            action_description = 'cleared'
        else:
            if override is None:
                override = EmployeeScheduleOverride(
                    employee=employee,
                    date=override_date,
                    created_by=request.user,
                )
            override.status = status
            override.start_time = form.cleaned_data['start_time']
            override.end_time = form.cleaned_data['end_time']
            override.reason = form.cleaned_data['reason']
            override.full_clean()
            override.save()
            changed = previous_signature != _employee_override_signature(override)
            action_description = 'updated'

        if changed:
            record_activity(
                f'Employee day schedule {action_description}',
                f'{employee.get_username()} · {override_date.isoformat()}',
                actor=request.user,
            )
            queue_employee_notifications(
                [employee],
                kind='employee-schedule',
                title='Your schedule was updated',
                body=(
                    f'{_friendly_calendar_date(override_date)} is now '
                    f'{"a working day" if status == EmployeeScheduleOverride.Status.WORKING else "a day off" if status == EmployeeScheduleOverride.Status.OFF else "using your weekly schedule"}.'
                ),
                destination_url=_calendar_notification_url(override_date),
                metadata={
                    'employee_id': employee.pk,
                    'date': override_date.isoformat(),
                    'status': status,
                },
                created_by=request.user,
                exclude_users=[request.user],
            )

    messages.success(
        request,
        f'{_friendly_calendar_date(override_date)} {action_description}.'
        if changed
        else 'No employee day schedule changes were needed.',
    )
    return _workspace_redirect(
        request,
        'calendar',
        schedule_employee=employee.pk,
        schedule_day=override_date.isoformat(),
        week=week_anchor.isoformat(),
    )


def _notification_request_data(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body.decode('utf-8') or '{}')
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST


@require_POST
@team_required
def notification_device_register(request):
    if request.user.is_superuser and (
        not getattr(settings, "GCC_MOBILE_OWNER_PUSH_ENABLED", False)
        or not is_mobile_webview(request)
    ):
        return JsonResponse(
            {"error": "Owner mobile notifications are disabled."},
            status=403,
        )
    data = _notification_request_data(request)
    token = str(data.get('token') or data.get('expo_push_token') or '').strip()
    if not token or len(token) > 255 or not token.startswith(('ExpoPushToken[', 'ExponentPushToken[')):
        return JsonResponse({'error': 'A valid Expo push token is required.'}, status=400)

    now = timezone.now()
    with transaction.atomic():
        MobilePushDevice.objects.filter(token=token).exclude(
            employee=request.user,
            client__isnull=True,
        ).update(
            is_active=False,
            deactivated_at=now,
            updated_at=now,
        )
        device, _ = MobilePushDevice.objects.update_or_create(
            token=token,
            defaults={
                'employee': request.user,
                'client': None,
                'platform': str(data.get('platform') or '')[:20],
                'is_active': True,
                'last_seen_at': now,
                'deactivated_at': None,
            },
        )
    return JsonResponse({'ok': True, 'device_id': str(device.pk)})


@require_POST
@team_required
def notification_device_deactivate(request):
    data = _notification_request_data(request)
    token = str(data.get('token') or data.get('expo_push_token') or '').strip()
    devices = MobilePushDevice.objects.filter(employee=request.user, is_active=True)
    if token:
        devices = devices.filter(token=token)
    updated = devices.update(
        is_active=False,
        deactivated_at=timezone.now(),
        updated_at=timezone.now(),
    )
    return JsonResponse({'ok': True, 'deactivated': updated})


@require_POST
@login_required
def client_notification_device_register(request):
    if not _is_active_client(request.user):
        raise PermissionDenied

    client = get_object_or_404(Client, user=request.user)
    data = _notification_request_data(request)
    token = str(data.get('token') or data.get('expo_push_token') or '').strip()
    if not token or len(token) > 255 or not token.startswith(('ExpoPushToken[', 'ExponentPushToken[')):
        return JsonResponse({'error': 'A valid Expo push token is required.'}, status=400)

    now = timezone.now()
    with transaction.atomic():
        MobilePushDevice.objects.filter(token=token).exclude(
            client=client,
            employee__isnull=True,
        ).update(
            is_active=False,
            deactivated_at=now,
            updated_at=now,
        )
        device, _ = MobilePushDevice.objects.update_or_create(
            token=token,
            defaults={
                'employee': None,
                'client': client,
                'platform': str(data.get('platform') or '')[:20],
                'is_active': True,
                'last_seen_at': now,
                'deactivated_at': None,
            },
        )
    return JsonResponse({'ok': True, 'device_id': str(device.pk)})


@require_POST
@login_required
def client_notification_device_deactivate(request):
    if not _is_active_client(request.user):
        raise PermissionDenied

    client = get_object_or_404(Client, user=request.user)
    data = _notification_request_data(request)
    token = str(data.get('token') or data.get('expo_push_token') or '').strip()
    devices = MobilePushDevice.objects.filter(
        client=client,
        employee__isnull=True,
        is_active=True,
    )
    if token:
        devices = devices.filter(token=token)
    now = timezone.now()
    updated = devices.update(
        is_active=False,
        deactivated_at=now,
        updated_at=now,
    )
    return JsonResponse({'ok': True, 'deactivated': updated})


@require_POST
@staff_required
def dashboard_notification_mark_read(request, pk):
    notification = get_object_or_404(EmployeeNotification, pk=pk, employee=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return _workspace_redirect(request, "notifications")


@require_POST
@staff_required
def dashboard_notifications_mark_all_read(request):
    EmployeeNotification.objects.filter(
        employee=request.user,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).update(read_at=timezone.now(), updated_at=timezone.now())
    return _workspace_redirect(request, "notifications")


@require_POST
@staff_required
def dashboard_notification_clear(request, pk):
    now = timezone.now()
    with transaction.atomic():
        notification = get_object_or_404(
            EmployeeNotification.objects.select_for_update(),
            pk=pk,
            employee=request.user,
        )
        update_fields = ["dismissed_at", "updated_at"]
        notification.dismissed_at = notification.dismissed_at or now
        if notification.read_at is None:
            notification.read_at = now
            update_fields.append("read_at")
        notification.save(update_fields=update_fields)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "cleared": True})
    return _workspace_redirect(request, "notifications")


@require_POST
@staff_required
def dashboard_notifications_clear_all(request):
    now = timezone.now()
    notifications = EmployeeNotification.objects.filter(
        employee=request.user,
        dismissed_at__isnull=True,
    )
    with transaction.atomic():
        notifications.filter(read_at__isnull=True).update(
            read_at=now,
            updated_at=now,
        )
        cleared = notifications.update(
            dismissed_at=now,
            updated_at=now,
        )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "cleared": cleared})
    return _workspace_redirect(request, "notifications")


@require_GET
@team_required
def employee_notifications(request):
    return render(
        request,
        'operations/workspace.html',
        _workspace_context(request, 'notifications', team_mode=True),
    )


@require_POST
@team_required
def employee_notification_mark_read(request, pk):
    notification = get_object_or_404(EmployeeNotification, pk=pk, employee=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=['read_at', 'updated_at'])
    return JsonResponse({'ok': True, 'unread': EmployeeNotification.objects.filter(employee=request.user, read_at__isnull=True, dismissed_at__isnull=True).count()})


@require_POST
@team_required
def employee_notifications_mark_all_read(request):
    updated = EmployeeNotification.objects.filter(employee=request.user, read_at__isnull=True, dismissed_at__isnull=True).update(
        read_at=timezone.now(),
        updated_at=timezone.now(),
    )
    return JsonResponse({'ok': True, 'marked_read': updated})


@require_POST
@team_required
def employee_notification_clear(request, pk):
    now = timezone.now()
    with transaction.atomic():
        notification = get_object_or_404(
            EmployeeNotification.objects.select_for_update(),
            pk=pk,
            employee=request.user,
        )
        update_fields = ["dismissed_at", "updated_at"]
        notification.dismissed_at = notification.dismissed_at or now
        if notification.read_at is None:
            notification.read_at = now
            update_fields.append("read_at")
        notification.save(update_fields=update_fields)
    return JsonResponse({'ok': True, 'cleared': True, 'unread': EmployeeNotification.objects.filter(employee=request.user, read_at__isnull=True, dismissed_at__isnull=True).count()})


@require_POST
@team_required
def employee_notifications_clear_all(request):
    now = timezone.now()
    notifications = EmployeeNotification.objects.filter(
        employee=request.user,
        dismissed_at__isnull=True,
    )
    with transaction.atomic():
        notifications.filter(read_at__isnull=True).update(
            read_at=now,
            updated_at=now,
        )
        cleared = notifications.update(
            dismissed_at=now,
            updated_at=now,
        )
    return JsonResponse({'ok': True, 'cleared': cleared})


@require_POST
@team_required
def time_clock(request):
    employee = request.user
    if _can_access_dashboard(request.user) and request.POST.get("employee"):
        if not _can_manage_team(request.user):
            raise PermissionDenied
        employee = get_object_or_404(get_user_model(), pk=request.POST["employee"], is_staff=True)
    now = timezone.now()
    try:
        with transaction.atomic():
            active = TimeEntry.objects.select_for_update().filter(employee=employee, clock_out__isnull=True).first()
            if active:
                active.clock_out = now
                active.note = request.POST.get("note", active.note)
                active.save(update_fields=["clock_out", "note", "updated_at"])
                activity = ("Clocked out", employee.get_username(), active.project)
            else:
                project = None
                task = None
                if request.POST.get("project"):
                    project = get_object_or_404(_visible_projects_for_user(request.user), pk=request.POST["project"])
                if request.POST.get("task"):
                    task = get_object_or_404(_visible_tasks_for_user(request.user), pk=request.POST["task"])
                    if task.project_id and (not project or task.project_id != project.pk):
                        raise ValidationError("The selected task must belong to the selected project.")
                TimeEntry.objects.create(
                    employee=employee,
                    project=project,
                    task=task,
                    clock_in=now,
                    note=request.POST.get("note", ""),
                )
                activity = ("Clocked in", employee.get_username(), project)
    except (IntegrityError, ValidationError) as exc:
        messages.error(request, str(exc) or "This employee already has an active time entry.")
    else:
        record_activity(activity[0], activity[1], actor=request.user, project=activity[2])
        messages.success(request, f"{employee.get_username()} {"clocked out." if activity[0] == "Clocked out" else "is clocked in."}")
    return _workspace_redirect(request, "time")


@require_POST
@staff_required
def time_update(request, pk):
    if not _can_manage_team(request.user):
        raise PermissionDenied
    entry = get_object_or_404(TimeEntry, pk=pk)
    form = TimeEntryForm(request.POST, instance=entry, project_queryset=_visible_projects_for_user(request.user), task_queryset=_visible_tasks_for_user(request.user))
    if form.is_valid():
        entry = form.save(commit=False)
        entry.adjusted_by = request.user
        entry.adjusted_at = timezone.now()
        entry.save()
        record_activity('Time entry corrected', entry.employee.get_username(), actor=request.user, project=entry.project)
        messages.success(request, "Time entry corrected.")
    else:
        messages.error(request, "Please correct the time entry.")
        return _render_workspace_form_error(request, "time", {"time_edit_form": form}, selections={"time_entry": entry.pk})
    return _workspace_redirect(request, "time")


@require_POST
@staff_required
@transaction.atomic
def document_upload(request):
    if not _can_manage_documents(request.user):
        raise PermissionDenied
    form = ProjectDocumentForm(request.POST, request.FILES, project_queryset=_visible_projects_for_user(request.user))
    if form.is_valid():
        document = form.save(commit=False)
        if not _visible_projects_for_user(request.user).filter(pk=document.project_id).exists():
            raise PermissionDenied
        document.uploaded_by = request.user
        document.save()
        record_activity("Project document uploaded", f"{document.project.title} · {document.title}", actor=request.user, project=document.project)
        _notify_project_staff(
            document.project,
            kind="document-uploaded",
            title="A project document was added",
            body=f"{document.title} was added to {document.project.title}.",
            created_by=request.user,
        )
        if document.visibility == ProjectDocument.Visibility.CLIENT:
            _notify_project_client(
                document.project,
                kind="document-published",
                title="A new project document is available",
                body=f"{document.title} is ready in your client portal.",
                created_by=request.user,
                )
        messages.success(request, "Project document uploaded.")
        return _workspace_redirect(request, "documents", project=document.project_id)
    messages.error(request, "Please choose a valid document and project.")
    return _render_workspace_form_error(request, "documents", {"document_form": form}, selections={"project": request.POST.get("project")})


@require_GET
@login_required
def document_file(request, pk):
    document = get_object_or_404(ProjectDocument.objects.select_related("project", "project__client"), pk=pk)
    if not request.user.is_authenticated:
        return redirect(f"{reverse('operations:login')}?{urlencode({'next': request.path})}")
    if not can_view_project_document(request.user, document):
        raise PermissionDenied
    try:
        opened = document.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(opened, content_type=mimetypes.guess_type(document.file.name)[0] or "application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{sanitize_uploaded_name(document.file.name.rsplit("/", 1)[-1])}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@require_GET
@login_required
def agreement_file(request, pk):
    agreement = get_object_or_404(
        Agreement.objects.select_related("project", "project__client"),
        pk=pk,
    )
    if not can_view_agreement(request.user, agreement):
        raise PermissionDenied
    if not agreement.signed_pdf:
        raise Http404
    try:
        opened = agreement.signed_pdf.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(opened, content_type="application/pdf")
    filename = sanitize_uploaded_name(agreement.signed_pdf.name.rsplit("/", 1)[-1])
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_POST
@workspace_staff_required
@transaction.atomic
def staff_message_reply(request, client_pk):
    team_mode = request.path.startswith("/team/")
    if not team_mode and not _can_manage_messages(request.user):
        raise PermissionDenied
    client = get_object_or_404(Client, pk=client_pk)
    project = (
        get_object_or_404(Project, pk=request.POST.get("project"), client=client)
        if team_mode and request.POST.get("project")
        else None
    )
    if team_mode and (
        project is None
        or not project.assigned_staff.filter(pk=request.user.pk).exists()
    ):
        raise PermissionDenied
    form = StaffMessageForm(request.POST)
    if form.is_valid():
        client_message = form.save(commit=False)
        client_message.client = client
        client_message.project = project
        client_message.sent_by = request.user
        client_message.is_read = False
        client_message.save()
        record_activity("Staff message sent", client.name, actor=request.user, project=project)
        queue_client_notifications(
            [client],
            kind="employee-reply",
            title="Grand Coast replied to your message",
            body="A member of the Grand Coast team replied in your client portal.",
            destination_url=_portal_notification_url(client, project=project, section="messages"),
            metadata={"client_id": str(client.pk)},
            created_by=request.user,
            project=project,
            message=client_message,
        )
        messages.success(request, "Your reply was added to the conversation.")
    else:
        messages.error(request, "Please write a message before replying.")
        return _render_workspace_form_error(
            request,
            "projects" if team_mode else "clients",
            {"staff_message_form": form},
            selections={"project" if team_mode else "client": project.pk if team_mode and project else client.pk},
        )
    if team_mode:
        return _workspace_redirect(request, "projects", project=project.pk)
    return _workspace_redirect(request, "clients", client=client.pk)

def staff_mark_message_read(request, pk):
    if not _can_manage_messages(request.user):
        raise PermissionDenied
    client_message = get_object_or_404(ClientMessage, pk=pk)
    client_message.is_read = True
    client_message.save(update_fields=["is_read"])
    return _workspace_redirect(request, "clients", client=client_message.client_id)


@require_POST
@staff_required
def employee_invite_create(request):
    if not _can_manage_team(request.user):
        raise PermissionDenied
    form = EmployeeInviteForm(request.POST, group_queryset=Group.objects.filter(name__in=["Manager", "Office", "Field", "Sales"]))
    if form.is_valid():
        invite, raw_token = create_employee_invite(
            email=form.cleaned_data["email"],
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            group=form.cleaned_data["group"],
            actor=request.user,
        )
        invite_url = request.build_absolute_uri(
            reverse("operations:employee-invite", kwargs={"token": raw_token})
        )
        outbox = queue_employee_invite_email(invite, invite_url, actor=request.user)
        outbox = deliver_email_outbox(outbox)
        request.session["last_employee_invite_url"] = invite_url
        request.session["last_employee_invite_email_status"] = (
            outbox.status if outbox else ""
        )
        request.session["last_employee_invite_recipient"] = invite.email
        record_activity('Employee invitation created', form.cleaned_data['email'], actor=request.user)
        if outbox and outbox.status == EmailOutbox.Status.SENT:
            messages.success(
                request,
                f'Employee invitation emailed to {invite.email}. Copy the one-time link below as a backup.',
            )
        elif outbox and outbox.status == EmailOutbox.Status.FAILED:
            messages.warning(
                request,
                'The employee invitation was created, but email delivery failed. '
                'Use the one-time copy link below.',
            )
        elif outbox:
            messages.info(
                request,
                'The employee invitation was created. Email delivery is pending or disabled; '
                'use the one-time copy link below.',
            )
        else:
            messages.warning(
                request,
                'The employee invitation was created, but no deliverable email address was available. '
                'Use the one-time copy link below.',
            )
    else:
        messages.error(request, "Please correct the employee invitation details.")
        return _render_workspace_form_error(request, "team", {"invite_form": form})
    return _workspace_redirect(request, "team")


@require_POST
@workspace_staff_required
def employee_profile_update(request, pk):
    team_mode = request.path.startswith("/team/")
    if not team_mode and not _can_access_dashboard(request.user):
        raise PermissionDenied
    if not _can_manage_team(request.user) and str(getattr(getattr(request.user, "employee_profile", None), "pk", "")) != str(pk):
        raise PermissionDenied
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    form = EmployeeProfileForm(
        request.POST,
        instance=profile,
        allow_status=_can_manage_team(request.user),
    )
    if form.is_valid():
        form.save()
        record_activity('Employee profile updated', str(profile), actor=request.user)
        messages.success(request, f"{profile} profile updated.")
    else:
        messages.error(request, "Please correct the employee profile.")
        return _render_workspace_form_error(request, "team", {"profile_form": form})
    return _workspace_redirect(request, "team")


@require_POST
@staff_required
def employee_profile_self_update(request):
    profile = EmployeeProfile.objects.filter(user=request.user).first()
    if profile is None:
        profile = EmployeeProfile(user=request.user)
    form = EmployeeProfileForm(request.POST, instance=profile, allow_status=False)
    if form.is_valid():
        profile = form.save(commit=False)
        profile.user = request.user
        profile.save()
        record_activity("Employee profile updated", str(profile), actor=request.user)
        messages.success(request, "Your profile title was saved.")
    else:
        messages.error(request, "Please correct your profile details.")
        return _render_workspace_form_error(request, "team", {"profile_form": form})
    return _workspace_redirect(request, "team")


@require_POST
@staff_required
def employee_password_reset_create(request, pk):
    if not _can_manage_team(request.user):
        raise PermissionDenied
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    group = profile.user.groups.filter(name__in=["Manager", "Office", "Field", "Sales"]).first()
    if group is None:
        group = Group.objects.get_or_create(name="Field")[0]
    invite, raw_token = create_employee_invite(
        email=profile.user.email,
        first_name=profile.user.first_name,
        last_name=profile.user.last_name,
        group=group,
        actor=request.user,
        employee=profile,
        purpose=EmployeeInvite.Purpose.PASSWORD_RESET,
    )
    request.session["last_reset_url"] = request.build_absolute_uri(reverse("operations:employee-invite", kwargs={"token": raw_token}))
    messages.success(request, "A one-time password reset link is ready to copy.")
    return _workspace_redirect(request, "team")


def employee_invite(request, token):
    if request.user.is_authenticated:
        raise PermissionDenied
    invite = find_employee_invite(token)
    if invite is None:
        return render(request, "operations/invite_invalid.html", {"invite_kind": "employee"}, status=410)
    form = EmployeeInviteAcceptForm(
        request.POST or None,
        purpose=invite.purpose,
        initial={
            "username": invite.email,
            "first_name": invite.first_name,
            "last_name": invite.last_name,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            user = complete_employee_invite(invite, form)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            login(request, user)
            messages.success(request, "Your Grand Coast employee account is ready.")
            return redirect("operations:team")
    return render(request, "operations/employee_invite.html", {"form": form, "invite": invite})


@require_POST
@staff_required
def lead_create(request):
    form = LeadForm(request.POST, staff_queryset=_staff_users())
    if form.is_valid():
        lead = form.save(commit=False)
        lead.created_by = request.user
        lead.save()
        record_activity("New lead added", f"{lead.name} · Lead pipeline", actor=request.user, lead=lead)
        messages.success(request, f"{lead.name} was added to the lead pipeline.")
        return _dashboard_redirect("leads", lead=lead.pk)
    messages.error(request, "Please correct the lead details and try again.")
    return _render_dashboard_form_error(request, "leads", {"lead_form": form}, new="lead")


@require_POST
@staff_required
def lead_delete(request, pk):
    lead = _active_lead_or_404(pk)
    now = timezone.now()
    lead.deleted_at = now
    lead.deleted_by = request.user
    lead.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
    record_activity("Lead moved to trash", lead.name, actor=request.user, lead=lead)
    messages.success(request, f"{lead.name} was moved to Trash. You can restore it from the lead pipeline.")
    return _dashboard_redirect("leads")


@require_POST
@staff_required
def lead_delete_all(request):
    now = timezone.now()
    deleted_count = Lead.objects.filter(deleted_at__isnull=True).update(
        deleted_at=now,
        deleted_by_id=request.user.pk,
        updated_at=now,
    )
    if deleted_count:
        record_activity(
            "All leads moved to trash",
            f"{deleted_count} active lead(s)",
            actor=request.user,
        )
        messages.success(
            request,
            f"{deleted_count} lead(s) moved to Trash. You can restore them from the lead pipeline.",
        )
    else:
        messages.info(request, "There are no active leads to move to Trash.")
    return _dashboard_redirect("leads")


@require_POST
@staff_required
def lead_restore(request, pk):
    lead = get_object_or_404(Lead, pk=pk, deleted_at__isnull=False)
    lead.deleted_at = None
    lead.deleted_by = None
    lead.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
    record_activity("Lead restored", lead.name, actor=request.user, lead=lead)
    messages.success(request, f"{lead.name} was restored to the lead pipeline.")
    return _dashboard_redirect("leads", trash="1")


@require_POST
@staff_required
def lead_update_status(request, pk):
    lead = _active_lead_or_404(pk)
    form = LeadStatusForm(request.POST, instance=lead)
    if form.is_valid():
        lead = form.save()
        record_activity(f"{lead.name} moved to {lead.get_status_display()}", "Lead pipeline", actor=request.user, lead=lead)
        messages.success(request, "Lead status updated.")
    return _dashboard_redirect("leads", lead=lead.pk)


@require_POST
@staff_required
def lead_assign(request, pk):
    if not _can_manage_tasks(request.user):
        raise PermissionDenied
    lead = _active_lead_or_404(pk)
    previous_assignee_id = lead.assigned_to_id
    form = LeadAssignmentForm(request.POST, instance=lead, staff_queryset=_staff_users())
    if form.is_valid():
        lead = form.save()
        assignee = lead.assigned_to.get_full_name() if lead.assigned_to else "Unassigned"
        record_activity("Lead assignment updated", f"{lead.name} · {assignee}", actor=request.user, lead=lead)
        if previous_assignee_id != lead.assigned_to_id:
            _notify_lead_assignment(lead, created_by=request.user)
        messages.success(request, "Lead assignment updated.")
    else:
        messages.error(request, "That employee cannot be assigned to this lead.")
    return _dashboard_redirect("leads", lead=lead.pk)


@require_POST
@staff_required
def lead_toggle_priority(request, pk):
    lead = _active_lead_or_404(pk)
    lead.priority = not lead.priority
    lead.save(update_fields=["priority", "updated_at"])
    record_activity(
        f"{lead.name} marked as {'priority' if lead.priority else 'standard'}",
        "Lead pipeline",
        actor=request.user,
        lead=lead,
    )
    messages.success(request, "Lead priority updated.")
    return _dashboard_redirect("leads", lead=lead.pk)


@require_POST
@staff_required
def lead_update_note(request, pk):
    lead = _active_lead_or_404(pk)
    form = LeadNoteForm(request.POST, instance=lead, prefix="admin-note")
    if form.is_valid():
        lead = form.save()
        record_activity("Internal admin note updated", f"{lead.name} · Lead record", actor=request.user, lead=lead)
        messages.success(request, "Admin note saved. It is visible only to staff.")
    else:
        messages.error(request, "Please correct the admin note and try again.")
        return _render_dashboard_form_error(
            request,
            "leads",
            {"lead_note_form": form},
            selections={"lead": lead.pk, "lead_tab": "notes"},
        )
    return _dashboard_redirect("leads", lead=lead.pk, lead_tab="notes")


@require_POST
@staff_required
def lead_create_followup(request, pk):
    lead = _active_lead_or_404(pk)
    form = QuickTaskForm(request.POST, prefix="followup", staff_queryset=_staff_users(), lead=lead)
    if form.is_valid():
        task = form.save(commit=False)
        task.lead = lead
        task.created_by = request.user
        task.save()
        form.save_m2m()
        record_activity("Task added", f"{lead.name} · {task.title}", actor=request.user, lead=lead)
        _notify_task_participants(
            task,
            kind="task-assignment",
            title="Lead follow-up assigned",
            body=f"{task.title} needs follow-up for {lead.name}.",
            created_by=request.user,
        )
        messages.success(request, "Task saved.")
    else:
        messages.error(request, "Please correct the task details and try again.")
        return _render_dashboard_form_error(
            request,
            "leads",
            {"follow_up_form": form},
            selections={"lead": lead.pk, "lead_tab": "notes"},
        )
    return _dashboard_redirect("leads", lead=lead.pk, lead_tab="notes")


def _external_estimate_from_form(form, request):
    """Create only the GCC recipient/link record for an external estimate."""
    client = form.cleaned_data["client"]
    lead = (
        Lead.objects.filter(
            client=client,
            deleted_at__isnull=True,
        )
        .exclude(status=Lead.Status.LOST)
        .order_by("-created_at")
        .first()
    )
    estimate = Estimate.objects.create(
        client=client,
        lead=lead,
        title=f"Estimate for {client.name}",
        created_by=request.user,
        external_url=form.cleaned_data["external_url"],
        external_status=Estimate.ExternalEstimateStatus.NOT_STARTED,
    )
    if lead and lead.status not in {Lead.Status.WON, Lead.Status.LOST}:
        lead.status = Lead.Status.QUOTED
        lead.save(update_fields=["status", "updated_at"])
    return estimate


@require_POST
@staff_required
def estimate_create(request):
    if not external_estimate_enabled_for(request.user):
        raise PermissionDenied
    form = ExternalEstimateCreateForm(
        request.POST,
        client_queryset=Client.objects.all(),
    )
    if form.is_valid():
        with transaction.atomic():
            estimate = _external_estimate_from_form(form, request)
            record_activity(
                f"Estimate #{estimate.number} created",
                f"{estimate.client or 'Unassigned client'} - external link recorded",
                actor=request.user,
                lead=estimate.lead,
                estimate=estimate,
            )
        messages.success(request, f"Estimate #{estimate.number} created.")
        return _dashboard_redirect("estimates", estimate=estimate.pk, delivery_prompt="1")
    messages.error(request, "Choose a client and provide a secure external estimate link.")
    return _render_dashboard_form_error(
        request,
        "estimates",
        {"estimate_create_form": form},
        new="estimate",
    )


@require_POST
@staff_required
def external_estimate_send(request, pk):
    """Deliver an external estimate through the explicitly selected channels."""
    estimate = get_object_or_404(
        Estimate.objects.select_related("client"),
        pk=pk,
    )
    if not can_manage_external_estimate(request.user, estimate=estimate):
        raise PermissionDenied
    client = estimate.client
    if not estimate.external_url or not client:
        messages.error(request, "Add an estimate link and client before sending.")
        return _dashboard_redirect("estimates", estimate=estimate.pk)

    submitted_modes = {
        str(mode).strip().lower()
        for mode in request.POST.getlist("delivery_mode")
        if str(mode).strip()
    }
    valid_modes = {"sms", "email", "portal", "all", "email_portal"}
    if submitted_modes - valid_modes:
        messages.error(request, "Choose valid estimate delivery methods.")
        return _dashboard_redirect("estimates", estimate=estimate.pk, delivery_prompt="1")

    selected_channels = submitted_modes & {"sms", "email", "portal"}
    if request.POST.get("delivery_all") == "1" or submitted_modes & {"all"}:
        selected_channels = {"sms", "email", "portal"}
    elif "email_portal" in submitted_modes:
        selected_channels.update({"email", "portal"})
    if not selected_channels:
        messages.error(request, "Select at least one estimate delivery method.")
        return _dashboard_redirect("estimates", estimate=estimate.pk, delivery_prompt="1")

    send_sms = "sms" in selected_channels
    send_email = "email" in selected_channels
    publish_to_portal = "portal" in selected_channels
    if send_sms and not any(character.isdigit() for character in str(client.phone or "")):
        messages.error(request, "Add a client phone number before choosing text message delivery.")
        return _dashboard_redirect("estimates", estimate=estimate.pk, delivery_prompt="1")
    if send_email and not client.email:
        messages.error(request, "Add a client email address before choosing email delivery.")
        return _dashboard_redirect("estimates", estimate=estimate.pk, delivery_prompt="1")

    if selected_channels == {"sms"}:
        messages.info(
            request,
            "Open the text message draft and tap Send. After sending it, record the estimate as Sent in Update estimate.",
        )
        return _dashboard_redirect("estimates", estimate=estimate.pk, sms_followup="1")

    idempotency_key = str(request.POST.get("idempotency_key") or "").strip()
    if not idempotency_key:
        idempotency_key = f"external-estimate-email:{estimate.pk}:{uuid.uuid4().hex}"
    idempotency_key = idempotency_key[:100]

    with transaction.atomic():
        locked = Estimate.objects.select_for_update().select_related("client").get(pk=estimate.pk)
        if locked.external_status == Estimate.ExternalEstimateStatus.REJECTED:
            messages.error(request, "This estimate is closed. Create a new estimate if the scope has changed.")
            return _dashboard_redirect("estimates", estimate=estimate.pk)
        target_status = (
            Estimate.ExternalEstimateStatus.SENT
            if publish_to_portal and locked.external_status == Estimate.ExternalEstimateStatus.NOT_STARTED
            else locked.external_status
        )
        target_visibility = True if publish_to_portal else locked.external_client_visible
        if target_visibility != locked.external_client_visible or (
            publish_to_portal and target_status != locked.external_status
        ):
            locked, _created = record_external_estimate_status(
                locked,
                actor=request.user,
                status=target_status,
                external_url=locked.external_url,
                external_client_visible=target_visibility,
                idempotency_key=f"{idempotency_key}:status",
            )
        outbox = None
        if send_email:
            portal_copy = (
                (
                    "The same link is also available in your Grand Coast client portal.\n\n"
                    if client.user_id
                    else "The link will be available in your Grand Coast client portal after you accept your invitation.\n\n"
                )
                if publish_to_portal
                else ""
            )
            outbox = queue_email(
                recipient=client.email,
                subject="Your Grand Coast estimate is ready",
                body=(
                    f"Hello {client.name},\n\n"
                    "Your Grand Coast Construction estimate is ready to review. "
                    "Open it here:\n\n"
                    f"{locked.external_url}\n\n"
                    f"{portal_copy}"
                    "Grand Coast Construction"
                ),
                actor=request.user,
                client=client,
                idempotency_key=idempotency_key,
            )

    delivery_redirect = {"estimate": estimate.pk}
    if send_sms:
        delivery_redirect["sms_followup"] = "1"
        if send_email or publish_to_portal:
            delivery_redirect["sms_after_other_channels"] = "1"

    if not send_email:
        if client.user_id:
            messages.success(request, f"Estimate link published to {client.name}'s client portal.")
        else:
            messages.success(
                request,
                "Estimate link is saved for the client portal. The client can open it after accepting a portal invitation.",
            )
        if send_sms:
            messages.info(request, "Open the text message draft and tap Send to finish text delivery.")
            return _dashboard_redirect("estimates", **delivery_redirect)
        return _dashboard_redirect("estimates", estimate=estimate.pk)

    outbox = deliver_email_outbox(outbox)
    if (
        not publish_to_portal
        and outbox
        and outbox.status != EmailOutbox.Status.FAILED
    ):
        with transaction.atomic():
            locked = Estimate.objects.select_for_update().select_related("client").get(pk=estimate.pk)
            if locked.external_status == Estimate.ExternalEstimateStatus.NOT_STARTED:
                record_external_estimate_status(
                    locked,
                    actor=request.user,
                    status=Estimate.ExternalEstimateStatus.SENT,
                    external_url=locked.external_url,
                    external_client_visible=locked.external_client_visible,
                    idempotency_key=f"{idempotency_key}:status",
                )
    if outbox and outbox.status == EmailOutbox.Status.SENT:
        messages.success(
            request,
            f"Estimate link emailed to {client.email}."
            + (
                " It is also published in the client portal."
                if publish_to_portal and client.user_id
                else " It is saved for the portal after the client accepts an invitation."
                if publish_to_portal
                else " It was not published to the client portal."
            ),
        )
    elif outbox and outbox.status == EmailOutbox.Status.FAILED:
        messages.warning(
            request,
            (
                "Email delivery failed. The estimate remains published in the client portal."
                if publish_to_portal and client.user_id
                else "Email delivery failed. The link is saved for the portal after the client accepts an invitation."
                if publish_to_portal
                else "Email delivery failed. The estimate was not published to the client portal. You can retry or send the link manually."
            ),
        )
    else:
        messages.info(
            request,
            f"Estimate email queued for {client.email}."
            + (
                " The link is also published in the client portal."
                if publish_to_portal and client.user_id
                else " The link will be available in the portal after the client accepts an invitation."
                if publish_to_portal
                else " The link was not published to the client portal."
            ),
        )
    return _dashboard_redirect("estimates", **delivery_redirect)


@require_POST
@staff_required
def estimate_update(request, pk):
    estimate = get_object_or_404(Estimate, pk=pk)
    messages.info(
        request,
        "Estimate details are maintained in the external estimate service. GCC keeps legacy internal estimates as read-only history.",
    )
    return _dashboard_redirect("estimates", estimate=estimate.pk)


@require_POST
@staff_required
def estimate_send(request, pk):
    estimate = get_object_or_404(Estimate, pk=pk)
    messages.info(
        request,
        "Use ‘Send estimate to client’ on the external estimate record. Existing internal estimates are read-only history.",
    )
    return _dashboard_redirect("estimates", estimate=estimate.pk)


def _accepted_estimate_for_project_client(client):
    return (
        Estimate.objects.filter(status=Estimate.Status.ACCEPTED)
        .filter(external_status=Estimate.ExternalEstimateStatus.APPROVED)
        .exclude(external_url="")
        .filter(Q(client=client) | Q(lead__client=client))
        .exclude(projects__isnull=False)
        .select_related("lead", "external_status_by")
        .order_by("-accepted_at", "-created_at")
        .first()
    )


@require_POST
@staff_required
@transaction.atomic
def project_create(request):
    form = ProjectCreateForm(request.POST, staff_queryset=_staff_users())
    if form.is_valid():
        project = form.save(commit=False)
        if project.client_id and not project.estimate_id:
            project.estimate = _accepted_estimate_for_project_client(project.client)
        if project.estimate_id and not project.client_id:
            project.client = project.estimate.client
        if project.estimate_id and not project.lead_id:
            project.lead = project.estimate.lead
        project.created_by = request.user
        project.save()
        # ModelForm.save(commit=False) postpones the many-to-many write. The
        # assigned team is part of project creation, so persist it now.
        form.save_m2m()
        if not project.assigned_staff.exists():
            project.next_step = "Assign project staff"
            project.save(update_fields=["next_step", "updated_at"])
        _create_default_milestones(project)
        record_activity("Project created", f"{project.title} · just now", actor=request.user, project=project)
        _notify_project_staff(
            project,
            kind="project-assignment",
            title="A project was assigned to you",
            body=f"{project.title} is ready for project work.",
            created_by=request.user,
            notify_owners=True,
        )
        _notify_project_client(
            project,
            kind="project-created",
            title=(
                "Your project team is assigned"
                if project.assigned_staff.exists()
                else "Project setup is in progress"
            ),
            body=(
                f"{project.title} has been created. Your project team is assigned and will share the next step here."
                if project.assigned_staff.exists()
                else f"{project.title} has been created. Grand Coast is assigning an employee; project work has not started yet."
            ),
            created_by=request.user,
            estimate=project.estimate,
        )
        messages.success(request, f"{project.title} was created.")
        return _dashboard_redirect("projects", project=project.pk)
    messages.error(request, "Please correct the project details and try again.")
    return _render_dashboard_form_error(request, "projects", {"project_form": form}, new="project")


def _create_default_milestones(project, approved=False):
    completed_through = 2 if approved else 1
    for index, title in enumerate(["Walkthrough", "Estimate approved", "Selections", "Construction", "Final walkthrough"], start=1):
        Milestone.objects.create(
            project=project,
            title=title,
            sort_order=index,
            is_complete=index <= completed_through,
        )


@require_POST
@staff_required
def estimate_create_project(request, pk):
    estimate = get_object_or_404(Estimate.objects.select_related("lead", "client"), pk=pk)
    try:
        project, created = create_project_from_estimate(
            estimate,
            actor=request.user,
            idempotency_key=f"project-from-estimate:{estimate.pk}",
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
        return _dashboard_redirect("estimates", estimate=estimate.pk)
    if not created:
        return _dashboard_redirect("projects", project=project.pk)
    messages.success(request, f"{project.title} is now in Projects.")
    return _dashboard_redirect("projects", project=project.pk)


@require_POST
@staff_required
@transaction.atomic
def project_update(request, pk):
    project = get_object_or_404(Project, pk=pk)
    previous_staff_ids = set(project.assigned_staff.values_list("pk", flat=True))
    form_data = request.POST.copy()
    for field_name in ("location", "project_type", "start_date", "target_date"):
        if field_name not in form_data:
            value = getattr(project, field_name)
            form_data[field_name] = str(value or "")
    if "is_published" not in form_data:
        # An unchecked checkbox is intentionally absent from POST data. The
        # owner must be able to take a published project off the public site.
        form_data["is_published"] = ""
    if "assigned_staff" not in form_data:
        # An empty multi-select is intentionally absent from POST data too;
        # treat it as clearing assignments rather than preserving stale ones.
        form_data.setlist("assigned_staff", [])
    if "lead" not in form_data and project.lead_id:
        form_data["lead"] = str(project.lead_id)
    form = ProjectForm(form_data, instance=project, staff_queryset=_staff_users())
    if form.is_valid():
        project = form.save()
        record_activity("Project details updated", f"{project.title} · just now", actor=request.user, project=project)
        current_staff_ids = set(project.assigned_staff.values_list("pk", flat=True))
        staff_changed = previous_staff_ids != current_staff_ids
        if staff_changed and not current_staff_ids and project.status != Project.Status.COMPLETE:
            project.next_step = "Assign project staff"
            project.save(update_fields=["next_step", "updated_at"])
        elif staff_changed and current_staff_ids and project.next_step == "Assign project staff":
            project.next_step = "Confirm project schedule"
            project.save(update_fields=["next_step", "updated_at"])
        additional_staff = get_user_model().objects.filter(pk__in=previous_staff_ids | current_staff_ids)
        if staff_changed:
            _notify_project_staff(
                project,
                kind="project-assignment",
                title="Project staff assignment changed",
                body=f"{project.title} now has an updated staff assignment.",
                created_by=request.user,
                notify_owners=True,
                additional_recipients=additional_staff,
            )
            _notify_project_client(
                project,
                kind="project-staff-assigned" if current_staff_ids else "project-setup-required",
                title=("Your project team is assigned" if current_staff_ids else "Project setup is in progress"),
                body=(
                    f"Grand Coast has assigned an employee to {project.title}. The team will coordinate the next steps here."
                    if current_staff_ids
                    else f"{project.title} needs a new employee assignment. Work will not move into execution until an employee is assigned."
                ),
                created_by=request.user,
                estimate=project.estimate,
            )
        messages.success(request, "Project details saved.")
    else:
        messages.error(request, "Please correct the project fields and try again.")
        return _render_dashboard_form_error(
            request,
            "projects",
            {"project_form": form},
            selections={"project": project.pk},
        )
    return _dashboard_redirect("projects", project=project.pk)


@require_POST
@staff_required
def milestone_toggle(request, pk):
    milestone = get_object_or_404(Milestone.objects.select_related("project"), pk=pk)
    is_complete = "on" in request.POST.getlist("is_complete")
    idempotency_key = str(request.POST.get("idempotency_key") or "").strip() or None
    updated_milestone = set_milestone_status(
        milestone,
        actor=request.user,
        is_complete=is_complete,
        idempotency_key=idempotency_key,
    )
    if updated_milestone.is_complete == is_complete:
        messages.success(
            request,
            f"{milestone.title} marked complete." if is_complete else f"{milestone.title} reopened.",
        )
    else:
        messages.error(request, "The milestone could not be updated. Reload the project and try again.")
    return _dashboard_redirect("projects", project=milestone.project.pk)


@require_POST
@workspace_staff_required
def project_add_update(request, pk):
    team_mode = request.path.startswith("/team/")
    if not team_mode and not _can_access_dashboard(request.user):
        raise PermissionDenied
    project = get_object_or_404(_visible_projects_for_user(request.user), pk=pk)
    if team_mode and not project.assigned_staff.filter(pk=request.user.pk).exists():
        raise PermissionDenied
    form = ProjectUpdateForm(request.POST)
    if form.is_valid():
        update = form.save(commit=False)
        if team_mode:
            update.visibility = ProjectUpdate.Visibility.INTERNAL
        idempotency_key = str(request.POST.get("idempotency_key") or "").strip()
        event_key = f"project-update:{idempotency_key}"[:100] if idempotency_key else None
        with transaction.atomic():
            locked_project = Project.objects.select_for_update().get(pk=project.pk)
            if event_key:
                existing_event = WorkflowEvent.objects.filter(idempotency_key=event_key).first()
                if existing_event:
                    if existing_event.project_id != locked_project.pk:
                        raise PermissionDenied
                    messages.info(request, "That project update was already published.")
                    return _workspace_redirect(request, "projects", project=locked_project.pk)
            update.project = locked_project
            update.created_by = request.user
            update.save()
            project = locked_project
            if not team_mode:
                project.next_step = update.title
                project.save(update_fields=["next_step", "updated_at"])
            if event_key:
                WorkflowEvent.objects.create(
                    idempotency_key=event_key,
                    event_type="project_update_created",
                    source="team" if team_mode else "web",
                    actor=request.user,
                    project=project,
                    related_model="ProjectUpdate",
                    related_id=str(update.pk),
                    after_state={
                        "title": update.title,
                        "visibility": update.visibility,
                    },
                )
        record_activity(
            "Project update added",
            f"{project.title} · {update.get_visibility_display()}",
            actor=request.user,
            project=project,
        )
        if update.visibility == ProjectUpdate.Visibility.CLIENT:
            _notify_project_client(
                project,
                kind="update-published",
                title="A new project update is available",
                body=f"{update.title} was published in your client portal.",
                created_by=request.user,
                metadata={"update_id": str(update.pk)},
            )
        messages.success(request, "Project update published to the selected audience.")
    else:
        messages.error(request, "Please add a title and update message.")
        return _render_workspace_form_error(
            request,
            "projects",
            {"project_update_form": form},
            selections={"project": project.pk},
        )
    return _workspace_redirect(request, "projects", project=project.pk)


@require_POST
@staff_required
@transaction.atomic
def project_update_draft(request, pk):
    update = get_object_or_404(
        ProjectUpdate.objects.select_for_update().select_related("project"),
        pk=pk,
    )
    project = update.project
    if update.visibility != ProjectUpdate.Visibility.INTERNAL:
        raise Http404

    draft = ProjectUpdateDraft.objects.select_for_update().filter(source_update=update).first()
    action = request.POST.get("draft_action", "")

    if draft and draft.status == ProjectUpdateDraft.Status.SENT:
        messages.info(request, "This client update has already been sent.")
        return _dashboard_redirect("projects", project=project.pk)

    if action == "delete":
        if draft:
            draft.delete()
            record_activity(
                "Client update draft deleted",
                f"{project.title} · {update.title}",
                actor=request.user,
                project=project,
            )
            messages.success(request, "Client draft deleted. The original internal update is unchanged.")
        else:
            messages.info(request, "There is no saved client draft to delete.")
        return _dashboard_redirect("projects", project=project.pk)

    if not project.client_id:
        messages.error(request, "Add a client to this project before saving or sending a client draft.")
        return _dashboard_redirect("projects", project=project.pk)
    if action not in {"keep", "send"}:
        messages.error(request, "Choose whether to keep the draft or send it to the client.")
        return _dashboard_redirect("projects", project=project.pk)

    existing_draft = draft is not None
    form = ProjectUpdateDraftForm(request.POST, instance=draft, prefix="project-update-draft")
    if not form.is_valid():
        for field_name in form.errors:
            form.fields[field_name].widget.attrs.update(
                {
                    "aria-invalid": "true",
                    "aria-describedby": f"project-update-draft-{field_name}-error",
                }
            )
        return _render_dashboard_form_error(
            request,
            "projects",
            {
                "project_update_draft_form": form,
                "project_update_draft_source": update,
                "project_update_draft_auto_open": True,
            },
            selections={"project": project.pk},
        )

    draft = form.save(commit=False)
    draft.source_update = update
    if not existing_draft:
        draft.created_by = request.user

    if action == "keep":
        draft.save()
        record_activity(
            "Client update draft saved",
            f"{project.title} · {draft.title}",
            actor=request.user,
            project=project,
        )
        messages.success(request, "Client draft saved. It is still private.")
    else:
        published_update = ProjectUpdate.objects.create(
            project=project,
            title=draft.title,
            body=draft.body,
            visibility=ProjectUpdate.Visibility.CLIENT,
            created_by=request.user,
        )
        draft.status = ProjectUpdateDraft.Status.SENT
        draft.published_update = published_update
        draft.sent_by = request.user
        draft.sent_at = timezone.now()
        draft.save()
        record_activity(
            "Project update sent to client",
            f"{project.title} · {draft.title}",
            actor=request.user,
            project=project,
        )
        _notify_project_client(
            project,
            kind="update-published",
            title="A new project update is available",
            body=f"{draft.title} was published in your client portal.",
            created_by=request.user,
            metadata={"update_id": str(published_update.pk)},
        )
        messages.success(request, "Update sent to the client.")

    return _dashboard_redirect("projects", project=project.pk)


@require_POST
@staff_required
def project_create_invite(request, pk):
    project = get_object_or_404(Project.objects.select_related("client"), pk=pk)
    if not project.client:
        messages.error(request, "This project needs a client contact before an invite can be created.")
        return _dashboard_redirect("projects", project=project.pk)
    invite, raw_token = create_client_invite(project.client, actor=request.user)
    _prepare_client_invite_delivery(
        request,
        client=project.client,
        invite=invite,
        raw_token=raw_token,
        actor=request.user,
    )
    record_activity("Client portal invite created", project.client.name, actor=request.user, project=project)
    return _dashboard_redirect("projects", project=project.pk)


def _media_library_context(request, *, form_overrides=None):
    form_overrides = form_overrides or {}
    destination = request.GET.get("destination", "client")
    if destination not in MediaPublication.Destination.values:
        destination = "client"
    project_filter = request.GET.get("media_project", "all")
    state = request.GET.get("state", "all")
    assets = MediaAsset.objects.select_related("project__client").prefetch_related("publications")
    if project_filter != "all":
        try:
            assets = assets.filter(project_id=uuid.UUID(project_filter))
        except (ValueError, TypeError):
            project_filter = "all"
    published = MediaPublication.objects.filter(destination=destination, published_at__isnull=False)
    if destination == "client":
        published = published.filter(shared_client__isnull=False, shared_client=F("asset__project__client"))
    else:
        published = published.filter(asset__project__is_published=True)
    published_ids = published.values("asset_id")
    if state == "published":
        assets = assets.filter(pk__in=published_ids)
    elif state == "draft":
        assets = assets.exclude(pk__in=published_ids)
    else:
        state = "all"
    page = Paginator(assets, 12).get_page(request.GET.get("page"))
    rows = []
    for asset in page:
        publication = publication_for(asset, destination) or MediaPublication(
            asset=asset, destination=destination,
            title=asset.title if destination == "client" else "",
            caption=asset.caption if destination == "client" else "",
        )
        form = MediaPublicationForm(instance=publication, prefix=f"media-{asset.pk}")
        if str(asset.pk) == str(form_overrides.get("media_error_id")):
            form = form_overrides["media_publication_form"]
        website_publish_blocker = ""
        if destination == "website":
            if not asset.project_id:
                website_publish_blocker = "Assign this file to a project before publishing it to the website."
            elif not asset.project.is_published:
                website_publish_blocker = "Publish the project page in Projects before publishing its media."
            elif not asset.project.public_title.strip() and not publication.published_at:
                website_publish_blocker = (
                    "Add a public project title in Projects before publishing new media. The title on this form only names the photo or video."
                )
        rows.append({"asset": asset, "publication": publication, "form": form,
                     "is_live": client_is_live(asset) if destination == "client" else website_is_live(asset),
                     "website_publish_blocker": website_publish_blocker,
                     "display_url": reverse("operations:media-file", kwargs={"pk": asset.pk})})
    return {
        "media_rows": rows, "media_page": page, "media_destination": destination,
        "media_state": state, "media_project": project_filter,
        "media_upload_form": form_overrides.get("media_upload_form") or MediaUploadForm(
            project_queryset=Project.objects.all(),
            initial={"destinations": [destination], "project": project_filter if project_filter != "all" else None},
        ),
    }


def _media_error(request, form, asset=None):
    overrides = {"media_publication_form": form, "media_error_id": asset.pk} if asset else {"media_upload_form": form}
    return _render_dashboard_form_error(
        request, "media", overrides,
        selections={"destination": request.POST.get("destination", "client"), "media_project": asset.project_id if asset else request.POST.get("project")},
    )


@require_POST
@workspace_staff_required
@transaction.atomic
def media_upload(request):
    team_mode = request.path.startswith("/team/")
    if not team_mode and not _can_access_dashboard(request.user):
        raise PermissionDenied
    project_queryset = _visible_projects_for_user(request.user)
    form = MediaUploadForm(request.POST, request.FILES, project_queryset=project_queryset)
    if form.is_valid():
        project = form.cleaned_data["project"]
        created = []
        for upload in form.cleaned_data["files"]:
            media_type = MediaAsset.MediaType.VIDEO if upload.name.lower().endswith((".mp4", ".mov", ".webm")) else MediaAsset.MediaType.PHOTO
            asset = MediaAsset.objects.create(
                project=project,
                title=upload.name.rsplit(".", 1)[0],
                file=upload,
                media_type=media_type,
                visibility=MediaAsset.Visibility.INTERNAL,
                uploaded_by=request.user,
            )
            created.append(asset)
            if not team_mode:
                for destination in form.cleaned_data["destinations"]:
                    MediaPublication.objects.create(
                        asset=asset, destination=destination,
                        title=asset.title if destination == "client" else "",
                    )
        record_activity("Project media uploaded", f"{len(created)} file(s) · {project.title}", actor=request.user, project=project)
        messages.success(request, f"{len(created)} file(s) uploaded as drafts. Review each file before sharing or publishing.")
        return _workspace_redirect(request, "media", media_project=project.pk, destination=request.POST.get("destination", "client"))
    messages.error(request, "Please choose a project and valid media files.")
    if not team_mode:
        return _media_error(request, form)
    return _render_workspace_form_error(
        request,
        "media",
        {"media_upload_form": form},
        selections={"media_project": request.POST.get("project")},
    )


@require_POST
@workspace_staff_required
@transaction.atomic
def media_edit(request, pk):
    team_mode = request.path.startswith("/team/")
    if not team_mode and not _can_access_dashboard(request.user):
        raise PermissionDenied
    asset = get_object_or_404(MediaAsset.objects.select_for_update().select_related("project__client"), pk=pk)
    if not team_mode:
        return _edit_media_publication(request, asset)
    project_queryset = _visible_projects_for_user(request.user)
    if team_mode and (
        not asset.project_id
        or not project_queryset.filter(pk=asset.project_id).exists()
    ):
        raise PermissionDenied
    form = MediaEditForm(request.POST, instance=asset, project_queryset=project_queryset)
    if form.is_valid():
        asset.caption = form.cleaned_data["caption"]
        asset.save(update_fields=["caption", "updated_at"])
        record_activity("Media details updated", f"{asset.title} · {asset.get_visibility_display()}", actor=request.user, project=asset.project)
        messages.success(request, "Media details saved.")
    else:
        messages.error(request, "Please correct the media details and try again.")
        return _render_workspace_form_error(
            request,
            "media",
            {"media_edit_form": form},
            selections={"media_project": asset.project_id},
        )
    return _workspace_redirect(request, "media", media_project=asset.project_id)


def _edit_media_publication(request, asset):
    destination = request.POST.get("destination")
    if destination not in MediaPublication.Destination.values:
        raise Http404
    publication = asset.publications.filter(destination=destination).first() or MediaPublication(asset=asset, destination=destination)
    action = request.POST.get("action", "save")
    if action == "unpublish":
        unpublish(publication)
        record_activity("Media unpublished", destination, actor=request.user, project=asset.project)
        messages.success(request, "Removed from this destination. The original file and draft are retained.")
    elif action in {"save", "publish", "preview"}:
        form = MediaPublicationForm(request.POST, instance=publication, prefix=f"media-{asset.pk}")
        if not form.is_valid():
            return _media_error(request, form, asset)
        publication = form.save(commit=False)
        if action == "publish":
            try:
                changed = publish(publication)
            except ValidationError as exc:
                if hasattr(exc, "error_dict"):
                    for name, errors in exc.error_dict.items():
                        form.add_error(name if name in form.fields else None, errors)
                else:
                    form.add_error(None, exc)
                return _media_error(request, form, asset)
            if changed:
                record_activity("Media shared with client" if destination == "client" else "Media published to website", publication.title, actor=request.user, project=asset.project)
                if destination == "client":
                    _notify_project_client(asset.project, kind="media-published", title="New project media is available",
                        body=f"{publication.title} was shared in your client portal.", created_by=request.user,
                        metadata={"media_id": str(asset.pk)})
            messages.success(request, "Shared with the client." if destination == "client" else "Published to the website.")
        else:
            publication.save()
            if action == "preview":
                return redirect(f"{reverse('operations:media-preview', kwargs={'pk': asset.pk})}?{urlencode({'destination': destination})}")
            messages.success(request, "Draft saved. Published content has not changed.")
    else:
        raise Http404
    return _dashboard_redirect("media", destination=destination, media_project=asset.project_id)


@require_GET
@staff_required
@never_cache
def media_preview(request, pk):
    asset = get_object_or_404(MediaAsset.objects.select_related("project__client"), pk=pk)
    destination = request.GET.get("destination", "website")
    publication = get_object_or_404(MediaPublication, asset=asset, destination=destination)
    return render(request, "operations/media_preview.html", {
        "media": media_presentation(asset, destination, preview=True, publication=publication),
        "asset": asset, "destination": destination,
    })


@require_http_methods(["GET", "POST"])
@staff_required
@transaction.atomic
def media_delete(request, pk):
    asset = get_object_or_404(MediaAsset.objects.select_for_update(), pk=pk)
    destination = request.GET.get("destination", "client")
    if destination not in MediaPublication.Destination.values:
        destination = "client"
    if request.method == "POST" and request.POST.get("confirm") == "delete":
        project_id = asset.project_id
        name, storage = asset.file.name, asset.file.storage
        record_activity("Original media deleted", asset.title, actor=request.user, project=asset.project)
        asset.delete()
        if name:
            transaction.on_commit(lambda: storage.delete(name))
        messages.success(request, "Original file and all its publications deleted.")
        return _dashboard_redirect("media", media_project=project_id, destination=destination)
    return render(request, "operations/media_delete.html", {"asset": asset, "destination": destination})


@require_POST
@staff_required
@transaction.atomic
def service_save(request, pk=None):
    instance = get_object_or_404(Service.objects.select_for_update(), pk=pk) if pk else Service()
    form = ServiceForm(request.POST, request.FILES, instance=instance, prefix=f"service-{pk}" if pk else "new-service")
    if form.is_valid():
        service = form.save(commit=False)
        if not service.slug:
            service.slug = f"{slugify(service.title)[:65] or 'service'}-{uuid.uuid4().hex[:8]}"
        service.save()
        record_activity("Service saved", service.title, actor=request.user)
        messages.success(request, "Service saved.")
        return _dashboard_redirect("content")
    overrides = {"service_error_id": pk, "service_edit_form": form} if pk else {"service_create_form": form}
    return _render_dashboard_form_error(request, "content", overrides)


@require_POST
@staff_required
@transaction.atomic
def service_move(request, pk):
    services = list(Service.objects.select_for_update().all())
    index = next((i for i, item in enumerate(services) if item.pk == pk), None)
    if index is None:
        raise Http404
    direction = request.POST.get("direction")
    if direction not in {"up", "down"}:
        raise Http404
    neighbor = index + (-1 if direction == "up" else 1)
    if 0 <= neighbor < len(services):
        services[index], services[neighbor] = services[neighbor], services[index]
        for position, item in enumerate(services):
            item.sort_order = position
        Service.objects.bulk_update(services, ["sort_order"])
    return _dashboard_redirect("content")


@require_GET
def service_image(request, pk):
    """Serve a service image through its configured storage backend.

    Local development uses MEDIA_ROOT; production uses Supabase-backed
    Django storage. Opening the FieldFile here keeps both environments on the
    same path instead of depending on a directly served MEDIA_URL.
    """
    service = get_object_or_404(Service, pk=pk)
    if not service.is_active and not _can_manage_content(request.user):
        raise Http404
    if not service.image:
        raise Http404
    try:
        response = FileResponse(service.image.open("rb"), content_type=mimetypes.guess_type(service.image.name)[0] or "application/octet-stream")
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@staff_required
def content_update(request):
    if not _can_manage_content(request.user):
        raise PermissionDenied
    instance = _site_settings()
    services = list(Service.objects.filter(is_active=True))
    process_steps = list(ProcessStep.objects.all())
    form = _content_form(instance, services, process_steps, data=request.POST)
    if form.is_valid():
        instance.headline = form.cleaned_data["headline"]
        instance.subheadline = form.cleaned_data["subheadline"]
        instance.featured_title = form.cleaned_data["featured_title"]
        instance.featured_body = form.cleaned_data["featured_body"]
        instance.google_review_url = form.cleaned_data["google_review_url"]
        instance.services_title = form.cleaned_data["services_title"]
        instance.services_intro = form.cleaned_data["services_intro"]
        instance.services_note = form.cleaned_data["services_note"]
        instance.save()
        for step in process_steps:
            step.title = form.cleaned_data[f"step_{step.key}"]
            step.save(update_fields=["title"])
        record_activity("Public content updated", "Content studio", actor=request.user)
        messages.success(request, "Public website content updated.")
    else:
        messages.error(request, "Please correct the content fields and try again.")
        return _render_dashboard_form_error(request, "content", {"content_form": form})
    return _dashboard_redirect("content")



@require_POST
@staff_required
def google_reviews_sync(request):
    if not _can_manage_content(request.user):
        raise PermissionDenied
    try:
        result = sync_google_reviews()
    except (GoogleReviewsConfigurationError, GoogleReviewsFetchError) as exc:
        messages.error(request, str(exc))
    else:
        place_name = result["display_name"] or "the configured Google listing"
        messages.success(
            request,
            f"Synced {result['review_count_cached']} reviews for {place_name}.",
        )
    return _dashboard_redirect("content")

@require_GET
def public_project_detail(request, pk):
    project = get_object_or_404(
        Project.objects.select_related("client").prefetch_related("milestones", "updates", "media_assets"),
        pk=pk,
        is_published=True,
    )
    public_media = sorted(
        [media_presentation(asset, "website") for asset in project.media_assets.prefetch_related("publications") if website_is_live(asset)],
        key=lambda item: item["sort_order"],
    )
    project.display_image = _project_image(project)
    return render(request, "operations/project_detail.html", {
        "project": project,
        "public_media": public_media,
        "site_settings": _site_settings(),
        "public_dashboard_url": _public_dashboard_url(request.user),
        "page": "projects",
    })


@require_GET
def project_cover_file(request, pk):
    project = get_object_or_404(Project.objects.select_related("client"), pk=pk)
    allowed = bool(project.is_published)
    if request.user.is_authenticated and request.user.is_staff:
        allowed = bool(
            _can_access_dashboard(request.user)
            or (
                _can_access_team(request.user)
                and project.assigned_staff.filter(pk=request.user.pk).exists()
            )
        )
    elif (
        _is_active_client(request.user)
        and project.client_id
        and project.client
        and project.client.user_id == request.user.id
    ):
        allowed = True
    if not allowed:
        if not request.user.is_authenticated:
            return redirect(f"{reverse('operations:login')}?{urlencode({'next': request.path})}")
        raise PermissionDenied
    if not project.cover:
        raise Http404
    try:
        opened = project.cover.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(
        opened,
        content_type=mimetypes.guess_type(project.cover.name)[0] or "application/octet-stream",
    )
    suffix = project.cover.name.rsplit(".", 1)[-1]
    response["Content-Disposition"] = f'inline; filename="project-{project.pk}.{suffix}"'
    response["Cache-Control"] = "private, no-store"
    return response


def _portal_client(request):
    return getattr(request.user, "client_record", None)


@never_cache
@require_GET
@login_required
def external_estimate_link(request, pk):
    """Permission-checked redirect to a manually published estimate."""
    estimate = get_object_or_404(Estimate.objects.select_related("client"), pk=pk)
    is_linked_client = bool(
        _is_active_client(request.user)
        and estimate.client_id
        and estimate.client.user_id == request.user.pk
        and estimate.external_client_visible
    )
    if not is_linked_client:
        raise Http404
    if not estimate.external_url:
        raise Http404
    # Do not copy the URL into logs or an intermediate page.
    return redirect(estimate.external_url)


@never_cache
@require_GET
@login_required
def external_invoice_link(request, pk):
    """Permission-checked redirect to a manually published Invoice."""
    schedule = get_object_or_404(
        PaymentSchedule.objects.select_related("project", "project__client"),
        pk=pk,
    )
    is_linked_client = bool(
        _is_active_client(request.user)
        and schedule.project.client_id
        and schedule.project.client.user_id == request.user.pk
        and schedule.external_client_visible
    )
    if not is_linked_client:
        raise Http404
    if not schedule.external_invoice_url:
        raise Http404
    return redirect(schedule.external_invoice_url)


_PORTAL_SECTION_LABELS = (
    ("overview", "Project overview"),
    ("updates", "Updates"),
    ("photos", "Photos & videos"),
    ("messages", "Messages"),
    ("estimate-files", "Estimate & files"),
    ("notifications", "Notifications"),
    ("profile", "Profile"),
)
_PORTAL_SECTION_KEYS = {key for key, _label in _PORTAL_SECTION_LABELS}


def _portal_navigation(request, client, project=None, estimate=None):
    base_params = {"client": str(client.pk)}
    if project is not None:
        base_params["project"] = str(project.pk)
    navigation = []
    for key, label in _PORTAL_SECTION_LABELS:
        params = dict(base_params)
        if key == "estimate-files" and estimate is not None:
            params["estimate"] = str(estimate.pk)
        destination = reverse("operations:portal-section", kwargs={"section": key})
        navigation.append({
            "key": key,
            "label": label,
            "url": f"{destination}?{urlencode(params)}",
        })
    return navigation


def _unread_client_team_messages(client):
    return (
        ClientMessage.objects.filter(client=client, is_read=False)
        .filter(
            Q(sent_by__is_staff=True)
            | Q(client_notifications__kind="employee-reply", client_notifications__client=client)
        )
        .distinct()
    )


def _client_navigation_counts(client):
    if client is None:
        return {"estimates": 0, "messages": 0, "notifications": 0}
    unread_team_messages_count = _unread_client_team_messages(client).count()
    unread_non_message_notifications_count = (
        ClientNotification.objects.filter(
            client=client,
            read_at__isnull=True,
            dismissed_at__isnull=True,
        )
        .exclude(
            Q(message__sent_by__is_staff=True)
            | Q(kind="employee-reply", message__isnull=False)
        )
        .count()
    )
    return {
        "estimates": Estimate.objects.filter(
            client=client,
            external_url__gt="",
            external_client_visible=True,
            external_status__in=[
                Estimate.ExternalEstimateStatus.SENT,
                Estimate.ExternalEstimateStatus.PENDING,
            ],
        ).exclude(status=Estimate.Status.DRAFT).count(),
        "messages": unread_team_messages_count,
        "notifications": unread_team_messages_count + unread_non_message_notifications_count,
    }


def _safe_notification_destination(destination_url, *, workspace):
    """Only show notification links that stay inside the viewer's workspace."""
    allowed_prefix = {
        "admin": "/dashboard/",
        "employee": "/team/",
        "client": "/portal/",
    }.get(workspace)
    try:
        parsed = urlsplit(str(destination_url or ""))
    except ValueError:
        return ""
    if (
        not allowed_prefix
        or parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith(allowed_prefix)
    ):
        return ""
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


@client_required
def portal(request, section="overview"):
    section = (section or "overview").lower()
    if section not in _PORTAL_SECTION_KEYS:
        raise Http404
    section_title = dict(_PORTAL_SECTION_LABELS)[section]
    client = _portal_client(request)
    if client is None:
        return render(request, "operations/portal.html", {
            "portal_client": None,
            "portal_projects": [],
            "portal_estimates": [],
            "portal_messages": [],
            "client_notifications": [],
            "client_estimate_review_count": 0,
            "client_unread_team_messages_count": 0,
            "client_unread_notifications_count": 0,
            "portal_nav": [],
            "portal_section": section,
            "portal_section_title": section_title,
        })
    if section == "messages" and not request.user.is_staff:
        unread_team_message_ids = list(
            _unread_client_team_messages(client).values_list("pk", flat=True)
        )
        if unread_team_message_ids:
            now = timezone.now()
            with transaction.atomic():
                ClientMessage.objects.filter(
                    client=client,
                    pk__in=unread_team_message_ids,
                ).update(is_read=True)
                ClientNotification.objects.filter(
                    client=client,
                    message_id__in=unread_team_message_ids,
                    read_at__isnull=True,
                ).update(read_at=now, updated_at=now)
    projects = list(
        Project.objects.filter(client=client)
        .select_related("estimate", "agreement")
        .prefetch_related(
            "milestones",
            "updates",
            "media_assets",
            "documents",
            "messages",
            "selections",
            "change_orders",
            "payment_schedules",
        )
    )
    portal_estimate_queryset = (
        Estimate.objects.filter(client=client)
        .exclude(status=Estimate.Status.DRAFT)
        .filter(Q(external_url="") | Q(external_client_visible=True))
        .select_related("lead", "external_status_by")
        .prefetch_related("projects")
    )
    portal_estimates = list(portal_estimate_queryset)
    client_navigation_counts = _client_navigation_counts(client)
    client_estimate_review_count = client_navigation_counts["estimates"]
    for estimate in portal_estimates:
        estimate.portal_project = estimate.projects.order_by("-created_at").first()
    requested_estimate = next(
        (estimate for estimate in portal_estimates if str(estimate.pk) == request.GET.get("estimate")),
        None,
    ) if request.GET.get("estimate") else None
    selected_project = None
    if request.GET.get("project"):
        selected_project = next((project for project in projects if str(project.pk) == request.GET["project"]), None)
    elif requested_estimate and requested_estimate.portal_project:
        selected_project = next(
            (project for project in projects if project.pk == requested_estimate.portal_project.pk),
            None,
        )
    if selected_project is None and not requested_estimate and projects:
        selected_project = projects[0]
    updates = list(selected_project.updates.filter(visibility=ProjectUpdate.Visibility.CLIENT)[:8]) if selected_project else []
    media = sorted(
        [media_presentation(asset, "client") for asset in selected_project.media_assets.prefetch_related("publications") if client_is_live(asset)],
        key=lambda item: item["sort_order"],
    ) if selected_project else []
    if selected_project:
        selected_project.progress = selected_project.progress_percent
        selected_project.display_image = _project_image(selected_project)
        selected_project.current_milestone_id = next(
            (
                milestone.pk
                for milestone in selected_project.milestones.all()
                if not milestone.is_complete
            ),
            None,
        )
    portal_estimate = requested_estimate
    if portal_estimate is None and selected_project and selected_project.estimate:
        portal_estimate = next(
            (estimate for estimate in portal_estimates if estimate.pk == selected_project.estimate_id),
            None,
        )
    if portal_estimate is None and not selected_project and portal_estimates:
        portal_estimate = portal_estimates[0]
    portal_documents = list(selected_project.documents.filter(visibility=ProjectDocument.Visibility.CLIENT)[:12]) if selected_project else []
    portal_agreement = (
        Agreement.objects.select_related("estimate").filter(project=selected_project).first()
        if selected_project
        else None
    )
    portal_selections = list(selected_project.selections.all()) if selected_project else []
    portal_change_orders = list(
        selected_project.change_orders.exclude(status=ChangeOrder.Status.DRAFT)
    ) if selected_project else []
    portal_payment_schedules = list(
        selected_project.payment_schedules.prefetch_related("payments").all()
    ) if selected_project else []
    external_estimate_portal_enabled = bool(
        external_estimate_enabled_for(request.user, project=selected_project)
        or (
            portal_estimate is not None
            and external_estimate_enabled_for(request.user, estimate=portal_estimate)
        )
    )
    # Conversations are client-wide, with an optional project relationship.
    # This keeps general questions and pre-project estimate discussions visible
    # to the same client without duplicating messages in the portal.
    portal_messages = list(
        client.messages.select_related(
            "project",
            "sent_by",
            "sent_by__employee_profile",
        ).order_by("-created_at")[:30]
    )
    portal_messages.sort(key=lambda message: message.created_at)
    visible_client_notifications = client.notifications.filter(dismissed_at__isnull=True).select_related(
        "project",
        "message",
        "message__project",
        "message__sent_by",
    )
    client_notifications = list(visible_client_notifications[:30])
    for notification in client_notifications:
        if notification.message_id or notification.kind == "employee-reply":
            notification.destination_url = _client_message_notification_url(client, notification)
        notification.requires_messages_tab = _client_notification_requires_message_open(notification)
        notification.destination_url = _safe_notification_destination(
            notification.destination_url,
            workspace="client",
        )
    client_unread_notifications_count = client_navigation_counts["notifications"]
    return render(request, "operations/portal.html", {
        "portal_client": client,
        "portal_projects": projects,
        "portal_estimates": portal_estimates,
        "portal_project": selected_project,
        "portal_estimate": portal_estimate,
        "external_estimate_portal_enabled": external_estimate_portal_enabled,
        "portal_updates": updates,
        "portal_latest_update": updates[0] if updates else None,
        "portal_media": media,
        "portal_documents": portal_documents,
        "portal_agreement": portal_agreement,
        "portal_selections": portal_selections,
        "portal_change_orders": portal_change_orders,
        "portal_payment_schedules": portal_payment_schedules,
        "portal_messages": portal_messages,
        "client_estimate_review_count": client_estimate_review_count,
        "client_unread_team_messages_count": client_navigation_counts["messages"],
        "client_notifications": client_notifications,
        "client_unread_notifications_count": client_unread_notifications_count,
        "google_review_url": _site_settings().google_review_url,
        "portal_message_form": ClientMessageForm(),
        "portal_nav": _portal_navigation(request, client, selected_project, portal_estimate),
        "portal_section": section,
        "portal_section_title": section_title,
    })


@never_cache
@require_GET
@client_required
def client_mobile_navigation_counts(request):
    """Return only the signed-in client's counts used by the native drawer."""
    return JsonResponse(_client_navigation_counts(_portal_client(request)))


def _client_can_access_estimate(request, estimate):
    client = _portal_client(request)
    return bool(client and estimate.client_id == client.id)


@require_POST
@login_required
def portal_accept_estimate(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    estimate = get_object_or_404(
        Estimate.objects.select_related("client", "lead", "lead__assigned_to"),
        pk=pk,
    )
    if not _client_can_access_estimate(request, estimate):
        raise PermissionDenied
    messages.info(
        request,
        "Review and accept the estimate in the linked estimate service. Grand Coast will record the outcome here after confirming it.",
    )
    return redirect(f"{reverse('operations:portal')}?estimate={estimate.pk}")


@require_POST
@login_required
def portal_accept_agreement(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    agreement = get_object_or_404(
        Agreement.objects.select_related("project", "project__client"),
        pk=pk,
    )
    if (
        not agreement.project.client_id
        or agreement.project.client.user_id != request.user.id
    ):
        raise PermissionDenied
    accepted = False
    if agreement.status == Agreement.Status.ISSUED:
        agreement, accepted = accept_agreement_command(
            agreement,
            actor=request.user,
            request=request,
            idempotency_key=f"agreement-accept:{agreement.pk}",
        )
    if accepted:
        project = agreement.project
        recipients = _owner_notification_recipients()
        if project.project_manager_id:
            recipients.append(project.project_manager)
        queue_employee_notifications(
            recipients,
            kind="agreement-accepted",
            title=f"Agreement accepted for {project.title}",
            body="The client accepted the agreement. Confirm the deposit and readiness checklist.",
            destination_url=reverse(
                "operations:project-operations",
                kwargs={"pk": project.pk},
            ),
            metadata={"agreement_id": str(agreement.pk)},
            created_by=request.user,
            project=project,
        )
        queue_client_notifications(
            [project.client],
            kind="agreement-accepted",
            title="Agreement accepted",
            body="Your agreement was recorded. Grand Coast will confirm the next project step.",
            destination_url=_portal_notification_url(project.client, project=project),
            metadata={"agreement_id": str(agreement.pk)},
            created_by=request.user,
            exclude_clients=[project.client],
            project=project,
        )
        messages.success(request, "Agreement accepted. Grand Coast will confirm the next step.")
    else:
        messages.info(request, "This agreement is not currently awaiting acceptance.")
    return redirect(
        f"{reverse('operations:portal')}?project={agreement.project_id}#overview"
    )


@require_POST
@login_required
def portal_selection_advance(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    selection = get_object_or_404(
        Selection.objects.select_related("project", "project__client"),
        pk=pk,
    )
    if (
        not selection.project.client_id
        or selection.project.client.user_id != request.user.id
    ):
        raise PermissionDenied
    if selection.status == Selection.Status.PENDING:
        advance_selection_command(
            selection,
            actor=request.user,
            status=Selection.Status.SUBMITTED,
            client_choice=request.POST.get("client_choice", ""),
            idempotency_key=f"selection-submit:{selection.pk}",
        )
        messages.success(request, "Your selection was submitted to Grand Coast.")
    else:
        messages.info(request, "This selection is no longer awaiting your choice.")
    return redirect(
        f"{reverse('operations:portal')}?project={selection.project_id}#overview"
    )


@require_POST
@login_required
def portal_approve_change_order(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    change_order = get_object_or_404(
        ChangeOrder.objects.select_related("project", "project__client"),
        pk=pk,
    )
    if (
        not change_order.project.client_id
        or change_order.project.client.user_id != request.user.id
    ):
        raise PermissionDenied
    approved = False
    if change_order.status == ChangeOrder.Status.SENT:
        change_order, approved = approve_change_order_command(
            change_order,
            actor=request.user,
            request=request,
            idempotency_key=f"change-order-approve:{change_order.pk}",
        )
    if approved:
        project = change_order.project
        recipients = _owner_notification_recipients()
        if project.project_manager_id:
            recipients.append(project.project_manager)
        queue_employee_notifications(
            recipients,
            kind="change-order-approved",
            title=f"CO-{change_order.number} approved",
            body=f"{project.title} contract value was updated by client approval.",
            destination_url=reverse(
                "operations:project-operations",
                kwargs={"pk": project.pk},
            ),
            metadata={"change_order_id": str(change_order.pk)},
            created_by=request.user,
            project=project,
        )
        messages.success(request, "Change order approved. The project balance was updated.")
    else:
        messages.info(request, "This change order is not currently awaiting approval.")
    return redirect(
        f"{reverse('operations:portal')}?project={change_order.project_id}#overview"
    )


@require_POST
@login_required
def portal_decline_estimate(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    estimate = get_object_or_404(
        Estimate.objects.select_related("client", "lead", "lead__assigned_to"),
        pk=pk,
    )
    if not _client_can_access_estimate(request, estimate):
        raise PermissionDenied
    messages.info(
        request,
        "Record your response in the linked estimate service. Grand Coast will update this status after confirming it.",
    )
    return redirect(f"{reverse('operations:portal')}?estimate={estimate.pk}")

@require_POST
@login_required
@transaction.atomic
def portal_send_message(request):
    if not _is_active_client(request.user):
        raise PermissionDenied
    client = _portal_client(request)
    if client is None or client.user_id != request.user.id:
        raise PermissionDenied
    project = None
    if request.POST.get("project"):
        project = get_object_or_404(Project, pk=request.POST["project"], client=client)
    form = ClientMessageForm(request.POST)
    if form.is_valid():
        client_message = form.save(commit=False)
        client_message.client = client
        client_message.project = project
        client_message.sent_by = request.user
        client_message.save()
        record_activity("New client message", client.name, actor=request.user, project=project)
        _notify_client_message_recipients(client, project, client_message, actor=request.user)
        messages.success(request, "Your message was added to the project conversation.")
    else:
        messages.error(request, "Please add a message before sending.")
    portal_url = reverse("operations:portal")
    query = {}
    if project:
        query["project"] = project.pk
    if request.POST.get("estimate"):
        query["estimate"] = request.POST["estimate"]
    return redirect(f"{portal_url}?{urlencode(query)}" if query else portal_url)


@require_POST
@login_required
def client_notification_mark_read(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    notification = get_object_or_404(
        ClientNotification.objects.filter(dismissed_at__isnull=True).select_related(
            "client",
            "project",
            "message",
            "message__project",
            "message__sent_by",
        ),
        pk=pk,
        client__user=request.user,
    )
    if _client_notification_requires_message_open(notification):
        return redirect(_client_message_notification_url(notification.client, notification))
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return redirect(reverse("operations:portal-section", kwargs={"section": "notifications"}))


@require_POST
@login_required
def client_notification_clear(request, pk):
    if not _is_active_client(request.user):
        raise PermissionDenied
    now = timezone.now()
    with transaction.atomic():
        notification = get_object_or_404(
            ClientNotification.objects.select_for_update(),
            pk=pk,
            client__user=request.user,
        )
        update_fields = ["dismissed_at", "updated_at"]
        notification.dismissed_at = notification.dismissed_at or now
        if notification.read_at is None:
            notification.read_at = now
            update_fields.append("read_at")
        notification.save(update_fields=update_fields)
    return redirect(reverse("operations:portal-section", kwargs={"section": "notifications"}))


@require_POST
@login_required
def client_notifications_mark_all_read(request):
    if not _is_active_client(request.user):
        raise PermissionDenied
    ClientNotification.objects.filter(
        client__user=request.user,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).exclude(
        Q(message__sent_by__is_staff=True, message__is_read=False)
        | Q(kind="employee-reply", message__isnull=False, message__is_read=False)
    ).update(read_at=timezone.now(), updated_at=timezone.now())
    return redirect(reverse("operations:portal-section", kwargs={"section": "notifications"}))

def client_invite(request, token):
    if request.user.is_authenticated:
        raise PermissionDenied
    invite = find_invite(token)
    if invite is None:
        return render(request, "operations/invite_invalid.html", status=410)
    form = ClientInviteAcceptForm(request.POST or None, initial={
        "email": invite.client.email,
        "first_name": invite.client.name.split(" ")[0],
        "last_name": " ".join(invite.client.name.split(" ")[1:]),
    })
    if request.method == "POST" and form.is_valid():
        try:
            user = complete_client_invite(invite, form)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            login(request, user)
            messages.success(request, "Your client portal account is ready.")
            return redirect("operations:portal")
    return render(request, "operations/invite_accept.html", {"form": form, "invite": invite})


@require_GET
def media_file(request, pk):
    asset = get_object_or_404(MediaAsset.objects.select_related("project__client"), pk=pk)
    allowed = can_view_media(request.user, asset)
    if not allowed:
        if not request.user.is_authenticated:
            return redirect(f"{reverse('operations:login')}?{urlencode({'next': request.path})}")
        raise PermissionDenied
    if asset.file:
        try:
            opened = asset.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404 from exc
        content_type = mimetypes.guess_type(asset.file.name)[0] or "application/octet-stream"
        response = FileResponse(opened, content_type=content_type)
        # Original filenames can contain internal client/address details.
        suffix = asset.file.name.rsplit(".", 1)[-1]
        response["Content-Disposition"] = f'inline; filename="media-{asset.pk}.{suffix}"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        return response
    if asset.fallback_image:
        return redirect(_asset(asset.fallback_image))
    raise Http404


@require_GET
@staff_required
def lead_attachment_file(request, pk):
    attachment = get_object_or_404(LeadAttachment.objects.select_related("lead"), pk=pk)
    if not can_view_lead(request.user, attachment.lead):
        raise PermissionDenied
    if not attachment.file:
        raise Http404
    try:
        opened = attachment.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    filename = sanitize_uploaded_name(attachment.original_name or attachment.file.name.rsplit("/", 1)[-1])
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = FileResponse(
        opened,
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


def legacy_dashboard(request, section="overview"):
    if section not in DASHBOARD_SECTIONS:
        section = "overview"
    return redirect("operations:dashboard-section", section=section)


def legacy_portal(request):
    return redirect("operations:portal")
