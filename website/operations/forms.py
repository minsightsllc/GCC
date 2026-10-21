from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import urlparse

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import (
    Client,
    ClientMessage,
    CalendarDayOverride,
    CONTACT_MAX_FILES,
    CONTACT_MAX_TOTAL_SIZE,
    CONTACT_UPLOAD_ACCEPT,
    AdminSecurityEvent,
    EmployeeInvite,
    EmployeeProfile,
    EmployeeScheduleOverride,
    EmployeeWeeklySchedule,
    Estimate,
    EstimateLineItem,
    Lead,
    MediaAsset,
    ProjectDocument,
    Project,
    ProjectUpdate,
    ProjectUpdateDraft,
    ScheduleEvent,
    Service,
    Subcontractor,
    Task,
    TimeEntry,
    validate_contact_upload,
    validate_uploaded_media,
)
from .turnstile import (
    TURNSTILE_ERROR_MESSAGE,
    is_mobile_owner_login,
    verify_turnstile_request,
)


def user_choice_label(user):
    """Show a person's name in staff selectors, with a safe username fallback."""
    full_name = user.get_full_name().strip()
    return full_name or user.get_username()


class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput
    upload_validator = staticmethod(validate_uploaded_media)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            cleaned = [single_file_clean(item, initial) for item in data]
        else:
            cleaned = [single_file_clean(data, initial)]
        for upload in cleaned:
            self.upload_validator(upload)
        return cleaned


class ContactUploadInput(MultipleFileInput):
    def __init__(self, attrs=None, **kwargs):
        attrs = {**(attrs or {}), "accept": CONTACT_UPLOAD_ACCEPT}
        super().__init__(attrs=attrs, **kwargs)


class ContactUploadField(MultipleFileField):
    widget = ContactUploadInput
    upload_validator = staticmethod(validate_contact_upload)


class ContactLeadForm(forms.Form):
    first_name = forms.CharField(max_length=80)
    last_name = forms.CharField(max_length=80)
    email = forms.EmailField()
    phone = forms.CharField(max_length=40, required=False)
    project_type = forms.CharField(max_length=120)
    location = forms.CharField(max_length=160)
    message = forms.CharField(widget=forms.Textarea, max_length=20000)
    photos = ContactUploadField(required=False)

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if not self.errors and not verify_turnstile_request(self.request, expected_action="contact"):
            raise ValidationError(TURNSTILE_ERROR_MESSAGE)
        return cleaned

    def clean_photos(self):
        uploads = self.cleaned_data.get("photos") or []
        if len(uploads) > CONTACT_MAX_FILES:
            raise ValidationError(f"You can upload up to {CONTACT_MAX_FILES} files per request.")
        if sum(upload.size for upload in uploads) > CONTACT_MAX_TOTAL_SIZE:
            raise ValidationError("The combined upload size must be 100 MB or smaller.")
        return uploads


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = [
            "name", "email", "phone", "service", "location", "budget", "budget_amount",
            "timeline", "source", "note", "address_line1", "address_line2", "city",
            "state", "postal_code", "status", "priority", "assigned_to",
        ]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {"note": "Client inquiry"}
        help_texts = {
            "note": (
                "The client's original message. This is read-only after the lead is created."
            )
        }

    def __init__(self, *args, staff_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assigned_to"].label_from_instance = user_choice_label


class LeadStatusForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["status"]


class LeadNoteForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["internal_note"]
        labels = {"internal_note": "Admin note (internal)"}
        help_texts = {
            "internal_note": (
                "Private to Grand Coast staff. This note is not shown in the client portal."
            )
        }
        widgets = {"internal_note": forms.Textarea(attrs={"rows": 3})}


class QuickTaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "due_date", "assigned_to", "priority"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "assigned_to": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "assignees",
                "aria-label": "Task assignees",
            }),
        }

    def __init__(self, *args, staff_queryset=None, lead=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lead is not None:
            # The lead is supplied by the selected record rather than as a
            # field in this compact follow-up form. Set it before ModelForm's
            # model validation runs.
            self.instance.lead = lead
        self.fields["assigned_to"].queryset = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assigned_to"].label_from_instance = user_choice_label
        self.fields["assigned_to"].label = "Assigned to"


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "lead", "client", "project", "milestone", "assigned_to", "watchers", "status", "priority", "due_date"]
        labels = {"watchers": "Supervisors"}
        help_texts = {
            "lead": "Choose a lead for pre-client follow-up. Selecting a client disables this field; selecting a lead disables the client and project fields.",
            "client": "Choose an existing client to connect this employee task to their account.",
            "project": "Choose a project for this client, or leave it unassigned if a project has not been created yet.",
            "milestone": "Choose a milestone after selecting a project.",
            "assigned_to": "Select every employee responsible for completing this task.",
            "watchers": "Supervisors can follow the task but are not assigned to complete it.",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "assigned_to": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "assignees",
                "aria-label": "Task assignees",
            }),
            "watchers": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "supervisors",
                "aria-label": "Task supervisors",
            }),
        }

    def __init__(self, *args, staff_queryset=None, client_queryset=None, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        staff = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assigned_to"].queryset = staff
        self.fields["watchers"].queryset = staff
        self.fields["assigned_to"].label_from_instance = user_choice_label
        self.fields["watchers"].label_from_instance = user_choice_label
        self.fields["assigned_to"].label = "Assigned to"
        lead_queryset = Lead.objects.filter(deleted_at__isnull=True)
        if not self.instance._state.adding and self.instance.lead_id:
            lead_queryset = lead_queryset.filter(
                Q(client__isnull=True) | Q(pk=self.instance.lead_id)
            )
        else:
            lead_queryset = lead_queryset.filter(client__isnull=True)
        self.fields["lead"].queryset = lead_queryset
        self.fields["client"].queryset = (
            client_queryset if client_queryset is not None else Client.objects.all()
        )
        self.fields["project"].queryset = (
            project_queryset if project_queryset is not None else Project.objects.all()
        )

        if (
            not self.is_bound
            and self.instance.pk
            and not self.instance.lead_id
            and not self.instance.client_id
            and self.instance.project_id
            and self.instance.project.client_id
        ):
            self.initial.setdefault("client", self.instance.project.client_id)

        lead_attrs = self.fields["lead"].widget.attrs
        lead_attrs["data-task-lead-select"] = ""
        client_attrs = self.fields["client"].widget.attrs
        client_attrs["data-task-client-select"] = ""
        project_attrs = self.fields["project"].widget.attrs
        project_attrs["data-task-project-select"] = ""
        project_attrs["data-project-client-map"] = json.dumps({
            str(project.pk): str(project.client_id or "")
            for project in self.fields["project"].queryset.select_related("client")
        })
        self.fields["milestone"].widget.attrs["data-task-milestone-select"] = ""

    def clean(self):
        cleaned = super().clean()
        lead = cleaned.get("lead")
        client = cleaned.get("client")
        project = cleaned.get("project")
        preserve_existing_lead_project = bool(
            not self.instance._state.adding
            and self.instance.lead_id
            and self.instance.project_id
            and lead
            and lead.pk == self.instance.lead_id
            and project
            and project.pk == self.instance.project_id
            and (client.pk if client else None) == self.instance.client_id
        )

        if lead and client and not preserve_existing_lead_project:
            self.add_error("client", "Clear the lead selection before choosing a client.")
        if lead and project and not preserve_existing_lead_project:
            self.add_error("project", "Clear the lead selection before choosing a project.")
        if project and not client and not preserve_existing_lead_project:
            if project.client_id:
                cleaned["client"] = project.client
                client = project.client
            else:
                self.add_error("client", "Choose a client before selecting a project.")
        if client and project and project.client_id != client.pk:
            self.add_error("project", "Choose a project that belongs to the selected client.")
        if not lead and not client and not project:
            self.add_error("client", "Choose a lead or client to connect this task to.")
        return cleaned


class TeamTaskUpdateForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "status", "priority", "due_date"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }


class LeadAssignmentForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["assigned_to"]

    def __init__(self, *args, staff_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assigned_to"].label_from_instance = user_choice_label


class EstimateCreateForm(forms.ModelForm):
    class Meta:
        model = Estimate
        fields = ["lead", "client", "title", "deposit_amount"]
        widgets = {"deposit_amount": forms.NumberInput(attrs={"min": "0", "step": "0.01"})}

    def __init__(self, *args, lead_queryset=None, client_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if lead_queryset is not None:
            self.fields["lead"].queryset = lead_queryset
        if client_queryset is not None:
            self.fields["client"].queryset = client_queryset

    def clean(self):
        cleaned = super().clean()
        lead = cleaned.get("lead")
        client = cleaned.get("client")
        if lead and not lead.client_id:
            raise ValidationError("Convert this lead into a client before creating an estimate.")
        if lead and client and lead.client_id != client.pk:
            raise ValidationError("Choose the client connected to this lead.")
        if not lead and not client:
            raise ValidationError("Choose a lead or client for this estimate.")
        return cleaned


class ExternalEstimateCreateForm(forms.ModelForm):
    class Meta:
        model = Estimate
        fields = ["client", "external_url"]
        labels = {
            "client": "Client",
            "external_url": "Estimate link",
        }
        widgets = {
            "external_url": forms.URLInput(
                attrs={"placeholder": "https://...", "autocomplete": "url"}
            ),
        }

    def __init__(self, *args, client_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].required = True
        self.fields["external_url"].required = True
        if client_queryset is not None:
            self.fields["client"].queryset = client_queryset

    def clean_external_url(self):
        value = (self.cleaned_data.get("external_url") or "").strip()
        URLValidator()(value)
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValidationError("Use an HTTPS estimate link.")
        if parsed.username or parsed.password:
            raise ValidationError("Estimate links cannot contain embedded credentials.")
        return value


class EstimateForm(forms.ModelForm):
    class Meta:
        model = Estimate
        fields = ["title", "status", "deposit_amount", "notes"]
        widgets = {
            "deposit_amount": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [
            (value, label)
            for value, label in self.fields["status"].choices
            if value != Estimate.Status.ACCEPTED
        ]
    def clean_deposit_amount(self):
        value = self.cleaned_data["deposit_amount"] or Decimal("0.00")
        if value < 0:
            raise ValidationError("Deposit amount cannot be negative.")
        return value


class EstimateLineItemForm(forms.ModelForm):
    class Meta:
        model = EstimateLineItem
        fields = ["description", "quantity", "unit_price", "sort_order"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"min": "0.01", "step": "0.01"}),
            "unit_price": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "sort_order": forms.HiddenInput(),
        }

    def clean(self):
        cleaned = super().clean()
        if not self.has_changed():
            return cleaned
        if not cleaned.get("description") and not self.cleaned_data.get("DELETE"):
            raise ValidationError("Add a description for each line item.")
        return cleaned


class BaseEstimateLineItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        remaining = [
            form
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and (form.instance.pk or form.has_changed())
        ]
        if not remaining:
            raise ValidationError("An estimate needs at least one line item.")


EstimateLineItemFormSet = inlineformset_factory(
    Estimate,
    EstimateLineItem,
    form=EstimateLineItemForm,
    formset=BaseEstimateLineItemFormSet,
    extra=1,
    can_delete=True,
    max_num=50,
)


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["title", "client", "lead", "assigned_staff", "location", "project_type", "admin_stage_note", "next_step", "summary", "is_published", "start_date", "target_date"]
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 4}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "target_date": forms.DateInput(attrs={"type": "date"}),
            "assigned_staff": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "assigned-staff",
                "aria-label": "Assigned workers",
            }),
        }

    def __init__(self, *args, staff_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_published"].label = "Publish on public website"
        self.fields["is_published"].help_text = (
            "Shows a public project page; this does not control client portal access."
        )
        self.fields["admin_stage_note"].label = "Admin-only stage note"
        self.fields["admin_stage_note"].help_text = (
            "Private staff reference only. Shared project progress and client/team alerts are controlled by milestones."
        )
        stage_note_choices = list(self.fields["admin_stage_note"].choices)
        if stage_note_choices and stage_note_choices[0][0] == "":
            stage_note_choices[0] = ("", "No admin note")
            self.fields["admin_stage_note"].choices = stage_note_choices
        self.fields["assigned_staff"].queryset = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assigned_staff"].label_from_instance = user_choice_label

    def clean(self):
        cleaned = super().clean()
        lead = cleaned.get("lead")
        client = cleaned.get("client")
        if lead and client and lead.client_id != client.pk:
            raise ValidationError("Choose the client connected to this lead.")
        if lead:
            has_accepted_estimate = lead.estimates.filter(
                status=Estimate.Status.ACCEPTED,
                external_status=Estimate.ExternalEstimateStatus.APPROVED,
            ).exclude(external_url="").exists()
            if self.instance.pk and self.instance.estimate_id:
                has_accepted_estimate = has_accepted_estimate or (
                    self.instance.estimate.status == Estimate.Status.ACCEPTED
                    and self.instance.estimate.external_status == Estimate.ExternalEstimateStatus.APPROVED
                    and bool(self.instance.estimate.external_url)
                    and self.instance.estimate.lead_id == lead.pk
                )
            if not has_accepted_estimate:
                self.add_error("lead", "Lead-derived projects require an externally approved estimate recorded in GCC.")
        return cleaned


class ProjectCreateForm(ProjectForm):
    """Create a project from its converted client record.

    The legacy ``lead`` relationship remains on ``Project`` for history and
    existing records, but it is not a user-selectable input once a lead has
    become a client. The create view resolves that relationship from the
    externally approved estimate recorded in GCC.
    """

    class Meta(ProjectForm.Meta):
        fields = [field for field in ProjectForm.Meta.fields if field != "lead"]

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client")
        if client:
            approved_external_estimate = Estimate.objects.filter(
                Q(client=client) | Q(lead__client=client),
                status=Estimate.Status.ACCEPTED,
                external_status=Estimate.ExternalEstimateStatus.APPROVED,
            ).exclude(external_url="").exclude(projects__isnull=False).exists()
            if not approved_external_estimate:
                self.add_error(
                    "client",
                    "Create this project after an external estimate has been approved and recorded in GCC.",
                )
        return cleaned


class ProjectUpdateForm(forms.ModelForm):
    class Meta:
        model = ProjectUpdate
        fields = ["title", "body", "visibility"]
        widgets = {"body": forms.Textarea(attrs={"rows": 4})}


class ProjectUpdateDraftForm(forms.ModelForm):
    class Meta:
        model = ProjectUpdateDraft
        fields = ["title", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 7})}


class MediaEditForm(forms.ModelForm):
    class Meta:
        model = MediaAsset
        fields = ["caption", "visibility", "project"]
        widgets = {"caption": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset if project_queryset is not None else Project.objects.all()


class MediaUploadForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.none())
    visibility = forms.ChoiceField(choices=MediaAsset.Visibility.choices)
    files = MultipleFileField(required=True)

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset if project_queryset is not None else Project.objects.none()


class ContentStudioForm(forms.Form):
    headline = forms.CharField(max_length=180)
    subheadline = forms.CharField(widget=forms.Textarea, max_length=1000)
    featured_title = forms.CharField(max_length=180)
    featured_body = forms.CharField(widget=forms.Textarea, max_length=1200)
    google_review_url = forms.URLField(required=False, assume_scheme="https", max_length=500)

    def __init__(self, *args, services=None, process_steps=None, **kwargs):
        super().__init__(*args, **kwargs)
        for service in services or Service.objects.filter(is_active=True):
            self.fields[f"service_{service.slug}_title"] = forms.CharField(max_length=120)
            self.fields[f"service_{service.slug}_copy"] = forms.CharField(widget=forms.Textarea, max_length=500)
        for step in process_steps or []:
            self.fields[f"step_{step.key}"] = forms.CharField(max_length=120)


class ClientMessageForm(forms.ModelForm):
    class Meta:
        model = ClientMessage
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 4, "placeholder": "Write a message to Grand Coast..."})}


class StaffMessageForm(forms.ModelForm):
    class Meta:
        model = ClientMessage
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a message to the client..."})}


class EmployeeProfileForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        fields = ["job_title", "phone", "is_active"]

    def __init__(self, *args, allow_status=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_status:
            self.fields.pop("is_active", None)


class AccountDeleteForm(forms.Form):
    password = forms.CharField(
        label="Current password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    confirmation = forms.CharField(
        label='Type DELETE to confirm',
        max_length=6,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if not self.user or not self.user.check_password(password):
            raise ValidationError("Enter your current password to continue.")
        return password

    def clean_confirmation(self):
        confirmation = self.cleaned_data.get("confirmation", "")
        if confirmation != "DELETE":
            raise ValidationError('Type DELETE exactly to confirm account deletion.')
        return confirmation


class EmployeeInviteForm(forms.ModelForm):
    class Meta:
        model = EmployeeInvite
        fields = ["email", "first_name", "last_name", "group"]

    def __init__(self, *args, group_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = group_queryset if group_queryset is not None else Group.objects.filter(name__in=["Manager", "Office", "Field", "Sales"])


class ScheduleEventForm(forms.ModelForm):
    class Meta:
        model = ScheduleEvent
        fields = ["title", "assignees", "project", "task", "start_at", "end_at", "location", "notes"]
        widgets = {
            "assignees": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "assignees",
                "aria-label": "Calendar event assignees",
            }),
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, staff_queryset=None, project_queryset=None, task_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignees"].queryset = staff_queryset if staff_queryset is not None else get_user_model().objects.filter(is_staff=True)
        self.fields["assignees"].label_from_instance = user_choice_label
        self.fields["project"].queryset = project_queryset if project_queryset is not None else Project.objects.all()
        self.fields["task"].queryset = task_queryset if task_queryset is not None else Task.objects.all()


class CalendarDayOverrideForm(forms.Form):
    NORMAL = 'normal'
    STATUS_CHOICES = (
        (NORMAL, 'Normal'),
        *CalendarDayOverride.Status.choices,
    )

    date = forms.DateField(
        label='Date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    status = forms.ChoiceField(label='Day type', choices=STATUS_CHOICES)
    short_start = forms.TimeField(
        label='Short-day start',
        required=False,
        widget=forms.TimeInput(attrs={'type': 'time'}),
    )
    short_end = forms.TimeField(
        label='Short-day end',
        required=False,
        widget=forms.TimeInput(attrs={'type': 'time'}),
    )
    reason = forms.CharField(
        label='Reason or note',
        required=False,
        max_length=180,
        widget=forms.TextInput(attrs={'placeholder': 'Holiday, training, or other note'}),
    )

    def __init__(self, *args, instance=None, **kwargs):
        initial = kwargs.get('initial') or {}
        kwargs['initial'] = initial
        self.instance = instance
        if instance is not None:
            initial.setdefault('date', instance.date)
            initial.setdefault('status', instance.status)
            initial.setdefault('short_start', instance.short_start)
            initial.setdefault('short_end', instance.short_end)
            initial.setdefault('reason', instance.reason)
        else:
            initial.setdefault('status', self.NORMAL)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        short_start = cleaned_data.get('short_start')
        short_end = cleaned_data.get('short_end')

        if status == CalendarDayOverride.Status.SHORT:
            if not short_start:
                self.add_error('short_start', 'Short days need a start time.')
            if not short_end:
                self.add_error('short_end', 'Short days need an end time.')
            if short_start and short_end and short_end <= short_start:
                self.add_error('short_end', 'The short-day end time must be after its start time.')
        else:
            cleaned_data['short_start'] = None
            cleaned_data['short_end'] = None
            if status == self.NORMAL:
                cleaned_data['reason'] = ''
        return cleaned_data


class EmployeeWeeklyScheduleForm(forms.ModelForm):
    class Meta:
        model = EmployeeWeeklySchedule
        fields = ["is_working", "start_time", "end_time"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }


class EmployeeScheduleOverrideForm(forms.Form):
    CLEAR = "clear"
    STATUS_CHOICES = (
        *EmployeeScheduleOverride.Status.choices,
        (CLEAR, "Clear / use weekly schedule"),
    )

    employee = forms.ModelChoiceField(queryset=get_user_model().objects.none(), label="Employee")
    date = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    status = forms.ChoiceField(label="Day status", choices=STATUS_CHOICES)
    start_time = forms.TimeField(
        label="Override start",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    end_time = forms.TimeField(
        label="Override end",
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
    )
    reason = forms.CharField(
        label="Reason or note",
        required=False,
        max_length=180,
        widget=forms.TextInput(attrs={"placeholder": "Adjusted hours, appointment, or day off"}),
    )

    def __init__(self, *args, employee_queryset=None, instance=None, initial=None, **kwargs):
        initial = {**(initial or {})}
        self.instance = instance
        if employee_queryset is not None:
            self.employee_queryset = employee_queryset
        else:
            self.employee_queryset = get_user_model().objects.none()
        initial.setdefault("status", EmployeeScheduleOverride.Status.WORKING)
        if instance is not None:
            initial.setdefault("employee", instance.employee_id)
            initial.setdefault("date", instance.date)
            initial.setdefault("status", instance.status)
            initial.setdefault("start_time", instance.start_time)
            initial.setdefault("end_time", instance.end_time)
            initial.setdefault("reason", instance.reason)
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = self.employee_queryset
        self.fields["employee"].label_from_instance = user_choice_label

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get("status")
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if status == EmployeeScheduleOverride.Status.WORKING:
            if not start_time:
                self.add_error("start_time", "Working overrides need a start time.")
            if not end_time:
                self.add_error("end_time", "Working overrides need an end time.")
            if start_time and end_time and end_time <= start_time:
                self.add_error("end_time", "The end time must be after the start time.")
        else:
            cleaned_data["start_time"] = None
            cleaned_data["end_time"] = None
            if status == self.CLEAR:
                cleaned_data["reason"] = ""
        return cleaned_data


class TimeEntryForm(forms.ModelForm):
    class Meta:
        model = TimeEntry
        fields = ["project", "task", "clock_in", "clock_out", "note"]
        widgets = {
            "clock_in": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "clock_out": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, project_queryset=None, task_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset if project_queryset is not None else Project.objects.all()
        self.fields["task"].queryset = task_queryset if task_queryset is not None else Task.objects.all()


class ProjectDocumentForm(forms.ModelForm):
    class Meta:
        model = ProjectDocument
        fields = ["project", "title", "category", "description", "file", "visibility"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset if project_queryset is not None else Project.objects.all()


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name", "company", "email", "phone"]


class EmployeeInviteAcceptForm(forms.Form):
    username = forms.CharField(max_length=150, required=False)
    first_name = forms.CharField(max_length=80, required=False)
    last_name = forms.CharField(max_length=80, required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm Password", widget=forms.PasswordInput)

    def __init__(self, *args, purpose=EmployeeInvite.Purpose.ONBOARDING, **kwargs):
        super().__init__(*args, **kwargs)
        self.purpose = purpose
        if purpose == EmployeeInvite.Purpose.PASSWORD_RESET:
            self.fields.pop("username")
            self.fields.pop("first_name")
            self.fields.pop("last_name")
        else:
            self.fields["username"].required = True
            self.fields["first_name"].required = True
            self.fields["last_name"].required = True

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if username and get_user_model().objects.filter(username__iexact=username).exists():
            raise ValidationError("That username is already in use.")
        return username

    def clean_password1(self):
        password = self.cleaned_data["password1"]
        validate_password(password)
        return password

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password1") != cleaned.get("password2"):
            raise ValidationError("Passwords do not match.")
        return cleaned


class ClientInviteAcceptForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        disabled=True,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    username = forms.CharField(
        max_length=150,
        label="Username",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "placeholder": "Create a unique username or use your email address",
            }
        ),
    )
    first_name = forms.CharField(
        max_length=80,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}),
    )
    last_name = forms.CharField(
        max_length=80,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise ValidationError("That username is already in use.")
        return username

    def clean_password1(self):
        password = self.cleaned_data["password1"]
        validate_password(password)
        return password

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password1") != cleaned.get("password2"):
            raise ValidationError("Passwords do not match.")
        return cleaned


class PublicAuthenticationForm(AuthenticationForm):
    def clean(self):
        if not verify_turnstile_request(self.request, expected_action="login"):
            raise ValidationError(TURNSTILE_ERROR_MESSAGE)
        return super().clean()

    def confirm_login_allowed(self, user):
        if user.is_superuser:
            if not is_mobile_owner_login(self.request):
                raise self.get_invalid_login_error()
            if not (user.is_active and user.is_staff):
                raise self.get_invalid_login_error()
            return
        if user.is_staff:
            is_employee = user.groups.filter(name__in=["Manager", "Office", "Field", "Sales"]).exists()
            has_active_profile = not EmployeeProfile.objects.filter(user=user, is_active=False).exists()
            if not is_employee or not has_active_profile:
                raise self.get_invalid_login_error()
        elif not (
            Client.objects.filter(user=user).exists()
            or Subcontractor.objects.filter(
                portal_user=user,
                status=Subcontractor.Status.ACTIVE,
            ).exists()
        ):
            raise self.get_invalid_login_error()
        super().confirm_login_allowed(user)


class PublicPasswordResetForm(PasswordResetForm):
    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if not self.errors and not verify_turnstile_request(self.request, expected_action="password-reset"):
            raise ValidationError(TURNSTILE_ERROR_MESSAGE)
        return cleaned

    def get_users(self, email):
        user_model = get_user_model()
        eligible_users = user_model._default_manager.filter(
            email__iexact=email,
            is_active=True,
        ).filter(
            Q(
                is_staff=True,
                is_superuser=False,
                groups__name__in=("Manager", "Office", "Field", "Sales"),
            )
            | Q(is_staff=False, client_record__isnull=False)
            | Q(
                is_staff=False,
                subcontractor_profile__status=Subcontractor.Status.ACTIVE,
            )
        ).exclude(employee_profile__is_active=False).distinct()
        return (user for user in eligible_users if user.has_usable_password())


class AdminPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        user_model = get_user_model()
        eligible_users = user_model._default_manager.filter(
            email__iexact=email,
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        return (user for user in eligible_users if user.has_usable_password())


class GrandCoastAdminAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        if not (user.is_active and user.is_staff and user.is_superuser):
            raise self.get_invalid_login_error()


class AdminGateIdentifierForm(forms.Form):
    identifier = forms.CharField(
        label="Admin username or email",
        max_length=254,
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )


class AdminPinForm(forms.Form):
    pin = forms.CharField(
        label="Admin PIN",
        max_length=6,
        min_length=6,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code"}),
    )

    def clean_pin(self):
        pin = self.cleaned_data["pin"]
        if not re.fullmatch(r"\d{6}", pin):
            raise ValidationError("Enter the six-digit PIN.")
        return pin


class AdminOtpForm(forms.Form):
    token = forms.CharField(
        label="Authenticator code",
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code"}),
    )

    def clean_token(self):
        token = self.cleaned_data["token"]
        if not re.fullmatch(r"\d{6}", token):
            raise ValidationError("Enter the six-digit authenticator code.")
        return token


class AdminRecoveryRequestForm(forms.Form):
    email = forms.EmailField(
        label="Admin email",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )


class AdminRecoveryResetForm(forms.Form):
    new_pin = forms.CharField(
        label="New six-digit admin PIN",
        max_length=6,
        min_length=6,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
    )
    new_pin_confirmation = forms.CharField(
        label="Confirm new PIN",
        max_length=6,
        min_length=6,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
    )

    def clean_new_pin(self):
        pin = self.cleaned_data["new_pin"]
        if not re.fullmatch(r"\d{6}", pin):
            raise ValidationError("Enter six numeric digits.")
        return pin

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("new_pin") and cleaned.get("new_pin") != cleaned.get("new_pin_confirmation"):
            raise ValidationError("The new PINs do not match.")
        return cleaned


class AdminPinSettingsForm(forms.Form):
    pin_enabled = forms.BooleanField(label="Enable an admin PIN", required=False)
    new_pin = forms.CharField(
        label="New admin PIN",
        required=False,
        max_length=6,
        help_text="Leave blank to keep the current PIN when editing.",
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
    )
    new_pin_confirmation = forms.CharField(
        label="Confirm new admin PIN",
        required=False,
        max_length=6,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
    )

    def __init__(self, *args, profile, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile = profile
        self.fields["pin_enabled"].initial = self.profile.pin_enabled

    def clean_new_pin(self):
        pin = self.cleaned_data.get("new_pin", "")
        if pin and not re.fullmatch(r"\d{6}", pin):
            raise ValidationError("Enter six numeric digits.")
        return pin

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("pin_enabled") and not self.profile.pin_hash and not cleaned.get("new_pin"):
            self.add_error("new_pin", "Enter a PIN before turning PIN protection on.")
        if cleaned.get("new_pin") != cleaned.get("new_pin_confirmation"):
            self.add_error("new_pin_confirmation", "The new PINs do not match.")
        return cleaned


class AdminTwoFactorStartForm(forms.Form):
    """Simple action form used for starting or disabling authenticator setup."""

    pass


class AdminIPBlockForm(forms.Form):
    ip_address = forms.GenericIPAddressField(
        label="IP address",
        protocol="both",
        unpack_ipv4=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "inputmode": "decimal",
                "placeholder": "198.51.100.24",
            }
        ),
    )
    reason = forms.CharField(
        label="Reason",
        max_length=220,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Repeated admin sign-in failures",
            }
        ),
    )


class AdminUserBlockForm(forms.Form):
    user = forms.ModelChoiceField(
        label="Administrator",
        queryset=get_user_model().objects.none(),
        empty_label="Choose an administrator",
    )
    reason = forms.CharField(
        label="Reason",
        max_length=220,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "Protect this administrator account",
            }
        ),
    )

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        if current_user is not None:
            queryset = queryset.exclude(pk=current_user.pk)
        self.fields["user"].queryset = queryset.order_by("username")
        self.fields["user"].label_from_instance = user_choice_label


class AdminSecurityEventFilterForm(forms.Form):
    q = forms.CharField(
        label="Search",
        max_length=254,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "IP, username, email, or user agent",
            }
        ),
    )
    event_type = forms.ChoiceField(
        label="Event",
        required=False,
        choices=[("", "All events"), *AdminSecurityEvent.EventType.choices],
    )
    outcome = forms.ChoiceField(
        label="Outcome",
        required=False,
        choices=[("", "All outcomes"), *AdminSecurityEvent.Outcome.choices],
    )
    review = forms.ChoiceField(
        label="Review",
        required=False,
        choices=[
            ("", "All review states"),
            ("unreviewed", "Unreviewed"),
            ("reviewed", "Reviewed"),
        ],
    )
    date_from = forms.DateField(
        label="From",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        label="To",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}),
    )
