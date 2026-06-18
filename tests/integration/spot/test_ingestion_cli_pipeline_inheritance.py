"""Spot test — Ingestion sync sweep step 3: ad-hoc CLI wrapper inherits the
registered source's pipeline_name link.

Concern (one): when DataHub stamps a dataset's emitted aspects with
``systemMetadata.pipelineName = <parent registered source URN>`` (the behaviour
of a ``datahub ingest`` run of a registered DATAHUB_MANAGED source), sync()
step 3 awards ``derivation=pipeline_name`` / authority ``high`` to BOTH:
  - the registered source whose ``datahub_source_urn`` equals that pipelineName, and
  - the ad-hoc CLI wrapper source auto-created by that run, whose display name is
    ``[CLI] <type> [<parent registered URN>]`` — but ONLY when the parsed parent
    URN matches a registered DATAHUB_MANAGED row in DataSpoke.

Fallbacks keep the step-2 ``matched``/``medium`` mapping:
  - wrapper whose parsed parent URN has no matching registered row, and
  - wrapper whose name carries no parseable parent URN.

A source that merely recipe-matches the same tables (no pipelineName
correspondence) likewise stays ``matched``/``medium``.

This is a spot concern (not api-wired): the dev-env DataHub executor does not run
CLI executions, so an api-wired pipeline cannot naturally create the ad-hoc CLI
wrapper + parent-stamped pipelineName state. The setup is ORM/SQL-seeded
(per feedback_spot_vs_api_wired_principle), and spot is stub-only
(feedback_spot_is_stub_only) — the DataHub client is stubbed.

Spec: spec/feature/BACKEND.md §Sync sweep step 3 (Observed enrichment) — ad-hoc
    CLI inheritance + fallback.
Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — `[CLI] <type> [<parent_urn>]`
    wrapper name grammar; systemMetadata.pipelineName = parent registered URN.
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source_dataset — derivation column;
    pipeline_name → high authority.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import (
    IngestionService,
    IngestionSourceDatasetRecord,
)
from tests.integration.util import dataspoke_db

# This test seeds and asserts purely on dataspoke.ingestion_source* rows with a
# stubbed DataHub client — no real dataset enumeration is needed, so no
# DUMMY_DATA_* constants (no PG/DataHub reset dependency).

# Two postgres dataset URNs in the `catalog` schema. The seeded sources' recipes
# carry `schema_pattern: ^catalog$`, so step-2's filter-matcher maps both of these
# to each source as derivation='matched' (the pre-state step 3 promotes).
_DS_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_DS_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

# A postgres recipe scoped to the catalog schema. parse_recipe needs source.type;
# schema_pattern drives the step-2 matcher onto _DS_A / _DS_B.
_CATALOG_RECIPE = '{"source": {"type": "postgres", "config": {"schema_pattern": {"allow": ["^catalog$"]}}}}'  # noqa: E501


class _StubDataHubForPipelineSync:
    """Minimal DataHub stub exposing only what IngestionService.sync() touches.

    - ``list_ingestion_sources`` returns the seeded source defs so step 1 upserts
      them as DATAHUB_MANAGED with the correct derived ad_hoc flag.
    - ``enumerate_datasets`` returns the two catalog dataset URNs so step 2's
      matcher maps them (derivation='matched').
    - ``get_pipeline_names`` is the pivot: it returns the per-dataset
      systemMetadata.pipelineName that step 3 reads.
    - ``list_execution_requests`` is a no-op (step 4 mirrors nothing here).
    """

    def __init__(
        self,
        sources: list[dict[str, Any]],
        datasets: list[str],
        pipeline_names: dict[str, str | None],
    ) -> None:
        self._sources = sources
        self._datasets = datasets
        self._pipeline_names = pipeline_names

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return self._sources

    async def enumerate_datasets(self) -> list[str]:
        return self._datasets

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str | None]:
        # Mirror the real client contract (src/shared/datahub/client.py
        # get_pipeline_names -> dict[str, str | None]): return an entry for
        # EVERY input URN, with None where no pipelineName is stamped.
        return {u: self._pipeline_names.get(u) for u in urns}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return []


async def _datasets_for_source(
    async_session: AsyncSession, source_urn: str
) -> dict[str, IngestionSourceDatasetRecord]:
    """Return {dataset_urn: record} for the source identified by its DataHub URN.

    Looks the source up by its stable ``datahub_source_urn`` (set by sync step 1),
    then reads its ingestion_source_dataset rows as service value objects so the
    test asserts on ``derivation`` and the derived ``authority`` property exactly
    as the service layer exposes them.
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


@pytest.mark.asyncio
async def test_ad_hoc_cli_wrapper_inherits_pipeline_name_from_registered_parent(
    async_session: AsyncSession,
) -> None:
    """sync() awards pipeline_name/high to BOTH the registered source and the ad-hoc
    CLI wrapper that parses to it.

    Setup: a registered DATAHUB_MANAGED source (URN PARENT) and an ad-hoc CLI
    wrapper whose name is ``[CLI] postgres [PARENT]`` (own URN ``cli-<hash>``).
    Both recipes scope to the catalog schema, so step 2 maps _DS_A/_DS_B to each as
    matched/medium. The stub stamps pipelineName=PARENT on both datasets.

    Expect: after the sweep, both _DS_A and _DS_B carry derivation='pipeline_name'
    (authority 'high') for the registered source AND for the ad-hoc wrapper — the
    wrapper inherits because its parsed parent URN matches the registered row.

    Spec: feature/BACKEND.md §Sync sweep step 3 — a dataset's pipelineName awards
        pipeline_name/high to every corresponding source: the registered source AND
        the ad-hoc CLI wrapper that inherits from it.
    Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — `[CLI] <type> [<parent_urn>]`
        grammar; pipelineName = parent registered URN.
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
    # The ad-hoc CLI wrapper auto-created by `datahub ingest` of the registered
    # source: cli- URN, [CLI] name embedding the parent registered URN.
    wrapper_source = {
        "urn": wrapper_urn,
        "name": f"[CLI] postgres [{parent_urn}]",
        "recipe": _CATALOG_RECIPE,
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

        for label, ds_map in (("registered", registered_ds), ("ad-hoc wrapper", wrapper_ds)):
            for urn in (_DS_A, _DS_B):
                rec = ds_map.get(urn)
                assert rec is not None, (
                    f"{label} source must have a mapping row for {urn!r}; got "
                    f"{sorted(ds_map)}. spec: feature/BACKEND.md §Sync sweep step 3."
                )
                assert rec.derivation == "pipeline_name", (
                    f"{label} source's mapping for {urn!r} must be promoted to "
                    f"derivation='pipeline_name'; got {rec.derivation!r}. spec: "
                    "feature/BACKEND.md §Sync sweep step 3 — pipelineName awards "
                    "pipeline_name to the registered source AND its ad-hoc CLI wrapper."
                )
                assert rec.authority == "high", (
                    f"{label} source's mapping for {urn!r} must have authority 'high'; "
                    f"got {rec.authority!r}. spec: feature/BACKEND_SCHEMA.md — "
                    "pipeline_name derivation → high authority."
                )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_ad_hoc_cli_wrapper_no_matching_registered_parent_stays_matched(
    async_session: AsyncSession,
) -> None:
    """A CLI wrapper whose parsed parent URN matches NO registered row stays
    matched/medium (fallback).

    Setup: an ad-hoc CLI wrapper named ``[CLI] postgres [<UNSEEDED PARENT>]`` whose
    bracketed parent URN is a valid dataHubIngestionSource URN but is NOT present
    as a registered DATAHUB_MANAGED row in DataSpoke. The dataset's pipelineName is
    the unseeded parent. There is no registered row to award the link, so the
    wrapper must inherit nothing and retain its step-2 matched mapping.

    Spec: feature/BACKEND.md §Sync sweep step 3 — Fallback: if no registered source
        row matches the parsed parent URN, the ad-hoc source inherits nothing and
        retains its step-2 filter-matcher mapping (matched/medium).
    """
    await dataspoke_db.reset_ingestion_sources()

    # A parent URN that is NEVER registered as a source row in DataSpoke.
    unseeded_parent_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex

    wrapper_source = {
        "urn": wrapper_urn,
        "name": f"[CLI] postgres [{unseeded_parent_urn}]",
        "recipe": _CATALOG_RECIPE,
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[wrapper_source],
            datasets=[_DS_A, _DS_B],
            # pipelineName points at the unseeded parent — no registered row matches.
            pipeline_names={_DS_A: unseeded_parent_urn, _DS_B: unseeded_parent_urn},
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        wrapper_ds = await _datasets_for_source(async_session, wrapper_urn)
        for urn in (_DS_A, _DS_B):
            rec = wrapper_ds.get(urn)
            assert rec is not None, (
                f"wrapper must still hold its step-2 matched row for {urn!r}; got "
                f"{sorted(wrapper_ds)}. spec: feature/BACKEND.md §Sync sweep step 3 fallback."
            )
            assert rec.derivation == "matched", (
                f"wrapper's mapping for {urn!r} must stay derivation='matched' when no "
                f"registered parent matches; got {rec.derivation!r}. spec: "
                "feature/BACKEND.md §Sync sweep step 3 — Fallback retains matched/medium."
            )
            assert rec.authority == "medium", (
                f"wrapper's mapping for {urn!r} must stay authority 'medium'; got "
                f"{rec.authority!r}. spec: feature/BACKEND_SCHEMA.md — matched → medium."
            )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_ad_hoc_cli_wrapper_unparseable_name_stays_matched(
    async_session: AsyncSession,
) -> None:
    """A CLI wrapper whose name carries no parseable parent URN stays matched/medium.

    Setup: a system CLI wrapper named ``[CLI] datahub-documents`` (no trailing
    parent-URN bracket). Even though a real registered source exists and its
    pipelineName is stamped on the datasets, the wrapper cannot parse a parent URN
    from its name, so it inherits nothing — its own matched rows are unaffected,
    while the registered parent is still promoted (sanity anchor for the same sweep).

    Spec: feature/BACKEND.md §Sync sweep step 3 — Fallback: if the parent URN cannot
        be parsed from the wrapper's display name, the ad-hoc source inherits nothing
        and retains matched/medium.
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
    # System CLI wrapper — no `[<urn>]` suffix → unparseable parent.
    sys_wrapper_source = {
        "urn": sys_wrapper_urn,
        "name": "[CLI] datahub-documents",
        "recipe": _CATALOG_RECIPE,
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
            "(anchor). spec: feature/BACKEND.md §Sync sweep step 3."
        )

        # The unparseable-name wrapper stays matched/medium.
        wrapper_ds = await _datasets_for_source(async_session, sys_wrapper_urn)
        for urn in (_DS_A, _DS_B):
            rec = wrapper_ds.get(urn)
            assert rec is not None, (
                f"wrapper must still hold its step-2 matched row for {urn!r}; got "
                f"{sorted(wrapper_ds)}. spec: feature/BACKEND.md §Sync sweep step 3 fallback."
            )
            assert rec.derivation == "matched", (
                f"wrapper with an unparseable name must keep derivation='matched' for "
                f"{urn!r}; got {rec.derivation!r}. spec: feature/BACKEND.md §Sync sweep "
                "step 3 — unparseable name → inherit nothing."
            )
            assert rec.authority == "medium", (
                f"wrapper's mapping for {urn!r} must stay authority 'medium'; got "
                f"{rec.authority!r}. spec: feature/BACKEND_SCHEMA.md — matched → medium."
            )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_one_pipeline_name_promotes_both_registered_source_and_inheriting_wrapper(
    async_session: AsyncSession,
) -> None:
    """A single dataset pipelineName fans out to a registered source AND the ad-hoc
    CLI wrapper that inherits from it — pipeline_name/high on both (1:many fan-out).

    This is the 1:many case called out in the impl: a single pipelineName can award
    pipeline_name/high to more than one source row. The two corresponding rows are a
    registered DATAHUB_MANAGED source (URN PARENT) and the inheriting CLI wrapper
    whose name embeds PARENT (``[CLI] postgres [PARENT]``) — NOT two registered rows
    (a ``datahub_source_urn`` is unique per registered source, so two registered rows
    sharing one URN is unrealistic). Only one dataset (_DS_A) carries the
    pipelineName; both rows must receive a pipeline_name mapping for _DS_A, while
    _DS_B (no pipelineName) stays matched on both.

    Spec: feature/BACKEND.md §Sync sweep step 3 — a dataset's pipelineName awards
        pipeline_name/high to EVERY source that corresponds to it (registered parent
        and its inheriting ad-hoc CLI wrapper).
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
        "name": f"[CLI] postgres [{parent_urn}]",
        "recipe": _CATALOG_RECIPE,
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }

    service = IngestionService(
        datahub=_StubDataHubForPipelineSync(
            sources=[registered_source, wrapper_source],
            datasets=[_DS_A, _DS_B],
            # Only _DS_A carries the pipelineName; _DS_B has none (stays matched).
            pipeline_names={_DS_A: parent_urn},
        ),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        await service.sync()

        registered_ds = await _datasets_for_source(async_session, parent_urn)
        wrapper_ds = await _datasets_for_source(async_session, wrapper_urn)

        # _DS_A is promoted on BOTH rows (1:many fan-out).
        for label, ds_map in (("registered", registered_ds), ("ad-hoc wrapper", wrapper_ds)):
            assert ds_map[_DS_A].derivation == "pipeline_name", (
                f"{label} source must receive pipeline_name for _DS_A (1:many fan-out); "
                f"got {ds_map[_DS_A].derivation!r}. spec: feature/BACKEND.md §Sync sweep "
                "step 3 — every corresponding source receives the link."
            )
            assert ds_map[_DS_A].authority == "high"
            # _DS_B carries no pipelineName → stays matched/medium on both rows.
            assert ds_map[_DS_B].derivation == "matched", (
                f"{label} source's _DS_B (no pipelineName) must stay matched; got "
                f"{ds_map[_DS_B].derivation!r}. spec: feature/BACKEND.md §Sync sweep step 3 — "
                "only datasets carrying the pipelineName are promoted."
            )
            assert ds_map[_DS_B].authority == "medium"
    finally:
        await dataspoke_db.reset_ingestion_sources()
