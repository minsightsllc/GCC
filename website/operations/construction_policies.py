"""Shared authorization rules for the Grand Coast operating system.

These helpers intentionally scope every query by the authenticated actor.  A
route must use these policies even when a record is linked from a hidden page;
navigation is never treated as an authorization boundary.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import EmployeeProfile, Estimate, Lead, Project, Subcontractor


STAFF_ROLES = {"Owner", "Manager", "Office", "Field", "Sales"}


def feature_enabled(name, default=True):
    setting_name = f"GCC_{str(name).upper()}_ENABLED"
    return bool(getattr(settings, setting_name, default))


def role_names(user):
    if not getattr(user, "is_authenticated", False):
        return set()
    return set(user.groups.values_list("name", flat=True))


def is_active_user(user):
    return bool(getattr(user, "is_authenticated", False) and user.is_active)


def is_owner(user):
    return bool(is_active_user(user) and (user.is_superuser or "Owner" in role_names(user)))


def is_staff_user(user):
    return bool(
        is_active_user(user)
        and user.is_staff
        and (
            user.is_superuser
            or role_names(user) & STAFF_ROLES
        )
        and not EmployeeProfile.objects.filter(user=user, is_active=False).exists()
    )


def is_manager(user):
    return bool(is_owner(user) or (is_staff_user(user) and "Manager" in role_names(user)))


def is_office(user):
    return bool(is_staff_user(user) and (is_owner(user) or "Office" in role_names(user)))


def is_field(user):
    return bool(is_staff_user(user) and "Field" in role_names(user) and not is_owner(user))


def is_sales(user):
    return bool(is_staff_user(user) and "Sales" in role_names(user))


def is_client(user):
    return bool(is_active_user(user) and not user.is_staff and hasattr(user, "client_record"))


def is_subcontractor(user):
    if not is_active_user(user) or not hasattr(user, "subcontractor_profile"):
        return False
    return user.subcontractor_profile.status == Subcontractor.Status.ACTIVE


def can_access_operating_system(user):
    return bool(
        feature_enabled("operating_system")
        and (is_staff_user(user) or is_client(user) or is_subcontractor(user))
    )


def can_view_media(user, media):
    from .publishing import client_is_live, website_is_live

    project = getattr(media, "project", None)
    if is_owner(user) or website_is_live(media):
        return True
    # Public files are available to everyone; client feeds filter destinations separately.
    if is_client(user):
        return bool(project and can_view_project(user, project) and client_is_live(media))
    if project is None or not can_view_project(user, project):
        return False
    if is_subcontractor(user):
        return client_is_live(media)
    return True


def can_view_agreement(user, agreement):
    project = getattr(agreement, "project", None)
    if project is None or not can_view_project(user, project):
        return False
    return bool(is_client(user) or can_view_financials(user, project))


def visible_projects(user):
    """Return only projects the actor may know exist."""
    operational_projects = Project.objects.filter(is_portfolio_only=False)
    if not can_access_operating_system(user):
        return Project.objects.none()
    if is_owner(user):
        return operational_projects
    if is_manager(user) or is_office(user) or is_field(user):
        return operational_projects.filter(
            Q(project_manager=user) | Q(assigned_staff=user)
        ).distinct()
    if is_client(user):
        return operational_projects.filter(client__user=user)
    if is_subcontractor(user):
        return operational_projects.filter(
            subcontractor_assignments__subcontractor__portal_user=user
        ).distinct()
    return Project.objects.none()


def can_view_project(user, project):
    if not getattr(project, "pk", None):
        return False
    return visible_projects(user).filter(pk=project.pk).exists()


def require_project_access(user, project, *, mutate=False):
    if not can_view_project(user, project):
        raise PermissionDenied("You do not have access to this project.")
    if mutate and not can_mutate_project(user, project):
        raise PermissionDenied("You do not have permission to change this project.")
    return project


def can_view_financials(user, project):
    """Financials are limited to the owner and the assigned manager."""
    if not is_active_user(user):
        return False
    # Django superusers are the canonical Grand Coast Owner account.
    if getattr(user, 'is_superuser', False):
        return True
    if not can_view_project(user, project):
        return False
    return bool(is_owner(user) or (is_manager(user) and (
        project.project_manager_id == user.pk or project.assigned_staff.filter(pk=user.pk).exists()
    )))


def can_mutate_project(user, project):
    if not can_view_project(user, project):
        return False
    if is_owner(user):
        return True
    return bool(is_manager(user) and (
        project.project_manager_id == user.pk or project.assigned_staff.filter(pk=user.pk).exists()
    ))


def can_manage_sales(user):
    return bool(is_owner(user) or is_manager(user) or is_office(user) or is_sales(user))


def visible_leads(user):
    if not can_manage_sales(user):
        return Lead.objects.none()
    base = Lead.objects.filter(deleted_at__isnull=True)
    if is_owner(user) or is_manager(user):
        return base
    return base.filter(Q(assigned_to=user) | Q(created_by=user)).distinct()


def can_view_lead(user, lead):
    return bool(getattr(lead, "pk", None) and visible_leads(user).filter(pk=lead.pk).exists())


def can_manage_construction(user, project=None):
    if project is None:
        return bool(is_owner(user) or is_manager(user))
    return bool(can_mutate_project(user, project))


def can_manage_project_operations(user, project=None):
    """Allow non-financial project execution work for authorized staff."""
    if project is not None and not can_view_project(user, project):
        return False
    return bool(is_owner(user) or is_manager(user) or is_office(user))


def _execution_loop_allowlist(setting_name):
    raw = str(getattr(settings, setting_name, "") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def execution_loop_enabled_for(user, project=None):
    """Return the server-side pilot gate for the enhanced project workspace."""
    if (
        not feature_enabled("operating_system", default=True)
        or not feature_enabled("execution_loop", default=False)
    ):
        return False
    if not is_active_user(user):
        return False
    user_ids = _execution_loop_allowlist("GCC_EXECUTION_LOOP_USER_IDS")
    if user_ids and str(user.pk) not in user_ids:
        return False
    project_ids = _execution_loop_allowlist("GCC_EXECUTION_LOOP_PROJECT_IDS")
    if project is not None and project_ids and str(project.pk) not in project_ids:
        return False
    return True


def external_estimate_enabled_for(user, *, project=None, estimate=None):
    """Return whether the automatic estimate-status surface is available."""
    return bool(
        feature_enabled("operating_system", default=True)
        and is_active_user(user)
    )


def can_manage_external_estimate(user, *, project=None, estimate=None):
    """Only Owner, Manager, and Office can manually record estimate and invoice status."""
    if not external_estimate_enabled_for(user, project=project, estimate=estimate):
        return False
    if not (is_owner(user) or is_manager(user) or is_office(user)):
        return False
    if project is not None:
        return can_view_project(user, project)
    if estimate is not None:
        if can_view_estimate(user, estimate):
            return True
        linked_project = estimate.projects.order_by("-created_at").first()
        return bool(linked_project and can_view_project(user, linked_project))
    return True


def can_view_external_estimate(user, *, project=None, estimate=None):
    """View only the small status surface, never provider credentials or details."""
    if not external_estimate_enabled_for(user, project=project, estimate=estimate):
        return False
    if project is not None:
        return can_view_project(user, project)
    if estimate is not None:
        if can_view_estimate(user, estimate):
            return True
        linked_project = estimate.projects.order_by("-created_at").first()
        return bool(linked_project and can_view_project(user, linked_project))
    return False


def visible_estimates(user):
    if is_client(user):
        return Estimate.objects.filter(
            client__user=user,
        ).exclude(status=Estimate.Status.DRAFT).filter(
            Q(external_url="") | Q(external_client_visible=True)
        )
    if not can_manage_sales(user):
        return Estimate.objects.none()

    leads = visible_leads(user)
    scope = Q(lead__in=leads) | Q(client__leads__in=leads) | Q(created_by=user)
    if is_owner(user) or is_manager(user):
        return Estimate.objects.all()
    return Estimate.objects.filter(scope).distinct()


def can_view_estimate(user, estimate):
    return bool(
        getattr(estimate, "pk", None)
        and visible_estimates(user).filter(pk=estimate.pk).exists()
    )


def can_submit_field_work(user, project):
    return bool(
        can_view_project(user, project)
        and (is_owner(user) or is_manager(user) or is_office(user) or is_field(user) or is_subcontractor(user))
    )


def can_view_client_surface(user, project):
    return bool(is_client(user) and project.client_id == getattr(user.client_record, "pk", None))


def can_view_project_document(user, document):
    project = getattr(document, "project", None)
    if project is None or not can_view_project(user, project):
        return False
    if is_client(user):
        return document.visibility == "client"
    if is_subcontractor(user):
        return document.visibility == "client" or document.category.lower() in {
            "plans",
            "plan",
            "scope",
            "field",
            "subcontractor",
        }
    if is_field(user):
        return document.visibility == "client" or document.category.lower() in {
            "plans",
            "plan",
            "scope",
            "field",
            "notes",
        }
    return True


def can_view_subcontractor_assignment(user, assignment):
    project = getattr(assignment, "project", None)
    if project is None:
        return False
    if is_subcontractor(user):
        return bool(
            assignment.subcontractor.portal_user_id == getattr(user, "pk", None)
        )
    return bool(
        can_view_project(user, project)
        and (is_owner(user) or is_manager(user) or is_office(user) or is_field(user))
    )


def can_manage_subcontractors(user, project=None):
    if project is not None and not can_view_project(user, project):
        return False
    return bool(is_owner(user) or is_manager(user))


def can_view_permit(user, permit):
    project = getattr(permit, "project", None)
    if project is None or not can_view_project(user, project):
        return False
    if is_subcontractor(user):
        return False
    return True


def can_view_closeout(user, item):
    project = getattr(item, "project", None)
    if project is None or not can_view_project(user, project):
        return False
    return bool(
        is_client(user)
        or is_owner(user)
        or is_manager(user)
        or is_office(user)
        or is_field(user)
    )


def ai_retrieval_scope(user):
    """Return a serializable scope description; callers still filter querysets."""
    if not can_access_operating_system(user):
        return {
            "project_ids": [],
            "financial_project_ids": [],
            "financials": False,
            "role": "anonymous",
        }
    projects = visible_projects(user)
    project_ids = list(projects.values_list("pk", flat=True))
    if is_owner(user):
        financial_project_ids = [str(pk) for pk in project_ids]
    elif is_manager(user):
        financial_project_ids = [
            str(pk)
            for pk in projects.filter(
                Q(project_manager=user) | Q(assigned_staff=user)
            ).values_list("pk", flat=True).distinct()
        ]
    else:
        financial_project_ids = []
    return {
        "project_ids": [str(pk) for pk in project_ids],
        "financial_project_ids": financial_project_ids,
        "financials": bool(financial_project_ids),
        "role": "owner" if is_owner(user) else next(iter(role_names(user)), "client"),
    }


def role_label(user):
    if is_owner(user):
        return "Owner"
    names = role_names(user)
    for name in ("Manager", "Office", "Sales", "Field"):
        if name in names:
            return name
    if is_subcontractor(user):
        return "Subcontractor"
    if is_client(user):
        return "Client"
    return "User"
