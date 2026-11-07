"""Remove uploaded objects after their database records stop referencing them."""

from __future__ import annotations

import logging

from django.apps import apps
from django.db import transaction
from django.db.models import FileField
from django.db.models.signals import post_delete, post_save, pre_save


logger = logging.getLogger(__name__)
_OLD_FILES_ATTR = "_operations_old_storage_files"
_OLD_POSTER_SNAPSHOT_ATTR = "_operations_old_poster_snapshot"
_OLD_POSTER_DETAILS_ATTR = "_operations_old_poster_details"


def _file_fields(model):
    return [field for field in model._meta.concrete_fields if isinstance(field, FileField)]


def _file_name(instance, field):
    value = getattr(instance, field.attname, None)
    return str(getattr(value, "name", value) or "")


def _snapshot_poster_names(snapshot):
    if not isinstance(snapshot, dict):
        return set()
    return {
        slide["poster_image"]
        for slide in snapshot.get("slides", [])
        if isinstance(slide, dict) and slide.get("poster_image")
    }


def _snapshot_poster_details(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    return {
        slide["poster_image"]: str(slide.get("asset_id", ""))
        for slide in snapshot.get("slides", [])
        if isinstance(slide, dict) and slide.get("poster_image")
    }


def _has_live_poster_reference(name, using):
    post_model = apps.get_model("operations", "WebsiteGalleryPost")
    snapshots = post_model._base_manager.using(using).values_list("published_data", flat=True)
    return any(
        name in _snapshot_poster_names(snapshot)
        for snapshot in snapshots.iterator()
    )


def _delete_file_if_unreferenced(storage, model, field_name, name, using):
    try:
        from .storage_retention import (
            has_active_storage_reference,
            has_storage_retention_reference,
        )

        if has_active_storage_reference(name, using=using):
            return
        if has_storage_retention_reference(name, using=using):
            return
        storage.delete(name)
    except Exception:
        # The database change has already committed. Leave enough detail in the
        # server log to retry cleanup without exposing file names to users.
        logger.exception("Could not delete an unreferenced upload from configured storage.")


def _schedule_file_delete(storage, model, field_name, name, using):
    if not name:
        return
    transaction.on_commit(
        lambda storage=storage, model=model, field_name=field_name, name=name, using=using: (
            _delete_file_if_unreferenced(storage, model, field_name, name, using)
        ),
        using=using,
    )


def _capture_existing_files(sender, instance, using, raw=False, **kwargs):
    if raw or not instance.pk:
        return
    fields = _file_fields(sender)
    if not fields:
        return
    previous = sender._base_manager.using(using).filter(pk=instance.pk).values(
        *(field.attname for field in fields)
    ).first()
    if previous is None:
        return
    setattr(
        instance,
        _OLD_FILES_ATTR,
        {field.attname: (previous[field.attname] or "", field) for field in fields},
    )


def _delete_replaced_files(sender, instance, using, raw=False, update_fields=None, **kwargs):
    previous = getattr(instance, _OLD_FILES_ATTR, None)
    if previous is None:
        return
    delattr(instance, _OLD_FILES_ATTR)
    changed_fields = set(update_fields or ())
    for field in _file_fields(sender):
        if changed_fields and field.name not in changed_fields:
            continue
        old_name, old_field = previous[field.attname]
        new_name = _file_name(instance, field)
        if old_name and old_name != new_name:
            _schedule_file_delete(old_field.storage, sender, field.name, old_name, using)


def _delete_record_files(sender, instance, using, **kwargs):
    for field in _file_fields(sender):
        _schedule_file_delete(field.storage, sender, field.name, _file_name(instance, field), using)


def _capture_published_posters(sender, instance, using, raw=False, **kwargs):
    if raw or not instance.pk:
        return
    previous = sender._base_manager.using(using).filter(pk=instance.pk).values_list(
        "published_data", flat=True
    ).first()
    setattr(instance, _OLD_POSTER_SNAPSHOT_ATTR, _snapshot_poster_names(previous))
    setattr(instance, _OLD_POSTER_DETAILS_ATTR, _snapshot_poster_details(previous))


def _delete_removed_published_posters(sender, instance, using, raw=False, **kwargs):
    previous = getattr(instance, _OLD_POSTER_SNAPSHOT_ATTR, set())
    previous_details = getattr(instance, _OLD_POSTER_DETAILS_ATTR, {})
    removed_names = previous - _snapshot_poster_names(instance.published_data)
    for name in removed_names:
        item_model = apps.get_model("operations", "WebsiteGalleryPostItem")
        field = item_model._meta.get_field("poster_image")
        asset_id = previous_details.get(name)
        item = item_model._base_manager.using(using).filter(
            post_id=instance.pk,
            asset_id=asset_id,
        ).first() if asset_id else None
        if item is not None:
            from .storage_retention import retain_replaced_upload

            retained = retain_replaced_upload(
                item,
                field.name,
                name,
                actor=getattr(instance, "_storage_retention_actor", None),
            )
            if retained is not None:
                continue
        _schedule_file_delete(field.storage, item_model, field.name, name, using)
    if hasattr(instance, _OLD_POSTER_SNAPSHOT_ATTR):
        delattr(instance, _OLD_POSTER_SNAPSHOT_ATTR)
    if hasattr(instance, _OLD_POSTER_DETAILS_ATTR):
        delattr(instance, _OLD_POSTER_DETAILS_ATTR)


def _delete_post_snapshot_posters(sender, instance, using, **kwargs):
    item_model = apps.get_model("operations", "WebsiteGalleryPostItem")
    field = item_model._meta.get_field("poster_image")
    for name in _snapshot_poster_names(instance.published_data):
        _schedule_file_delete(field.storage, item_model, field.name, name, using)


def register_storage_cleanup_signals():
    """Connect cleanup to every operations FileField, including bulk deletes."""
    for model in apps.get_models():
        if model._meta.app_label != "operations" or not _file_fields(model):
            continue
        dispatch = model._meta.label_lower
        pre_save.connect(
            _capture_existing_files,
            sender=model,
            dispatch_uid=f"operations.storage_cleanup.capture.{dispatch}",
            weak=False,
        )
        post_save.connect(
            _delete_replaced_files,
            sender=model,
            dispatch_uid=f"operations.storage_cleanup.replace.{dispatch}",
            weak=False,
        )
        post_delete.connect(
            _delete_record_files,
            sender=model,
            dispatch_uid=f"operations.storage_cleanup.delete.{dispatch}",
            weak=False,
        )

    post_model = apps.get_model("operations", "WebsiteGalleryPost")
    pre_save.connect(
        _capture_published_posters,
        sender=post_model,
        dispatch_uid="operations.storage_cleanup.capture_published_posters",
        weak=False,
    )
    post_save.connect(
        _delete_removed_published_posters,
        sender=post_model,
        dispatch_uid="operations.storage_cleanup.delete_removed_published_posters",
        weak=False,
    )
    post_delete.connect(
        _delete_post_snapshot_posters,
        sender=post_model,
        dispatch_uid="operations.storage_cleanup.delete_post_snapshot_posters",
        weak=False,
    )
