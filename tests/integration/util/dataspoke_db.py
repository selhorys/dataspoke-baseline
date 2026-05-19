"""DataSpoke operational-DB reset utilities for integration tests.

Connects to the DataSpoke operational Postgres (port 9201, `dataspoke` schema)
and purges rows that accumulate from prior runs. Source-side reset utilities
(`postgres.py`, `kafka.py`, `datahub.py`) cover the dummy data and DataHub
catalog, but the operational DB is the canonical store for ingestion configs,
validation configs/results, metagen state, ontogen state, the registry, the
events audit log, and embeddings — none of which the source resetters touch.

Two entry points:

- `reset_all()` — TRUNCATE every table in the `dataspoke` schema. Used by
  `python -m tests.integration.util --reset-all` so a single command yields a
  fully clean slate.
- `purge_urn(urn)` — Hard-delete operational rows for a single dataset URN.
  Used by the api-wired `purge_urns` conftest fixture so each test starts with
  a clean slot for its own URN even when reset-all hasn't been run.

Tables and URN-keyed columns are discovered at runtime via `information_schema`
so future schema additions are auto-included.
"""

from __future__ import annotations

import json
import os

import asyncpg

from src.backend.metrics.bootstrap import _FACTORY_DEFAULTS
from tests.integration.util.postgres import _load_dotenv

_load_dotenv()

_DS_HOST = os.environ.get("DATASPOKE_POSTGRES_HOST", "localhost")
_DS_PORT = int(os.environ.get("DATASPOKE_POSTGRES_PORT", "9201"))
_DS_USER = os.environ.get("DATASPOKE_POSTGRES_USER", "dataspoke")
_DS_PASSWORD = os.environ.get("DATASPOKE_POSTGRES_PASSWORD", "")
_DS_DB = os.environ.get("DATASPOKE_POSTGRES_DB", "dataspoke")
_SCHEMA = "dataspoke"


async def _get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=_DS_HOST,
        port=_DS_PORT,
        user=_DS_USER,
        password=_DS_PASSWORD,
        database=_DS_DB,
    )


async def reset_all() -> None:
    """TRUNCATE every table in the `dataspoke` schema, then re-seed factory metrics.

    Factory-default metric_definitions are spec-mandated initial state
    (USE_CASE_en.md §UC5 §Factory defaults) — the API seeds them at startup
    via src.backend.metrics.bootstrap.seed_factory_defaults. After TRUNCATE
    the API isn't restarted, so we re-seed inline from the same SSOT.
    """
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_type = 'BASE TABLE'",
            _SCHEMA,
        )
        if not rows:
            return
        tables = ", ".join(f'{_SCHEMA}.{r["table_name"]}' for r in rows)
        await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")

        for d in _FACTORY_DEFAULTS:
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.metric_definitions "
                "(id, mode, metric_type, title, description, metrics, "
                " metric_conf, dataset_filter, schedule_tier, is_enabled) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10)",
                d["id"],
                d["mode"],
                d["metric_type"],
                d["title"],
                d["description"],
                json.dumps(d["metrics"]),
                json.dumps(d["metric_conf"]),
                json.dumps(d["dataset_filter"]),
                d["schedule_tier"],
                d["is_enabled"],
            )
    finally:
        await conn.close()


async def purge_urn(urn: str) -> None:
    """Hard-delete operational rows scoped to a single dataset URN.

    Covers every `dataspoke.*` table that has a `dataset_urn` column, plus
    `events` rows where `entity_type='dataset' AND entity_id=urn`.
    """
    conn = await _get_connection()
    try:
        urn_keyed = await conn.fetch(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = $1 AND column_name = 'dataset_urn'",
            _SCHEMA,
        )
        for r in urn_keyed:
            await conn.execute(
                f'DELETE FROM {_SCHEMA}.{r["table_name"]} WHERE dataset_urn = $1',
                urn,
            )
        await conn.execute(
            f"DELETE FROM {_SCHEMA}.events "
            "WHERE entity_type = 'dataset' AND entity_id = $1",
            urn,
        )
    finally:
        await conn.close()
