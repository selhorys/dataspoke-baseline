"""Spot tests — Ingestion Control: CLI/ad-hoc classification + list filter.

Two concerns, one per test:

1. **Sync classification** (direct Python): drive ``IngestionService.sync()`` with a
   stubbed DataHub client whose ``list_ingestion_sources`` returns one CLI source
   (executorId ``__datahub_cli_ingestion`` / ``cli-`` URN / ``[CLI] `` name), one
   regular UI source, and one regular UI source that carries a CLI-looking
   ``pipeline_name`` (proving the sweep ignores ``pipeline_name`` when classifying —
   it is NOT a marker). Assert the persisted ``ingestion_source`` rows carry
   ``ad_hoc=true``, ``ad_hoc=false``, and ``ad_hoc=false`` respectively. Spot is the
   natural home because
   seeding a real ``[CLI]``/``cli-`` DataHub source for an api-wired flow isn't natural
   (per feedback_spot_vs_api_wired_principle), and spot is stub-only
   (feedback_spot_is_stub_only).

2. **List filter tri-state** (REST): seed an ad-hoc + a regular DATAHUB_MANAGED row,
   then GET /spoke/ingestion/sources with no ``ad_hoc`` (both), ``?ad_hoc=true``
   (ad-hoc only), and ``?ad_hoc=false`` (regular only); assert the ``ad_hoc`` field is
   carried on the wire.

Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — Ad-hoc (CLI) source classification
Spec: spec/API.md §Ingestion — GET /spoke/ingestion/sources ad_hoc filter + ad_hoc response field
Spec: spec/feature/BACKEND.md §Ingestion Service §Sync sweep step 1 (Source defs)
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — ad_hoc column
Spec: spec/USE_CASE_en.md §UC1
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService
from tests.integration.util import dataspoke_db

# catalog schema triggers PG reset + DataHub ingest (so enumerate_datasets has
# something real to return when needed); the ad-hoc tests don't depend on it.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_SOURCES_BASE = "/api/v1/spoke/ingestion/sources"

# A regular DataHub recipe — minimal valid shape (parse_recipe needs source.type).
_REGULAR_RECIPE = {"source": {"type": "postgres", "config": {}}}


# ── Stub DataHub client for the sync-classification test ──────────────────────


class _StubDataHubForSync:
    """Minimal DataHub stub exposing only what IngestionService.sync() touches.

    ``list_ingestion_sources`` returns the two seeded source dicts; every other
    DataHub surface the sweep calls is a deterministic no-op so the run reaches
    completion without a real GMS. The point under test is purely the step-1
    ad_hoc classification of the returned source defs.
    """

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self._sources = sources

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return self._sources

    async def enumerate_datasets(self) -> list[str]:
        return []

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str]:
        return {}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_sync_classifies_cli_source_ad_hoc_and_ui_source_regular(
    async_session: AsyncSession,
) -> None:
    """sync() flags a CLI source ad_hoc=true; UI sources stay ad_hoc=false — and a
    CLI-looking pipeline_name on a UI source does NOT make it ad-hoc.

    A CLI source (created by ``datahub ingest`` or a Run click) is still listed by
    listIngestionSources, so it syncs as DATAHUB_MANAGED — but DataSpoke flags it
    ad_hoc=true. A regular UI source classifies ad_hoc=false. Critically, a regular
    UI source that carries a ``pipeline_name`` looking like ``[CLI] ...`` must STILL
    classify ad_hoc=false: ``pipeline_name`` is not an ad-hoc marker, so the sweep
    must not consult it. This is where that invariant carries real behavioral risk
    (the classifier signature alone can't enforce that sync ignores pipeline_name).

    spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — Ad-hoc (CLI) source
        classification; "pipeline_name is NOT a marker".
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 1 — DATAHUB_MANAGED upsert.
    spec: feature/BACKEND_SCHEMA.md §ingestion_source — ad_hoc column set on insert.
    """
    await dataspoke_db.reset_ingestion_sources()

    cli_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex
    ui_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    ui_pipeline_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())

    cli_source = {
        "urn": cli_urn,
        "name": "[CLI] ingest run",
        "recipe": '{"source": {"type": "postgres", "config": {}}}',
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }
    ui_source = {
        "urn": ui_urn,
        "name": "My Postgres",
        "recipe": '{"source": {"type": "postgres", "config": {}}}',
        "schedule": None,
        "executor_id": "default",
    }
    # Regular UI source (executorId 'default', non-cli- guid URN, plain name) that
    # ALSO carries a CLI-looking pipeline_name. None of the three real markers fires,
    # so the sweep must classify it ad_hoc=false — pipeline_name is not consulted.
    ui_cli_pipeline_source = {
        "urn": ui_pipeline_urn,
        "name": "Analytics Postgres",
        "recipe": '{"source": {"type": "postgres", "config": {}}}',
        "schedule": None,
        "executor_id": "default",
        "pipeline_name": "[CLI] something",
    }

    service = IngestionService(
        datahub=_StubDataHubForSync(
            [cli_source, ui_source, ui_cli_pipeline_source]
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        # Read the persisted rows back by their DataHub source URN.
        result = await async_session.execute(
            text(
                "SELECT datahub_source_urn, ad_hoc FROM dataspoke.ingestion_source "
                "WHERE datahub_source_urn = ANY(:urns)"
            ),
            {"urns": [cli_urn, ui_urn, ui_pipeline_urn]},
        )
        ad_hoc_by_urn = {row[0]: row[1] for row in result.all()}

        assert ad_hoc_by_urn.get(cli_urn) is True, (
            f"CLI source {cli_urn!r} must persist ad_hoc=true; got "
            f"{ad_hoc_by_urn.get(cli_urn)!r}. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — CLI sources flagged ad_hoc."
        )
        assert ad_hoc_by_urn.get(ui_urn) is False, (
            f"UI source {ui_urn!r} must persist ad_hoc=false; got "
            f"{ad_hoc_by_urn.get(ui_urn)!r}. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — UI sources are not ad-hoc."
        )
        assert ad_hoc_by_urn.get(ui_pipeline_urn) is False, (
            f"UI source {ui_pipeline_urn!r} with a CLI-looking pipeline_name must STILL "
            f"persist ad_hoc=false; got {ad_hoc_by_urn.get(ui_pipeline_urn)!r}. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — pipeline_name is NOT a marker; "
            "the sweep must not consult it when classifying."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


# ── List filter tri-state (REST) ──────────────────────────────────────────────


async def _seed_managed_source(
    async_session: AsyncSession, *, name: str, ad_hoc: bool
) -> str:
    """Insert a DATAHUB_MANAGED ingestion_source row directly and return its id.

    DATAHUB_MANAGED rows are created only by the sync sweep, never via the public
    API, so the list-filter spot test seeds them at the DB layer (the natural span
    for this concern per feedback_spot_vs_api_wired_principle).
    """
    source_id = uuid.uuid4()
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, "
            " datahub_source_urn, ad_hoc, status) "
            "VALUES (:id, 'DATAHUB_MANAGED', :name, 'postgres', "
            " '{\"source\": {\"type\": \"postgres\", \"config\": {}}}'::jsonb, "
            " NULL, NULL, :urn, :ad_hoc, 'OK')"
        ),
        {
            "id": str(source_id),
            "name": name,
            "urn": "urn:li:dataHubIngestionSource:" + str(source_id),
            "ad_hoc": ad_hoc,
        },
    )
    await async_session.commit()
    return str(source_id)


@pytest.mark.asyncio
async def test_list_sources_ad_hoc_filter_is_tri_state(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/ingestion/sources?ad_hoc filters tri-state and carries ad_hoc on the wire.

    - no ad_hoc param → both the ad-hoc and the regular DATAHUB_MANAGED row are returned.
    - ?ad_hoc=true  → only the ad-hoc row.
    - ?ad_hoc=false → only the regular row.
    Every returned row exposes the ad_hoc boolean field.

    spec: API.md §Ingestion — GET /spoke/ingestion/sources ad_hoc filter + ad_hoc field.
    spec: feature/BACKEND_SCHEMA.md §ingestion_source — ad_hoc surfaced via list.
    """
    await dataspoke_db.reset_ingestion_sources()

    try:
        ad_hoc_id = await _seed_managed_source(
            async_session, name="cli-managed-spot", ad_hoc=True
        )
        regular_id = await _seed_managed_source(
            async_session, name="ui-managed-spot", ad_hoc=False
        )

        # No ad_hoc param — both rows returned, each carrying the ad_hoc field.
        all_resp = await api_client.get(
            _SOURCES_BASE,
            headers=admin_headers,
            params={"mode": "DATAHUB_MANAGED", "limit": 100},
        )
        assert all_resp.status_code == 200, (
            f"GET sources (no ad_hoc) expected 200; got {all_resp.status_code}: {all_resp.text}"
        )
        all_body = all_resp.json()
        all_by_id = {s["id"]: s for s in all_body["sources"]}
        assert ad_hoc_id in all_by_id and regular_id in all_by_id, (
            "Unfiltered list must include both the ad-hoc and the regular source. "
            f"Returned ids: {sorted(all_by_id)}. "
            "spec: API.md §Ingestion — ad_hoc=None applies no constraint."
        )
        # ad_hoc field present and correct on each.
        assert all_by_id[ad_hoc_id]["ad_hoc"] is True, (
            f"ad-hoc source must carry ad_hoc=true on the wire; got "
            f"{all_by_id[ad_hoc_id].get('ad_hoc')!r}. "
            "spec: API.md §Ingestion — ad_hoc response field."
        )
        assert all_by_id[regular_id]["ad_hoc"] is False, (
            f"regular source must carry ad_hoc=false on the wire; got "
            f"{all_by_id[regular_id].get('ad_hoc')!r}. "
            "spec: API.md §Ingestion — ad_hoc response field."
        )

        # ?ad_hoc=true — only the ad-hoc row.
        true_resp = await api_client.get(
            _SOURCES_BASE,
            headers=admin_headers,
            params={"mode": "DATAHUB_MANAGED", "ad_hoc": "true", "limit": 100},
        )
        assert true_resp.status_code == 200
        true_ids = {s["id"] for s in true_resp.json()["sources"]}
        assert ad_hoc_id in true_ids, (
            "?ad_hoc=true must include the ad-hoc source. "
            "spec: API.md §Ingestion — ad_hoc=true filters to ad-hoc sources."
        )
        assert regular_id not in true_ids, (
            "?ad_hoc=true must exclude the regular source. "
            "spec: API.md §Ingestion — ad_hoc=true filters to ad-hoc sources."
        )

        # ?ad_hoc=false — only the regular row.
        false_resp = await api_client.get(
            _SOURCES_BASE,
            headers=admin_headers,
            params={"mode": "DATAHUB_MANAGED", "ad_hoc": "false", "limit": 100},
        )
        assert false_resp.status_code == 200
        false_ids = {s["id"] for s in false_resp.json()["sources"]}
        assert regular_id in false_ids, (
            "?ad_hoc=false must include the regular source. "
            "spec: API.md §Ingestion — ad_hoc=false filters to non-ad-hoc sources."
        )
        assert ad_hoc_id not in false_ids, (
            "?ad_hoc=false must exclude the ad-hoc source. "
            "spec: API.md §Ingestion — ad_hoc=false filters to non-ad-hoc sources."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()
