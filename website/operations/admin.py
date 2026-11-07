from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.db.models import Q
from django.contrib.auth.models import Group
from django.contrib import admin
from django.db import transaction
from django.contrib.admin.helpers import ActionForm
from django.urls import reverse
from django.utils.html import format_html

from unfold.admin import ModelAdmin

from .admin_site import GrandCoastAdminSite
from .forms import AdminSuperuserCreationForm, user_choice_label
from .models import (
    Activity,
    AdminAccessBlock,
    AdminSecurityEvent,
    Agreement,
    AttentionItem,
    Blocker,
    BudgetLine,
    CalendarDayOverride,
    ChangeOrder,
    Client,
    ClientInvite,
    ClientMessage,
    Commitment,
    CostEntry,
    DailyReport,
    EmailOutbox,
    EmployeeInvite,
    EmployeeProfile,
    EmployeeNotification,
    EmployeeScheduleOverride,
    EmployeeWeeklySchedule,
    Estimate,
    EstimateLineItem,
    Lead,
    LeadAttachment,
    Inspection,
    MaterialRequest,
    MediaAsset,
    Milestone,
    ProcessStep,
    Project,
    ProjectDocument,
    ProjectUpdate,
    PaymentRecord,
    PaymentSchedule,
    Permit,
    PreconstructionItem,
    AiActionDraft,
    MobilePushDevice,
    PushDelivery,
    ScheduleEvent,
    Service,
    SiteSettings,
    Task,
    TimeEntry,
    Selection,
    SiteVisit,
    Subcontractor,
    SubcontractorAssignment,
    WarrantyItem,
    WorkflowEvent,
)

from .construction_services import initialize_warranty
from .construction_policies import STAFF_ROLES, assignable_staff_users


grand_coast_admin_site = GrandCoastAdminSite(name="admin")


class GrandCoastActionForm(ActionForm):
    """Keep Unfold's action controls synchronized with its Alpine state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action"].widget.attrs["x-model"] = "action"
        self.fields["select_across"].widget.attrs["x-model"] = "selectAcross"


class GrandCoastModelAdmin(ModelAdmin):
    action_form = GrandCoastActionForm

    @staticmethod
    def _label_user_field(db_field, formfield):
        if (
            formfield is not None
            and db_field.remote_field
            and db_field.remote_field.model == get_user_model()
        ):
            formfield.label_from_instance = user_choice_label
        return formfield

    @staticmethod
    def _limit_assignment_choices(db_field, kwargs):
        assignment_fields = {
            "assigned_to",
            "assigned_staff",
            "project_manager",
            "watchers",
            "assignees",
        }
        if (
            db_field.name in assignment_fields
            and db_field.remote_field
            and db_field.remote_field.model == get_user_model()
        ):
            kwargs["queryset"] = assignable_staff_users()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        self._limit_assignment_choices(db_field, kwargs)
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        return self._label_user_field(db_field, formfield)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        self._limit_assignment_choices(db_field, kwargs)
        formfield = super().formfield_for_manytomany(db_field, request, **kwargs)
        return self._label_user_field(db_field, formfield)


@admin.register(Client, site=grand_coast_admin_site)
class ClientAdmin(GrandCoastModelAdmin):
    list_display = ("name", "email", "user", "updated_at")
    search_fields = ("name", "email", "company")


@admin.register(EmployeeProfile, site=grand_coast_admin_site)
class EmployeeProfileAdmin(GrandCoastModelAdmin):
    list_display = ("user", "job_title", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
        "job_title",
    )


@admin.register(EmployeeInvite, site=grand_coast_admin_site)
class EmployeeInviteAdmin(GrandCoastModelAdmin):
    list_display = ("email", "group", "purpose", "expires_at", "accepted_at")
    list_filter = ("purpose", "accepted_at", "group")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("token_hash", "accepted_at")


@admin.register(ClientInvite, site=grand_coast_admin_site)
class ClientInviteAdmin(GrandCoastModelAdmin):
    list_display = ("client", "expires_at", "accepted_at", "created_at")
    list_filter = ("accepted_at",)
    search_fields = ("client__name", "client__email")
    readonly_fields = ("token_hash",)


@admin.register(Lead, site=grand_coast_admin_site)
class LeadAdmin(GrandCoastModelAdmin):
    list_display = (
        "name",
        "service",
        "location",
        "status",
        "assigned_to",
        "priority",
        "created_at",
    )
    list_filter = ("status", "priority")
    search_fields = (
        "name",
        "email",
        "service",
        "location",
        "assigned_to__username",
        "assigned_to__first_name",
        "assigned_to__last_name",
    )
    inlines = []

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = super().get_readonly_fields(request, obj)
        if obj and "note" not in readonly_fields:
            return (*readonly_fields, "note")
        return readonly_fields

    @transaction.atomic
    def delete_model(self, request, obj):
        from .storage_retention import retain_lead_attachments_before_delete

        retain_lead_attachments_before_delete(obj, actor=request.user)
        super().delete_model(request, obj)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        from .storage_retention import retain_lead_attachments_before_delete

        for lead in queryset.select_for_update().iterator():
            retain_lead_attachments_before_delete(lead, actor=request.user)
            lead.delete()


class LeadAttachmentInline(admin.TabularInline):
    model = LeadAttachment
    extra = 0
    can_delete = False
    fields = ("original_name", "protected_file", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="File")
    def protected_file(self, obj):
        if not obj or not obj.pk or not obj.file:
            return "No file"
        filename = obj.original_name or obj.file.name.rsplit("/", 1)[-1]
        return format_html(
            '<a href="{}">Download {}</a>',
            reverse("operations:lead-attachment-file", kwargs={"pk": obj.pk}),
            filename,
        )


LeadAdmin.inlines = [LeadAttachmentInline]


@admin.register(Estimate, site=grand_coast_admin_site)
class EstimateAdmin(GrandCoastModelAdmin):
    list_display = (
        "number",
        "title",
        "client",
        "status",
        "deposit_amount",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("title", "client__name", "lead__name")
    # Estimate and invoice link fields are changed through the permission-checked Operations
    # command, never through a generic admin save.
    readonly_fields = (
        "sent_at",
        "accepted_at",
        "declined_at",
        "accepted_by",
        "external_url",
        "external_reference",
        "external_total",
        "external_deposit_amount",
        "external_status",
        "external_status_at",
        "external_status_by",
        "external_status_note",
        "external_client_visible",
    )


@admin.register(EstimateLineItem, site=grand_coast_admin_site)
class EstimateLineItemAdmin(GrandCoastModelAdmin):
    list_display = ("estimate", "description", "quantity", "unit_price", "line_total")
    search_fields = ("description", "estimate__title")


@admin.register(Project, site=grand_coast_admin_site)
class ProjectAdmin(GrandCoastModelAdmin):
    list_display = ("title", "client", "status", "archived_at", "updated_at")
    list_filter = ("status", "archived_at")
    search_fields = ("title", "location", "client__name", "assigned_staff__username")
    readonly_fields = ("archived_at", "cover")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.archived_at:
            return (*fields, "status")
        return fields

    def has_delete_permission(self, request, obj=None):
        # Project deletion must go through the archive flow so linked history
        # and external storage references are handled deliberately.
        return False

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if obj.status == Project.Status.COMPLETE:
            obj.operational_phase = Project.OperationalPhase.WARRANTY
            obj.next_step = obj.next_step or "Warranty support"
        super().save_model(request, obj, form, change)
        if obj.status == Project.Status.COMPLETE:
            initialize_warranty(obj, actor=request.user)
    exclude = (
        "is_published",
        "public_title",
        "public_location",
        "public_summary",
        "show_client_name",
        "gallery_layout",
        "gallery_accent",
    )


@admin.register(Milestone, site=grand_coast_admin_site)
class MilestoneAdmin(GrandCoastModelAdmin):
    list_display = ("project", "title", "sort_order", "is_complete")
    list_filter = ("is_complete",)


@admin.register(Task, site=grand_coast_admin_site)
class TaskAdmin(GrandCoastModelAdmin):
    list_display = (
        "title",
        "assignee_names",
        "project",
        "lead",
        "status",
        "priority",
        "due_date",
    )
    list_filter = ("status", "priority")
    search_fields = (
        "title",
        "description",
        "project__title",
        "lead__name",
        "assigned_to__username",
    )

    @admin.display(description="Assigned to")
    def assignee_names(self, task):
        names = [person.get_full_name() or person.get_username() for person in task.assigned_to.all()]
        return ", ".join(names) or "Unassigned"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("assigned_to")


@admin.register(ProjectUpdate, site=grand_coast_admin_site)
class ProjectUpdateAdmin(GrandCoastModelAdmin):
    list_display = ("project", "title", "visibility", "created_at")
    list_filter = ("visibility",)
    search_fields = ("project__title", "title", "body")


@admin.register(MediaAsset, site=grand_coast_admin_site)
class MediaAssetAdmin(GrandCoastModelAdmin):
    list_display = ("title", "project", "media_type", "visibility", "created_at")
    list_filter = ("media_type", "visibility")
    search_fields = ("title", "caption", "project__title")
    readonly_fields = ("visibility", "file")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProjectDocument, site=grand_coast_admin_site)
class ProjectDocumentAdmin(GrandCoastModelAdmin):
    list_display = (
        "title",
        "project",
        "category",
        "visibility",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("visibility", "category")
    search_fields = ("title", "description", "project__title")
    readonly_fields = ("file",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Activity, site=grand_coast_admin_site)
class ActivityAdmin(GrandCoastModelAdmin):
    list_display = ("message", "detail", "actor", "created_at")
    search_fields = ("message", "detail")
    readonly_fields = tuple(field.name for field in Activity._meta.fields)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(trashed_at__isnull=True)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdminSecurityEvent, site=grand_coast_admin_site)
class AdminSecurityEventAdmin(GrandCoastModelAdmin):
    list_display = (
        "created_at",
        "event_type",
        "outcome",
        "ip_address",
        "user",
        "email_status",
        "reviewed_at",
    )
    list_filter = ("event_type", "outcome", "email_status", "reviewed_at")
    search_fields = (
        "ip_address",
        "attempted_identifier",
        "user__username",
        "user__email",
        "user_agent",
        "path",
        "detail",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(trashed_at__isnull=True)
    readonly_fields = (
        "id",
        "event_type",
        "outcome",
        "attempted_identifier",
        "user",
        "ip_address",
        "user_agent",
        "path",
        "detail",
        "reviewed_at",
        "reviewed_by",
        "email_status",
        "email_attempt_count",
        "email_last_attempt_at",
        "email_sent_at",
        "email_error",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdminAccessBlock, site=grand_coast_admin_site)
class AdminAccessBlockAdmin(GrandCoastModelAdmin):
    list_display = (
        "created_at",
        "scope",
        "ip_address",
        "user",
        "is_active",
        "created_by",
        "revoked_at",
    )
    list_filter = ("scope", "is_active")
    search_fields = ("ip_address", "user__username", "user__email", "reason")
    readonly_fields = (
        "id",
        "scope",
        "ip_address",
        "user",
        "reason",
        "is_active",
        "created_by",
        "created_at",
        "revoked_by",
        "revoked_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Service, site=grand_coast_admin_site)
class ServiceAdmin(GrandCoastModelAdmin):
    list_display = ("title", "slug", "sort_order", "is_active")
    list_filter = ("is_active",)
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ("image",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProcessStep, site=grand_coast_admin_site)
class ProcessStepAdmin(GrandCoastModelAdmin):
    list_display = ("title", "key", "sort_order")
    readonly_fields = ("image",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Agreement, site=grand_coast_admin_site)
class AgreementAdmin(GrandCoastModelAdmin):
    readonly_fields = ("signed_pdf",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteSettings, site=grand_coast_admin_site)
class SiteSettingsAdmin(GrandCoastModelAdmin):
    list_display = ("__str__", "updated_at")


@admin.register(LeadAttachment, site=grand_coast_admin_site)
class LeadAttachmentAdmin(GrandCoastModelAdmin):
    list_display = ("lead", "original_name", "lead_message", "protected_file", "created_at")
    list_select_related = ("lead",)
    search_fields = ("original_name", "lead__name", "lead__email", "lead__note")
    fields = ("lead", "original_name", "lead_message", "protected_file", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Contact message")
    def lead_message(self, obj):
        return obj.lead.note or "No message provided."

    @admin.display(description="File")
    def protected_file(self, obj):
        if not obj or not obj.pk or not obj.file:
            return "No file"
        filename = obj.original_name or obj.file.name.rsplit("/", 1)[-1]
        return format_html(
            '<a href="{}">Download {}</a>',
            reverse("operations:lead-attachment-file", kwargs={"pk": obj.pk}),
            filename,
        )
class ChangeOrderAdmin(GrandCoastModelAdmin):
    list_display = ("project", "number", "title", "status", "price_impact", "approved_at")
    list_filter = ("status",)
    search_fields = ("title", "description", "project__title")

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == ChangeOrder.Status.APPROVED:
            return tuple(field.name for field in obj._meta.fields) + ("supporting_documents",)
        return ("id", "number", "approved_at", "approved_by", "approval_ip", "approved_snapshot", "locked_at")

    def save_related(self, request, form, formsets, change):
        if form.instance.status == ChangeOrder.Status.APPROVED:
            return
        super().save_related(request, form, formsets, change)

    def has_delete_permission(self, request, obj=None):
        return bool(obj is None or obj.status != ChangeOrder.Status.APPROVED)


class PaymentRecordAdmin(GrandCoastModelAdmin):
    list_display = ("project", "amount", "received_on", "method", "created_by", "voided_at")
    list_filter = ("method", "voided_at")
    search_fields = ("project__title", "reference", "notes")
    readonly_fields = tuple(field.name for field in PaymentRecord._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class WorkflowEventAdmin(GrandCoastModelAdmin):
    list_display = ("created_at", "event_type", "source", "actor", "project", "related_model")
    list_filter = ("event_type", "source")
    search_fields = ("event_type", "related_model", "related_id", "project__title")
    readonly_fields = tuple(field.name for field in WorkflowEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CostEntryAdmin(GrandCoastModelAdmin):
    list_display = ("project", "description", "amount", "incurred_on", "is_void", "created_by")
    list_filter = ("is_void", "source")
    search_fields = ("project__title", "description", "vendor")

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return tuple(field.name for field in CostEntry._meta.fields)
        return ("id", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False


class CommitmentAdmin(GrandCoastModelAdmin):
    list_display = ("project", "description", "amount", "status", "due_date")
    list_filter = ("status",)
    search_fields = ("project__title", "description", "subcontractor__company")

    def get_readonly_fields(self, request, obj=None):
        if not obj:
            return ("id", "created_at", "updated_at")
        return (
            "id",
            "project",
            "subcontractor",
            "budget_line",
            "description",
            "amount",
            "due_date",
            "created_by",
            "created_at",
            "updated_at",
        )

    def has_delete_permission(self, request, obj=None):
        return False


grand_coast_admin_site.register(AttentionItem, GrandCoastModelAdmin)
grand_coast_admin_site.register(Blocker, GrandCoastModelAdmin)
grand_coast_admin_site.register(BudgetLine, GrandCoastModelAdmin)
grand_coast_admin_site.register(ChangeOrder, ChangeOrderAdmin)
grand_coast_admin_site.register(Commitment, CommitmentAdmin)
grand_coast_admin_site.register(CostEntry, CostEntryAdmin)
grand_coast_admin_site.register(DailyReport, GrandCoastModelAdmin)
grand_coast_admin_site.register(EmailOutbox, GrandCoastModelAdmin)
grand_coast_admin_site.register(Inspection, GrandCoastModelAdmin)
grand_coast_admin_site.register(MaterialRequest, GrandCoastModelAdmin)
grand_coast_admin_site.register(PaymentRecord, PaymentRecordAdmin)
class PaymentScheduleAdmin(GrandCoastModelAdmin):
    readonly_fields = (
        "external_invoice_url",
        "external_invoice_reference",
        "external_invoice_status",
        "external_invoice_status_at",
        "external_invoice_status_by",
        "external_invoice_status_note",
        "external_client_visible",
    )


grand_coast_admin_site.register(PaymentSchedule, PaymentScheduleAdmin)
grand_coast_admin_site.register(Permit, GrandCoastModelAdmin)
grand_coast_admin_site.register(PreconstructionItem, GrandCoastModelAdmin)
grand_coast_admin_site.register(Selection, GrandCoastModelAdmin)
grand_coast_admin_site.register(SiteVisit, GrandCoastModelAdmin)
grand_coast_admin_site.register(Subcontractor, GrandCoastModelAdmin)
grand_coast_admin_site.register(SubcontractorAssignment, GrandCoastModelAdmin)
grand_coast_admin_site.register(WarrantyItem, GrandCoastModelAdmin)
grand_coast_admin_site.register(WorkflowEvent, WorkflowEventAdmin)
grand_coast_admin_site.register(AiActionDraft, GrandCoastModelAdmin)

grand_coast_admin_site.register(ClientMessage, GrandCoastModelAdmin)
grand_coast_admin_site.register(ScheduleEvent, GrandCoastModelAdmin)
grand_coast_admin_site.register(TimeEntry, GrandCoastModelAdmin)
grand_coast_admin_site.register(CalendarDayOverride, GrandCoastModelAdmin)
grand_coast_admin_site.register(EmployeeWeeklySchedule, GrandCoastModelAdmin)
grand_coast_admin_site.register(EmployeeScheduleOverride, GrandCoastModelAdmin)
grand_coast_admin_site.register(MobilePushDevice, GrandCoastModelAdmin)
grand_coast_admin_site.register(EmployeeNotification, GrandCoastModelAdmin)
grand_coast_admin_site.register(PushDelivery, GrandCoastModelAdmin)

# Keep the lower-level Django user/group records available in the same private
# Unfold site. Security settings for administrators are deliberately managed
# through the branded /gccad/security/ page rather than exposing hashes or
# authenticator secrets in a model form.
class AccountTypeFilter(admin.SimpleListFilter):
    title = "account type"
    parameter_name = "account_type"

    def lookups(self, request, model_admin):
        return (
            ("administrator", "Administrator"),
            ("staff", "Staff"),
            ("client", "Client"),
            ("subcontractor", "Subcontractor"),
            ("unclassified", "Other / unclassified"),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "administrator":
            return queryset.filter(is_superuser=True)
        if value == "staff":
            return queryset.filter(is_staff=True, is_superuser=False).filter(
                Q(groups__name__in=STAFF_ROLES) | Q(employee_profile__isnull=False)
            ).distinct()
        if value == "client":
            return queryset.filter(client_record__isnull=False)
        if value == "subcontractor":
            return queryset.filter(subcontractor_profile__isnull=False)
        if value == "unclassified":
            categorized = (
                Q(is_superuser=True)
                | Q(client_record__isnull=False)
                | Q(subcontractor_profile__isnull=False)
                | (
                    Q(is_staff=True, is_superuser=False)
                    & (Q(groups__name__in=STAFF_ROLES) | Q(employee_profile__isnull=False))
                )
            )
            return queryset.exclude(categorized).distinct()
        return queryset


class GrandCoastUserAdmin(UserAdmin):
    action_form = GrandCoastActionForm
    add_form = AdminSuperuserCreationForm
    change_list_template = "admin/auth/user/change_list.html"
    show_add_link = True
    add_fieldsets = (
        (
            "Superuser account",
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "first_name",
                    "last_name",
                    "password1",
                    "password2",
                    "confirm_full_access",
                ),
            },
        ),
    )
    list_display = (*UserAdmin.list_display, "account_type", "is_superuser")
    list_filter = (*UserAdmin.list_filter, AccountTypeFilter)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("client_record", "subcontractor_profile", "employee_profile")
            .prefetch_related("groups")
        )

    @admin.display(description="Account type", ordering="is_staff")
    def account_type(self, user):
        roles = []
        group_names = {group.name for group in user.groups.all()}
        has_client = getattr(user, "client_record", None) is not None
        has_subcontractor = getattr(user, "subcontractor_profile", None) is not None
        has_employee_profile = getattr(user, "employee_profile", None) is not None

        if user.is_superuser:
            roles.append("Administrator")
        if has_client:
            roles.append("Client")
        if has_subcontractor:
            roles.append("Subcontractor")
        if user.is_staff and not user.is_superuser:
            if group_names & STAFF_ROLES or has_employee_profile:
                roles.append("Staff")
            else:
                roles.append("Staff (role not set)")
        return ", ".join(roles) if roles else "No portal role"

    def has_add_permission(self, request):
        user = request.user
        return bool(
            user.is_authenticated
            and user.is_active
            and user.is_staff
            and user.is_superuser
            and self.has_change_permission(request)
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.is_active = True
            obj.is_staff = True
            obj.is_superuser = True
        super().save_model(request, obj, form, change)


class GrandCoastGroupAdmin(GroupAdmin):
    action_form = GrandCoastActionForm


grand_coast_admin_site.register(get_user_model(), GrandCoastUserAdmin)
grand_coast_admin_site.register(Group, GrandCoastGroupAdmin)
