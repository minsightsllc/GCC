from __future__ import annotations

from collections import defaultdict

from django.apps import apps
from django.core.cache import cache
from django.db import connection


TABLE_ESTIMATE_CACHE_KEY = "gcc_database_table_sizes_v1"
TABLE_ESTIMATE_CACHE_SECONDS = 15 * 60


def get_postgres_table_estimates() -> dict:
    """Return catalog-based PostgreSQL table and index sizes, never table scans."""
    if connection.vendor != "postgresql":
        return {"status": "unsupported", "categories": [], "largest_tables": []}

    cached = cache.get(TABLE_ESTIMATE_CACHE_KEY)
    if cached is not None:
        return cached

    table_models = {}
    for model in apps.get_models(include_auto_created=True):
        table_models.setdefault(model._meta.db_table, model)

    query = """
        SELECT
            c.relname,
            pg_total_relation_size(c.oid),
            pg_table_size(c.oid),
            pg_indexes_size(c.oid),
            COALESCE(s.n_live_tup, 0)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables AS s ON s.relid = c.oid
        WHERE c.relkind IN ('r', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
        ORDER BY pg_total_relation_size(c.oid) DESC
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()

    categories = defaultdict(
        lambda: {"name": "", "bytes": 0, "table_bytes": 0, "index_bytes": 0, "estimated_rows": 0, "table_count": 0}
    )
    tables = []
    for table_name, total_bytes, table_bytes, index_bytes, estimated_rows in rows:
        model = table_models.get(table_name)
        category = _category_for_model(model, table_name)
        bucket = categories[category]
        bucket["name"] = category
        bucket["bytes"] += int(total_bytes or 0)
        bucket["table_bytes"] += int(table_bytes or 0)
        bucket["index_bytes"] += int(index_bytes or 0)
        bucket["estimated_rows"] += max(int(estimated_rows or 0), 0)
        bucket["table_count"] += 1
        tables.append(
            {
                "name": table_name,
                "category": category,
                "bytes": int(total_bytes or 0),
                "table_bytes": int(table_bytes or 0),
                "index_bytes": int(index_bytes or 0),
                "estimated_rows": max(int(estimated_rows or 0), 0),
            }
        )

    result = {
        "status": "ready",
        "categories": sorted(categories.values(), key=lambda item: item["bytes"], reverse=True),
        "largest_tables": tables[:12],
        "total_bytes": sum(table["bytes"] for table in tables),
        "estimated_rows": sum(table["estimated_rows"] for table in tables),
    }
    cache.set(TABLE_ESTIMATE_CACHE_KEY, result, TABLE_ESTIMATE_CACHE_SECONDS)
    return result


def _category_for_model(model, table_name: str) -> str:
    if model is None:
        return "Other database records"

    name = model.__name__.casefold()
    app_label = model._meta.app_label.casefold()
    if name in {"activity", "adminsecurityevent", "adminaccessblock", "adminsecurityprofile", "adminrecoverytoken"}:
        return "Audit & security"
    if any(token in name for token in ("client", "lead", "message", "invite", "translation")):
        return "Clients, leads & messages"
    if any(token in name for token in ("employee", "subcontractor", "schedule", "timeentry", "timelog")):
        return "Team & workforce"
    if any(token in name for token in ("site", "service", "process", "gallery", "publication", "review")):
        return "Public website"
    if any(token in name for token in ("project", "estimate", "document", "media", "milestone", "payment", "task", "cost", "permit", "inspection", "warranty", "agreement", "selection")):
        return "Projects & operations"
    if app_label in {"auth", "admin", "contenttypes", "sessions", "guardian", "otp_totp"}:
        return "Accounts & system"
    if app_label == "operations":
        return "Operations records"
    return "Other database records"
