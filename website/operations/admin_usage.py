"""Cached, server-side storage usage summaries for the private admin home."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


logger = logging.getLogger("operations.admin_usage")
_CACHE_TIMEOUT_SECONDS = 15 * 60
_BYTES_PER_GB = 1_000_000_000


def get_admin_storage_usage() -> dict:
    """Return the cached Supabase and Render usage/capacity cards."""
    cache_key = _cache_key()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    checked_at = timezone.now()
    with ThreadPoolExecutor(max_workers=2) as executor:
        supabase_future = executor.submit(_get_supabase_usage)
        render_future = executor.submit(_get_render_postgres_usage)
        supabase = supabase_future.result()
        render = render_future.result()

    result = {
        "meters": [_format_meter(supabase), _format_meter(render)],
        "checked_at": checked_at,
    }
    transient_failure = any(
        meter.get("status") in {"unavailable", "quota_unavailable"}
        for meter in (supabase, render)
    )
    cache.set(cache_key, result, 60 if transient_failure else _CACHE_TIMEOUT_SECONDS)
    return result


def invalidate_admin_storage_usage_cache() -> None:
    """Refresh the Supabase/PostgreSQL meter after a retention purge."""
    cache.delete(_cache_key())


def get_admin_translation_usage() -> dict:
    """Return this app's current-month Google message-translation counter."""
    from django.db import DatabaseError

    from .message_translations import MONTHLY_CHARACTER_LIMIT
    from .models import MessageTranslationUsage

    month_start = timezone.localdate().replace(day=1)
    next_month = month_start + timedelta(days=32)
    reset_date = next_month.replace(day=1)
    month_label = month_start.strftime("%B %Y")
    try:
        usage = MessageTranslationUsage.objects.filter(
            month_start=month_start
        ).first()
    except DatabaseError:
        return {
            "status": "unavailable",
            "month_label": month_label,
            "reset_date": reset_date,
        }

    used = max(int(usage.characters_reserved if usage else 0), 0)
    remaining = max(MONTHLY_CHARACTER_LIMIT - used, 0)
    percent = min(used / MONTHLY_CHARACTER_LIMIT * 100, 100)
    if not getattr(settings, "GCC_GOOGLE_TRANSLATE_API_KEY", ""):
        status = "not_configured"
    elif used >= MONTHLY_CHARACTER_LIMIT:
        status = "limit_reached"
    else:
        status = "ready"

    return {
        "status": status,
        "used": used,
        "used_display": f"{used:,}",
        "limit": MONTHLY_CHARACTER_LIMIT,
        "limit_display": f"{MONTHLY_CHARACTER_LIMIT:,}",
        "remaining": remaining,
        "remaining_display": f"{remaining:,}",
        "percent": percent,
        "month_label": month_label,
        "reset_date": reset_date,
        "updated_at": usage.updated_at if usage else None,
    }


def _cache_key() -> str:
    """Separate cached values when an environment's resource configuration changes."""
    options = getattr(settings, "SUPABASE_STORAGE_OPTIONS", {}) or {}
    stable_config = {
        "endpoint": options.get("endpoint_url", ""),
        "bucket": options.get("bucket_name", ""),
        "supabase_org": getattr(settings, "SUPABASE_ORG_SLUG", ""),
        "supabase_token": hashlib.sha256(
            getattr(settings, "SUPABASE_MANAGEMENT_API_TOKEN", "").encode()
        ).hexdigest()[:16],
        "render_resource": getattr(settings, "RENDER_POSTGRES_ID", ""),
    }
    digest = hashlib.sha256(json.dumps(stable_config, sort_keys=True).encode()).hexdigest()[:16]
    return f"gcc_admin_storage_usage_v2_{digest}"


def _get_supabase_usage() -> dict:
    options = getattr(settings, "SUPABASE_STORAGE_OPTIONS", {}) or {}
    endpoint = options.get("endpoint_url")
    access_key = options.get("access_key")
    secret_key = options.get("secret_key")
    region = options.get("region_name")
    if not all((endpoint, access_key, secret_key, region)):
        return {"status": "not_configured", "source": "supabase"}

    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=4,
                read_timeout=8,
                retries={"max_attempts": 1},
            ),
        )

        # Supabase's storage quota covers every bucket in the project, so list
        # all project buckets instead of reporting only GCC's configured one.
        bucket_names = [
            item["Name"]
            for item in client.list_buckets().get("Buckets", [])
            if item.get("Name")
        ]
        used_bytes = 0
        for bucket_name in bucket_names:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name):
                used_bytes += sum(
                    int(item.get("Size", 0))
                    for item in page.get("Contents", [])
                    if item.get("Size") is not None
                )
    except Exception:  # Provider exceptions vary across boto3 versions.
        return {"status": "unavailable", "source": "supabase"}

    quota_gb, quota_status = _get_supabase_plan_quota()
    if quota_gb is None:
        return {
            "status": quota_status,
            "source": "supabase",
            "used_bytes": used_bytes,
        }
    return {
        "status": "ready",
        "source": "supabase",
        "used_bytes": used_bytes,
        "capacity_bytes": int(quota_gb * _BYTES_PER_GB),
    }


def _get_supabase_plan_quota() -> tuple[float | None, str]:
    """Fetch the organization plan and map its included storage quota."""
    token = getattr(settings, "SUPABASE_MANAGEMENT_API_TOKEN", "")
    org_slug = getattr(settings, "SUPABASE_ORG_SLUG", "")
    if not token or not org_slug:
        return None, "quota_not_configured"

    request = Request(
        f"https://api.supabase.com/v1/organizations/{quote(org_slug, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            organization = json.loads(response.read().decode("utf-8"))
        plan = str(organization.get("plan", "")).strip().lower()
    except Exception:
        return None, "quota_unavailable"

    # Supabase publishes the included Storage Size allowance for these plans.
    # Enterprise is custom, so don't guess a limit for it.
    quotas = {"free": 1.0, "pro": 100.0, "team": 100.0}
    quota_gb = quotas.get(plan)
    if quota_gb is None:
        if plan == "enterprise":
            return None, "quota_custom"
        return None, "quota_unavailable"
    return quota_gb, "ready"


def _get_render_postgres_usage() -> dict:
    api_key = getattr(settings, "RENDER_API_KEY", "")
    postgres_id = getattr(settings, "RENDER_POSTGRES_ID", "")
    if not api_key or not postgres_id:
        return {"status": "not_configured", "source": "render"}

    end_time = timezone.now()
    params = urlencode(
        {
            "startTime": (end_time - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "endTime": end_time.isoformat().replace("+00:00", "Z"),
            "resolutionSeconds": 60,
            "resource": postgres_id,
        }
    )

    def fetch(metric_name: str):
        request = Request(
            f"https://api.render.com/v1/metrics/{metric_name}?{params}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            usage_future = executor.submit(fetch, "disk-usage")
            capacity_future = executor.submit(fetch, "disk-capacity")
            used_value, used_unit = _latest_metric(usage_future.result())
            capacity_value, capacity_unit = _latest_metric(capacity_future.result())
        used_bytes = _to_bytes(used_value, used_unit)
        capacity_bytes = _to_bytes(capacity_value, capacity_unit)
        if used_bytes < 0 or capacity_bytes <= 0:
            raise ValueError("Render returned an invalid disk metric")
    except HTTPError as exc:
        logger.warning("Render Postgres metrics returned HTTP %s.", exc.code)
        return {"status": "unavailable", "source": "render"}
    except Exception as exc:  # Keep provider/schema failures from breaking admin access.
        logger.warning("Render Postgres usage refresh failed (%s).", type(exc).__name__)
        return {"status": "unavailable", "source": "render"}

    return {
        "status": "ready",
        "source": "render",
        "used_bytes": used_bytes,
        "capacity_bytes": capacity_bytes,
    }


def _latest_metric(payload) -> tuple[float, str]:
    """Read the newest value and unit from Render's metric series response."""
    points = []

    def visit(value, inherited_unit=""):
        if isinstance(value, dict):
            unit = value.get("unit") or inherited_unit
            samples = value.get("values")
            if isinstance(samples, list):
                for sample in samples:
                    if isinstance(sample, dict) and "value" in sample:
                        points.append((sample.get("timestamp", ""), sample["value"], unit))
                    elif isinstance(sample, (list, tuple)) and len(sample) >= 2:
                        points.append((sample[0], sample[1], unit))
            for key, child in value.items():
                if key != "values":
                    visit(child, unit)
        elif isinstance(value, list):
            for child in value:
                visit(child, inherited_unit)

    visit(payload)
    valid = []
    for timestamp, value, unit in points:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            valid.append((str(timestamp), number, str(unit or "")))
    if not valid:
        raise ValueError("Render returned no disk metric values")
    _, value, unit = max(valid, key=lambda point: point[0])
    if not unit:
        raise ValueError("Render disk metric omitted its unit")
    return value, unit


def _to_bytes(value: float, unit: str) -> int:
    normalized = unit.strip().lower().replace("bytes", "b").replace("byte", "b")
    factors = {
        "b": 1,
        "kb": 1_000,
        "mb": 1_000_000,
        "gb": 1_000_000_000,
        "tb": 1_000_000_000_000,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    if normalized not in factors:
        raise ValueError("Render returned an unknown disk metric unit")
    return int(round(value * factors[normalized]))


def _format_meter(meter: dict) -> dict:
    """Add safe presentation values while preserving explicit error states."""
    formatted = {**meter, "used_gb": None, "capacity_gb": None, "remaining_gb": None, "percent": None}
    if meter.get("used_bytes") is not None:
        formatted["used_gb"] = meter["used_bytes"] / _BYTES_PER_GB
    if meter.get("capacity_bytes") is not None:
        capacity = meter["capacity_bytes"]
        used = meter["used_bytes"]
        formatted["capacity_gb"] = capacity / _BYTES_PER_GB
        formatted["remaining_gb"] = max(capacity - used, 0) / _BYTES_PER_GB
        formatted["percent"] = min(used / capacity * 100, 100)
    return formatted
