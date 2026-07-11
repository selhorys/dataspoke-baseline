"""Spot tests — Ingestion sync sweep: CLI wrapper inherits the registered parent's
pipeline_name link, and the regular parent aggregates the wrapper's run events.

DataHub auto-creates a CLI wrapper source (own URN ``…:cli-<hash>``,
``executor_id`` ``__datahub_cli_…``) when a registered DATAHUB_MANAGED source runs,
books the run on the wrapper, reports a recipe carrying top-level
``pipeline_name = <parent registered URN>``, and stamps
``systemMetadata.pipelineName = <parent registered URN>`` on the aspects the run
emits. DataSpoke stores the wrapper as internal plumbing **linked to its registered
parent** via the self-referential ``parent_source_id`` FK — resolved from the
wrapper's ``recipe.pipeline_name`` (step 1 Pass B) — then:

  - **step 3 (observed enrichment)** awards ``derivation=pipeline_name`` / authority
    ``high`` to the registered parent (whose ``datahub_source_urn`` equals the
    pipelineName) **and** to the wrapper, which inherits the link directly through its
    stored ``parent_source_id`` — no re-parsing of the wrapper name; and
  - **the regular parent aggregates the wrapper's run events**: the per-source event
    endpoint unions the parent's own events with its linked wrappers' events, each row
    carrying a derived ``wrapper`` flag.

A wrapper whose ``recipe.pipeline_name`` matches no stored regular parent (or whose
recipe carries no ``pipeline_name`` at all) is an **orphan**: step 1 does not store
it, so it owns no mapping rows and contributes no events. The cosmetic display name is
never used for linking.

This is a spot concern (not api-wired): the dev-env DataHub executor does not run CLI
executions, so an api-wired pipeline cannot naturally create the wrapper +
parent-stamped pipelineName state. Setup is ORM/SQL-seeded
(feedback_spot_vs_api_wired_principle) and the DataHub client is stubbed
(feedback_spot_is_stub_only).

Spec: spec/feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 1 — wrapper linked via
parent_source_id
    (resolved from recipe.pipeline_name); orphan not stored.
Spec: spec/feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 3 — pipelineName awards
pipeline_name to
    the registered parent AND the wrapper that inherits via parent_source_id.
Spec: spec/feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4 — the regular source
aggregates events
    across itself and its linked wrappers (derived wrapper flag).
Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — wrapper→parent linkage via
    recipe.pipeline_name; orphan wrapper = stale (not stored).
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name → high.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import (
    IngestionService,
    IngestionSourceDatasetRecord,
)
from tests.integration.util import dataspoke_db

# These tests seed and assert purely on dataspoke.ingestion_source* / events rows with
# a stubbed DataHub client — no real dataset enumeration is needed, so no DUMMY_DATA_*
# constants (no PG/DataHub reset dependency).

# Two postgres dataset URNs in the `catalog` schema. The seeded sources' recipes carry
# `schema_pattern: ^catalog$`, so step-2's filter-matcher maps both of these to each
# source as derivation='matched' (the pre-state step 3 promotes).
_DS_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_DS_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

# A postgres recipe scoped to the catalog schema. parse_recipe needs source.type;
# schema_pattern drives the step-2 matcher onto _DS_A / _DS_B.
_CATALOG_RECIPE = '{"source": {"type": "postgres", "config": {"schema_pattern": {"allow": ["^catalog$"]}}}}'  # noqa: E501


def _wrapper_recipe(pipeline_name: str | None) -> str:
    """A CLI-wrapper recipe scoped to the catalog schema.

    DataHub's wrapper recipe carries a top-level ``pipeline_name`` field set to the
    registered parent's source URN — this is the field step 1 Pass B reads to resolve
    the parent link (the display name is never used). When *pipeline_name* is None the
    recipe omits the key entirely, modelling a wrapper whose recipe carries no
    pipeline_name (→ orphan).
    """
    if pipeline_name is None:
        return _CATALOG_RECIPE
    return (
        '{"source": {"type": "postgres", "config": {"schema_pattern": '
        '{"allow": ["^catalog$"]}}}, '
        f'"pipeline_name": "{pipeline_name}"}}'
    )


class _StubDataHubForPipelineSync:
    """Minimal DataHub stub exposing only what IngestionService.sync() touches.

    - ``list_ingestion_sources`` returns the seeded source defs so step 1 upserts them
      (regular with parent_source_id NULL; wrapper linked via Pass B).
    - ``enumerate_datasets`` returns the two catalog dataset URNs so step 2's matcher
      maps them (derivation='matched').
    - ``get_pipeline_names`` is the pivot: it returns the per-dataset
      systemMetadata.pipelineName that step 3 reads.
    - ``list_execution_requests`` returns terminal requests per source URN so step 4
      mirrors INGESTION events (used by the parent-aggregation test).
    """

    def __init__(
        self,
        sources: list[dict[str, Any]],
        datasets: list[str],
        pipeline_names: dict[str, str | None],
        execution_requests: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._sources = sources
        self._datasets = datasets
        self._pipeline_names = pipeline_names
        self._execution_requests = execution_requests or {}

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return self._sources

    async def enumerate_datasets(self) -> list[str]:
        return self._datasets

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str | None]:
        # Mirror the real client contract (src/shared/datahub/client.py
        # get_pipeline_names -> dict[str, str | None]): return an entry for EVERY
        # input URN, with None where no pipelineName is stamped.
        return {u: self._pipeline_names.get(u) for u in urns}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return self._execution_requests.get(source_urn, [])


async def _datasets_for_source(
    async_session: AsyncSession, source_urn: str
) -> dict[str, IngestionSourceDatasetRecord]:
    """Return {dataset_urn: record} for the source identified by its DataHub URN.

    Looks the source up by its stable ``datahub_source_urn`` (set by sync step 1),
    then reads its ingestion_source_dataset rows as service value objects so the test
    asserts on ``derivation`` and the derived ``authority`` property exactly as the
    service layer exposes them.
    """
    id_result = await async_session.execute(
        text(
            "SELECT id FROM dataspoke.ingestion_source WHERE datahub_source_urn = :urn"
        ),
        {"urn": source_urn},
    )
    source_id = id_result.scalar_one()

    rows_result = await async_session.execute(
        text(
            "SELECT source_id, dataset_urn, derivation, first_seen_at, last_seen_at "
            "FROM dataspoke.ingestion_source_dataset WHERE source_id = :sid"
        ),
        {"sid": str(source_id)},
    )
    out: dict[str, IngestionSourceDatasetRecord] = {}
    for row in rows_result.mappings().all():
        out[row["dataset_urn"]] = IngestionSourceDatasetRecord(
            source_id=str(row["source_id"]),
            dataset_urn=row["dataset_urn"],
            derivation=row["derivation"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )
    return out


async def _source_id_for_urn(async_session: AsyncSession, source_urn: str) -> str:
    result = await async_session.execute(
        text(
            "SELECT id FROM dataspoke.ingestion_source WHERE datahub_source_urn = :urn"
        ),
        {"urn": source_urn},
    )
    return str(result.scalar_one())


@pytest.mark.asyncio
async def test_wrapper_inherits_pipeline_name_from_registered_parent(
    async_session: AsyncSession,
) -> None:
    """sync() awards pipeline_name/high to BOTH the registered parent and the linked
    CLI wrapper that inherits via parent_source_id.

    Setup: a registered DATAHUB_MANAGED source (URN PARENT) and a CLI wrapper (own URN
    ``cli-<hash>``, CLI executor) whose ``recipe.pipeline_name`` == PARENT. Step 1
    stores the wrapper linked to PARENT (parent_source_id set) via recipe.pipeline_name.
    Both recipes scope to the catalog schema, so step 2 maps _DS_A/_DS_B to each as
    matched/medium. The stub stamps pipelineName=PARENT on both datasets.

    Expect: after the sweep, both _DS_A and _DS_B carry derivation='pipeline_name'
    (authority 'high') for the registered parent AND for the wrapper — the wrapper
    inherits through its stored parent link.

    Spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 3 — a dataset's
    pipelineName awards
        pipeline_name/high to the registered parent AND the wrapper that inherits via
        parent_source_id (no re-parsing of the wrapper name).
    Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — wrapper recipe.pipeline_name =
        parent registered URN == stamped systemMetadata.pipelineName.
    """
    await dataspoke_db.reset_ingestion_sources()

    parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex

    registered_source = {
        "urn": parent_urn,
        "name": "uc1-catalog-postgres",
        "recipe": _CATALOG_RECIPE,
        "schedule": None,
        "executor_id": "default",
    }
    wrapper_source = {
        "urn": wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": _wrapper_recipe(parent_urn),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[registered_source, wrapper_source],
            datasets=[_DS_A, _DS_B],
            pipeline_names={_DS_A: parent_urn, _DS_B: parent_urn},
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        registered_ds = await _datasets_for_source(async_session, parent_urn)
        wrapper_ds = await _datasets_for_source(async_session, wrapper_urn)

        for label, ds_map in (("registered", registered_ds), ("wrapper", wrapper_ds)):
            for urn in (_DS_A, _DS_B):
                rec = ds_map.get(urn)
                assert rec is not None, (
                    f"{label} source must have a mapping row for {urn!r}; got "
                    f"{sorted(ds_map)}. spec: feature/BACKEND.md §Ingestion Service (Sync + "
                    f"mapping sweep) step 3."
                )
                assert rec.derivation == "pipeline_name", (
                    f"{label} source's mapping for {urn!r} must be promoted to "
                    f"derivation='pipeline_name'; got {rec.derivation!r}. spec: "
                    "feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 3 — "
                    "pipelineName awards "
                    "pipeline_name to the registered parent AND its linked wrapper."
                )
                assert rec.authority == "high", (
                    f"{label} source's mapping for {urn!r} must have authority 'high'; "
                    f"got {rec.authority!r}. spec: feature/BACKEND_SCHEMA.md — "
                    "pipeline_name derivation → high authority."
                )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_orphan_wrapper_no_matching_parent_is_not_stored(
    async_session: AsyncSession,
) -> None:
    """A CLI wrapper whose ``recipe.pipeline_name`` matches NO stored regular parent is
    an orphan and is NOT stored.

    Setup: a CLI wrapper (cli- URN, CLI executor) whose ``recipe.pipeline_name`` is a
    valid dataHubIngestionSource URN that is NOT present as a registered
    DATAHUB_MANAGED row. There is no parent to link, so step 1 Pass B treats the
    wrapper as stale and does not store it — it owns no mapping rows.

    Spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 1 — orphan wrappers
    (recipe.pipeline_name
        matches no stored regular parent) are not stored.
    Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — orphan wrapper = stale.
    """
    await dataspoke_db.reset_ingestion_sources()

    unseeded_parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex

    wrapper_source = {
        "urn": wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": _wrapper_recipe(unseeded_parent_urn),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[wrapper_source],
            datasets=[_DS_A, _DS_B],
            pipeline_names={_DS_A: unseeded_parent_urn, _DS_B: unseeded_parent_urn},
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        result = await async_session.execute(
            text(
                "SELECT id FROM dataspoke.ingestion_source WHERE datahub_source_urn = :urn"
            ),
            {"urn": wrapper_urn},
        )
        assert result.scalar_one_or_none() is None, (
            f"Orphan wrapper {wrapper_urn!r} (recipe.pipeline_name matches no stored "
            "regular parent) must NOT be stored. spec: feature/BACKEND.md §Ingestion Service (Sync "
            "+ mapping sweep) "
            "step 1 — orphan wrappers are stale and not stored. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — orphan = stale."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_wrapper_without_pipeline_name_is_not_stored(
    async_session: AsyncSession,
) -> None:
    """A CLI wrapper whose recipe carries no ``pipeline_name`` is an orphan and is NOT
    stored — while the registered parent on the same sweep is still promoted.

    Setup: a system CLI wrapper (cli- URN, CLI executor) whose recipe omits the
    top-level ``pipeline_name`` key entirely, alongside a registered source whose
    pipelineName is stamped on the datasets. The wrapper cannot resolve a parent (no
    pipeline_name to match), so step 1 does not store it; the registered parent is
    still promoted (sanity anchor for the same sweep).

    Spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 1 — a wrapper whose
    recipe carries no
        pipeline_name is an orphan and not stored.
    Spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 3 — the registered
    parent is still
        promoted on the sweep.
    """
    await dataspoke_db.reset_ingestion_sources()

    parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    sys_wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex

    registered_source = {
        "urn": parent_urn,
        "name": "uc1-catalog-postgres",
        "recipe": _CATALOG_RECIPE,
        "schedule": None,
        "executor_id": "default",
    }
    sys_wrapper_source = {
        "urn": sys_wrapper_urn,
        "name": "[CLI] datahub-documents",
        "recipe": _wrapper_recipe(None),  # recipe omits top-level pipeline_name
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[registered_source, sys_wrapper_source],
            datasets=[_DS_A, _DS_B],
            pipeline_names={_DS_A: parent_urn, _DS_B: parent_urn},
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        # Sanity anchor: the registered parent IS promoted on the same sweep.
        registered_ds = await _datasets_for_source(async_session, parent_urn)
        assert registered_ds[_DS_A].derivation == "pipeline_name", (
            "registered parent must be promoted to pipeline_name on this sweep "
            "(anchor). spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 3."
        )

        # The wrapper with no recipe.pipeline_name is an orphan → not stored.
        result = await async_session.execute(
            text(
                "SELECT id FROM dataspoke.ingestion_source WHERE datahub_source_urn = :urn"
            ),
            {"urn": sys_wrapper_urn},
        )
        assert result.scalar_one_or_none() is None, (
            f"Wrapper {sys_wrapper_urn!r} whose recipe carries no pipeline_name must "
            "NOT be stored (no resolvable parent). spec: feature/BACKEND.md §Ingestion Service "
            "(Sync + mapping sweep) "
            "step 1 — no recipe.pipeline_name → orphan → not stored."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_regular_source_aggregates_wrapper_run_events(
    async_session: AsyncSession,
) -> None:
    """The regular parent's event endpoint surfaces the wrapper's run events, each
    tagged wrapper=true; the parent's own events are tagged wrapper=false.

    Setup: a registered DATAHUB_MANAGED source (URN PARENT) and a CLI wrapper whose
    ``recipe.pipeline_name`` == PARENT (linked via Pass B). The stub books a terminal
    SUCCESS execution request on the WRAPPER's URN (DataHub records a managed source's
    runs on the wrapper, not the parent). Step 4 mirrors it as an INGESTION.COMPLETE
    event on the wrapper row.

    Expect: ``get_events_for_source(parent_id)`` returns that event with wrapper=true
    and status='success' — the run surfaces on the regular source the user looks at.
    (The event's detail.execution_request_urn is used here as a discriminator to locate
    the right row; per BACKEND.md §Event Catalogue it is the spec'd identity key for
    sync-mirrored ingestion events.)

    Spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4 — the regular source
    aggregates events
        across itself and its linked wrappers (each row carries the derived wrapper flag);
        SUCCESS → INGESTION.COMPLETE → status='success'.
    Spec: API.md §Ingestion — GET /sources/{id}/event includes linked-wrapper events
        carrying wrapper: bool.
    """
    await dataspoke_db.reset_ingestion_sources()

    parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex
    exec_request_urn = "urn:li:dataHubExecutionRequest:" + uuid.uuid4().hex

    registered_source = {
        "urn": parent_urn,
        "name": "uc1-catalog-postgres",
        "recipe": _CATALOG_RECIPE,
        "schedule": None,
        "executor_id": "default",
    }
    wrapper_source = {
        "urn": wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": _wrapper_recipe(parent_urn),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }
    # DataHub books the run on the WRAPPER's URN — a terminal SUCCESS request.
    start_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    execution_requests = {
        wrapper_urn: [
            {
                "urn": exec_request_urn,
                "status": "SUCCESS",
                "startTimeMs": start_ms,
                "durationMs": 4200,
            }
        ],
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[registered_source, wrapper_source],
            datasets=[_DS_A, _DS_B],
            pipeline_names={_DS_A: parent_urn, _DS_B: parent_urn},
            execution_requests=execution_requests,
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        parent_id = await _source_id_for_urn(async_session, parent_urn)
        events, total = await service.get_events_for_source(parent_id, limit=50)

        # The wrapper's run surfaces on the regular parent's event list.
        complete = [e for e in events if e["event_type"] == "INGESTION.COMPLETE"]
        assert complete, (
            "Regular parent's event list must include the wrapper's INGESTION.COMPLETE "
            f"run; got events: {events}. "
            "spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4 — parent "
            "aggregates wrapper events."
        )
        # Discriminator: execution_request_urn is the spec'd identity key for
        # sync-mirrored ingestion events (BACKEND.md §Event Catalogue), used here to
        # pick the right row.
        wrapper_evt = next(
            (
                e
                for e in complete
                if (e.get("detail") or {}).get("execution_request_urn") == exec_request_urn
            ),
            None,
        )
        assert wrapper_evt is not None, (
            f"Parent must surface the wrapper run with execution_request_urn="
            f"{exec_request_urn!r}; got {complete}. "
            "spec: API.md §Ingestion — GET /sources/{id}/event includes wrapper events."
        )
        # Derived wrapper flag is true (the event was booked on the wrapper, not the parent).
        assert wrapper_evt["wrapper"] is True, (
            f"Event mirrored from a linked wrapper must carry wrapper=true; got "
            f"{wrapper_evt['wrapper']!r}. spec: API.md §Ingestion — derived wrapper flag; "
            "spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4."
        )
        # spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4 — status mapping
        #   SUCCESS/SUCCEEDED → INGESTION.COMPLETE → status='success'.
        assert wrapper_evt["status"] == "success", (
            f"A SUCCESS run mirrors as status='success'; got {wrapper_evt['status']!r}. "
            "spec: feature/BACKEND.md §Ingestion Service (Sync + mapping sweep) step 4 — "
            "SUCCESS→status='success'."
        )
        assert total >= 1
    finally:
        await dataspoke_db.reset_ingestion_sources()
