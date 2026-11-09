"""Shared lifecycle operations for uploads backed by Supabase storage."""

from __future__ import annotations

import logging
import mimetypes
import hashlib
import json
from pathlib import PurePosixPath

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage, storages
from django.core.cache import cache
from django.db import transaction
from django.db.models import FileField
from django.utils import timezone

from .models import Activity, MediaAsset, Project, ProjectDocument, StorageTrash


logger = logging.getLogger(__name__)
PROTECTED_FILE_FIELDS = {
    ("operations.agreement", "signed_pdf"),
    ("operations.costentry", "receipt"),
}


class StorageRetentionError(Exception):
    """Expected validation failure in a storage-retention action."""


def storage_for_alias(alias):
    if alias == StorageTrash.StorageAlias.CONTACT:
        return storages["contact_submissions"]
    return default_storage


def storage_alias_for_field(field):
    return (
        StorageTrash.StorageAlias.CONTACT
        if getattr(field.storage, "alias", "") == "contact_submissions"
        else StorageTrash.StorageAlias.DEFAULT
    )


def iter_managed_file_fields():
    """Yield every GCC operations FileField except fields on the retention metadata."""
    for model in apps.get_app_config("operations").get_models():
        if model is StorageTrash:
            continue
        for field in model._meta.concrete_fields:
            if isinstance(field, FileField):
                yield model, field


def has_active_storage_reference(storage_name, *, using="default"):
    if not storage_name:
        return False
    for model, field in iter_managed_file_fields():
        if model._base_manager.using(using).filter(**{field.name: storage_name}).exists():
            return True
    return _has_published_poster_reference(storage_name, using=using)


def _has_published_poster_reference(storage_name, *, using="default"):
    try:
        post_model = apps.get_model("operations", "WebsiteGalleryPost")
    except LookupError:
        return False
    for snapshot in post_model._base_manager.using(using).values_list("published_data", flat=True).iterator():
        if not isinstance(snapshot, dict):
            continue
        if any(
            isinstance(slide, dict) and slide.get("poster_image") == storage_name
            for slide in snapshot.get("slides", [])
        ):
            return True
    return False


def has_storage_retention_reference(storage_name, *, using="default", exclude_id=None):
    if not storage_name:
        return False
    refs = StorageTrash.objects.using(using).filter(storage_name=storage_name)
    if exclude_id:
        refs = refs.exclude(pk=exclude_id)
    return refs.exists()


def is_storage_name_restricted(storage_name, *, using="default"):
    """True while an object is in Trash or the private recovered-files area."""
    return has_storage_retention_reference(storage_name, using=using)


def retained_storage_names(*, using="default"):
    return StorageTrash.objects.using(using).values_list("storage_name", flat=True).distinct()


def hidden_source_ids(model, field_name, *, using="default"):
    """Return source records that point to an object currently held privately."""
    names = retained_storage_names(using=using)
    field = model._meta.get_field(field_name)
    hidden_ids = set(
        StorageTrash.objects.using(using).filter(
            source_model=model._meta.label_lower,
            source_field=field_name,
        ).values_list("source_id", flat=True)
    )
    hidden_ids.update(
        str(pk)
        for pk in model._base_manager.using(using)
        .exclude(**{field.name: ""})
        .filter(**{f"{field.name}__in": names})
        .values_list("pk", flat=True)
    )
    return hidden_ids


def is_source_file_restricted(instance, field_name):
    name = str(getattr(getattr(instance, field_name, None), "name", "") or "")
    if name and is_storage_name_restricted(name, using=instance._state.db or "default"):
        return True
    return StorageTrash.objects.using(instance._state.db or "default").filter(
        source_model=instance._meta.label_lower,
        source_id=str(instance.pk),
        source_field=field_name,
    ).exists()


def _source_project(instance):
    if isinstance(instance, Project):
        return instance
    project = getattr(instance, "project", None)
    if project is not None:
        return project
    post = getattr(instance, "post", None)
    return getattr(post, "project", None) if post is not None else None


def _source_title(instance, field):
    for candidate in ("title", "original_name", "description", "name", "key"):
        value = getattr(instance, candidate, "")
        if value:
            return str(value)[:220]
    return f"{field.verbose_name}: {PurePosixPath(str(getattr(instance, field.name).name)).name}"[:220]


def _sharing_status(instance, field_name):
    if isinstance(instance, MediaAsset):
        destinations = []
        for publication in instance.publications.all():
            if publication.published_at:
                destinations.append("client shared" if publication.destination == "client" else "website published")
        return ", ".join(destinations) or "Private"
    if isinstance(instance, ProjectDocument):
        return "Client visible" if instance.visibility == ProjectDocument.Visibility.CLIENT else "Internal"
    if isinstance(instance, Project) and field_name == "cover":
        return "Public project page" if instance.is_published else "Private project"
    if instance._meta.label_lower == "operations.websitegallerypostitem":
        post = getattr(instance, "post", None)
        return "Website published" if post and post.published_at else "Draft only"
    if instance._meta.label_lower in {"operations.service", "operations.processstep"}:
        return "Public website" if getattr(instance, "is_active", False) else "Hidden from website"
    if instance._meta.label_lower == "operations.leadattachment":
        return "Private lead attachment"
    if instance._meta.label_lower == "operations.agreement":
        return "Signed agreement"
    if instance._meta.label_lower == "operations.costentry":
        return "Financial receipt"
    return "Private"


def _size_or_none(storage, name):
    try:
        return int(storage.size(name))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        logger.info("A managed upload size is currently unavailable.")
        return None


def _s3_size_index():
    """Read the configured bucket once instead of issuing a HEAD per inventory row."""
    if not getattr(settings, "USE_SUPABASE_STORAGE", False):
        return None
    options = getattr(settings, "SUPABASE_STORAGE_OPTIONS", {}) or {}
    required = ("endpoint_url", "access_key", "secret_key", "bucket_name", "region_name")
    if not all(options.get(key) for key in required):
        return {}
    cache_key = "gcc_storage_inventory_sizes_" + hashlib.sha256(
        json.dumps({key: options.get(key) for key in required}, sort_keys=True).encode()
    ).hexdigest()[:18]
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=options["endpoint_url"],
            aws_access_key_id=options["access_key"],
            aws_secret_access_key=options["secret_key"],
            region_name=options["region_name"],
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=4,
                read_timeout=8,
                retries={"max_attempts": 1},
            ),
        )
        sizes = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=options["bucket_name"]):
            sizes.update({
                item["Key"]: int(item["Size"])
                for item in page.get("Contents", [])
                if item.get("Key") and item.get("Size") is not None
            })
    except Exception:
        logger.info("Configured file-storage sizes are temporarily unavailable.")
        sizes = {}
    cache.set(cache_key, sizes, 5 * 60)
    return sizes


def _record_activity(message, detail, actor=None):
    # Deliberately do not attach Activity.project: project deletion would cascade
    # these retention audit entries away with the archived project.
    Activity.objects.create(message=message[:220], detail=detail[:220], actor=actor)


def _protected_file(model, field_name):
    return (model._meta.label_lower, field_name) in PROTECTED_FILE_FIELDS


def _post_poster_snapshot_clear(item, storage_name):
    post = item.post
    snapshot = post.published_data if isinstance(post.published_data, dict) else {}
    slides = snapshot.get("slides", [])
    changed = False
    for slide in slides:
        if isinstance(slide, dict) and slide.get("poster_image") == storage_name:
            slide["poster_image"] = ""
            changed = True
    if changed:
        post.published_data = snapshot
        post.save(update_fields=["published_data", "updated_at"])


@transaction.atomic
def move_file_to_trash(model_label, record_id, field_name, *, actor=None):
    """Detach one upload from its record and retain the object for recovery."""
    try:
        app_label, model_name = model_label.split(".", 1)
        model = apps.get_model(app_label, model_name)
    except (ValueError, LookupError) as exc:
        raise StorageRetentionError("That file record is unavailable.") from exc
    if model._meta.app_label != "operations" or model is StorageTrash:
        raise StorageRetentionError("That file cannot be moved to Storage Trash.")
    try:
        field = model._meta.get_field(field_name)
    except Exception as exc:
        raise StorageRetentionError("That file field is unavailable.") from exc
    if not isinstance(field, FileField):
        raise StorageRetentionError("That record does not contain a managed file.")
    if _protected_file(model, field_name):
        raise StorageRetentionError("Signed agreements and financial receipts can only be removed with their archived project.")

    instance = model._base_manager.select_for_update().filter(pk=record_id).first()
    if instance is None:
        raise StorageRetentionError("That file record no longer exists.")
    stored_file = getattr(instance, field_name)
    storage_name = str(getattr(stored_file, "name", "") or "")
    if not storage_name:
        raise StorageRetentionError("That record no longer has a file to move.")

    storage = field.storage
    project = _source_project(instance)
    record_title = _source_title(instance, field)
    content_type = mimetypes.guess_type(storage_name)[0] or "application/octet-stream"
    trash_item = StorageTrash.objects.create(
        storage_name=storage_name,
        storage_alias=storage_alias_for_field(field),
        original_name=PurePosixPath(storage_name.replace("\\", "/")).name[:255],
        size_bytes=_size_or_none(storage, storage_name),
        content_type=content_type[:160],
        source_model=model._meta.label_lower,
        source_id=str(instance.pk),
        source_field=field_name,
        record_title=record_title,
        project_id_snapshot=str(project.pk) if project else "",
        project_title_snapshot=str(project.title)[:220] if project else "",
        sharing_status=_sharing_status(instance, field_name)[:160],
        moved_by=actor if getattr(actor, "is_authenticated", False) else None,
    )

    if isinstance(instance, MediaAsset):
        from .publishing import unpublish

        for publication in instance.publications.select_for_update().all():
            if publication.published_at or publication.published_data:
                unpublish(publication)
        instance.visibility = MediaAsset.Visibility.INTERNAL
    elif isinstance(instance, ProjectDocument):
        instance.visibility = ProjectDocument.Visibility.INTERNAL
    elif model._meta.label_lower == "operations.websitegallerypostitem":
        _post_poster_snapshot_clear(instance, storage_name)

    setattr(instance, field_name, "")
    update_fields = [field_name]
    if hasattr(instance, "visibility") and field_name != "visibility":
        update_fields.append("visibility")
    if any(item.name == "updated_at" for item in model._meta.concrete_fields):
        update_fields.append("updated_at")
    instance.save(update_fields=update_fields)
    detail = f"{record_title} · {trash_item.original_name}"
    if project:
        detail = f"{project.title} · {detail}"
    _record_activity("File moved to Storage Trash", detail, actor)
    return trash_item


@transaction.atomic
def retain_replaced_upload(instance, field_name, storage_name, *, actor=None):
    """Put an old replacement in Trash once no live record or public snapshot uses it."""
    if not storage_name:
        return None
    model = type(instance)
    field = model._meta.get_field(field_name)
    if not isinstance(field, FileField) or _protected_file(model, field_name):
        return None
    database = instance._state.db or "default"
    if has_active_storage_reference(storage_name, using=database):
        return None
    existing = StorageTrash.objects.using(database).filter(storage_name=storage_name).first()
    if existing:
        return existing
    project = _source_project(instance)
    record_title = _source_title(instance, field)
    trash_item = StorageTrash.objects.using(database).create(
        storage_name=storage_name,
        storage_alias=storage_alias_for_field(field),
        original_name=PurePosixPath(storage_name.replace("\\", "/")).name[:255],
        size_bytes=_size_or_none(field.storage, storage_name),
        content_type=(mimetypes.guess_type(storage_name)[0] or "application/octet-stream")[:160],
        source_model=model._meta.label_lower,
        source_id=str(instance.pk),
        source_field=field_name,
        record_title=record_title,
        project_id_snapshot=str(project.pk) if project else "",
        project_title_snapshot=str(project.title)[:220] if project else "",
        sharing_status=_sharing_status(instance, field_name)[:160],
        moved_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    detail = f"{record_title} · {trash_item.original_name}"
    if project:
        detail = f"{project.title} · {detail}"
    _record_activity("Replaced file moved to Storage Trash", detail, actor)
    return trash_item


@transaction.atomic
def retain_lead_attachments_before_delete(lead, *, actor=None):
    """Keep uploaded lead files recoverable when the lead record is removed."""
    from .models import LeadAttachment

    attachments = LeadAttachment.objects.select_for_update().filter(lead=lead).order_by("pk")
    retained_count = 0
    for attachment in attachments:
        if attachment.file:
            move_file_to_trash(
                "operations.leadattachment",
                attachment.pk,
                "file",
                actor=actor,
            )
            retained_count += 1
    return retained_count


def _source_instance(item, *, lock=False):
    try:
        app_label, model_name = item.source_model.split(".", 1)
        model = apps.get_model(app_label, model_name)
        if model._meta.app_label != "operations" or model is StorageTrash:
            return None, None
        instance = model._base_manager
        if lock:
            instance = instance.select_for_update()
        instance = instance.filter(pk=item.source_id).first()
        if instance is None:
            return None, None
        field = model._meta.get_field(item.source_field)
        if not isinstance(field, FileField):
            return None, None
        return instance, field
    except Exception:
        return None, None


@transaction.atomic
def restore_storage_item(item_id, *, actor=None):
    item = StorageTrash.objects.select_for_update().filter(pk=item_id).first()
    if item is None:
        raise StorageRetentionError("That retained file no longer exists.")
    if item.status == StorageTrash.Status.RECOVERED:
        raise StorageRetentionError("That file is already in Recovered files.")
    item.status = StorageTrash.Status.RECOVERED
    item.restored_by = actor if getattr(actor, "is_authenticated", False) else None
    item.restored_at = timezone.now()
    item.last_error = ""
    item.save(update_fields=["status", "restored_by", "restored_at", "last_error", "updated_at"])
    _record_activity("File restored to Recovered files", f"{item.record_title} · {item.original_name}", actor)
    return item


@transaction.atomic
def move_recovered_file_to_trash(item_id, *, actor=None):
    item = StorageTrash.objects.select_for_update().filter(pk=item_id).first()
    if item is None or item.status != StorageTrash.Status.RECOVERED:
        raise StorageRetentionError("That recovered file is unavailable.")
    item.status = StorageTrash.Status.TRASHED
    item.moved_by = actor if getattr(actor, "is_authenticated", False) else None
    item.moved_at = timezone.now()
    item.last_error = ""
    item.save(update_fields=["status", "moved_by", "moved_at", "last_error", "updated_at"])
    _record_activity("Recovered file moved to Storage Trash", f"{item.record_title} · {item.original_name}", actor)
    return item


@transaction.atomic
def reattach_recovered_file(item_id, *, actor=None):
    item = StorageTrash.objects.select_for_update().filter(pk=item_id).first()
    if item is None or item.status != StorageTrash.Status.RECOVERED:
        raise StorageRetentionError("Restore the file before reattaching it.")
    instance, field = _source_instance(item, lock=True)
    if instance is None:
        raise StorageRetentionError("The original record was deleted. This file remains available in Recovered files for download or permanent deletion.")
    current_file = getattr(instance, field.name)
    if getattr(current_file, "name", ""):
        raise StorageRetentionError("The original record already has a file. This recovered file was left private.")
    setattr(instance, field.name, item.storage_name)
    update_fields = [field.name]
    if any(existing.name == "updated_at" for existing in instance._meta.concrete_fields):
        update_fields.append("updated_at")
    instance.save(update_fields=update_fields)
    title = item.record_title
    original_name = item.original_name
    item.delete()
    _record_activity("Recovered file reattached", f"{title} · {original_name}", actor)
    return instance


def _safe_delete_storage_object(item):
    storage = storage_for_alias(item.storage_alias)
    try:
        if storage.exists(item.storage_name):
            storage.delete(item.storage_name)
    except Exception as exc:
        logger.exception("Could not permanently delete a file from Storage Trash.")
        item.status = StorageTrash.Status.PURGE_FAILED
        item.last_error = "Storage deletion failed. Retry this file from Storage & Retention."
        item.save(update_fields=["status", "last_error", "updated_at"])
        return "failed"
    return "deleted"


@transaction.atomic
def permanently_delete_storage_item(item_id, *, actor=None):
    item = StorageTrash.objects.select_for_update().filter(pk=item_id).first()
    if item is None:
        raise StorageRetentionError("That Trash item no longer exists.")
    if item.status not in {StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED}:
        raise StorageRetentionError("Recovered files must be moved to Trash before permanent deletion.")
    has_active_reference = has_active_storage_reference(
        item.storage_name,
        using=item._state.db or "default",
    )
    has_other_retention = has_storage_retention_reference(
        item.storage_name,
        using=item._state.db or "default",
        exclude_id=item.pk,
    )
    title = item.record_title
    original_name = item.original_name
    if has_active_reference or has_other_retention:
        item.delete()
        reason = "another record still uses this file" if has_active_reference else "another Trash item still retains this file"
        _record_activity("Trash item removed", f"{title} · {original_name}; storage retained because {reason}.", actor)
        return "retained"

    result = _safe_delete_storage_object(item)
    if result == "failed":
        return "failed"
    item.delete()
    _record_activity("File permanently deleted from Storage Trash", f"{title} · {original_name}", actor)
    return "deleted"


def empty_storage_trash(*, actor=None):
    """Purge each trash object independently so failures remain retryable."""
    results = {"deleted": 0, "retained": 0, "failed": 0}
    item_ids = list(
        StorageTrash.objects.filter(
            status__in=[StorageTrash.Status.TRASHED, StorageTrash.Status.PURGE_FAILED]
        ).values_list("pk", flat=True)
    )
    for item_id in item_ids:
        try:
            results[permanently_delete_storage_item(item_id, actor=actor)] += 1
        except StorageRetentionError:
            results["failed"] += 1
    if results["deleted"]:
        from .admin_usage import invalidate_admin_storage_usage_cache

        invalidate_admin_storage_usage_cache()
    return results


def prepare_archived_project_files_for_purge(project, graph, *, actor=None):
    """Retain every project upload until post-commit storage deletion succeeds."""
    created_or_existing = {}
    database = project._state.db or "default"
    for instance in graph:
        for field in instance._meta.concrete_fields:
            if not isinstance(field, FileField):
                continue
            stored_file = getattr(instance, field.name)
            storage_name = str(getattr(stored_file, "name", "") or "")
            if not storage_name:
                continue
            alias = storage_alias_for_field(field)
            existing = StorageTrash.objects.using(database).filter(
                storage_alias=alias,
                storage_name=storage_name,
            ).first()
            if existing:
                created_or_existing[str(existing.pk)] = existing
                continue
            item = StorageTrash.objects.using(database).create(
                storage_name=storage_name,
                storage_alias=alias,
                original_name=PurePosixPath(storage_name.replace("\\", "/")).name[:255],
                size_bytes=_size_or_none(field.storage, storage_name),
                content_type=(mimetypes.guess_type(storage_name)[0] or "application/octet-stream")[:160],
                source_model=instance._meta.label_lower,
                source_id=str(instance.pk),
                source_field=field.name,
                record_title=_source_title(instance, field),
                project_id_snapshot=str(project.pk),
                project_title_snapshot=str(project.title)[:220],
                sharing_status=_sharing_status(instance, field.name)[:160],
                status=StorageTrash.Status.TRASHED,
                moved_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            created_or_existing[str(item.pk)] = item
    for item in StorageTrash.objects.using(database).filter(
        project_id_snapshot=str(project.pk)
    ):
        created_or_existing[str(item.pk)] = item
    ids = list(created_or_existing)
    StorageTrash.objects.using(database).filter(pk__in=ids).update(
        status=StorageTrash.Status.TRASHED,
        project_id_snapshot="",
        last_error="",
        updated_at=timezone.now(),
    )

    def purge_after_commit(ids=tuple(ids), actor_id=getattr(actor, "pk", None), database=database):
        from django.contrib.auth import get_user_model

        actor_record = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
        for item_id in ids:
            try:
                permanently_delete_storage_item(item_id, actor=actor_record)
            except StorageRetentionError:
                logger.info("An archived project upload remained in Storage Trash for manual review.")
        from .admin_usage import invalidate_admin_storage_usage_cache

        invalidate_admin_storage_usage_cache()

    transaction.on_commit(purge_after_commit, using=database)


def storage_file_rows():
    """Build inventory rows for active uploads and Trash/Recovered references."""
    size_index = _s3_size_index()
    retained_by_key = {}
    for item in StorageTrash.objects.all():
        key = item.storage_name
        retained_by_key.setdefault(key, []).append(item)

    rows_by_key = {}
    for model, field in iter_managed_file_fields():
        related = []
        for relation in model._meta.get_fields():
            if getattr(relation, "many_to_one", False) and not relation.auto_created:
                related.append(relation.name)
        queryset = model._base_manager.all()
        if related:
            queryset = queryset.select_related(*related)
        if model._meta.label_lower == "operations.mediaasset":
            queryset = queryset.select_related("website_post_item__post")
        if model._meta.label_lower == "operations.websitegallerypostitem":
            queryset = queryset.select_related("post", "asset")
        for instance in queryset.iterator(chunk_size=200):
            stored_file = getattr(instance, field.name)
            storage_name = str(getattr(stored_file, "name", "") or "")
            if not storage_name:
                continue
            alias = storage_alias_for_field(field)
            key = storage_name
            if key in retained_by_key:
                continue
            record_title = _source_title(instance, field)
            public_post_item = (
                getattr(instance, "website_post_item", None)
                if isinstance(instance, MediaAsset)
                else None
            )
            if public_post_item is not None:
                record_title = public_post_item.post.title
            project = _source_project(instance)
            if size_index is not None:
                size = size_index.get(storage_name)
            else:
                try:
                    size = int(field.storage.size(storage_name))
                except (FileNotFoundError, OSError, ValueError, TypeError):
                    size = None
            content_type = mimetypes.guess_type(storage_name)[0] or "application/octet-stream"
            row = rows_by_key.get(key)
            reference = {
                "source_model": model._meta.label_lower,
                "source_id": str(instance.pk),
                "source_field": field.name,
                "label": record_title,
                "project_title": str(project.title) if project else "",
            }
            if row is None:
                rows_by_key[key] = {
                    "key": f"{model._meta.label_lower}:{instance.pk}:{field.name}",
                    "status": "active",
                    "storage_name": storage_name,
                    "storage_alias": alias,
                    "original_name": PurePosixPath(storage_name.replace("\\", "/")).name,
                    "size_bytes": size,
                    "content_type": content_type,
                    "type_label": content_type.split("/", 1)[0].title() if "/" in content_type else "File",
                    "record_title": record_title,
                    "record_type": (
                        "Public Gallery Media"
                        if public_post_item is not None
                        else str(model._meta.verbose_name).title()
                    ),
                    "project_title": str(project.title) if project else "",
                    "sharing_status": _sharing_status(instance, field.name),
                    "protected": _protected_file(model, field.name),
                    "references": [reference],
                    "source_model": model._meta.label_lower,
                    "source_id": str(instance.pk),
                    "source_field": field.name,
                    "_source_record": instance,
                }
            else:
                row["references"].append(reference)
                # A legacy project asset and its detached public-copy record can
                # intentionally share one stored object. Prefer the independent
                # public post as the inventory destination when both are found.
                if public_post_item is not None:
                    row.update({
                        "record_title": public_post_item.post.title,
                        "record_type": "Public Gallery Media",
                        "project_title": "",
                        "sharing_status": _sharing_status(instance, field.name),
                        "source_model": model._meta.label_lower,
                        "source_id": str(instance.pk),
                        "source_field": field.name,
                        "_source_record": instance,
                    })

    rows = list(rows_by_key.values())
    for key, items in retained_by_key.items():
        item = items[0]
        instance, field = _source_instance(item)
        available = bool(instance is not None and not getattr(getattr(instance, item.source_field, None), "name", ""))
        rows.append({
            "key": f"retained:{item.pk}",
            "id": item.pk,
            "status": item.status,
            "storage_name": item.storage_name,
            "storage_alias": item.storage_alias,
            "original_name": item.original_name,
            "size_bytes": item.size_bytes,
            "content_type": item.content_type or "application/octet-stream",
            "type_label": (item.content_type or "file").split("/", 1)[0].title(),
            "record_title": item.record_title or item.original_name,
            "record_type": item.source_model.rsplit(".", 1)[-1].replace("_", " ").title(),
            "project_title": item.project_title_snapshot,
            "sharing_status": item.sharing_status,
            "protected": False,
            "references": [{
                "source_model": item.source_model,
                "source_id": item.source_id,
                "source_field": item.source_field,
                "label": item.record_title,
                "project_title": item.project_title_snapshot,
            }],
            "source_model": item.source_model,
            "source_id": item.source_id,
            "source_field": item.source_field,
            "_source_record": instance,
            "can_reattach": available and item.status == StorageTrash.Status.RECOVERED,
            "trash_count": len(items),
        })
    return rows
