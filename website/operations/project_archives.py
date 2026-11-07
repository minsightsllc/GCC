"""Export and permanently remove locally stored completed-project data."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path as FilePath
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import FileField
from django.utils import timezone
from django.utils.text import slugify

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


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return str(value)


def _record_payload(instance):
    values = {}
    for field in instance._meta.concrete_fields:
        raw_value = getattr(instance, field.attname)
        if field.many_to_one or field.one_to_one:
            values[field.name + "_id"] = _json_value(raw_value)
        else:
            values[field.name] = _json_value(raw_value)
    many_to_many = {}
    for field in instance._meta.many_to_many:
        manager = getattr(instance, field.name)
        related = list(manager.all())
        if related and related[0]._meta.concrete_model == get_user_model()._meta.concrete_model:
            many_to_many[field.name] = [
                {
                    "id": _json_value(person.pk),
                    "username": person.get_username(),
                    "name": person.get_full_name() or person.get_username(),
                }
                for person in related
            ]
        else:
            many_to_many[field.name] = [_json_value(item.pk) for item in related]
    return {
        "model": instance._meta.label,
        "id": _json_value(instance.pk),
        "fields": values,
        "many_to_many": many_to_many,
    }


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
        digest = hashlib.sha256()
        byte_count = 0
        with storage.open(name, "rb") as source:
            with archive.open(archive_name, "w") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
        file_manifest.append(
            {
                "model": instance._meta.label,
                "record_id": str(instance.pk),
                "field": field.name,
                "stored_name": name,
                "archive_path": archive_name,
                "size_bytes": byte_count,
                "sha256": digest.hexdigest(),
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
        digest = hashlib.sha256()
        byte_count = 0
        with storage_for_alias(item.storage_alias).open(item.storage_name, "rb") as source:
            with archive.open(archive_name, "w") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
        file_manifest.append(
            {
                "model": item.source_model,
                "record_id": item.source_id,
                "field": item.source_field,
                "stored_name": item.storage_name,
                "archive_path": archive_name,
                "size_bytes": byte_count,
                "sha256": digest.hexdigest(),
                "retention_status": item.status,
            }
        )
        included_names.add(item.storage_name)


def project_export_file(project):
    """Build a ZIP of project-owned rows and original local files in a spooled file."""
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
        payloads = []
        for instance in records:
            payloads.append(_record_payload(instance))
            _copy_record_files(instance, archive, file_manifest)
        _copy_retained_project_files(project, archive, file_manifest)
        archive.writestr(
            "records.json",
            json.dumps(payloads, ensure_ascii=False, indent=2, default=str),
        )
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "project_id": str(project.pk),
                    "project_title": project.title,
                    "exported_at": timezone.now().isoformat(),
                    "record_count": len(records),
                    "file_count": len(file_manifest),
                    "files": file_manifest,
                    "scope": (
                        "Project-linked local records and original files. "
                        "General client conversations, external estimate-provider files, "
                        "and external payment data are not included."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
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
