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

from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS
from src.backend.auth.users import _hash_password  # noqa: SLF001
from src.backend.metrics.bootstrap import _FACTORY_DEFAULTS
from tests.integration.util.postgres import _load_dotenv

_load_dotenv()

_DS_HOST = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
_DS_PORT = int(os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201"))
_DS_USER = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
_DS_PASSWORD = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
_DS_DB = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")
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
    """TRUNCATE every table in the `dataspoke` schema, then re-seed factory rows.

    Three items require inline re-seeding because the API is not restarted after
    TRUNCATE, so startup seeding won't re-run:

    - metric_definitions: factory defaults are spec-mandated initial state
      (USE_CASE_en.md §UC5 §Factory defaults); seeded from the same SSOT as
      src.backend.metrics.bootstrap.seed_factory_defaults.

    - runtime_config: singleton row (id=1) must be restored to mirror the
      post-install.sh dev state. Base values come from RUNTIME_CONFIG_DEFAULTS;
      llm_provider and llm_model are overridden from the DATASPOKE_DEV_LLM_PROVIDER
      / DATASPOKE_DEV_LLM_MODEL env vars (same variables install.sh PATCHes) so
      the dev provider/model is preserved across resets.

    - bootstrap admin user (dataspoke@dataspoke.local / "DataSpoke Admin" / role
      "Admin"): TRUNCATE wipes the users table. The API is not restarted, so the
      startup/post-install bootstrap endpoint won't re-run. The admin must exist
      for any authenticated test or manual session to succeed after a reset.
      Password is hashed using the same _hash_password helper as the canonical
      internal_bootstrap endpoint in src/api/routers/admin.py.
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

        rc_seed = {
            **RUNTIME_CONFIG_DEFAULTS,
            "llm_provider": os.environ.get(
                "DATASPOKE_DEV_LLM_PROVIDER",
                RUNTIME_CONFIG_DEFAULTS["llm_provider"],
            ),
            "llm_model": os.environ.get(
                "DATASPOKE_DEV_LLM_MODEL",
                RUNTIME_CONFIG_DEFAULTS["llm_model"],
            ),
            "stub_redis_client": True,
            "stub_llm_client": True,
            "stub_pgvector_manager": True,
            "stub_notification_service": True,
        }
        cols = list(rc_seed.keys())
        placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.runtime_config (id, {col_list}) "
            f"VALUES ($1, {placeholders})",
            1,
            *[rc_seed[c] for c in cols],
        )

        datahub_gms = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
        datahub_kafka = os.environ.get("DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS", "")
        if datahub_gms and datahub_kafka:
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.peripheral_config (name, settings) VALUES ($1, $2::jsonb)",
                "datahub",
                json.dumps({"gms_url": datahub_gms, "kafka_brokers": datahub_kafka}),
            )
        langfuse_host = os.environ.get("DATASPOKE_TEST_LANGFUSE_HOST", "")
        langfuse_pk = os.environ.get("DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY", "")
        if langfuse_host and langfuse_pk:
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.peripheral_config (name, settings) VALUES ($1, $2::jsonb)",
                "langfuse",
                json.dumps({"host": langfuse_host, "public_key": langfuse_pk}),
            )

        await conn.execute(
            f"INSERT INTO {_SCHEMA}.users (email, name, password_hash, role) "
            "VALUES ($1, $2, $3, $4)",
            "dataspoke@dataspoke.local",
            "DataSpoke Admin",
            _hash_password("dataspoke"),  # noqa: SLF001
            "Admin",
        )
    finally:
        await conn.close()


async def reset_ingestion_sources() -> None:
    """DELETE all rows from ingestion_source (cascades to ingestion_source_dataset).

    Used by UC1 api-wired tests before each run to guarantee a clean slate.
    ``reset_all()`` covers this via TRUNCATE; this is the narrow helper for when
    only ingestion state needs clearing (e.g. between individual UC1 test steps).
    """
    conn = await _get_connection()
    try:
        await conn.execute(f"DELETE FROM {_SCHEMA}.ingestion_source_dataset")
        await conn.execute(f"DELETE FROM {_SCHEMA}.ingestion_source")
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
