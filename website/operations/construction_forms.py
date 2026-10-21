from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from .models import (
    Blocker,
    ChangeOrder,
    Commitment,
    CostEntry,
    DailyReport,
    Estimate,
    Inspection,
    MaterialRequest,
    PaymentRecord,
    PaymentSchedule,
    Permit,
    PreconstructionItem,
    ProblemReport,
    Selection,
    SiteVisit,
    Subcontractor,
    SubcontractorAssignment,
    Task,
    WarrantyItem,
)


def _clean_https_url(value, label):
    value = (value or "").strip()
    if not value:
        return ""
    URLValidator()(value)
    if not value.lower().startswith("https://"):
        raise ValidationError({label: "Use an HTTPS link."})
    return value


class ExternalEstimateStatusForm(forms.ModelForm):
    class Meta:
        model = Estimate
        fields = [
            "external_url",
            "external_status",
            "external_client_visible",
        ]
        labels = {
            "external_url": "Estimate link",
            "external_status": "Status",
            "external_client_visible": "Publish estimate link to client portal",
        }

    def clean_external_url(self):
        return _clean_https_url(self.cleaned_data.get("external_url"), "external_url")

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("external_status")
        link = cleaned.get("external_url") or getattr(self.instance, "external_url", "")
        if status and status != Estimate.ExternalEstimateStatus.NOT_STARTED and not link:
            self.add_error("external_url", "Add the estimate link before recording this status.")
        return cleaned

class ExternalInvoiceStatusForm(forms.ModelForm):
    class Meta:
        model = PaymentSchedule
        fields = [
            "external_invoice_url",
            "external_invoice_status",
            "external_client_visible",
        ]
        labels = {
            "external_invoice_url": "Invoice or payment link",
            "external_invoice_status": "Status",
            "external_client_visible": "Publish invoice link to client portal",
        }

    def clean_external_invoice_url(self):
        return _clean_https_url(self.cleaned_data.get("external_invoice_url"), "external_invoice_url")


class SiteVisitForm(forms.ModelForm):
    class Meta:
        model = SiteVisit
        fields = [
            "assigned_to",
            "scheduled_at",
            "address",
            "scope",
            "measurements",
            "client_requests",
            "existing_conditions",
            "potential_additional_work",
            "notes",
        ]
        widgets = {
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "scope": forms.Textarea(attrs={"rows": 4}),
            "measurements": forms.Textarea(attrs={"rows": 3}),
            "client_requests": forms.Textarea(attrs={"rows": 3}),
            "existing_conditions": forms.Textarea(attrs={"rows": 3}),
            "potential_additional_work": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class BlockerForm(forms.ModelForm):
    class Meta:
        model = Blocker
        fields = ["title", "description", "category", "severity", "assigned_to", "due_date"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }


class PreconstructionItemForm(forms.ModelForm):
    class Meta:
        model = PreconstructionItem
        fields = ["owner", "due_date", "notes"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, owner_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if owner_queryset is not None:
            self.fields["owner"].queryset = owner_queryset


class ChangeOrderForm(forms.ModelForm):
    class Meta:
        model = ChangeOrder
        fields = ["title", "description", "price_impact", "schedule_impact_days"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "price_impact": forms.NumberInput(attrs={"step": "0.01"}),
            "schedule_impact_days": forms.NumberInput(attrs={"min": "0", "step": "1"}),
        }

    def clean_price_impact(self):
        value = self.cleaned_data["price_impact"]
        if abs(value) > Decimal("9999999999.99"):
            raise ValidationError("Price impact is outside the supported range.")
        return value

    def clean_schedule_impact_days(self):
        value = self.cleaned_data["schedule_impact_days"]
        if value < 0:
            raise ValidationError("Schedule impact cannot be negative.")
        return value


class SelectionForm(forms.ModelForm):
    class Meta:
        model = Selection
        fields = [
            "category",
            "item_name",
            "description",
            "vendor",
            "allowance",
            "client_choice",
            "due_date",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "client_choice": forms.Textarea(attrs={"rows": 3}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "allowance": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
        }


class PaymentRecordForm(forms.ModelForm):
    class Meta:
        model = PaymentRecord
        fields = ["schedule", "amount", "received_on", "method", "reference", "notes"]
        widgets = {
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, schedule_queryset=None, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None and self.instance.project_id is None:
            self.instance.project = project
        if schedule_queryset is not None:
            self.fields["schedule"].queryset = schedule_queryset

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value <= 0:
            raise ValidationError("Payment amount must be greater than zero.")
        return value


class DailyReportForm(forms.ModelForm):
    class Meta:
        model = DailyReport
        fields = [
            "report_date",
            "summary",
            "work_completed",
            "labor_count",
            "hours_worked",
            "weather",
            "equipment",
            "notes",
        ]
        widgets = {
            "report_date": forms.DateInput(attrs={"type": "date"}),
            "summary": forms.Textarea(attrs={"rows": 4}),
            "work_completed": forms.Textarea(attrs={"rows": 4}),
            "equipment": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "hours_worked": forms.NumberInput(attrs={"min": "0", "step": "0.25"}),
        }


class MaterialRequestForm(forms.ModelForm):
    class Meta:
        model = MaterialRequest
        fields = ["description", "quantity", "needed_by", "vendor", "notes"]
        widgets = {
            "needed_by": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class MaterialRequestStatusForm(forms.Form):
    status = forms.ChoiceField(choices=MaterialRequest.Status.choices)


class ProblemReportForm(forms.ModelForm):
    class Meta:
        model = ProblemReport
        fields = ["title", "description", "severity"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class InspectionResultForm(forms.ModelForm):
    class Meta:
        model = Inspection
        fields = ["status", "result_notes", "corrective_action", "rescheduled_at"]
        widgets = {
            "result_notes": forms.Textarea(attrs={"rows": 3}),
            "corrective_action": forms.Textarea(attrs={"rows": 3}),
            "rescheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class InspectionForm(forms.ModelForm):
    class Meta:
        model = Inspection
        fields = ["inspection_type", "permit", "scheduled_at"]
        widgets = {
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, permit_queryset=None, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None and self.instance.project_id is None:
            self.instance.project = project
        if permit_queryset is not None:
            self.fields["permit"].queryset = permit_queryset


class PermitForm(forms.ModelForm):
    class Meta:
        model = Permit
        fields = ["permit_type", "jurisdiction", "permit_number", "status", "expires_at", "notes"]
        widgets = {
            "expires_at": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [
            (Permit.Status.PENDING, "Pending"),
            (Permit.Status.SUBMITTED, "Submitted"),
        ]


class PermitStatusForm(forms.Form):
    status = forms.ChoiceField(choices=Permit.Status.choices)
    permit_number = forms.CharField(max_length=100, required=False)
    expires_at = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class SelectionAdvanceForm(forms.Form):
    status = forms.ChoiceField(choices=Selection.Status.choices)
    client_choice = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class CostEntryForm(forms.ModelForm):
    class Meta:
        model = CostEntry
        fields = ["budget_line", "description", "vendor", "amount", "incurred_on", "source"]
        widgets = {
            "incurred_on": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01"}),
        }

    def __init__(self, *args, budget_line_queryset=None, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None and self.instance.project_id is None:
            self.instance.project = project
        if budget_line_queryset is not None:
            self.fields["budget_line"].queryset = budget_line_queryset


class CommitmentForm(forms.ModelForm):
    class Meta:
        model = Commitment
        fields = ["budget_line", "subcontractor", "description", "amount", "status", "due_date"]
        widgets = {
            "amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(
        self,
        *args,
        budget_line_queryset=None,
        subcontractor_queryset=None,
        project=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if project is not None and self.instance.project_id is None:
            self.instance.project = project
        if budget_line_queryset is not None:
            self.fields["budget_line"].queryset = budget_line_queryset
        if subcontractor_queryset is not None:
            self.fields["subcontractor"].queryset = subcontractor_queryset
        if self.instance._state.adding:
            self.fields["status"].choices = [
                (Commitment.Status.PLANNED, "Planned"),
                (Commitment.Status.COMMITTED, "Committed"),
            ]

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value <= 0:
            raise ValidationError("Commitment amount must be greater than zero.")
        return value


class CommitmentStatusForm(forms.Form):
    status = forms.ChoiceField(choices=Commitment.Status.choices)


class CloseoutActionForm(forms.Form):
    status = forms.ChoiceField(choices=[
        ("complete", "Complete"),
        ("not_applicable", "Not applicable"),
    ])
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class WarrantyResolutionForm(forms.Form):
    status = forms.ChoiceField(choices=[
        (WarrantyItem.Status.RESOLVED, "Resolved"),
        (WarrantyItem.Status.CLOSED, "Closed"),
    ])
    resolution = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class SubcontractorAssignmentForm(forms.ModelForm):
    class Meta:
        model = SubcontractorAssignment
        fields = [
            "subcontractor",
            "task",
            "work_package",
            "scope",
            "start_date",
            "end_date",
            "status",
            "notes",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "scope": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, project=None, subcontractor_queryset=None, task_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if subcontractor_queryset is not None:
            self.fields["subcontractor"].queryset = subcontractor_queryset
        else:
            self.fields["subcontractor"].queryset = Subcontractor.objects.filter(status=Subcontractor.Status.ACTIVE)
        if task_queryset is not None:
            self.fields["task"].queryset = task_queryset
        elif project is not None:
            self.fields["task"].queryset = project.tasks.all()


class AssignmentStatusForm(forms.Form):
    status = forms.ChoiceField(choices=SubcontractorAssignment.Status.choices)


class ProjectTaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "milestone", "assigned_to", "status", "priority", "due_date"]
        labels = {"assigned_to": "Assigned to"}
        help_texts = {"assigned_to": "Select every employee responsible for completing this task."}
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "assigned_to": forms.SelectMultiple(attrs={
                "size": 4,
                "data-worker-selector": "assignees",
                "aria-label": "Task assignees",
            }),
        }

    def __init__(self, *args, project=None, staff_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.instance.project = project
            self.fields["milestone"].queryset = project.milestones.all()
        if staff_queryset is not None:
            self.fields["assigned_to"].queryset = staff_queryset
        self.fields["assigned_to"].label_from_instance = (
            lambda user: user.get_full_name().strip() or user.get_username()
        )


class TaskStatusForm(forms.Form):
    status = forms.ChoiceField(choices=Task.Status.choices)
