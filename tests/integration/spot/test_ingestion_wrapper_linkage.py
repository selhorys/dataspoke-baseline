"""Spot tests — Ingestion Control: CLI wrapper linkage + list hides wrappers.

DataHub auto-creates a **CLI wrapper source** (own URN ``…:cli-<hash>``,
``executor_id`` ``__datahub_cli_…``) when a registered DATAHUB_MANAGED source runs,
and books the run on the wrapper rather than the parent. The wrapper's reported
recipe carries a top-level ``pipeline_name`` field = the registered parent's source
URN. DataSpoke treats wrappers as internal plumbing: stored only when their
``recipe.pipeline_name`` resolves to a registered source (linked via the
self-referential ``parent_source_id`` FK), hidden from the list, with their run
events surfaced on the regular parent. A wrapper whose ``recipe.pipeline_name``
matches no stored regular parent — or that carries no ``pipeline_name`` at all — is
an **orphan** and is not stored. The cosmetic display name is never used for linking.

Two concerns, one per test:

1. **Sync linkage + orphan drop** (direct Python): drive ``IngestionService.sync()``
   with a stubbed DataHub client returning a regular source, a wrapper whose
   ``recipe.pipeline_name`` matches it, and an orphan wrapper whose
   ``recipe.pipeline_name`` matches no registered source. Assert the regular row
   persists with ``parent_source_id IS NULL``, the matching wrapper persists with
   ``parent_source_id`` = the parent's id, and the orphan wrapper is **not stored**.

2. **List hides wrappers** (REST): seed a regular DATAHUB_MANAGED row and a wrapper
   row linked to it directly in the DB, then GET /spoke/ingestion/sources?mode=
   DATAHUB_MANAGED and assert only the regular row appears — wrappers never surface.

Spot is the natural home: seeding a real ``cli-``/``__datahub_cli_`` DataHub source
for an api-wired flow isn't natural (feedback_spot_vs_api_wired_principle), and spot
is stub-only (feedback_spot_is_stub_only).

Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — wrapper→parent linkage via
    recipe.pipeline_name; orphan wrapper = stale (not stored).
Spec: spec/feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source defs) — Pass
A/Pass B (Pass B links via
    recipe.pipeline_name); orphan wrappers dropped; wrappers never listed.
Spec: spec/API.md §Ingestion — GET /spoke/ingestion/sources returns regular
    DATAHUB_MANAGED sources only (CLI wrapper sources are internal, never listed).
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — parent_source_id internal FK.
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

# No dummy-data constants: the sync-linkage test drives IngestionService.sync()
# with a stubbed DataHub client (_StubDataHubForSync) and the list test seeds rows
# directly in the DB, so no real DataHub example datasets are consulted.
# spec: TESTING.md §Per-Module Dummy-Data Reset — modules with no constants are no-ops.

_SOURCES_BASE = "/api/v1/spoke/ingestion/sources"


# ── Stub DataHub client for the sync-linkage test ─────────────────────────────


class _StubDataHubForSync:
    """Minimal DataHub stub exposing only what IngestionService.sync() touches.

    ``list_ingestion_sources`` returns the seeded source dicts; every other DataHub
    surface the sweep calls is a deterministic no-op so the run reaches completion
    without a real GMS. The point under test is purely step-1 wrapper linkage.
    """

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self._sources = sources

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return self._sources

    async def enumerate_datasets(self) -> list[str]:
        return []

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str | None]:
        return {u: None for u in urns}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_sync_links_wrapper_to_parent_and_drops_orphan(
    async_session: AsyncSession,
) -> None:
    """sync() stores a wrapper with parent_source_id = its registered parent's id, the
    regular source with parent_source_id NULL, and does NOT store an orphan wrapper.

    Setup (DataHub list): a regular registered source (URN PARENT); a CLI wrapper
    (cli- URN, ``__datahub_cli_`` executor) whose ``recipe.pipeline_name`` == PARENT;
    and an orphan CLI wrapper whose ``recipe.pipeline_name`` == an unseeded URN that
    matches no registered source. Pass A upserts the regular row (parent_source_id
    NULL); Pass B resolves the first wrapper to PARENT's id via recipe.pipeline_name
    and drops the orphan as stale. Display names carry no parent and are never used
    for linking.

    spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source defs) — Pass
    A regular (parent_source_id
        NULL), Pass B wrappers (linked when recipe.pipeline_name resolves; orphan not
        stored).
    spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — linkage via
        recipe.pipeline_name; orphan wrapper = stale.
    spec: feature/BACKEND_SCHEMA.md §ingestion_source — a row is a wrapper iff
        parent_source_id IS NOT NULL; a regular source iff NULL.
    """
    await dataspoke_db.reset_ingestion_sources()

    parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex
    orphan_parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    orphan_wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex

    regular_source = {
        "urn": parent_urn,
        "name": "uc1-registered-postgres",
        "recipe": '{"source": {"type": "postgres", "config": {}}}',
        "schedule": None,
        "executor_id": "default",
    }
    # Wrapper auto-created by a run of the registered source: cli- URN, CLI executor,
    # reported recipe carrying top-level pipeline_name = the parent registered URN.
    wrapper_source = {
        "urn": wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": (
            '{"source": {"type": "postgres", "config": {}}, '
            f'"pipeline_name": "{parent_urn}"}}'
        ),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }
    # Orphan wrapper: recipe.pipeline_name is a URN that is NEVER a registered source.
    orphan_wrapper_source = {
        "urn": orphan_wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": (
            '{"source": {"type": "postgres", "config": {}}, '
            f'"pipeline_name": "{orphan_parent_urn}"}}'
        ),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForSync(
            [wrapper_source, regular_source, orphan_wrapper_source]
        ),  # wrapper listed BEFORE its parent — two-pass must still resolve it
        db=async_session,  # type: ignore[arg-type]
    )
    try:
        await service.sync()

        result = await async_session.execute(
            text(
                "SELECT datahub_source_urn, id, parent_source_id "
                "FROM dataspoke.ingestion_source "
                "WHERE datahub_source_urn = ANY(:urns)"
            ),
            {"urns": [parent_urn, wrapper_urn, orphan_wrapper_urn]},
        )
        by_urn = {row[0]: row for row in result.all()}

        # Regular source stored with parent_source_id NULL.
        assert parent_urn in by_urn, (
            f"Regular registered source {parent_urn!r} must be stored. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source "
            "defs) Pass A."
        )
        parent_row = by_urn[parent_urn]
        assert parent_row[2] is None, (
            f"Regular source must persist parent_source_id IS NULL; got "
            f"{parent_row[2]!r}. spec: feature/BACKEND_SCHEMA.md §ingestion_source — "
            "a regular DATAHUB_MANAGED source has parent_source_id NULL."
        )

        # Wrapper stored linked to its registered parent's id.
        assert wrapper_urn in by_urn, (
            f"Wrapper {wrapper_urn!r} whose recipe.pipeline_name resolves must be "
            "stored. spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 "
            "(Source defs) Pass B."
        )
        wrapper_row = by_urn[wrapper_urn]
        assert str(wrapper_row[2]) == str(parent_row[1]), (
            f"Wrapper must persist parent_source_id = its registered parent's id "
            f"({parent_row[1]!r}); got {wrapper_row[2]!r}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source "
            "defs) Pass B — wrapper linked to "
            "the regular source whose datahub_source_urn equals recipe.pipeline_name."
        )

        # Orphan wrapper NOT stored.
        assert orphan_wrapper_urn not in by_urn, (
            f"Orphan wrapper {orphan_wrapper_urn!r} (recipe.pipeline_name matches no "
            "registered source) must NOT be stored. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source "
            "defs) Pass B — orphan wrappers are "
            "stale and not stored. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — orphan wrapper = stale."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


# ── List hides wrappers (REST) ────────────────────────────────────────────────


async def _seed_regular_managed_source(
    async_session: AsyncSession, *, name: str
) -> str:
    """Insert a regular DATAHUB_MANAGED ingestion_source row (parent_source_id NULL).

    DATAHUB_MANAGED rows are created only by the sync sweep, never via the public API,
    so this list-filter spot test seeds them at the DB layer (the natural span for
    this concern per feedback_spot_vs_api_wired_principle).
    """
    source_id = uuid.uuid4()
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, "
            " datahub_source_urn, parent_source_id, status) "
            "VALUES (:id, 'DATAHUB_MANAGED', :name, 'postgres', "
            " '{\"source\": {\"type\": \"postgres\", \"config\": {}}}'::jsonb, "
            " NULL, NULL, :urn, NULL, 'OK')"
        ),
        {
            "id": str(source_id),
            "name": name,
            "urn": "urn:li:dataHubIngestionSource:" + str(source_id),
        },
    )
    await async_session.commit()
    return str(source_id)


async def _seed_wrapper_source(
    async_session: AsyncSession, *, name: str, parent_id: str
) -> str:
    """Insert a CLI wrapper ingestion_source row (parent_source_id = parent_id)."""
    source_id = uuid.uuid4()
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, "
            " datahub_source_urn, parent_source_id, status) "
            "VALUES (:id, 'DATAHUB_MANAGED', :name, 'postgres', "
            " '{\"source\": {\"type\": \"postgres\", \"config\": {}}}'::jsonb, "
            " NULL, NULL, :urn, :parent_id, 'OK')"
        ),
        {
            "id": str(source_id),
            "name": name,
            "urn": "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex,
            "parent_id": parent_id,
        },
    )
    await async_session.commit()
    return str(source_id)


@pytest.mark.asyncio
async def test_list_sources_hides_wrappers(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED returns regular sources only.

    A CLI wrapper row (parent_source_id IS NOT NULL) is internal plumbing and must
    never appear in the list. Seed one regular row and one wrapper linked to it, then
    assert the list contains the regular id but not the wrapper id — and that no
    returned row exposes a parent_source_id / ad_hoc field on the wire.

    spec: API.md §Ingestion — DataHub CLI wrapper sources are internal and never
        listed; mode=DATAHUB_MANAGED returns regular sources only.
    spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source defs) —
    list_sources hard-filters
        parent_source_id IS NULL.
    spec: feature/BACKEND_SCHEMA.md §ingestion_source — parent_source_id never exposed.
    """
    await dataspoke_db.reset_ingestion_sources()

    try:
        regular_id = await _seed_regular_managed_source(
            async_session, name="ui-managed-spot"
        )
        wrapper_id = await _seed_wrapper_source(
            async_session, name="[CLI] postgres", parent_id=regular_id
        )

        list_resp = await api_client.get(
            _SOURCES_BASE,
            headers=admin_headers,
            params={"mode": "DATAHUB_MANAGED", "limit": 100},
        )
        assert list_resp.status_code == 200, (
            f"GET sources expected 200; got {list_resp.status_code}: {list_resp.text}"
        )
        body = list_resp.json()
        ids = {s["id"] for s in body["sources"]}

        assert regular_id in ids, (
            f"Regular DATAHUB_MANAGED source {regular_id!r} must appear in the list. "
            f"Returned ids: {sorted(ids)}. "
            "spec: API.md §Ingestion — regular sources are listed."
        )
        assert wrapper_id not in ids, (
            f"Wrapper source {wrapper_id!r} must NOT appear in the list (internal "
            f"plumbing). Returned ids: {sorted(ids)}. "
            "spec: API.md §Ingestion — DataHub CLI wrapper sources are never listed. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep, step 1 (Source "
            "defs) — list hides wrappers."
        )

        # No internal/removed fields leak on the wire.
        for s in body["sources"]:
            assert "parent_source_id" not in s, (
                f"parent_source_id is internal and must not appear on the wire; row: {s}. "
                "spec: feature/BACKEND_SCHEMA.md §ingestion_source — internal FK."
            )
            assert "ad_hoc" not in s, (
                f"ad_hoc was removed and must not appear on the wire; row: {s}. "
                "spec: API.md §Ingestion — no ad_hoc field."
            )
    finally:
        await dataspoke_db.reset_ingestion_sources()
