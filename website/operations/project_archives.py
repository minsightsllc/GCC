"""Export and permanently remove locally stored completed-project data."""

from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path as FilePath
from unicodedata import normalize
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from django.db import transaction
from django.db.models import FileField
from django.utils import timezone
from django.utils.text import slugify
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Agreement, Estimate, Project


def project_data_graph(project):
    """Return project-owned rows, including SET_NULL children and their descendants."""
    database = project._state.db or "default"
    pending = [project]
    seen = set()
    ordered = []
    while pending:
        instance = pending.pop()
        key = (instance._meta.label_lower, str(instance.pk))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(instance)
        for relation in instance._meta.related_objects:
            if relation.many_to_many or not (relation.one_to_many or relation.one_to_one):
                continue
            related_model = relation.related_model
            children = related_model._base_manager.using(database).filter(
                **{relation.field.name: instance.pk}
            )
            pending.extend(children.iterator())
    return ordered


def _register_archive_fonts():
    """Register ReportLab's bundled Unicode font for names and Spanish text."""
    from reportlab import __file__ as reportlab_path

    fonts_dir = FilePath(reportlab_path).resolve().parent / "fonts"
    for name, filename in (
        ("GccArchiveSans", "Vera.ttf"),
        ("GccArchiveSans-Bold", "VeraBd.ttf"),
    ):
        try:
            pdfmetrics.getFont(name)
        except KeyError:
            pdfmetrics.registerFont(TTFont(name, str(fonts_dir / filename)))


def _human_label(value):
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
    return " ".join(label.replace("_", " ").replace(".", " ").split()).title()


def _readable_value(value, *, field=None, depth=0):
    """Render model values as readable prose instead of serialized data."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    if isinstance(value, date):
        return value.strftime("%b %d, %Y").replace(" 0", " ")
    if isinstance(value, Decimal):
        field_name = getattr(field, "name", "")
        if any(token in field_name for token in ("amount", "price", "cost", "total")):
            return f"${value:,.2f}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "Binary data attached"
    if isinstance(value, dict):
        if depth >= 3:
            return "Additional structured details recorded"
        pieces = []
        for key, item in value.items():
            if any(term in str(key).lower() for term in ("password", "secret", "token", "hash")):
                continue
            rendered = _readable_value(item, depth=depth + 1)
            if rendered:
                pieces.append(f"{_human_label(key)}: {rendered}")
        return "; ".join(pieces)
    if isinstance(value, (list, tuple, set)):
        if depth >= 3:
            return "Additional structured details recorded"
        return "; ".join(
            rendered
            for item in value
            if (rendered := _readable_value(item, depth=depth + 1))
        )
    if isinstance(value, str):
        candidate = value.strip()
        if len(candidate) == 36:
            try:
                UUID(candidate)
            except ValueError:
                pass
            else:
                return "Linked record reference"
    if hasattr(value, "_meta"):
        if hasattr(value, "get_full_name"):
            return value.get_full_name() or value.get_username()
        return str(value)
    return str(value).strip()


def _record_heading(instance):
    if instance._meta.model_name.lower() == "clientmessage":
        sender = getattr(instance, "sent_by", None)
        sender_name = _readable_value(sender) if sender else "Project participant"
        timestamp = getattr(instance, "created_at", None)
        suffix = f" - {_readable_value(timestamp)}" if timestamp else ""
        return f"Message from {sender_name}{suffix}"
    for field_name in (
        "title", "name", "number", "event_type", "kind", "label", "report_date",
    ):
        value = getattr(instance, field_name, None)
        if value:
            rendered = _readable_value(value)
            if rendered:
                return rendered[:140] + ("..." if len(rendered) > 140 else "")
    return str(instance)[:140]


def _export_record_fields(instance):
    hidden_fields = {
        "idempotency_key", "dedupe_key", "token", "token_hash", "password", "secret",
        "secret_key", "api_key", "authorization", "signature_hash", "acceptance_hash",
        "related_id", "source_id", "source_event_ids", "project_id_snapshot",
    }
    values = []
    for field in instance._meta.concrete_fields:
        if field.primary_key or isinstance(field, FileField) or field.name in hidden_fields:
            continue
        if any(term in field.name.lower() for term in ("password", "secret", "token", "hash")):
            continue
        if field.many_to_one or field.one_to_one:
            raw_value = getattr(instance, field.name, None)
        else:
            raw_value = getattr(instance, field.attname, None)
        display_method = getattr(instance, f"get_{field.name}_display", None)
        if field.choices and callable(display_method) and raw_value not in (None, ""):
            raw_value = display_method()
        rendered = _readable_value(raw_value, field=field)
        if rendered:
            values.append((str(field.verbose_name).capitalize(), rendered))
    for field in instance._meta.many_to_many:
        rendered = _readable_value(list(getattr(instance, field.name).all()))
        if rendered:
            values.append((str(field.verbose_name).capitalize(), rendered))
    return values


def _project_pdf_styles():
    _register_archive_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ArchiveEyebrow", fontName="GccArchiveSans-Bold", fontSize=8,
        leading=11, textColor=colors.HexColor("#4969A4"), spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveTitle", fontName="Times-Bold", fontSize=23, leading=27,
        textColor=colors.HexColor("#203456"), spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveSubtitle", fontName="GccArchiveSans", fontSize=9,
        leading=13, textColor=colors.HexColor("#53637A"), spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveSection", fontName="Times-Bold", fontSize=15,
        leading=19, textColor=colors.HexColor("#203456"), spaceBefore=12,
        spaceAfter=7, keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveRecord", fontName="GccArchiveSans-Bold", fontSize=10,
        leading=14, textColor=colors.HexColor("#203456"), spaceBefore=7,
        spaceAfter=3, keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveLabel", fontName="GccArchiveSans-Bold", fontSize=8,
        leading=11, textColor=colors.HexColor("#53637A"), spaceBefore=4,
        spaceAfter=1, keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveBody", fontName="GccArchiveSans", fontSize=8.5,
        leading=12, textColor=colors.HexColor("#24364F"), splitLongWords=1,
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name="ArchiveSmall", fontName="GccArchiveSans", fontSize=7.5,
        leading=10, textColor=colors.HexColor("#53637A"), splitLongWords=1,
    ))
    return styles


def _archive_paragraph(value, style):
    text = normalize("NFC", str(value))
    text = text.translate(str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2212": "-", "\u00a0": " ",
    }))
    text = "".join(character for character in text if character in "\r\n\t" or ord(character) >= 32)
    text = escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return Paragraph(text.replace("\n", "<br/>"), style)


def _archive_source_label(file_item, records_by_key):
    source = records_by_key.get((file_item.get("model"), str(file_item.get("record_id"))))
    if source is not None:
        title = next(
            (
                getattr(source, field_name)
                for field_name in ("title", "name", "number")
                if getattr(source, field_name, None)
            ),
            None,
        )
        detail = _readable_value(title) if title is not None else ""
        if not detail:
            detail = file_item.get("record_title", "")
        return f"{source._meta.verbose_name.title()}: {detail}" if detail else source._meta.verbose_name.title()
    return file_item.get("record_title") or file_item.get("model", "Project record")


def _project_details_pdf(project, records, file_manifest):
    """Create a readable project report PDF; original uploads remain separate ZIP files."""
    from io import BytesIO

    styles = _project_pdf_styles()
    buffer = BytesIO()
    project_title = project.title or "Completed project"
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.68 * inch,
        leftMargin=0.68 * inch,
        topMargin=0.86 * inch,
        bottomMargin=0.68 * inch,
        title=f"Completed project archive - {project_title}",
        author="Grand Coast Construction Inc.",
        pageCompression=1,
    )
    navy = colors.HexColor("#203456")
    gold = colors.HexColor("#D9A51F")

    def draw_page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(gold)
        canvas.setLineWidth(1.2)
        canvas.line(
            document.leftMargin,
            letter[1] - 0.57 * inch,
            letter[0] - document.rightMargin,
            letter[1] - 0.57 * inch,
        )
        canvas.setFont("GccArchiveSans-Bold", 7.5)
        canvas.setFillColor(navy)
        canvas.drawString(document.leftMargin, letter[1] - 0.44 * inch, "GRAND COAST CONSTRUCTION INC.")
        canvas.setFont("GccArchiveSans", 7.5)
        canvas.setFillColor(colors.HexColor("#53637A"))
        canvas.drawRightString(letter[0] - document.rightMargin, 0.38 * inch, f"Completed project archive  |  Page {document.page}")
        canvas.restoreState()

    story = [
        _archive_paragraph("PROJECT ARCHIVE", styles["ArchiveEyebrow"]),
        _archive_paragraph(project_title, styles["ArchiveTitle"]),
        _archive_paragraph(
            "Readable project record and attachment guide. Original uploaded documents and media are included separately in the files folder of this ZIP.",
            styles["ArchiveSubtitle"],
        ),
        _archive_paragraph(
            "Includes project-linked records and conversations, local estimate history, operational history, and original uploads. General client conversations and data held only by external estimate or payment providers are not included.",
            styles["ArchiveBody"],
        ),
        HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D7E0EE"), spaceAfter=8),
        _archive_paragraph("Project overview", styles["ArchiveSection"]),
    ]

    try:
        client = project.client if project.client_id else None
    except Exception:
        client = None
    overview = [
        ("Project", project_title),
        ("Status", _readable_value(project.get_status_display() if hasattr(project, "get_status_display") else project.status)),
        ("Client", _readable_value(client) if client else "Not linked"),
        ("Location", _readable_value(project.location or ", ".join(
            value for value in (
                getattr(project, "address_line1", ""), getattr(project, "address_line2", ""),
                getattr(project, "city", ""), getattr(project, "state", ""), getattr(project, "postal_code", ""),
            ) if value
        )) or "Not recorded"),
        ("Project code", _readable_value(getattr(project, "project_code", "")) or "Not assigned"),
        ("Archived", _readable_value(getattr(project, "archived_at", None)) or "Not recorded"),
        ("Exported", _readable_value(timezone.now())),
        ("Records included", str(len(records))),
        ("Original files included", str(len(file_manifest))),
    ]
    if client:
        overview.extend(
            (label, _readable_value(getattr(client, field_name, "")))
            for label, field_name in (("Client email", "email"), ("Client phone", "phone"))
            if getattr(client, field_name, "")
        )
    overview_rows = [
        [_archive_paragraph(label, styles["ArchiveLabel"]), _archive_paragraph(value, styles["ArchiveBody"])]
        for label, value in overview
    ]
    overview_table = Table(overview_rows, colWidths=[1.52 * inch, 5.25 * inch], hAlign="LEFT")
    overview_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F7FB")),
        ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor("#D7E0EE")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D7E0EE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.extend([overview_table, Spacer(1, 7)])

    overview_field_labels = {"title", "status", "client", "location", "project code", "archived at"}
    additional_project_fields = [
        (label, value)
        for label, value in _export_record_fields(project)
        if label.lower() not in overview_field_labels
    ]
    if additional_project_fields:
        story.append(_archive_paragraph("Additional project details", styles["ArchiveSection"]))
        for label, value in additional_project_fields:
            story.append(_archive_paragraph(label, styles["ArchiveLabel"]))
            story.append(_archive_paragraph(value, styles["ArchiveBody"]))

    section_names = {
        "estimate": "Estimate history",
        "estimatelineitem": "Estimate history",
        "milestone": "Project progress",
        "clientmessage": "Project messages",
        "projectupdate": "Project updates",
        "projectdocument": "Documents and media",
        "mediaasset": "Documents and media",
        "agreement": "Documents and media",
        "activity": "Activity and workflow history",
        "workflowevent": "Activity and workflow history",
        "project": "Project information",
    }
    section_order = [
        "Project information", "Estimate history", "Project progress", "Project messages",
        "Project updates", "Documents and media", "Activity and workflow history",
    ]
    grouped = defaultdict(list)
    for instance in records:
        if instance is project:
            continue
        model_name = instance._meta.model_name.lower()
        section = section_names.get(model_name, instance._meta.verbose_name_plural.title())
        grouped[section].append(instance)
    for section in sorted(grouped, key=lambda item: section_order.index(item) if item in section_order else len(section_order)):
        story.append(_archive_paragraph(section, styles["ArchiveSection"]))
        for instance in grouped[section]:
            heading = _record_heading(instance)
            story.append(_archive_paragraph(heading, styles["ArchiveRecord"]))
            story.append(HRFlowable(width="100%", thickness=0.35, color=colors.HexColor("#E2E8F1"), spaceAfter=3))
            fields = _export_record_fields(instance)
            if not fields:
                story.append(_archive_paragraph("No additional details were recorded.", styles["ArchiveSmall"]))
            for label, value in fields:
                story.append(_archive_paragraph(label, styles["ArchiveLabel"]))
                story.append(_archive_paragraph(value, styles["ArchiveBody"]))
            story.append(Spacer(1, 5))

    story.append(_archive_paragraph("Original files in this ZIP", styles["ArchiveSection"]))
    story.append(_archive_paragraph(
        "These are copies of the original stored uploads. Use the archive location to find each file within this ZIP.",
        styles["ArchiveBody"],
    ))
    records_by_key = {
        (instance._meta.label, str(instance.pk)): instance
        for instance in records
    }
    file_rows = [[
        _archive_paragraph("File", styles["ArchiveLabel"]),
        _archive_paragraph("Linked record", styles["ArchiveLabel"]),
        _archive_paragraph("Size", styles["ArchiveLabel"]),
        _archive_paragraph("ZIP location", styles["ArchiveLabel"]),
    ]]
    for item in file_manifest:
        name = item.get("original_name") or FilePath(item.get("stored_name", "file")).name
        file_rows.append([
            _archive_paragraph(name, styles["ArchiveBody"]),
            _archive_paragraph(_archive_source_label(item, records_by_key), styles["ArchiveSmall"]),
            _archive_paragraph(_format_file_size(item.get("size_bytes", 0)), styles["ArchiveSmall"]),
            _archive_paragraph(item.get("archive_path", ""), styles["ArchiveSmall"]),
        ])
    if len(file_rows) == 1:
        story.append(_archive_paragraph("No original files were attached to this project archive.", styles["ArchiveBody"]))
    else:
        file_table = Table(
            file_rows,
            colWidths=[1.45 * inch, 2.0 * inch, 0.65 * inch, 2.65 * inch],
            repeatRows=1,
            hAlign="LEFT",
        )
        file_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F4F9")),
            ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor("#D7E0EE")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D7E0EE")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(file_table)

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()


def _format_file_size(size_bytes):
    size = max(0, int(size_bytes or 0))
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _copy_record_files(instance, archive, file_manifest):
    for field in instance._meta.concrete_fields:
        if not isinstance(field, FileField):
            continue
        stored_file = getattr(instance, field.name)
        name = str(getattr(stored_file, "name", "") or "")
        if not name:
            continue
        storage = field.storage
        safe_model = slugify(instance._meta.label_lower.replace(".", "-")) or "record"
        basename = FilePath(name.replace("\\", "/")).name or "file"
        safe_basename = slugify(FilePath(basename).stem) or "file"
        suffix = FilePath(basename).suffix[:20]
        archive_name = f"files/{safe_model}/{instance.pk}/{safe_basename}{suffix}"
        byte_count = 0
        with storage.open(name, "rb") as source:
            with archive.open(archive_name, "w") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    byte_count += len(chunk)
        file_manifest.append(
            {
                "model": instance._meta.label,
                "record_id": str(instance.pk),
                "record_title": _record_heading(instance),
                "field": field.name,
                "stored_name": name,
                "original_name": basename,
                "archive_path": archive_name,
                "size_bytes": byte_count,
            }
        )


def _copy_retained_project_files(project, archive, file_manifest):
    from pathlib import PurePosixPath

    from .models import StorageTrash
    from .storage_retention import storage_for_alias

    included_names = {item.get("stored_name") for item in file_manifest}
    retained = StorageTrash.objects.filter(project_id_snapshot=str(project.pk)).order_by("moved_at", "pk")
    for item in retained:
        if item.storage_name in included_names:
            continue
        safe_basename = slugify(PurePosixPath(item.original_name).stem) or "file"
        suffix = PurePosixPath(item.original_name).suffix[:20]
        archive_name = f"files/recovered/{item.pk}/{safe_basename}{suffix}"
        byte_count = 0
        with storage_for_alias(item.storage_alias).open(item.storage_name, "rb") as source:
            with archive.open(archive_name, "w") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    byte_count += len(chunk)
        file_manifest.append(
            {
                "model": item.source_model,
                "record_id": item.source_id,
                "record_title": item.record_title or item.source_model,
                "field": item.source_field,
                "stored_name": item.storage_name,
                "original_name": item.original_name,
                "archive_path": archive_name,
                "size_bytes": byte_count,
                "retention_status": item.status,
            }
        )
        included_names.add(item.storage_name)


def project_export_file(project):
    """Build a ZIP with a readable PDF report and original project files."""
    records = project_data_graph(project)
    # The project points to its local estimate, but estimates point to projects
    # in the opposite direction. Include the local estimate revision family and
    # its line items without traversing into the client, other projects, or
    # general conversations. Provider-hosted documents are never fetched.
    if project.estimate_id:
        estimate_family = estimate_history_family(project.estimate)
        for estimate in estimate_family.prefetch_related("line_items"):
            records.append(estimate)
            records.extend(estimate.line_items.all())
    unique_records = []
    seen_record_keys = set()
    for instance in records:
        record_key = (instance._meta.label_lower, str(instance.pk))
        if record_key not in seen_record_keys:
            seen_record_keys.add(record_key)
            unique_records.append(instance)
    records = unique_records
    output = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    file_manifest = []
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
        for instance in records:
            _copy_record_files(instance, archive, file_manifest)
        _copy_retained_project_files(project, archive, file_manifest)
        archive.writestr(
            "project-details.pdf",
            _project_details_pdf(project, records, file_manifest),
            compress_type=ZIP_DEFLATED,
        )
    output.seek(0)
    return output


def estimate_history_family(estimate):
    """Collect a linked estimate and every revision in its family."""
    pending = [estimate.pk]
    seen = set()
    while pending:
        pk = pending.pop()
        if pk in seen:
            continue
        seen.add(pk)
        row = Estimate.objects.filter(pk=pk).first()
        if row is None:
            continue
        if row.revision_of_id and row.revision_of_id not in seen:
            pending.append(row.revision_of_id)
        pending.extend(
            Estimate.objects.filter(revision_of_id=row.pk)
            .values_list("pk", flat=True)
        )
    return Estimate.objects.filter(pk__in=seen)


def estimate_history_delete_blocked(project, estimate):
    family = estimate_history_family(estimate)
    family_ids = family.values("pk")
    project_id = getattr(project, "pk", project)
    shared_project = Project.objects.exclude(pk=project_id).filter(
        estimate_id__in=family_ids
    ).exists()
    shared_agreement = Agreement.objects.exclude(project_id=project_id).filter(
        estimate_id__in=family_ids
    ).exists()
    return shared_project or shared_agreement


@transaction.atomic
def permanently_delete_project(project, *, delete_estimate_history=False, actor=None):
    """Delete project-owned rows and optionally its unshared estimate revision family."""
    locked = Project.objects.select_for_update().select_related("estimate").get(pk=project.pk)
    estimate = locked.estimate if delete_estimate_history else None
    if estimate and estimate_history_delete_blocked(locked, estimate):
        raise ValueError("The linked estimate history is referenced by another project or agreement.")
    graph = project_data_graph(locked)
    from .storage_retention import prepare_archived_project_files_for_purge

    prepare_archived_project_files_for_purge(locked, graph, actor=actor)
    # Child-first queryset deletes preserve post_delete file cleanup, including SET_NULL records.
    for instance in reversed(graph[1:]):
        instance.__class__._base_manager.filter(pk=instance.pk).delete()
    locked.delete()
    if estimate:
        family = estimate_history_family(estimate)
        if estimate_history_delete_blocked(locked, estimate):
            raise ValueError("The linked estimate history is referenced by another project or agreement.")
        family.delete()
    return len(graph)
