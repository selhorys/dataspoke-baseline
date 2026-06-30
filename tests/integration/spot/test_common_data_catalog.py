"""Spot test for the dataset-catalog list — multiple covering ingestion sources.

Route under test:
  GET /api/v1/spoke/common/data  (collection root — DatasetListItem rows)

Concern covered:
- A dataset claimed by SEVERAL ingestion sources lists EVERY covering source in its
  row's ``ingestion`` array, each carrying {source_id, name, mode, platform}.

``ingestion_source_dataset`` is keyed ``(source_id, dataset_urn)``, so a dataset may be
covered by more than one source. The api-wired pipeline never naturally produces a
multi-source dataset (each UC1 run creates one source mapping its own slice), so this
concern is spot-owned with raw-SQL seeded state per spec/TESTING.md §Spot integration tests.

The list endpoint pages ``dataset_registry`` and resolves coverage by URN without
calling DataHub, so a synthetic registered URN is fully isolated and deterministic
(no ES-indexing dependency). Rows are seeded directly via asyncpg and cleaned up in a
``finally`` block. The empty-``ingestion`` (uncovered) and ``validation.covered``
true/false cases are exercised over real catalog URNs in
tests/integration/api_wired/test_uc5_02_dataset_catalog.py (naturally reachable there),
not duplicated here.

Prerequisites (per spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/spot/test_common_data_catalog.py

Spec:
- spec/API.md §Data Resource — GET /spoke/common/data: row.ingestion is a list of
  {source_id, name, mode, platform} for EVERY covering source (empty when none).
- spec/feature/BACKEND.md §Ingestion Service — reverse_lookup_all_batch returns all
  covering sources per URN.
- spec/TESTING.md §Spot integration tests — coverage rule.
"""

import os
import uuid

import asyncpg
import httpx
import pytest

_CATALOG_URL = "/api/v1/spoke/common/data"

# A synthetic registered URN that no other module touches — covered by two sources.
_MULTI_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,spot_multi.s.title,DEV)"


async def _get_ds_conn() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")),
        user=os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke"),
        password=os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", ""),
        database=os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke"),
    )


def _row_for(body: dict, urn: str) -> dict | None:
    return next((d for d in body["datasets"] if d["dataset_urn"] == urn), None)


@pytest.mark.asyncio
async def test_catalog_row_lists_all_covering_ingestion_sources(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A dataset covered by two sources lists BOTH in its row's ingestion array,
    each with {source_id, name, mode, platform}.

    Spec: spec/API.md §Data Resource — row.ingestion lists EVERY covering source
          ({source_id, name, mode, platform}); a dataset may be covered by several.
    Spec: spec/feature/BACKEND.md §Ingestion Service — reverse_lookup_all_batch.
    """
    source_a = uuid.uuid4()
    source_b = uuid.uuid4()
    conn = await _get_ds_conn()
    try:
        # Register the synthetic URN so it appears in the catalog page.
        await conn.execute(
            "INSERT INTO dataspoke.dataset_registry (dataset_urn, datahub_registered) "
            "VALUES ($1, TRUE) "
            "ON CONFLICT (dataset_urn) DO UPDATE SET datahub_registered = TRUE",
            _MULTI_URN,
        )
        # Two regular ACTIVE_CUSTOM_MANAGED sources (postgres), distinct names.
        for sid, name in ((source_a, "spot multi source A"), (source_b, "spot multi source B")):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                sid, "ACTIVE_CUSTOM_MANAGED", name, "postgres",
                '{"source": {"type": "postgres", "config": {}}}',
            )
            # Both sources cover the same URN (PK is (source_id, dataset_urn)).
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) VALUES ($1, $2, $3)",
                sid, _MULTI_URN, "emitted",
            )

        resp = await api_client.get(
            _CATALOG_URL, headers=admin_headers, params={"limit": 500, "offset": 0}
        )
        assert resp.status_code == 200, (
            f"GET /spoke/common/data must return 200; got {resp.status_code}: {resp.text}."
        )
        row = _row_for(resp.json(), _MULTI_URN)
        assert row is not None, (
            f"the registered synthetic URN {_MULTI_URN!r} must appear in the catalog. "
            "spec: API.md §Data Resource — lists every registered dataset"
        )

        ingestion = row["ingestion"]
        assert isinstance(ingestion, list), (
            f"row.ingestion must be a list; got {type(ingestion).__name__}. "
            "spec: API.md §Data Resource — row.ingestion is a list of covering sources"
        )
        served_ids = {s["source_id"] for s in ingestion}
        assert served_ids == {str(source_a), str(source_b)}, (
            "row.ingestion must list EVERY covering source (both A and B); "
            f"got source_ids {served_ids}, expected {{{source_a}, {source_b}}}. "
            "spec: API.md §Data Resource — a dataset may be covered by several sources"
        )
        for s in ingestion:
            assert {"source_id", "name", "mode", "platform"} <= set(s), (
                f"each ingestion entry must carry source_id/name/mode/platform; got {s!r}. "
                "spec: API.md §Data Resource — row.ingestion entry shape"
            )
            assert s["mode"] == "ACTIVE_CUSTOM_MANAGED", (
                f"ingestion.mode must echo the source mode; got {s['mode']!r}."
            )
            assert s["platform"] == "postgres", (
                f"ingestion.platform must be the source platform; got {s['platform']!r}. "
                "spec: API.md §Data Resource — row.ingestion.platform"
            )
            assert s["name"], "ingestion.name must be the non-empty source name."

        # validation coverage field is present and a bool for the row.
        assert isinstance(row["validation"]["covered"], bool), (
            f"row.validation.covered must be a bool; got {row['validation']!r}. "
            "spec: API.md §Data Resource — row.validation ({covered})"
        )
    finally:
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = $1",
            _MULTI_URN,
        )
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])",
            [source_a, source_b],
        )
        await conn.execute(
            "DELETE FROM dataspoke.dataset_registry WHERE dataset_urn = $1", _MULTI_URN
        )
        await conn.close()
