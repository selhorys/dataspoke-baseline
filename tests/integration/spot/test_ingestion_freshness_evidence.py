"""Spot tests — the two `ingestion-freshness` evidence reads, against real JSONB.

`ingestion-freshness` resolves each dataset's evidence in two tiers:

- tier 1, `IngestionService.latest_ingestion_observed_by_dataset(source_ids)` — the newest
  observation instant per `(source, dataset URN)` pair, restricted to the observation
  producers by `detail->>'source' IN (…)`;
- tier 2, `IngestionService.latest_ingestion_complete_by_source(source_ids)` — the newest
  non-dry-run `INGESTION.COMPLETE` per source, producer-agnostic.

Both predicates read JSONB keys that may be missing, present-as-null, or present, and the
distinctions are only real in PostgreSQL. Tier 2's dry-run term is
`NOT COALESCE((detail->>'dry_run')::boolean, false)`: on a **missing** key
`detail->>'dry_run'` is SQL `NULL`, the cast keeps it `NULL`, `COALESCE` turns it into
`false`, and the row is included — which is the intended reading, since only the inline
`ACTIVE_CUSTOM_MANAGED` record ever sets the flag. A fake session cannot demonstrate that
chain; it would restate it in Python, where the three shapes are indistinguishable once
they have become `None`. Tier 1's grouping (`GROUP BY entity_id, detail->>'dataset_urn'`)
has the same property.

spec: TESTING.md §Spot integration tests §Boundary — "a spot test may call dataspoke
  Python directly (e.g., a backend service or a workflow stub)".
spec: feedback_spot_vs_api_wired_principle — spot for raw-SQL/ORM-seeded state. A dry-run
  `COMPLETE` sitting *newer* than a real one, and a source carrying observations for two
  different datasets, are states api-wired's pipeline setup cannot naturally reach.

Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — the two-tier evidence
  table; "The **dry-run exclusion is required on tier 2 regardless**, or case 1 survives
  untouched in the fallback path; a producer that carries no ``dry_run`` key at all (the
  mirror and both observation producers) is included, since only the inline
  ``ACTIVE_CUSTOM_MANAGED`` record ever sets it."
Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 2 "is
  **source-grained, not producer-filtered**: any ``COMPLETE`` on the owning source
  qualifies".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import ARRAY, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService

# No dummy-data constants: every row this module reads is one it seeded itself.
# spec: TESTING.md §Per-Module Dummy-Data Reset — modules with no constants are no-ops.

_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,spot_freshness_evidence."


def _urn(name: str) -> str:
    """A dataset URN unique to this module, so no other test's rows can collide."""
    return f"{_URN_PREFIX}{name},DEV)"


async def _seed_source(
    db: AsyncSession,
    *,
    name: str,
    mode: str = "ACTIVE_CUSTOM_MANAGED",
    parent_source_id: str | None = None,
) -> str:
    """Insert one ``ingestion_source`` row and return its id."""
    source_id = str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, "
            " datahub_source_urn, parent_source_id, status) "
            "VALUES (:id, :mode, :name, 'postgres', CAST(:recipe AS jsonb), "
            " NULL, NULL, NULL, :parent, 'OK')"
        ),
        {
            "id": source_id,
            "mode": mode,
            "name": name,
            "recipe": json.dumps({"source": {"type": "postgres", "config": {}}}),
            "parent": parent_source_id,
        },
    )
    await db.commit()
    return source_id


async def _seed_event(
    db: AsyncSession,
    *,
    entity_id: str,
    event_type: str,
    detail: dict,
    occurred_at: datetime,
    status: str = "success",
    entity_type: str = "ingestion_source",
) -> None:
    """Insert one ``events`` row with a controlled ``detail`` and ``occurred_at``.

    ``detail`` goes in as real JSONB, so ``{"dry_run": None}`` stays a JSON null rather
    than becoming a missing key — the two shapes the ``COALESCE`` chain must treat alike.
    """
    await db.execute(
        text(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), :etype, :eid, :evtype, :status, "
            " CAST(:detail AS jsonb), :at)"
        ),
        {
            "etype": entity_type,
            "eid": entity_id,
            "evtype": event_type,
            "status": status,
            "detail": json.dumps(detail),
            "at": occurred_at,
        },
    )
    await db.commit()


async def _cleanup(db: AsyncSession, source_ids: list[str]) -> None:
    """Delete only the rows this module seeded — scoped, so concurrent state survives.

    The ``TEXT[]`` parameters bind a Python list through ``bindparam(ARRAY(Text()))``
    rather than a ``"{a,b}"`` literal: the literal form is accepted by psycopg and
    rejected by asyncpg, which is the driver behind ``async_session``.
    spec: project_asyncpg_textarray_seed_binding.
    """
    await db.rollback()
    if not source_ids:
        return
    await db.execute(
        text("DELETE FROM dataspoke.events WHERE entity_id = ANY(:ids)").bindparams(
            bindparam("ids", type_=ARRAY(Text()))
        ),
        {"ids": source_ids},
    )
    await db.execute(
        text(
            "DELETE FROM dataspoke.ingestion_source WHERE id::text = ANY(:ids)"
        ).bindparams(bindparam("ids", type_=ARRAY(Text()))),
        {"ids": source_ids},
    )
    await db.commit()


# ── Tier 1: latest_ingestion_observed_by_dataset ──────────────────────────────


@pytest.mark.asyncio
async def test_tier_1_keys_each_instant_to_its_own_dataset(
    async_session: AsyncSession,
) -> None:
    """Each ``(source, dataset)`` pair gets its own newest observation instant.

    Two datasets under one source, each with two observations, so the answer is wrong in
    two distinguishable ways: keyed on the source alone both datasets would read the
    estate maximum, and without the ``max`` aggregation the pair would be keyed to
    whichever row happened to come back last.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1 is "``max(occurred_at)``
      over the observation events the owning source booked **for that dataset**".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    first = _urn("tier1-first")
    second = _urn("tier1-second")
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-evidence-tier1-keys")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        first_newest = now - timedelta(hours=1)
        second_newest = now - timedelta(hours=5)
        for dataset_urn, occurred_at in (
            (first, now - timedelta(hours=9)),
            (first, first_newest),
            (second, now - timedelta(hours=11)),
            (second, second_newest),
        ):
            await _seed_event(
                async_session,
                entity_id=source,
                event_type="INGESTION.COMPLETE",
                detail={
                    "source": "last_ingested_observation",
                    "dataset_urn": dataset_urn,
                },
                occurred_at=occurred_at,
            )

        observed = await service.latest_ingestion_observed_by_dataset([source])

        assert observed.get((source, first)) == first_newest, (
            f"the first dataset must read its own newest observation; got "
            f"{observed.get((source, first))!r}, expected {first_newest!r}."
        )
        assert observed.get((source, second)) == second_newest, (
            f"the second dataset must read its own newest observation, not the estate "
            f"maximum; got {observed.get((source, second))!r}, expected "
            f"{second_newest!r}. spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_tier_1_reads_only_the_observation_producers(
    async_session: AsyncSession,
) -> None:
    """Only the two observation producers supply tier-1 evidence.

    Both sides are seeded on the same source and dataset, and the disqualified rows are
    the *newer* ones, so any leak changes the answer:

      - ``passive_observation`` — 6h ago, the answer
      - a run-level ``COMPLETE`` with no ``detail.source`` key but a ``dataset_urn`` —
        1h ago, must not qualify (a run is not a per-dataset claim)
      - an ``INGESTION.FAIL`` observation-shaped row — 30m ago, must not qualify
        (observation is success-only, and tier 1 reads ``COMPLETE``)

    The second row is deliberately impossible in production — no run-level producer writes
    a scalar ``dataset_urn`` — precisely so the producer term is the only thing that can
    exclude it here.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1 is restricted to
      "``detail.source ∈ {passive_observation, last_ingested_observation}``".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    dataset_urn = _urn("tier1-producers")
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-evidence-tier1-producers")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        observed_at = now - timedelta(hours=6)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail={
                "source": "passive_observation",
                "dataset_urn": dataset_urn,
                "operation_type": "INSERT",
            },
            occurred_at=observed_at,
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail={"run_id": "run-1", "dataset_urn": dataset_urn},
            occurred_at=now - timedelta(hours=1),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="error",
            detail={
                "source": "passive_observation",
                "dataset_urn": dataset_urn,
                "operation_type": "INSERT",
            },
            occurred_at=now - timedelta(minutes=30),
        )

        observed = await service.latest_ingestion_observed_by_dataset([source])

        assert observed.get((source, dataset_urn)) == observed_at, (
            f"only an observation-producer INGESTION.COMPLETE may supply tier-1 evidence; "
            f"got {observed.get((source, dataset_urn))!r}, expected {observed_at!r}. "
            "spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_tier_1_counts_a_wrappers_observation_as_the_parents(
    async_session: AsyncSession,
) -> None:
    """An observation booked on a CLI wrapper is keyed to the owning parent.

    The parent carries no observation of its own, so the union is the only reason the pair
    exists at all. A second, unrelated source in the same call carries its own observation,
    proving the query returns keys and that the wrapper's did not simply leak onto every
    source.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — "The owning source's
      **CLI-wrapper runs count as its own** … so a source's events are the union of its own
      and its wrappers'".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    wrapped_urn = _urn("tier1-wrapped")
    other_urn = _urn("tier1-other")
    source_ids: list[str] = []
    try:
        parent = await _seed_source(
            async_session, name="spot-evidence-tier1-parent", mode="DATAHUB_MANAGED"
        )
        wrapper = await _seed_source(
            async_session,
            name="[CLI] spot-evidence-tier1-parent",
            mode="DATAHUB_MANAGED",
            parent_source_id=parent,
        )
        other = await _seed_source(async_session, name="spot-evidence-tier1-other")
        source_ids = [wrapper, parent, other]
        now = datetime.now(tz=UTC)
        wrapper_at = now - timedelta(hours=2)
        other_at = now - timedelta(hours=3)
        await _seed_event(
            async_session,
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            detail={"source": "last_ingested_observation", "dataset_urn": wrapped_urn},
            occurred_at=wrapper_at,
        )
        await _seed_event(
            async_session,
            entity_id=other,
            event_type="INGESTION.COMPLETE",
            detail={"source": "last_ingested_observation", "dataset_urn": other_urn},
            occurred_at=other_at,
        )

        observed = await service.latest_ingestion_observed_by_dataset([parent, other])

        assert observed.get((other, other_urn)) == other_at, (
            "backstop: the unwrapped source's own observation must be keyed, or the "
            "wrapper result below only proves the query returned something."
        )
        assert observed.get((parent, wrapped_urn)) == wrapper_at, (
            f"an observation booked on the wrapper must be keyed to the owning parent; "
            f"got keys {sorted(observed)}. "
            "spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
        assert (wrapper, wrapped_urn) not in observed, (
            "the wrapper must not get a key of its own — its rows belong to the parent."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_tier_1_over_an_empty_source_list_returns_an_empty_mapping(
    async_session: AsyncSession,
) -> None:
    """An empty source-id list returns ``{}``.

    The measurer reaches this whenever no dataset in scope has an owning source.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    assert await service.latest_ingestion_observed_by_dataset([]) == {}


# ── Tier 2: the dry-run exclusion ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier_2_excludes_a_dry_run_complete(
    async_session: AsyncSession,
) -> None:
    """A newer dry-run ``COMPLETE`` must not supply the source-level fallback.

    Both sides on one source, with the dry run the *newer* of the two, so a missing
    exclusion is visible as a changed timestamp rather than as an extra row. A dry run
    emits nothing to DataHub by definition, yet a dry run without errors still books
    ``INGESTION.COMPLETE`` — so without this term one operator's dry run marks every
    dataset mapped to the source ingested-in-time.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — "**A dry run emits nothing
      by definition**, yet a dry run without errors still books ``INGESTION.COMPLETE``
      (carrying ``detail.dry_run = true``)"; tier 2 is "``max(occurred_at)`` over **every**
      ``INGESTION.COMPLETE`` booked on the owning source — no producer filter, **excluding
      dry runs**".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-evidence-dryrun")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        real_run_at = now - timedelta(hours=8)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail={"run_id": "run-real", "dry_run": False},
            occurred_at=real_run_at,
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail={"run_id": "run-dry", "dry_run": True},
            occurred_at=now - timedelta(minutes=5),
        )

        latest = await service.latest_ingestion_complete_by_source([source])

        assert latest.get(source) == real_run_at, (
            f"the newer dry run must not supply the fallback; got {latest.get(source)!r}, "
            f"expected the real run at {real_run_at!r}. "
            "spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_a_source_whose_only_complete_is_a_dry_run_has_no_tier_2_evidence(
    async_session: AsyncSession,
) -> None:
    """A source whose only ``COMPLETE`` is a dry run is absent from the result.

    Absence, not a timestamp: the measurer distinguishes "no evidence" from "evidence at
    time T" by key membership, and a dry run is no evidence at all. A second source in the
    same call carries a real run, so the absence is a filtered result rather than an empty
    query.

    spec: feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is failed when
      its resolved evidence "is older than ``metric_conf.time_window_sec``, or absent on
      both tiers".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        dry_only = await _seed_source(async_session, name="spot-evidence-dry-only")
        real = await _seed_source(async_session, name="spot-evidence-real-run")
        source_ids = [dry_only, real]
        now = datetime.now(tz=UTC)
        real_at = now - timedelta(minutes=20)
        await _seed_event(
            async_session,
            entity_id=dry_only,
            event_type="INGESTION.COMPLETE",
            detail={"run_id": "run-dry-only", "dry_run": True},
            occurred_at=now - timedelta(minutes=10),
        )
        await _seed_event(
            async_session,
            entity_id=real,
            event_type="INGESTION.COMPLETE",
            detail={"run_id": "run-real", "dry_run": False},
            occurred_at=real_at,
        )

        latest = await service.latest_ingestion_complete_by_source([dry_only, real])

        assert latest.get(real) == real_at, (
            "backstop: the real run must be keyed, or the absence below only proves the "
            "query returned nothing at all."
        )
        assert dry_only not in latest, (
            f"a source whose only COMPLETE is a dry run must be absent from the result; "
            f"got keys {sorted(latest)}. "
            "spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "detail"),
    [
        ("missing key", {"source": "last_ingested_observation", "dataset_urn": "x"}),
        ("explicit JSON null", {"run_id": "run-null-flag", "dry_run": None}),
        ("explicitly false", {"run_id": "run-false-flag", "dry_run": False}),
    ],
)
async def test_a_complete_that_is_not_flagged_a_dry_run_still_supplies_tier_2(
    async_session: AsyncSession, label: str, detail: dict
) -> None:
    """Absent, JSON-null and ``false`` ``dry_run`` flags all still supply the fallback.

    The three shapes are indistinguishable once read into Python, and only the first is
    what production actually writes for the mirror and both observation producers — they
    carry no ``dry_run`` key at all. In SQL each takes a different route through
    ``NOT COALESCE((detail->>'dry_run')::boolean, false)``, and an over-eager exclusion
    (say ``detail ? 'dry_run'``, or a ``NOT`` without the ``COALESCE``) would empty tier 2
    for exactly the producers `PASSIVE` depends on.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — "a producer that carries no
      ``dry_run`` key at all (the mirror and both observation producers) is included,
      since only the inline ``ACTIVE_CUSTOM_MANAGED`` record ever sets it."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name=f"spot-evidence-flag-{label}")
        source_ids = [source]
        occurred_at = datetime.now(tz=UTC) - timedelta(minutes=25)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail=detail,
            occurred_at=occurred_at,
        )

        latest = await service.latest_ingestion_complete_by_source([source])

        assert latest.get(source) == occurred_at, (
            f"{label}: a COMPLETE that is not flagged as a dry run must supply the "
            f"source-level fallback; got {latest.get(source)!r}, expected "
            f"{occurred_at!r}. spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_tier_2_stays_producer_agnostic_and_admits_an_observation(
    async_session: AsyncSession,
) -> None:
    """A per-dataset observation still qualifies as tier-2 evidence.

    This is by design rather than by omission: a `PASSIVE` source books no run-level event
    at all, so blacklisting the observation producers here would not narrow the fallback
    but empty it, leaving every passive dataset without an observation of its own reading
    permanently stale.

    spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — "A producer blacklist on
      tier 2 would not merely narrow the fallback, it would **empty** it for ``PASSIVE``
      … Tier 2 is therefore source-grained and producer-agnostic by design, not by
      omission."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(
            async_session, name="spot-evidence-tier2-passive", mode="PASSIVE"
        )
        source_ids = [source]
        observed_at = datetime.now(tz=UTC) - timedelta(minutes=40)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            detail={
                "source": "passive_observation",
                "dataset_urn": _urn("tier2-passive"),
                "operation_type": "INSERT",
            },
            occurred_at=observed_at,
        )

        latest = await service.latest_ingestion_complete_by_source([source])

        assert latest.get(source) == observed_at, (
            f"an observation must still qualify as source-level fallback evidence; got "
            f"{latest.get(source)!r}, expected {observed_at!r}. "
            "spec: feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
    finally:
        await _cleanup(async_session, source_ids)
