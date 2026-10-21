from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import get_valid_filename

from .storage import ContactSubmissionStorage


MAX_UPLOAD_SIZE = 50 * 1024 * 1024
CALENDAR_TIME_ZONE = ZoneInfo('America/Los_Angeles')
CONTACT_MAX_FILES = 8
CONTACT_MAX_TOTAL_SIZE = 100 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".mp4",
    ".mov",
    ".webm",
}


def validate_uploaded_media(upload):
    if not upload:
        return
    if upload.size > MAX_UPLOAD_SIZE:
        raise ValidationError("Files must be 50 MB or smaller.")
    name = upload.name.lower()
    if not any(name.endswith(extension) for extension in ALLOWED_UPLOAD_EXTENSIONS):
        raise ValidationError("Upload a JPG, PNG, WEBP, GIF, HEIC, MP4, MOV, or WEBM file.")


ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
}
ALLOWED_CONTACT_UPLOAD_EXTENSIONS = ALLOWED_UPLOAD_EXTENSIONS | ALLOWED_DOCUMENT_EXTENSIONS
CONTACT_UPLOAD_ACCEPT = ",".join(sorted(ALLOWED_CONTACT_UPLOAD_EXTENSIONS))


def sanitize_uploaded_name(name):
    filename = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return get_valid_filename(filename) or "uploaded-file"


def _read_upload_header(upload, max_bytes=512):
    try:
        position = upload.tell()
    except (AttributeError, OSError):
        position = 0
    try:
        upload.seek(0)
        return upload.read(max_bytes) or b""
    except (AttributeError, OSError):
        return b""
    finally:
        try:
            upload.seek(position)
        except (AttributeError, OSError):
            pass


def _contact_file_signature_matches(upload, extension):
    header = _read_upload_header(upload)
    if extension in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if extension == ".heic":
        return b"ftyp" in header[:32] and any(
            brand in header[:64] for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
        )
    if extension in {".mp4", ".mov"}:
        return b"ftyp" in header[:32]
    if extension == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    if extension == ".pdf":
        return header.startswith(b"%PDF-")
    if extension in {".doc", ".xls"}:
        return header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if extension in {".docx", ".xlsx"}:
        return header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if extension in {".csv", ".txt"}:
        return b"\x00" not in header
    return False


def validate_contact_upload(upload):
    if not upload:
        return
    if upload.size > MAX_UPLOAD_SIZE:
        raise ValidationError("Files must be 50 MB or smaller.")
    extension = Path(str(upload.name)).suffix.lower()
    if extension not in ALLOWED_CONTACT_UPLOAD_EXTENSIONS:
        raise ValidationError(
            "Upload a JPG, PNG, WEBP, GIF, HEIC, MP4, MOV, WEBM, PDF, DOC, DOCX, XLS, XLSX, CSV, or TXT file."
        )
    if not _contact_file_signature_matches(upload, extension):
        raise ValidationError("The file contents do not match the selected file type.")


def validate_uploaded_document(upload):
    if not upload:
        return
    if upload.size > MAX_UPLOAD_SIZE:
        raise ValidationError("Documents must be 50 MB or smaller.")
    name = upload.name.lower()
    if not any(name.endswith(extension) for extension in ALLOWED_DOCUMENT_EXTENSIONS):
        raise ValidationError("Upload a PDF, DOC, DOCX, XLS, XLSX, CSV, or TXT file.")


def validate_construction_document(upload):
    validate_uploaded_document(upload)
    extension = Path(str(upload.name)).suffix.lower()
    if not _contact_file_signature_matches(upload, extension):
        raise ValidationError("The file contents do not match the selected document type.")


def validate_signed_pdf(upload):
    if not upload:
        return
    validate_construction_document(upload)
    if Path(str(upload.name)).suffix.lower() != ".pdf":
        raise ValidationError("Signed agreement files must be PDF documents.")


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Client(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    company = models.CharField(max_length=160, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=40, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_record",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

class ClientInvite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="invites")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_client_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_usable(self):
        from django.utils import timezone

        return self.accepted_at is None and self.expires_at > timezone.now()


class Lead(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        QUALIFIED = "qualified", "Qualified"
        QUOTED = "quoted", "Quoted"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    class WorkflowStage(models.TextChoices):
        NEW = "new", "New lead"
        CONTACTED = "contacted", "Contacted"
        SITE_VISIT = "site_visit", "Site visit"
        ESTIMATING = "estimating", "Estimating"
        PROPOSAL_SENT = "proposal_sent", "Proposal sent"
        FOLLOW_UP = "follow_up", "Follow-up"
        APPROVED = "approved", "Approved"
        DEPOSIT = "deposit", "Deposit"
        SOLD_SCHEDULED = "sold_scheduled", "Sold / scheduled"
        ON_HOLD = "on_hold", "On hold"
        LOST = "lost", "Lost"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_leads",
    )
    name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=40, blank=True)
    service = models.CharField(max_length=120)
    location = models.CharField(max_length=160)
    budget = models.CharField(max_length=120, blank=True)
    budget_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    timeline = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=120, default="Website form")
    note = models.TextField(blank=True)
    internal_note = models.TextField(blank=True, default="", verbose_name="Internal admin note")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    workflow_stage = models.CharField(
        max_length=24,
        choices=WorkflowStage.choices,
        default=WorkflowStage.NEW,
        db_index=True,
    )
    next_action = models.CharField(max_length=220, blank=True)
    next_action_due = models.DateField(null=True, blank=True)
    address_line1 = models.CharField(max_length=180, blank=True)
    address_line2 = models.CharField(max_length=180, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=40, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    priority = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_leads",
    )
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deleted_leads",
    )

    class Meta:
        ordering = ["-priority", "-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return self.name

    @property
    def follow_ups(self):
        "Compatibility accessor for older dashboard templates and integrations."
        return self.tasks


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        COMPLETE = "complete", "Complete"

    class Priority(models.TextChoices):
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    client = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    project = models.ForeignKey(
        "Project",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    milestone = models.ForeignKey(
        "Milestone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    assigned_to = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_tasks",
    )
    watchers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="watched_tasks",
        verbose_name="supervisors",
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.NORMAL)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_tasks",
    )

    class Meta:
        ordering = ["status", "due_date", "-created_at"]
        permissions = [
            ("manage_team_tasks", "Can manage team task assignments"),
        ]

    def clean(self):
        if self.milestone_id and (
            not self.project_id or self.milestone.project_id != self.project_id
        ):
            raise ValidationError('The selected milestone must belong to the selected project.')
        if self.client_id and self.project_id and self.project.client_id != self.client_id:
            raise ValidationError("The selected project must belong to the selected client.")
        if not self.lead_id and not self.client_id and not self.project_id:
            raise ValidationError("A task must be attached to a lead, client, or project.")
        if self.milestone_id and self.project_id and self.milestone.project_id != self.project_id:
            raise ValidationError("The selected milestone must belong to the selected project.")

    @property
    def completed(self):
        return self.status == self.Status.COMPLETE

    def __str__(self):
        return self.title


class Estimate(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"

    class Kind(models.TextChoices):
        PRELIMINARY = "preliminary", "Preliminary budget"
        FINAL = "final", "Final estimate"

    class ExternalEstimateStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SENT = "sent", "Sent"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.PositiveIntegerField(unique=True)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="estimates")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="estimates")
    title = models.CharField(max_length=180)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True)
    revision_number = models.PositiveIntegerField(default=1)
    revision_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revisions",
    )
    estimate_kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.FINAL)
    exclusions = models.TextField(blank=True)
    assumptions = models.TextField(blank=True)
    timeline_summary = models.CharField(max_length=220, blank=True)
    warranty_terms = models.TextField(blank=True)
    payment_schedule = models.JSONField(default=list, blank=True)
    # These fields are deliberately a small external-status layer. Detailed
    # estimate content, signatures, invoices, and payment collection remain
    # in the selected third-party estimate service and are not copied here.
    external_url = models.URLField(max_length=500, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)
    external_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    external_deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    external_status = models.CharField(
        max_length=16,
        choices=ExternalEstimateStatus.choices,
        default=ExternalEstimateStatus.NOT_STARTED,
    )
    external_status_at = models.DateTimeField(null=True, blank=True)
    external_status_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_external_estimate_statuses",
    )
    external_status_note = models.TextField(blank=True)
    external_client_visible = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_estimates",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_estimates",
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "locked_at",
                "number",
                "lead_id",
                "client_id",
                "title",
                "status",
                "deposit_amount",
                "estimate_kind",
                "exclusions",
                "assumptions",
                "timeline_summary",
                "warranty_terms",
                "payment_schedule",
                "sent_at",
                "accepted_at",
                "declined_at",
                "accepted_by_id",
            ).first()
            if old and old["locked_at"] is not None:
                protected = (
                    "locked_at",
                    "number",
                    "lead_id",
                    "client_id",
                    "title",
                    "status",
                    "deposit_amount",
                    "estimate_kind",
                    "exclusions",
                    "assumptions",
                    "timeline_summary",
                    "warranty_terms",
                    "payment_schedule",
                    "sent_at",
                    "accepted_at",
                    "declined_at",
                    "accepted_by_id",
                )
                if any(getattr(self, field) != old[field] for field in protected):
                    raise ValidationError("Accepted estimates are immutable; create a revision instead.")
        if not self.number:
            last_number = Estimate.objects.order_by("-number").values_list("number", flat=True).first()
            self.number = max(last_number or 1000, 1000) + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.locked_at is not None:
            raise ValidationError("Accepted estimates cannot be deleted.")
        return super().delete(*args, **kwargs)

    @property
    def total(self):
        return sum((item.line_total for item in self.line_items.all()), Decimal("0.00"))

    def __str__(self):
        return f"#{self.number} {self.title}"


class EstimateLineItem(models.Model):
    class Category(models.TextChoices):
        LABOR = "labor", "Labor"
        MATERIALS = "materials", "Materials"
        SUBCONTRACTOR = "subcontractor", "Subcontractor"
        ALLOWANCE = "allowance", "Allowance"
        OWNER_PROVIDED = "owner_provided", "Owner-provided"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=180)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    unit = models.CharField(max_length=40, blank=True)
    category = models.CharField(max_length=24, choices=Category.choices, default=Category.OTHER)
    cost_code = models.CharField(max_length=40, blank=True)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    owner_provided = models.BooleanField(default=False)
    is_allowance = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values(
                "estimate_id",
                "description",
                "quantity",
                "unit_price",
                "unit",
                "category",
                "cost_code",
                "estimated_cost",
                "owner_provided",
                "is_allowance",
                "notes",
                "sort_order",
            ).first()
            locked = Estimate.objects.filter(
                pk=self.estimate_id,
                locked_at__isnull=False,
            ).exists()
            protected = (
                "estimate_id",
                "description",
                "quantity",
                "unit_price",
                "unit",
                "category",
                "cost_code",
                "estimated_cost",
                "owner_provided",
                "is_allowance",
                "notes",
                "sort_order",
            )
            if locked and (
                old is None
                or any(getattr(self, field) != old[field] for field in protected)
            ):
                raise ValidationError("Line items for an accepted estimate are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if Estimate.objects.filter(pk=self.estimate_id, locked_at__isnull=False).exists():
            raise ValidationError("Line items for an accepted estimate cannot be deleted.")
        return super().delete(*args, **kwargs)

    @property
    def line_total(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    def clean(self):
        if self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})
        if self.unit_price < 0:
            raise ValidationError({"unit_price": "Unit price cannot be negative."})

    def __str__(self):
        return self.description


class Project(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        SELECTIONS = "selections", "Selections"
        CONSTRUCTION = "construction", "Construction"
        FINAL = "final", "Final walkthrough"
        COMPLETE = "complete", "Complete"
        ON_HOLD = "on_hold", "On hold"

    class OperationalPhase(models.TextChoices):
        PRECONSTRUCTION = "preconstruction", "Preconstruction"
        CONSTRUCTION = "construction", "Construction"
        CLOSEOUT = "closeout", "Closeout"
        WARRANTY = "warranty", "Warranty"

    class HealthStatus(models.TextChoices):
        ON_TRACK = "on_track", "On track"
        WATCH = "watch", "Watch"
        AT_RISK = "at_risk", "At risk"
        BLOCKED = "blocked", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    estimate = models.ForeignKey(Estimate, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    assigned_staff = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_projects",
    )
    title = models.CharField(max_length=180)
    location = models.CharField(max_length=160, blank=True)
    project_type = models.CharField(max_length=80, default="renovation")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PLANNING)
    admin_stage_note = models.CharField(
        max_length=30,
        choices=Status.choices,
        blank=True,
        default="",
    )
    project_code = models.CharField(max_length=32, blank=True, db_index=True)
    operational_phase = models.CharField(
        max_length=20,
        choices=OperationalPhase.choices,
        default=OperationalPhase.PRECONSTRUCTION,
        db_index=True,
    )
    health_status = models.CharField(
        max_length=16,
        choices=HealthStatus.choices,
        default=HealthStatus.ON_TRACK,
        db_index=True,
    )
    health_note = models.CharField(max_length=240, blank=True)
    address_line1 = models.CharField(max_length=180, blank=True)
    address_line2 = models.CharField(max_length=180, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=40, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    project_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_construction_projects",
    )
    construction_ready_at = models.DateTimeField(null=True, blank=True)
    next_step = models.CharField(max_length=180, blank=True)
    summary = models.TextField(blank=True)
    is_published = models.BooleanField(default=True)
    cover = models.FileField(upload_to="projects/covers/", blank=True, validators=[validate_uploaded_media])
    fallback_image = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_projects",
    )

    class Meta:
        ordering = ["-updated_at", "title"]

    def __str__(self):
        return self.title

    @property
    def progress_percent(self):
        milestones = list(self.milestones.all())
        if not milestones:
            return 0
        return round(sum(1 for milestone in milestones if milestone.is_complete) / len(milestones) * 100)


    @property
    def needs_staff_assignment(self):
        return (
            self.status != self.Status.COMPLETE
            and not self.assigned_staff.exists()
        )

class Milestone(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=120)
    sort_order = models.PositiveIntegerField(default=0)
    is_complete = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title


class ProjectUpdate(models.Model):
    class Visibility(models.TextChoices):
        CLIENT = "client", "Client-visible"
        INTERNAL = "internal", "Internal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="updates")
    title = models.CharField(max_length=180)
    body = models.TextField()
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.CLIENT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="project_updates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ProjectUpdateDraft(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_update = models.OneToOneField(
        ProjectUpdate,
        on_delete=models.CASCADE,
        related_name="client_draft",
    )
    published_update = models.OneToOneField(
        ProjectUpdate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_draft_record",
    )
    title = models.CharField(max_length=180)
    body = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_project_update_drafts",
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_project_update_drafts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]


class MediaAsset(models.Model):
    class MediaType(models.TextChoices):
        PHOTO = "photo", "Photo"
        VIDEO = "video", "Video"

    class Context(models.TextChoices):
        PROGRESS = "progress", "Progress"
        EXISTING_CONDITION = "existing_condition", "Existing condition"
        SITE_VISIT = "site_visit", "Site visit"
        DAILY_REPORT = "daily_report", "Daily report"
        PROBLEM_REPORT = "problem_report", "Problem report"
        CHANGE_ORDER = "change_order", "Change order"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        CLIENT = "client", "Client-only"
        INTERNAL = "internal", "Internal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="media_assets")
    site_visit = models.ForeignKey(
        "SiteVisit",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="media_assets",
    )
    title = models.CharField(max_length=180)
    file = models.FileField(upload_to="projects/media/%Y/%m/", blank=True, validators=[validate_uploaded_media])
    media_type = models.CharField(max_length=10, choices=MediaType.choices, default=MediaType.PHOTO)
    context = models.CharField(max_length=24, choices=Context.choices, default=Context.PROGRESS)
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.INTERNAL)
    caption = models.TextField(blank=True)
    fallback_image = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_media",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class LeadAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(
        storage=ContactSubmissionStorage,
        upload_to="contact-form/%Y/%m/",
        validators=[validate_contact_upload],
    )
    original_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ClientMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="messages")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    body = models.TextField()
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_client_messages",
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Activity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.CharField(max_length=220)
    detail = models.CharField(max_length=220, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="operations_activity",
    )
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    estimate = models.ForeignKey(Estimate, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class SiteSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    headline = models.CharField(max_length=180, default="Build with confidence.")
    subheadline = models.TextField(
        default="Thoughtful construction, clear communication, and a better experience from first walkthrough to final handoff."
    )
    google_review_url = models.URLField(blank=True, max_length=500)
    google_places_display_name = models.CharField(max_length=180, blank=True)
    google_places_rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    google_places_review_count = models.PositiveIntegerField(null=True, blank=True)
    google_reviews_synced_at = models.DateTimeField(null=True, blank=True)
    featured_title = models.CharField(max_length=180, default="Coastal Bathroom Renovation")
    featured_body = models.TextField(
        default="A calm, highly functional renovation shaped around durable materials, thoughtful storage, and the small details that make a space feel finished."
    )
    featured_project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="featured_on_site",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Grand Coast site settings"


class GoogleReview(models.Model):
    provider_review_id = models.CharField(max_length=512, unique=True)
    place_id = models.CharField(max_length=255)
    author_name = models.CharField(max_length=180, default="Google user")
    author_uri = models.URLField(blank=True, max_length=1000)
    author_photo_uri = models.URLField(blank=True, max_length=2000)
    rating = models.DecimalField(max_digits=2, decimal_places=1)
    review_text = models.TextField(blank=True)
    publish_time = models.DateTimeField(null=True, blank=True)
    google_maps_uri = models.URLField(blank=True, max_length=2000)
    display_order = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "-publish_time", "pk"]
        indexes = [models.Index(fields=["place_id", "display_order"])]

    def __str__(self):
        return f"{self.author_name} · {self.rating} stars"


class Service(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField()
    image_path = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title


class ProcessStep(models.Model):
    key = models.SlugField(max_length=40, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField()
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return self.title


class EmployeeProfile(TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee_profile",
    )
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["user__first_name", "user__last_name", "user__username"]

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class EmployeeInvite(models.Model):
    class Purpose(models.TextChoices):
        ONBOARDING = "onboarding", "Onboarding"
        PASSWORD_RESET = "password_reset", "Password reset"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        EmployeeProfile,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="access_invites",
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    group = models.ForeignKey(
        "auth.Group",
        on_delete=models.PROTECT,
        related_name="employee_invites",
    )
    purpose = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.ONBOARDING)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_employee_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_usable(self):
        from django.utils import timezone

        return self.accepted_at is None and self.expires_at > timezone.now()


class ScheduleEvent(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=180)
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="schedule_events",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="schedule_events",
    )
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="schedule_events",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    location = models.CharField(max_length=180, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_schedule_events",
    )

    class Meta:
        ordering = ["start_at"]

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError("Schedule events must end after they start.")
        if self.task_id and self.task.project_id and (
            not self.project_id or self.task.project_id != self.project_id
        ):
            raise ValidationError("The selected task must belong to the selected project.")
        if self.start_at and self.end_at:
            conflicting_overrides = _calendar_overrides_conflicting_with_event(self)
            if conflicting_overrides:
                conflict_labels = ', '.join(
                    f'{override.date.isoformat()} ({override.get_status_display()})'
                    for override in conflicting_overrides
                )
                raise ValidationError(
                    {
                        'start_at': (
                            'This event conflicts with the following calendar day override(s): '
                            f'{conflict_labels}.'
                        )
                    }
                )

    def __str__(self):
        return self.title


class CalendarDayOverride(TimeStampedModel):
    class Status(models.TextChoices):
        SHORT = 'short', 'Short day'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    date = models.DateField(unique=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    short_start = models.TimeField(null=True, blank=True)
    short_end = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=180, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_calendar_day_overrides',
    )

    class Meta:
        ordering = ['date']

    def clean(self):
        errors = {}
        if self.status == self.Status.SHORT:
            if not self.short_start:
                errors['short_start'] = 'Short days need a start time.'
            if not self.short_end:
                errors['short_end'] = 'Short days need an end time.'
            if self.short_start and self.short_end and self.short_end <= self.short_start:
                errors['short_end'] = 'The short-day end time must be after its start time.'
        elif self.short_start or self.short_end:
            errors['short_start'] = 'Only short days can have custom hours.'

        if errors:
            raise ValidationError(errors)

        if self.date and self.status:
            conflicting_events = _calendar_events_conflicting_with_override(self)
            if conflicting_events:
                event_labels = ', '.join(event.title for event in conflicting_events[:5])
                if len(conflicting_events) > 5:
                    event_labels += ', ...'
                raise ValidationError(
                    {
                        'date': (
                            'Resolve these scheduled events before applying this day override: '
                            f'{event_labels}.'
                        )
                    }
                )

    def __str__(self):
        return f'{self.date.isoformat()} - {self.get_status_display()}'


class EmployeeWeeklySchedule(TimeStampedModel):
    class Weekday(models.IntegerChoices):
        MONDAY = 0, 'Monday'
        TUESDAY = 1, 'Tuesday'
        WEDNESDAY = 2, 'Wednesday'
        THURSDAY = 3, 'Thursday'
        FRIDAY = 4, 'Friday'
        SATURDAY = 5, 'Saturday'
        SUNDAY = 6, 'Sunday'

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='weekly_schedules',
    )
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    is_working = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ['employee', 'weekday']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'weekday'],
                name='unique_employee_weekday_schedule',
            ),
        ]

    def clean(self):
        errors = {}
        if self.is_working:
            if not self.start_time:
                errors['start_time'] = 'Working days need a start time.'
            if not self.end_time:
                errors['end_time'] = 'Working days need an end time.'
            if self.start_time and self.end_time and self.end_time <= self.start_time:
                errors['end_time'] = 'The end time must be after the start time.'
        elif self.start_time or self.end_time:
            errors['start_time'] = 'Not-scheduled days cannot have working hours.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.employee} - {self.get_weekday_display()}'


class EmployeeScheduleOverride(TimeStampedModel):
    class Status(models.TextChoices):
        WORKING = 'working', 'Working'
        OFF = 'off', 'Day off'

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='schedule_overrides',
    )
    date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=180, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_employee_schedule_overrides',
    )

    class Meta:
        ordering = ['date', 'employee']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date'],
                name='unique_employee_schedule_override_date',
            ),
        ]

    def clean(self):
        errors = {}
        if self.status == self.Status.WORKING:
            if not self.start_time:
                errors['start_time'] = 'Working overrides need a start time.'
            if not self.end_time:
                errors['end_time'] = 'Working overrides need an end time.'
            if self.start_time and self.end_time and self.end_time <= self.start_time:
                errors['end_time'] = 'The end time must be after the start time.'
        elif self.start_time or self.end_time:
            errors['start_time'] = 'Days off cannot have working hours.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.employee} - {self.date.isoformat()}'


class MobilePushDevice(TimeStampedModel):
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='mobile_push_devices',
    )
    client = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='mobile_push_devices',
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen_at', '-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(employee__isnull=False, client__isnull=True)
                    | models.Q(employee__isnull=True, client__isnull=False)
                ),
                name='mobile_push_device_one_owner',
            ),
        ]

    def __str__(self):
        owner = self.employee or self.client or 'Unassigned'
        return f'{owner} - {self.platform or "mobile"}'


class EmployeeNotification(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='employee_notifications',
    )
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=180)
    body = models.TextField()
    lead = models.ForeignKey(
        Lead,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employee_notifications",
    )
    estimate = models.ForeignKey(
        Estimate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employee_notifications",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employee_notifications",
    )
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employee_notifications",
    )
    message = models.ForeignKey(
        ClientMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employee_notifications",
    )
    destination_url = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_employee_notifications',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', 'read_at', 'created_at']),
            models.Index(fields=['employee', 'dismissed_at', 'created_at']),
        ]

    @property
    def is_read(self):
        return self.read_at is not None


class ClientNotification(TimeStampedModel):
    """A durable client-facing alert with the same contract as employee alerts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=180)
    body = models.TextField()
    lead = models.ForeignKey(
        Lead,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_notifications",
    )
    estimate = models.ForeignKey(
        Estimate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_notifications",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_notifications",
    )
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_notifications",
    )
    message = models.ForeignKey(
        ClientMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_notifications",
    )
    destination_url = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_client_notifications",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "read_at", "created_at"], name="ops_clientnotif_read_idx"),
            models.Index(fields=["client", "dismissed_at", "created_at"], name="ops_clientnotif_dismissed_idx"),
        ]

    @property
    def is_read(self):
        return self.read_at is not None


class PushDelivery(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        INVALID = 'invalid', 'Invalid token'

    notification = models.ForeignKey(
        EmployeeNotification,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='push_deliveries',
    )
    client_notification = models.ForeignKey(
        ClientNotification,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='push_deliveries',
    )
    device = models.ForeignKey(
        MobilePushDevice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='push_deliveries',
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    expo_ticket_id = models.CharField(max_length=180, blank=True)
    failure_detail = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['notification', 'device'],
                name='unique_notification_push_device',
            ),
            models.UniqueConstraint(
                fields=['client_notification', 'device'],
                name='unique_client_notification_push_device',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(notification__isnull=False, client_notification__isnull=True)
                    | models.Q(notification__isnull=True, client_notification__isnull=False)
                ),
                name='push_delivery_one_notification',
            ),
        ]


def _as_local_datetime(value):
    if timezone.is_naive(value):
        return timezone.make_aware(value, CALENDAR_TIME_ZONE)
    return timezone.localtime(value, CALENDAR_TIME_ZONE)


def _local_day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min), CALENDAR_TIME_ZONE)
    end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), CALENDAR_TIME_ZONE)
    return start, end


def schedule_event_local_dates(event):
    'Return every local calendar date touched by a schedule event.'
    if not event.start_at or not event.end_at or event.end_at <= event.start_at:
        return []
    local_start = _as_local_datetime(event.start_at)
    local_end = _as_local_datetime(event.end_at)
    days = []
    current_day = local_start.date()
    while current_day <= local_end.date():
        day_start, day_end = _local_day_bounds(current_day)
        if max(local_start, day_start) < min(local_end, day_end):
            days.append(current_day)
        current_day += timedelta(days=1)
    return days


def _event_conflicts_with_override(event, override):
    event_start = _as_local_datetime(event.start_at)
    event_end = _as_local_datetime(event.end_at)
    day_start, day_end = _local_day_bounds(override.date)
    overlap_start = max(event_start, day_start)
    overlap_end = min(event_end, day_end)
    if overlap_start >= overlap_end:
        return False
    if override.status == CalendarDayOverride.Status.CLOSED:
        return True
    if override.status != CalendarDayOverride.Status.SHORT:
        return False

    allowed_start = timezone.make_aware(
        datetime.combine(override.date, override.short_start),
        CALENDAR_TIME_ZONE,
    )
    allowed_end = timezone.make_aware(
        datetime.combine(override.date, override.short_end),
        CALENDAR_TIME_ZONE,
    )
    return event_start < allowed_start or event_end > allowed_end


def _calendar_events_conflicting_with_override(override):
    day_start, day_end = _local_day_bounds(override.date)
    events = ScheduleEvent.objects.filter(
        start_at__lt=day_end,
        end_at__gt=day_start,
    ).order_by('start_at')
    return [event for event in events if _event_conflicts_with_override(event, override)]


def _calendar_overrides_conflicting_with_event(event):
    dates = schedule_event_local_dates(event)
    if not dates:
        return []
    overrides = CalendarDayOverride.objects.filter(date__in=dates).order_by('date')
    return [override for override in overrides if _event_conflicts_with_override(event, override)]


def effective_employee_schedule(employee, day):
    """Return the effective Pacific-time shift for an employee and local date."""
    employee_override = EmployeeScheduleOverride.objects.filter(
        employee=employee,
        date=day,
    ).first()
    weekly_schedule = None
    if employee_override is None:
        weekly_schedule = EmployeeWeeklySchedule.objects.filter(
            employee=employee,
            weekday=day.weekday(),
        ).first()

    company_override = CalendarDayOverride.objects.filter(date=day).first()
    source = employee_override or weekly_schedule
    if employee_override is not None:
        source_status = employee_override.status
        source_start = employee_override.start_time
        source_end = employee_override.end_time
        source_label = 'Date override'
        reason = employee_override.reason
    elif weekly_schedule is not None and weekly_schedule.is_working:
        source_status = EmployeeScheduleOverride.Status.WORKING
        source_start = weekly_schedule.start_time
        source_end = weekly_schedule.end_time
        source_label = 'Weekly schedule'
        reason = ''
    else:
        source_status = EmployeeScheduleOverride.Status.OFF
        source_start = None
        source_end = None
        source_label = 'Not scheduled'
        reason = ''

    if company_override is not None and company_override.status == CalendarDayOverride.Status.CLOSED:
        return {
            'date': day,
            'status': EmployeeScheduleOverride.Status.OFF,
            'is_working': False,
            'start_time': None,
            'end_time': None,
            'source': source_label,
            'reason': company_override.reason or 'Company closed',
            'employee_override': employee_override,
            'weekly_schedule': weekly_schedule,
            'company_override': company_override,
            'is_company_closed': True,
            'is_company_short': False,
        }

    if source_status != EmployeeScheduleOverride.Status.WORKING or not source_start or not source_end:
        return {
            'date': day,
            'status': EmployeeScheduleOverride.Status.OFF,
            'is_working': False,
            'start_time': None,
            'end_time': None,
            'source': source_label,
            'reason': reason,
            'employee_override': employee_override,
            'weekly_schedule': weekly_schedule,
            'company_override': company_override,
            'is_company_closed': False,
            'is_company_short': bool(
                company_override and company_override.status == CalendarDayOverride.Status.SHORT
            ),
        }

    effective_start = source_start
    effective_end = source_end
    is_short = bool(
        company_override and company_override.status == CalendarDayOverride.Status.SHORT
    )
    if is_short:
        effective_start = max(effective_start, company_override.short_start)
        effective_end = min(effective_end, company_override.short_end)
        if effective_start >= effective_end:
            return {
                'date': day,
                'status': EmployeeScheduleOverride.Status.OFF,
                'is_working': False,
                'start_time': None,
                'end_time': None,
                'source': source_label,
                'reason': company_override.reason or reason or 'No hours available',
                'employee_override': employee_override,
                'weekly_schedule': weekly_schedule,
                'company_override': company_override,
                'is_company_closed': False,
                'is_company_short': True,
            }

    return {
        'date': day,
        'status': EmployeeScheduleOverride.Status.WORKING,
        'is_working': True,
        'start_time': effective_start,
        'end_time': effective_end,
        'source': source_label,
        'reason': reason or (company_override.reason if is_short else ''),
        'employee_override': employee_override,
        'weekly_schedule': weekly_schedule,
        'company_override': company_override,
        'is_company_closed': False,
        'is_company_short': is_short,
    }


class TimeEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="time_entries",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_entries",
    )
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_entries",
    )
    clock_in = models.DateTimeField()
    clock_out = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    adjusted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="adjusted_time_entries",
    )
    adjusted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-clock_in"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(clock_out__isnull=True),
                name="one_open_time_entry_per_employee",
            ),
        ]

    def clean(self):
        if self.clock_out and self.clock_out <= self.clock_in:
            raise ValidationError("Clock-out must be after clock-in.")
        if self.task_id and self.task.project_id and (
            not self.project_id or self.task.project_id != self.project_id
        ):
            raise ValidationError("The selected task must belong to the selected project.")


class ProjectDocument(models.Model):
    class Visibility(models.TextChoices):
        CLIENT = "client", "Client-visible"
        INTERNAL = "internal", "Internal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=180)
    category = models.CharField(max_length=80, default="Project document")
    description = models.TextField(blank=True)
    file = models.FileField(upload_to="projects/documents/%Y/%m/", validators=[validate_uploaded_document])
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.INTERNAL)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class AdminSecurityEvent(models.Model):
    class EventType(models.TextChoices):
        IDENTIFIER_FAILURE = "identifier_failure", "Invalid admin identifier"
        PASSWORD_FAILURE = "password_failure", "Invalid admin password"
        PIN_FAILURE = "pin_failure", "Invalid admin PIN"
        OTP_FAILURE = "otp_failure", "Invalid authenticator code"
        RECOVERY_FAILURE = "recovery_failure", "Failed administration recovery"
        PASSWORD_RESET_FAILURE = "password_reset_failure", "Failed admin password reset"
        LOGIN_SUCCESS = "login_success", "Successful admin sign-in"
        ACCESS_BLOCKED = "access_blocked", "Blocked administration access"

    class Outcome(models.TextChoices):
        FAILURE = "failure", "Failure"
        SUCCESS = "success", "Success"
        BLOCKED = "blocked", "Blocked"

    class EmailStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        NO_RECIPIENT = "no_recipient", "No recipient"
        DISABLED = "disabled", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    attempted_identifier = models.CharField(max_length=254, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="admin_security_events",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    path = models.CharField(max_length=255, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_admin_security_events",
    )
    email_status = models.CharField(
        max_length=16,
        choices=EmailStatus.choices,
        default=EmailStatus.NOT_REQUIRED,
    )
    email_attempt_count = models.PositiveIntegerField(default=0)
    email_last_attempt_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ip_address", "created_at"], name="admin_sec_event_ip_time"),
            models.Index(fields=["event_type", "created_at"], name="admin_sec_event_type_time"),
            models.Index(fields=["outcome", "created_at"], name="admin_sec_event_outcome_time"),
            models.Index(fields=["reviewed_at", "created_at"], name="admin_sec_event_review_time"),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.created_at:%Y-%m-%d %H:%M:%S}"


class APIRateLimitBucket(models.Model):
    """Fixed-window counters used by the API rate limiter."""

    identity = models.CharField(max_length=191, unique=True)
    window_started_at = models.DateTimeField()
    request_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["window_started_at"], name="api_rate_window_idx"),
        ]


class AdminAccessBlock(models.Model):
    class Scope(models.TextChoices):
        IP = "ip", "IP address"
        USER = "user", "Administrator"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=4, choices=Scope.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="admin_access_blocks",
    )
    reason = models.CharField(max_length=220, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_admin_access_blocks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_admin_access_blocks",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["ip_address"],
                condition=models.Q(scope="ip", is_active=True),
                name="unique_active_admin_ip_block",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(scope="user", is_active=True),
                name="unique_active_admin_user_block",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(scope="ip", ip_address__isnull=False, user__isnull=True)
                    | models.Q(scope="user", ip_address__isnull=True, user__isnull=False)
                ),
                name="admin_block_matches_scope",
            ),
        ]

    def clean(self):
        errors = {}
        if self.scope == self.Scope.IP:
            if not self.ip_address:
                errors["ip_address"] = "IP blocks need an IP address."
            if self.user_id:
                errors["user"] = "IP blocks cannot target an administrator."
        elif self.scope == self.Scope.USER:
            if not self.user_id:
                errors["user"] = "Administrator locks need an administrator account."
            if self.ip_address:
                errors["ip_address"] = "Administrator locks cannot target an IP address."
        else:
            errors["scope"] = "Choose a valid block scope."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        target = self.ip_address or (self.user.get_username() if self.user_id else "Unknown target")
        return f"{self.get_scope_display()} - {target}"


class AdminSecurityProfile(TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_security_profile",
    )
    pin_enabled = models.BooleanField(default=False)
    pin_hash = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"Admin security for {self.user.get_username()}"


class AdminRecoveryToken(models.Model):
    class Purpose(models.TextChoices):
        SECURITY_RESET = "security_reset", "Security reset"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_recovery_tokens",
    )
    purpose = models.CharField(max_length=30, choices=Purpose.choices, default=Purpose.SECURITY_RESET)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose", "expires_at"]),
        ]

    @property
    def is_usable(self):
        return self.used_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.get_purpose_display()} for {self.user.get_username()}"


class SiteVisit(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="site_visits")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="site_visits")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_site_visits",
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)
    address = models.CharField(max_length=240, blank=True)
    scope = models.TextField(blank=True)
    measurements = models.TextField(blank=True)
    client_requests = models.TextField(blank=True)
    existing_conditions = models.TextField(blank=True)
    potential_additional_work = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_site_visits",
    )

    class Meta:
        ordering = ["-scheduled_at", "-created_at"]
        indexes = [models.Index(fields=["lead", "status"]), models.Index(fields=["project", "status"])]

    def __str__(self):
        return f"Site visit - {self.lead.name}"


class PreconstructionItem(TimeStampedModel):
    class Category(models.TextChoices):
        DESIGN = "design", "Architectural / design"
        ENGINEERING = "engineering", "Engineering"
        PERMIT = "permit", "Permits"
        SELECTION = "selection", "Client selections"
        BID = "bid", "Subcontractor bids"
        PROCUREMENT = "procurement", "Material procurement"
        LONG_LEAD = "long_lead", "Long-lead items"
        SCHEDULE = "schedule", "Scheduling"
        APPROVAL = "approval", "Required approvals"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        COMPLETE = "complete", "Complete"
        BLOCKED = "blocked", "Blocked"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="readiness_items")
    key = models.SlugField(max_length=50)
    label = models.CharField(max_length=180)
    category = models.CharField(max_length=20, choices=Category.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    required = models.BooleanField(default=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_readiness_items",
    )
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_readiness_items",
    )

    class Meta:
        ordering = ["status", "category", "label"]
        constraints = [
            models.UniqueConstraint(fields=["project", "key"], name="unique_project_readiness_key"),
        ]

    def __str__(self):
        return self.label


class Blocker(TimeStampedModel):
    class Category(models.TextChoices):
        CLIENT_DECISION = "client_decision", "Client decision"
        PERMIT = "permit", "Permit"
        INSPECTION = "inspection", "Inspection"
        MATERIAL = "material", "Material"
        SUBCONTRACTOR = "subcontractor", "Subcontractor"
        BUDGET = "budget", "Budget"
        SCHEDULE = "schedule", "Schedule"
        DESIGN = "design", "Design"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="blockers")
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.NORMAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_project_blockers",
    )
    due_date = models.DateField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_project_blockers",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_project_blockers",
    )

    class Meta:
        ordering = ["status", "-severity", "due_date", "-created_at"]
        indexes = [models.Index(fields=["project", "status"]), models.Index(fields=["assigned_to", "status"])]

    def __str__(self):
        return self.title


class NativeUploadGrant(TimeStampedModel):
    """Short-lived, one-use capability for a native WebView upload."""

    class Target(models.TextChoices):
        PROJECT_MEDIA = "project_media", "Project media"
        PROJECT_DOCUMENT = "project_document", "Project document"
        SITE_VISIT_MEDIA = "site_visit_media", "Site visit media"
        DAILY_REPORT_MEDIA = "daily_report_media", "Daily report media"
        PROBLEM_MEDIA = "problem_media", "Problem report media"
        CHANGE_ORDER_MEDIA = "change_order_media", "Change order media"
        EXISTING_CONDITION = "existing_condition", "Existing condition"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="native_upload_grants",
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="native_upload_grants")
    target = models.CharField(max_length=32, choices=Target.choices)
    target_object_id = models.CharField(max_length=64, blank=True)
    visibility = models.CharField(max_length=20, default=MediaAsset.Visibility.INTERNAL)
    context = models.CharField(max_length=24, default=MediaAsset.Context.PROGRESS)
    idempotency_key = models.CharField(max_length=100, unique=True)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    file_size = models.PositiveBigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    media_asset = models.ForeignKey(
        MediaAsset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="native_upload_grants",
    )
    project_document = models.ForeignKey(
        ProjectDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="native_upload_grants",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor", "expires_at"]),
            models.Index(fields=["project", "target", "expires_at"]),
        ]

    @property
    def is_usable(self):
        return self.used_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.get_target_display()} for {self.project_id}"


class Permit(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        EXPIRED = "expired", "Expired"
        DENIED = "denied", "Denied"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="permits")
    permit_type = models.CharField(max_length=140)
    jurisdiction = models.CharField(max_length=160, blank=True)
    permit_number = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    document = models.ForeignKey(
        ProjectDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="permit_records",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_permits",
    )

    class Meta:
        ordering = ["status", "expires_at", "permit_type"]

    def __str__(self):
        return self.permit_number or self.permit_type


class Inspection(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="inspections")
    permit = models.ForeignKey(Permit, null=True, blank=True, on_delete=models.SET_NULL, related_name="inspections")
    inspection_type = models.CharField(max_length=140)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    result_notes = models.TextField(blank=True)
    corrective_action = models.TextField(blank=True)
    rescheduled_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_inspections",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_inspections",
    )

    class Meta:
        ordering = ["status", "scheduled_at"]
        indexes = [models.Index(fields=["project", "status"]), models.Index(fields=["scheduled_at", "status"])]

    def clean(self):
        if self.permit_id and self.permit.project_id != self.project_id:
            raise ValidationError("The selected permit must belong to the selected project.")

    def __str__(self):
        return self.inspection_type


class Selection(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received"
        INSTALLED = "installed", "Installed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="selections")
    category = models.CharField(max_length=100)
    item_name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    vendor = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    allowance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    client_choice = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    installed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_selections",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_selections",
    )

    class Meta:
        ordering = ["status", "due_date", "category", "item_name"]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self):
        return f"{self.category}: {self.item_name}"


class Agreement(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        ACCEPTED = "accepted", "Accepted"
        VOID = "void", "Void"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="agreement")
    estimate = models.ForeignKey(Estimate, null=True, blank=True, on_delete=models.SET_NULL, related_name="agreements")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    contract_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    issued_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_agreements",
    )
    accepted_ip = models.GenericIPAddressField(null=True, blank=True)
    accepted_user_agent = models.CharField(max_length=500, blank=True)
    acceptance_hash = models.CharField(max_length=64, blank=True)
    signed_pdf = models.FileField(
        upload_to="projects/agreements/%Y/%m/",
        blank=True,
        validators=[validate_signed_pdf],
    )
    content_snapshot = models.JSONField(default=dict, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_agreements",
    )

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.signed_pdf and not str(self.signed_pdf.name).lower().endswith(".pdf"):
            raise ValidationError({"signed_pdf": "Signed agreement files must be PDF documents."})

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "locked_at",
                "project_id",
                "estimate_id",
                "status",
                "contract_value",
                "deposit_amount",
                "issued_at",
                "accepted_at",
                "accepted_by_id",
                "accepted_ip",
                "accepted_user_agent",
                "acceptance_hash",
                "signed_pdf",
                "content_snapshot",
                "created_by_id",
            ).first()
            if old and old["locked_at"] is not None:
                protected = (
                    "locked_at",
                    "project_id",
                    "estimate_id",
                    "status",
                    "contract_value",
                    "deposit_amount",
                    "issued_at",
                    "accepted_at",
                    "accepted_by_id",
                    "accepted_ip",
                    "accepted_user_agent",
                    "acceptance_hash",
                    "signed_pdf",
                    "content_snapshot",
                    "created_by_id",
                )
                current = {
                    field: self.signed_pdf.name if field == "signed_pdf" else getattr(self, field)
                    for field in protected
                }
                if any(current[field] != old[field] for field in protected):
                    raise ValidationError("Accepted agreements are immutable; create a revision instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.locked_at is not None:
            raise ValidationError("Accepted agreements cannot be deleted.")
        return super().delete(*args, **kwargs)

    @property
    def current_contract_value(self):
        change_total = self.project.change_orders.filter(status=ChangeOrder.Status.APPROVED).aggregate(
            total=models.Sum("price_impact")
        )["total"] or Decimal("0.00")
        return (self.contract_value + change_total).quantize(Decimal("0.01"))

    def __str__(self):
        return f"Agreement for {self.project.title}"


class PaymentSchedule(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready to bill"
        INVOICED = "invoiced", "Invoiced"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        WAIVED = "waived", "Waived"

    class ExternalInvoiceStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SENT = "sent", "Sent"
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="payment_schedules")
    milestone = models.ForeignKey(Milestone, null=True, blank=True, on_delete=models.SET_NULL, related_name="payment_schedules")
    sequence = models.PositiveIntegerField(default=1)
    description = models.CharField(max_length=180)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    # Invoice tracking is manual and intentionally does not create or
    # replace a Grand Coast PaymentRecord.  Staff record the internal payment
    # separately after confirming the external payment.
    external_invoice_url = models.URLField(max_length=500, blank=True)
    external_invoice_reference = models.CharField(max_length=120, blank=True)
    external_invoice_status = models.CharField(
        max_length=16,
        choices=ExternalInvoiceStatus.choices,
        default=ExternalInvoiceStatus.NOT_STARTED,
    )
    external_invoice_status_at = models.DateTimeField(null=True, blank=True)
    external_invoice_status_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_external_invoice_statuses",
    )
    external_invoice_status_note = models.TextField(blank=True)
    external_client_visible = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_payment_schedules",
    )

    class Meta:
        ordering = ["sequence", "due_date", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["project", "sequence"], name="unique_project_payment_sequence"),
        ]

    @property
    def received_amount(self):
        return self.payments.filter(voided_at__isnull=True).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")

    @property
    def remaining_amount(self):
        return max((self.amount - self.received_amount).quantize(Decimal("0.01")), Decimal("0.00"))

    def clean(self):
        if self.amount < 0:
            raise ValidationError({"amount": "Payment schedule amounts cannot be negative."})
        if self.milestone_id and self.milestone.project_id != self.project_id:
            raise ValidationError("The selected milestone must belong to the selected project.")

    def __str__(self):
        return self.description


class PaymentRecord(TimeStampedModel):
    class Method(models.TextChoices):
        CHECK = "check", "Check"
        ACH = "ach", "ACH / bank transfer"
        CASH = "cash", "Cash"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="payment_records")
    schedule = models.ForeignKey(PaymentSchedule, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    received_on = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.OTHER)
    reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_payment_records",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_payment_records",
    )

    class Meta:
        ordering = ["-received_on", "-created_at"]
        indexes = [models.Index(fields=["project", "received_on"])]

    def clean(self):
        if self.amount <= 0:
            raise ValidationError({"amount": "Payment amounts must be greater than zero."})
        if self.schedule_id and self.schedule.project_id != self.project_id:
            raise ValidationError("The selected payment schedule must belong to the selected project.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "idempotency_key",
                "project_id",
                "schedule_id",
                "amount",
                "received_on",
                "method",
                "reference",
                "notes",
                "voided_at",
                "voided_by_id",
                "created_by_id",
            ).first()
            protected = (
                "idempotency_key",
                "project_id",
                "schedule_id",
                "amount",
                "received_on",
                "method",
                "reference",
                "notes",
                "voided_at",
                "voided_by_id",
                "created_by_id",
            )
            if old and any(getattr(self, field) != old[field] for field in protected):
                raise ValidationError("Payment history is immutable; record a correcting entry instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Payment history cannot be deleted.")

    def __str__(self):
        return f"{self.project.title} payment {self.amount}"


class BudgetLine(TimeStampedModel):
    class Category(models.TextChoices):
        LABOR = "labor", "Labor"
        MATERIALS = "materials", "Materials"
        SUBCONTRACTOR = "subcontractor", "Subcontractors"
        PERMITS = "permits", "Permits"
        DESIGN = "design", "Design / engineering"
        EQUIPMENT = "equipment", "Equipment"
        MISCELLANEOUS = "miscellaneous", "Miscellaneous"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="budget_lines")
    description = models.CharField(max_length=180)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.MISCELLANEOUS)
    cost_code = models.CharField(max_length=40, blank=True)
    original_budget = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    approved_change = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    committed = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    actual = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_allowance = models.BooleanField(default=False)
    source_estimate_line = models.ForeignKey(
        EstimateLineItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="budget_lines",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_budget_lines",
    )

    class Meta:
        ordering = ["category", "description"]
        indexes = [models.Index(fields=["project", "category"])]

    @property
    def current_budget(self):
        return (self.original_budget + self.approved_change).quantize(Decimal("0.01"))

    @property
    def remaining_budget(self):
        return (self.current_budget - self.committed - self.actual).quantize(Decimal("0.01"))

    def clean(self):
        for field_name in ("original_budget", "committed", "actual"):
            if getattr(self, field_name) < 0:
                raise ValidationError({field_name: "Budget values cannot be negative."})
        if self.source_estimate_line_id and self.source_estimate_line.estimate.projects.exclude(pk=self.project_id).exists():
            raise ValidationError("The source estimate line must belong to this project.")

    def __str__(self):
        return self.description


class CostEntry(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="cost_entries")
    budget_line = models.ForeignKey(BudgetLine, null=True, blank=True, on_delete=models.PROTECT, related_name="cost_entries")
    vendor = models.CharField(max_length=180, blank=True)
    description = models.CharField(max_length=180)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    incurred_on = models.DateField(default=timezone.localdate)
    source = models.CharField(max_length=40, default="manual")
    receipt = models.FileField(
        upload_to="projects/receipts/%Y/%m/",
        blank=True,
        validators=[validate_construction_document],
    )
    is_void = models.BooleanField(default=False)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_cost_entries",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_cost_entries",
    )

    class Meta:
        ordering = ["-incurred_on", "-created_at"]
        indexes = [models.Index(fields=["project", "incurred_on"])]

    def clean(self):
        if self.amount <= 0:
            raise ValidationError({"amount": "Cost entries must be greater than zero."})
        if self.budget_line_id and self.budget_line.project_id != self.project_id:
            raise ValidationError("The selected budget line must belong to the selected project.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "idempotency_key",
                "project_id",
                "budget_line_id",
                "vendor",
                "description",
                "amount",
                "incurred_on",
                "source",
                "receipt",
                "is_void",
                "voided_at",
                "voided_by_id",
                "created_by_id",
            ).first()
            if old:
                immutable = (
                    "idempotency_key",
                    "project_id",
                    "budget_line_id",
                    "vendor",
                    "description",
                    "amount",
                    "incurred_on",
                    "source",
                    "created_by_id",
                )
                if any(getattr(self, field) != old[field] for field in immutable):
                    raise ValidationError("Cost history is immutable; record a correcting entry instead.")
                receipt_name = self.receipt.name if self.receipt else ""
                if receipt_name != (old["receipt"] or ""):
                    raise ValidationError("Cost receipts are immutable; attach a correcting entry instead.")
                if old["is_void"]:
                    if (
                        self.is_void != old["is_void"]
                        or self.voided_at != old["voided_at"]
                        or self.voided_by_id != old["voided_by_id"]
                    ):
                        raise ValidationError("Voided cost entries are immutable.")
                elif self.is_void:
                    if not self.voided_at or not self.voided_by_id:
                        raise ValidationError("Voiding a cost entry requires an actor and timestamp.")
                elif self.voided_at != old["voided_at"] or self.voided_by_id != old["voided_by_id"]:
                    raise ValidationError("Cost history is immutable; use the void workflow.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Cost history cannot be deleted.")

    def __str__(self):
        return self.description


class Subcontractor(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.CharField(max_length=180)
    contact_name = models.CharField(max_length=160, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    portal_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subcontractor_profile",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_subcontractors",
    )

    class Meta:
        ordering = ["company", "contact_name"]

    def __str__(self):
        return self.company


class Commitment(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        COMMITTED = "committed", "Committed"
        FULFILLED = "fulfilled", "Fulfilled"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="commitments")
    subcontractor = models.ForeignKey(Subcontractor, null=True, blank=True, on_delete=models.PROTECT, related_name="commitments")
    budget_line = models.ForeignKey(BudgetLine, null=True, blank=True, on_delete=models.PROTECT, related_name="commitments")
    description = models.CharField(max_length=180)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PLANNED)
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_commitments",
    )

    class Meta:
        ordering = ["status", "due_date", "description"]

    def clean(self):
        if self.amount < 0:
            raise ValidationError({"amount": "Commitments cannot be negative."})
        if self.budget_line_id and self.budget_line.project_id != self.project_id:
            raise ValidationError("The selected budget line must belong to the selected project.")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "idempotency_key",
                "project_id",
                "subcontractor_id",
                "budget_line_id",
                "description",
                "amount",
                "due_date",
                "status",
                "created_by_id",
            ).first()
            if old:
                immutable = (
                    "idempotency_key",
                    "project_id",
                    "subcontractor_id",
                    "budget_line_id",
                    "description",
                    "amount",
                    "due_date",
                    "created_by_id",
                )
                if any(getattr(self, field) != old[field] for field in immutable):
                    raise ValidationError("Commitment history is immutable; create a revision instead.")
                if old["status"] == self.Status.FULFILLED and self.status != old["status"]:
                    raise ValidationError("Fulfilled commitments cannot be reopened.")
                if old["status"] == self.Status.CANCELLED and self.status != old["status"]:
                    raise ValidationError("Cancelled commitments cannot be reopened.")
                if old["status"] == self.Status.COMMITTED and self.status == self.Status.PLANNED:
                    raise ValidationError("Committed commitments cannot move backward.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Commitment history cannot be deleted.")

    def __str__(self):
        return self.description


class ChangeOrder(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent for approval"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        VOID = "void", "Void"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="change_orders")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=180)
    description = models.TextField()
    price_impact = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    schedule_impact_days = models.IntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_change_orders",
    )
    approval_ip = models.GenericIPAddressField(null=True, blank=True)
    approved_snapshot = models.JSONField(default=dict, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    supporting_documents = models.ManyToManyField(ProjectDocument, blank=True, related_name="change_orders")
    supporting_media = models.ManyToManyField(MediaAsset, blank=True, related_name="change_orders")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_change_orders",
    )

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["project", "number"], name="unique_project_change_order_number"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values(
                "locked_at",
                "project_id",
                "number",
                "title",
                "description",
                "price_impact",
                "schedule_impact_days",
                "status",
                "sent_at",
                "approved_at",
                "approved_by_id",
                "approval_ip",
                "approved_snapshot",
                "created_by_id",
            ).first()
            if old and old["locked_at"] is not None:
                protected = (
                    "locked_at",
                    "project_id",
                    "number",
                    "title",
                    "description",
                    "price_impact",
                    "schedule_impact_days",
                    "status",
                    "sent_at",
                    "approved_at",
                    "approved_by_id",
                    "approval_ip",
                    "approved_snapshot",
                    "created_by_id",
                )
                if any(getattr(self, field) != old[field] for field in protected):
                    raise ValidationError("Approved change orders are immutable; create a revision instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.locked_at is not None:
            raise ValidationError("Approved change orders cannot be deleted.")
        return super().delete(*args, **kwargs)

    def clean(self):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values(
                "status", "title", "description", "price_impact", "schedule_impact_days"
            ).first()
            if old and old["status"] == self.Status.APPROVED:
                current = {
                    "status": self.status,
                    "title": self.title,
                    "description": self.description,
                    "price_impact": self.price_impact,
                    "schedule_impact_days": self.schedule_impact_days,
                }
                if any(current[key] != old[key] for key in ("title", "description", "price_impact", "schedule_impact_days")):
                    raise ValidationError("Approved change orders are immutable; create a revision instead.")
                if current["status"] != self.Status.APPROVED:
                    raise ValidationError("Approved change orders cannot be moved to another status.")

    def __str__(self):
        return f"CO-{self.number}: {self.title}"


class SubcontractorAssignment(TimeStampedModel):
    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ASSIGNED = "assigned", "Assigned"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="subcontractor_assignments")
    subcontractor = models.ForeignKey(Subcontractor, on_delete=models.PROTECT, related_name="assignments")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="subcontractor_assignments")
    work_package = models.CharField(max_length=180)
    scope = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROPOSED)
    notes = models.TextField(blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_subcontractor_assignments",
    )

    class Meta:
        ordering = ["status", "start_date", "work_package"]

    def clean(self):
        if self.task_id and self.task.project_id != self.project_id:
            raise ValidationError("The selected task must belong to the selected project.")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The subcontractor end date cannot be before its start date."})

    def __str__(self):
        return self.work_package


class DailyReport(TimeStampedModel):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="daily_reports")
    report_date = models.DateField(default=timezone.localdate)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_daily_reports",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    summary = models.TextField()
    work_completed = models.TextField(blank=True)
    labor_count = models.PositiveIntegerField(default=0)
    hours_worked = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    weather = models.CharField(max_length=120, blank=True)
    equipment = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_daily_reports",
    )
    media_assets = models.ManyToManyField(MediaAsset, blank=True, related_name="daily_reports")

    class Meta:
        ordering = ["-report_date", "-created_at"]
        indexes = [models.Index(fields=["project", "report_date"])]

    def clean(self):
        if self.hours_worked < 0:
            raise ValidationError({"hours_worked": "Hours worked cannot be negative."})

    def __str__(self):
        return f"{self.project.title} - {self.report_date}"


class MaterialRequest(TimeStampedModel):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="material_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_materials",
    )
    description = models.CharField(max_length=180)
    quantity = models.CharField(max_length=80, blank=True)
    needed_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    vendor = models.CharField(max_length=180, blank=True)
    notes = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_material_requests",
    )

    class Meta:
        ordering = ["status", "needed_by", "-created_at"]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self):
        return self.description


class ProblemReport(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="problem_reports")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="problem_reports")
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_construction_problems",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_construction_problems",
    )
    title = models.CharField(max_length=180)
    description = models.TextField()
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.NORMAL)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_construction_problems",
    )
    supporting_media = models.ManyToManyField(MediaAsset, blank=True, related_name="problem_reports")

    class Meta:
        ordering = ["status", "-created_at"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["severity", "status"]),
        ]

    def clean(self):
        if self.task_id and self.task.project_id != self.project_id:
            raise ValidationError("The selected task must belong to the selected project.")

    def __str__(self):
        return self.title


class CloseoutItem(TimeStampedModel):
    class Category(models.TextChoices):
        FINAL_INSPECTION = "final_inspection", "Final inspection"
        PUNCH_LIST = "punch_list", "Punch list"
        DOCUMENTS = "documents", "Closeout documents"
        WARRANTY = "warranty", "Warranty"
        CLIENT_WALKTHROUGH = "client_walkthrough", "Client walkthrough"
        FINAL_INVOICE = "final_invoice", "Final invoice"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        COMPLETE = "complete", "Complete"
        BLOCKED = "blocked", "Blocked"
        NOT_APPLICABLE = "not_applicable", "Not applicable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="closeout_items")
    key = models.SlugField(max_length=60)
    label = models.CharField(max_length=180)
    category = models.CharField(max_length=24, choices=Category.choices, default=Category.OTHER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    required = models.BooleanField(default=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_closeout_items",
    )
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_closeout_items",
    )

    class Meta:
        ordering = ["status", "category", "label"]
        constraints = [
            models.UniqueConstraint(fields=["project", "key"], name="unique_project_closeout_key"),
        ]

    def __str__(self):
        return self.label


class WarrantyItem(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="warranty_items")
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_warranty_items",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_warranty_items",
    )
    due_date = models.DateField(null=True, blank=True)
    warranty_until = models.DateField(null=True, blank=True)
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_warranty_items",
    )

    class Meta:
        ordering = ["status", "due_date", "-created_at"]
        indexes = [models.Index(fields=["project", "status"])]

    def __str__(self):
        return self.title


class WorkflowEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    event_type = models.CharField(max_length=80)
    source = models.CharField(max_length=40, default="web")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="construction_workflow_events",
    )
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="workflow_events")
    estimate = models.ForeignKey(Estimate, null=True, blank=True, on_delete=models.SET_NULL, related_name="workflow_events")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="workflow_events")
    related_model = models.CharField(max_length=80, blank=True)
    related_id = models.CharField(max_length=64, blank=True)
    before_state = models.JSONField(default=dict, blank=True)
    after_state = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self):
        return self.event_type

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValidationError("Workflow events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow events cannot be deleted.")


class AttentionItem(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dedupe_key = models.CharField(max_length=180, unique=True, null=True, blank=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="attention_items")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="attention_items")
    kind = models.CharField(max_length=60)
    title = models.CharField(max_length=220)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    due_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_attention_items",
    )
    source = models.CharField(max_length=80, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_attention_items",
    )

    class Meta:
        ordering = ["status", "-priority", "due_at", "-created_at"]
        indexes = [models.Index(fields=["status", "priority", "due_at"])]

    def __str__(self):
        return self.title


class EmailOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True)
    recipient = models.EmailField()
    subject = models.CharField(max_length=220)
    body = models.TextField()
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_outbox_items")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_outbox_items")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_email_outbox_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["status", "next_attempt_at", "created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.recipient}: {self.subject}"


class AiActionDraft(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXECUTED = "executed", "Executed"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="ai_action_drafts")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_ai_action_drafts",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_ai_action_drafts",
    )
    action_type = models.CharField(max_length=80)
    request_text = models.TextField()
    draft_text = models.TextField()
    source_event_ids = models.JSONField(default=list, blank=True)
    permission_scope = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    approved_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.action_type


@receiver(m2m_changed, sender=ChangeOrder.supporting_documents.through)
def prevent_locked_change_order_document_edits(sender, instance, action, **kwargs):
    if instance.locked_at is not None and action in {"pre_add", "pre_remove", "pre_clear"}:
        raise ValidationError("Approved change-order documents are immutable; create a revision instead.")


@receiver(m2m_changed, sender=ProblemReport.supporting_media.through)
def prevent_cross_project_problem_media(sender, instance, action, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    if MediaAsset.objects.filter(
        pk__in=pk_set,
    ).exclude(project_id=instance.project_id).exists():
        raise ValidationError("Problem-report media must belong to the same project.")
