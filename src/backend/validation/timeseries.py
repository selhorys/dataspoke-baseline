"""SQL-based timeseries engine for validation rules.

Supports executing SQL against PostgreSQL data sources to compute
timeseries metrics for the sql_timeseries custom rule subtype.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import IngestionConfig
from src.shared.exceptions import EntityNotFoundError

logger = logging.getLogger(__name__)


async def resolve_source_config(
    db: AsyncSession,
    dataset_urn: str,
    rule: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Return (source_type, locator, identifier, auth) for SQL execution.

    Resolution order:
    1. If rule contains a ``source`` dict with source_type/locator/identifier/auth,
       use it as an override.
    2. Otherwise, look up the ingestion config for dataset_urn in the DB.
    3. Raise EntityNotFoundError if neither source is available.
    """
    source_override = rule.get("source")
    if isinstance(source_override, dict) and source_override.get("source_type"):
        source_type: str = source_override["source_type"]
        locator: dict[str, Any] = source_override.get("locator", {})
        identifier: dict[str, Any] = source_override.get("identifier", {})
        auth: dict[str, Any] | None = source_override.get("auth")
        return source_type, locator, identifier, auth

    # Fall back to ingestion config
    result = await db.execute(
        select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)

    return row.source_type, row.locator, row.identifier, row.auth


async def execute_sql(
    source_type: str,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    sql: str,
) -> list[dict[str, Any]]:
    """Execute SQL against the source and return a list of row dicts.

    Currently supports PostgreSQL only.  Other source types raise NotImplementedError.
    """
    if source_type.upper() == "POSTGRESQL":
        return await _execute_postgresql(locator, identifier, auth, sql)

    raise NotImplementedError(f"SQL execution not supported for {source_type}")


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
    password = auth.get("password", auth.get("secret_ref", "")) if auth else ""

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
    source_type, locator, identifier, auth = await resolve_source_config(
        db, dataset_urn, rule
    )

    sql = rule.get("sql", "")
    rows = await execute_sql(source_type, locator, identifier, auth, sql)

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
