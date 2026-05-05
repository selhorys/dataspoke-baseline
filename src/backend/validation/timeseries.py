"""SQL-based timeseries engine for validation rules.

Supports executing SQL against PostgreSQL data sources to compute
timeseries metrics for the sql_timeseries custom rule subtype and for the
``source: query`` mode of freshness and volume rules.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import IngestionConfig
from src.shared.exceptions import EntityNotFoundError

logger = logging.getLogger(__name__)

# Canonical identifier regex — single source of truth for the validation package.
# Anchored with \A/\Z (not ^/$) and capped at 63 chars (PostgreSQL NAMEDATALEN-1).
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def quote_table_ref(platform: str, identifier: dict[str, Any]) -> str:
    """Build a quoted "schema"."table" reference for SQL execution.

    Validates schema_name and table against _IDENTIFIER_RE (PostgreSQL identifier
    rules, NAMEDATALEN=64). Raises ValueError on missing or malformed parts.
    """
    if platform != "postgres":
        raise NotImplementedError(f"Table ref quoting not supported for {platform}")
    schema = identifier.get("schema_name") or identifier.get("schema")
    table = identifier.get("table")
    if not schema or not _IDENTIFIER_RE.match(schema):
        raise ValueError(f"invalid schema_name: {schema!r}")
    if not table or not _IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid table: {table!r}")
    return f'"{schema}"."{table}"'


async def resolve_source_config(
    db: AsyncSession,
    dataset_urn: str,
    rule: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Return (platform, locator, identifier, auth) for SQL execution.

    Looks up the ingestion config for dataset_urn in the DB.
    Raises EntityNotFoundError if no ingestion config is available.
    """
    result = await db.execute(
        select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise EntityNotFoundError("config", dataset_urn)

    return row.platform, row.locator, row.identifier, row.auth


async def execute_sql(
    platform: str,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    sql: str,
) -> list[dict[str, Any]]:
    """Execute SQL against the source and return a list of row dicts.

    Currently supports PostgreSQL only.  Other platforms raise NotImplementedError.
    """
    if platform == "postgres":
        return await _execute_postgresql(locator, identifier, auth, sql)

    raise NotImplementedError(f"SQL execution not supported for {platform}")


def _resolve_postgresql_password(auth: dict[str, Any] | None) -> str:
    """Resolve the plaintext password from auth, mirroring the ingestion path.

    Handles the persisted reference shape ``{username, secret_ref: {name, key}}``.
    Raises on resolution failure so callers turn it into an ERROR result.
    """
    if not auth:
        return ""

    secret_ref = auth.get("secret_ref")
    if not secret_ref:
        return ""

    if not isinstance(secret_ref, dict):
        raise ValueError(f"invalid secret_ref shape in auth: {type(secret_ref).__name__}")

    name = secret_ref.get("name")
    key = secret_ref.get("key")
    if not name or not key:
        raise ValueError("auth.secret_ref missing name or key")

    from src.backend.ingestion.secret_resolver import (
        SecretRefMalformed,
        SecretRefNotFound,
        SecretResolverUnavailable,
        resolve_secret_ref,
    )

    try:
        return resolve_secret_ref(f"k8s-secret/{name}/{key}")
    except (SecretRefMalformed, SecretRefNotFound, SecretResolverUnavailable) as exc:
        raise RuntimeError("secret resolution failed for auth credentials") from exc


async def _execute_postgresql(
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    sql: str,
) -> list[dict[str, Any]]:
    """Connect to PostgreSQL via asyncpg, execute sql, return list of row dicts."""
    import asyncpg

    host = locator["host"]
    port = locator["port"]
    database = identifier.get("database", "")
    username = auth.get("username", "") if auth else ""
    password = _resolve_postgresql_password(auth)

    conn = await asyncpg.connect(
        host=host, port=port, user=username, password=password, database=database,
    )
    try:
        rows = await conn.fetch(sql)
    finally:
        await conn.close()

    return [dict(row) for row in rows]


async def execute_timeseries_sql(
    db: AsyncSession,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
) -> dict[str, Any]:
    """Full sql_timeseries execution pipeline.

    Steps:
    1. Resolve source connection config (rule override or ingestion config).
    2. Execute ``rule["sql"]`` against the source.
    3. If result is empty, return empty partitions/values.
    4. Resolve the target partition row: if caller specified a partition,
       filter rows to match; otherwise sort by ``rule["order"]`` columns
       descending and take the first row (latest partition).
    5. Extract ``rule["values"]`` columns from the resolved row.
    6. Return ``{"partitions": {...}, "values": {...}}``.
    """
    platform, locator, identifier, auth = await resolve_source_config(
        db, dataset_urn, rule
    )

    sql = rule.get("sql", "")
    rows = await execute_sql(platform, locator, identifier, auth, sql)

    if not rows:
        return {"partitions": {}, "values": {}}

    order_columns: list[str] = rule.get("order", [])
    partition_columns: list[str] = rule.get("partition", [])
    value_columns: list[str] = rule.get("values", [])

    # Resolve the target partition row
    if partition:
        # Caller specified a partition: filter rows to matching partition key/values
        matching = [
            row for row in rows
            if all(str(row.get(col)) == str(partition.get(col)) for col in partition_columns)
        ]
        target_row = matching[0] if matching else rows[0]
    else:
        # Sort by order columns descending and take the latest
        if order_columns:
            def _sort_key(row: dict[str, Any]) -> tuple:
                return tuple(row.get(col) for col in order_columns)

            sorted_rows = sorted(rows, key=_sort_key, reverse=True)
            target_row = sorted_rows[0]
        else:
            target_row = rows[0]

    # Extract partition values
    resolved_partitions = {
        col: target_row.get(col) for col in partition_columns if col in target_row
    }

    # Extract metric values
    resolved_values = {
        col: target_row.get(col) for col in value_columns if col in target_row
    }

    return {"partitions": resolved_partitions, "values": resolved_values}
