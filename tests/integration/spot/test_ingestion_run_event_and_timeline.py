"""Spot tests — the two ingestion read paths whose predicates are JSONB three-valued logic.

Two `IngestionService` reads gained a `detail`-shaped predicate:

- `get_latest_run_event(source_id)` — backs `attr/ingestion.latest_run`. Two terms: an
  event-type whitelist and a `detail.source` **blacklist of the observation producers**,
  whose `IS NULL` disjunct admits the inline `ACTIVE_CUSTOM_MANAGED` run record (which
  carries no `source` key at all).
- `get_events_for_source(..., dataset_urn=)` — backs both the per-source feed narrowed to
  one dataset and, through `DatasetService.get_events`, the per-dataset timeline. A row
  qualifies when its `detail.dataset_urn` equals the URN **or is absent**.

Neither can be covered at the unit tier, and the reason is specific rather than
stylistic: both rest on SQL's three-valued logic over a *missing* JSONB key.
`detail->>'source'` on a missing key is SQL `NULL`, and `NULL NOT IN (…)` evaluates to
`NULL` — which `WHERE` treats as false, silently dropping exactly the rows the query
exists to report. A fake session cannot reproduce that: it would have to reimplement the
predicate in Python, where `None not in {…}` is plainly `True` and the defect disappears.
Real PostgreSQL is the only place the assertion means anything.

spec: TESTING.md §Spot integration tests §Boundary — "a spot test may call dataspoke
  Python directly (e.g., a backend service or a workflow stub)".
spec: feedback_spot_vs_api_wired_principle — spot for raw-SQL/ORM-seeded state that
  api-wired's pipeline setup cannot naturally reach. A source whose feed carries an older
  run `FAIL` under a newer per-dataset observation, and a sibling dataset's observation
  that must be excluded from this dataset's timeline, are both states a pipeline run
  cannot be steered into.

Spec: spec/feature/BACKEND.md §Sync + mapping sweep step 4 — "**Source `latest_run` =
  latest terminal *run* outcome**, over run-level producers only. Two predicates, both
  required … The blacklist must treat an **absent** `detail.source` as run-level — the
  inline `ACTIVE_CUSTOM_MANAGED` record carries no `source` key, and a bare `NOT IN` over
  SQL `NULL` drops exactly the events `latest_run` exists to report."
Spec: spec/feature/BACKEND.md §Querying Events — the per-dataset timeline resolves the
  source's rows "by reverse-lookup plus the `detail.dataset_urn` predicate".
Spec: spec/feature/BACKEND.md §Event Catalogue §producers — the four producers and their
  `detail` key sets; "No run-level producer writes a scalar `detail.dataset_urn`".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import ARRAY, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService

# No dummy-data constants: every row this module reads is one it seeded itself, so no
# Imazon dataset needs to exist in DataHub or PostgreSQL.
# spec: TESTING.md §Per-Module Dummy-Data Reset — modules with no constants are no-ops.

_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,spot_run_event."


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
    """Insert one ``ingestion_source`` row and return its id.

    A wrapper is a row with ``parent_source_id IS NOT NULL``
    (spec/feature/BACKEND_SCHEMA.md §ingestion_source).
    """
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
    status: str,
    detail: dict,
    occurred_at: datetime,
    entity_type: str = "ingestion_source",
) -> None:
    """Insert one ``events`` row with a controlled ``detail`` and ``occurred_at``.

    ``detail`` goes in as real JSONB, so an explicit ``{"dataset_urn": null}`` stays a
    JSON null rather than becoming a missing key — the two absence shapes the timeline
    predicate must both cover.
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


# ── get_latest_run_event ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_newer_observation_does_not_outrank_an_older_run_failure(
    async_session: AsyncSession,
) -> None:
    """A newer per-dataset ``COMPLETE`` must not displace an older run ``FAIL``.

    The whole estate is one source with two events, and the observation is the newer of
    the two, so "the newest event for this source" and "the newest run" give opposite
    answers. Only the producer blacklist can distinguish them: both are ``INGESTION.*``,
    both are terminal-looking, and the observation's ``status`` is ``success``.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "a **``detail.source``
      blacklist** of the observation producers, so a newer per-dataset ``COMPLETE`` cannot
      outrank an older run ``FAIL``."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-run-event-fail-under-obs")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        failed_at = now - timedelta(hours=2)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="error",
            detail={"run_id": "run-fail-1", "platform": "postgres"},
            occurred_at=failed_at,
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "source": "last_ingested_observation",
                "dataset_urn": _urn("observed"),
            },
            occurred_at=now - timedelta(minutes=5),
        )

        latest = await service.get_latest_run_event(source)

        assert latest is not None, (
            "the run FAIL must be reported; a bare NOT IN over the missing `source` key "
            "on that row would drop it and return None. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert latest["event_type"] == "INGESTION.FAIL", (
            f"latest_run must report the older run FAIL, not the newer observation; got "
            f"{latest['event_type']!r} at {latest['occurred_at']!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert latest["status"] == "error"
        assert latest["occurred_at"] == failed_at
        assert latest["detail"]["run_id"] == "run-fail-1"
        assert latest["wrapper"] is False
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_a_key_less_inline_run_record_is_reported(
    async_session: AsyncSession,
) -> None:
    """The inline ACM run record, which carries no ``detail.source`` key, is reported.

    Isolated deliberately: the only event on the source is the key-less one, so the
    ``IS NULL`` disjunct is the sole reason anything comes back. This is the exact shape a
    bare ``detail->>'source' NOT IN (…)`` drops — ``NULL NOT IN (…)`` is ``NULL``, and
    ``WHERE`` treats that as false.

    spec: feature/BACKEND.md §Event Catalogue §producers — "**``detail.source`` is absent,
      not null, on the inline record.** A consumer's producer filter must therefore treat
      a missing key as run-level; ``detail->>'source'`` on a missing key is SQL ``NULL``,
      and a bare ``NOT IN`` silently drops those rows."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-run-event-keyless")
        source_ids = [source]
        completed_at = datetime.now(tz=UTC) - timedelta(minutes=30)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "run_id": "run-inline-1",
                "platform": "postgres",
                "dry_run": False,
                "discovered_urns": [_urn("a")],
                "discovered_urns_count": 1,
                "emitted_urns": [_urn("a")],
                "emitted_urns_count": 1,
                "errors": [],
                "warnings": [],
            },
            occurred_at=completed_at,
        )

        latest = await service.get_latest_run_event(source)

        assert latest is not None, (
            "an inline run record with no `detail.source` key must be reported — it is "
            "the ACTIVE_CUSTOM_MANAGED run outcome and the only run this source has. "
            "spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert latest["detail"]["run_id"] == "run-inline-1"
        assert latest["occurred_at"] == completed_at
        assert "source" not in latest["detail"], (
            "backstop: the seeded row must genuinely carry no `source` key, or this test "
            "proves nothing about the IS NULL disjunct."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_a_newer_lifecycle_event_is_not_reported_as_a_run(
    async_session: AsyncSession,
) -> None:
    """A newer ``INGESTION.SOURCE_UPDATE`` must not be read as a run outcome.

    ``SOURCE_UPDATE`` carries ``status="success"`` and no ``detail.source`` key, so the
    producer blacklist alone would keep it — only the event-type whitelist excludes it.
    Both sides are seeded and the lifecycle event is the newer one, so a missing whitelist
    reports a configuration edit as a successful run over a real failure.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "an **event-type whitelist**
      (``INGESTION.COMPLETE`` / ``INGESTION.FAIL``), so
      ``SOURCE_CREATE``/``SOURCE_UPDATE``/``SOURCE_DELETE`` and any future non-run
      ``INGESTION.*`` cannot be read as a run".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-run-event-lifecycle")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        failed_at = now - timedelta(hours=3)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="error",
            detail={"run_id": "run-fail-2", "platform": "postgres"},
            occurred_at=failed_at,
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.SOURCE_UPDATE",
            status="success",
            detail={"operation": "PATCH", "fields_changed": ["schedule"]},
            occurred_at=now - timedelta(minutes=1),
        )

        latest = await service.get_latest_run_event(source)

        assert latest is not None, "backstop: the run FAIL must still be found."
        assert latest["event_type"] == "INGESTION.FAIL", (
            f"a newer SOURCE_UPDATE must not be reported as a run; got "
            f"{latest['event_type']!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert latest["occurred_at"] == failed_at
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_the_newest_run_outcome_wins_across_run_level_producers(
    async_session: AsyncSession,
) -> None:
    """Among run-level events the newest wins, whichever producer wrote it.

    Three run-level rows in mixed insertion order — key-less inline, ``datahub_sync``
    mirror, key-less inline again — so a reader that took the first stored row, or that
    preferred one producer, would answer differently from one that ordered by
    ``occurred_at``.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "Source ``latest_run`` =
      latest terminal *run* outcome".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-run-event-ordering")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        newest_at = now - timedelta(minutes=10)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"run_id": "run-oldest"},
            occurred_at=now - timedelta(hours=5),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="failure",
            detail={
                "source": "datahub_sync",
                "execution_request_urn": "urn:li:dataHubExecutionRequest:newest",
                "duration_ms": 1200,
            },
            occurred_at=newest_at,
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"run_id": "run-middle"},
            occurred_at=now - timedelta(hours=1),
        )

        latest = await service.get_latest_run_event(source)

        assert latest is not None
        assert latest["occurred_at"] == newest_at, (
            f"the newest run-level event must win; got {latest['occurred_at']!r}, "
            f"expected {newest_at!r}. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4."
        )
        assert latest["detail"]["execution_request_urn"].endswith("newest")
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_a_feed_of_only_observations_reports_no_run(
    async_session: AsyncSession,
) -> None:
    """A source whose only events are observations reports ``None``.

    This is the `PASSIVE` reading. Both observation producers are seeded, so the absence
    is a filtered result rather than an empty feed, and the count is asserted through the
    unfiltered timeline read so the rows are provably there.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "**A ``PASSIVE`` source
      reports no ``latest_run``, by construction** … a passive source's only
      ``INGESTION.COMPLETE``s are per-dataset observations, and ``attr/ingestion.latest_run``
      is ``null`` for it."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(
            async_session, name="spot-run-event-passive", mode="PASSIVE"
        )
        source_ids = [source]
        now = datetime.now(tz=UTC)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "source": "passive_observation",
                "dataset_urn": _urn("passive-a"),
                "operation_type": "INSERT",
            },
            occurred_at=now - timedelta(minutes=20),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "source": "last_ingested_observation",
                "dataset_urn": _urn("passive-b"),
            },
            occurred_at=now - timedelta(minutes=10),
        )

        _rows, total = await service.get_events_for_source(source, offset=0, limit=50)
        assert total == 2, (
            f"backstop: both observations must be in the feed, or the None below only "
            f"proves the source has no events at all; got total={total}."
        )

        assert await service.get_latest_run_event(source) is None, (
            "a feed carrying only observations must produce no run outcome. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_a_run_booked_on_a_wrapper_is_reported_and_flagged(
    async_session: AsyncSession,
) -> None:
    """A run booked on the CLI wrapper is the parent's ``latest_run``, flagged ``wrapper``.

    DataHub books a managed source's executions on the auto-created wrapper rather than on
    the registered source, so a lookup by the registered id alone would report ``None``
    for every `DATAHUB_MANAGED` source. The parent carries no run of its own, which is what
    makes the union load-bearing here rather than incidental.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "**The regular source
      aggregates events across itself and its linked wrappers**: the per-source event
      endpoint and the per-dataset latest-run aggregation union the parent's own events
      with its wrappers' events".
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        parent = await _seed_source(
            async_session, name="spot-run-event-parent", mode="DATAHUB_MANAGED"
        )
        wrapper = await _seed_source(
            async_session,
            name="[CLI] spot-run-event-parent",
            mode="DATAHUB_MANAGED",
            parent_source_id=parent,
        )
        source_ids = [wrapper, parent]
        run_at = datetime.now(tz=UTC) - timedelta(minutes=15)
        await _seed_event(
            async_session,
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "source": "datahub_sync",
                "execution_request_urn": "urn:li:dataHubExecutionRequest:on-wrapper",
                "duration_ms": 900,
            },
            occurred_at=run_at,
        )

        latest = await service.get_latest_run_event(parent)

        assert latest is not None, (
            "a run booked on the CLI wrapper must surface as the parent's latest_run. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert latest["occurred_at"] == run_at
        assert latest["wrapper"] is True, (
            f"the row must be flagged as sourced from a wrapper; got "
            f"wrapper={latest['wrapper']!r}."
        )
    finally:
        await _cleanup(async_session, source_ids)


# ── get_events_for_source(dataset_urn=…) — the per-dataset timeline predicate ──


@pytest.mark.asyncio
async def test_the_timeline_keeps_run_rows_and_drops_a_siblings_observation(
    async_session: AsyncSession,
) -> None:
    """Narrowed to one dataset: run-level rows stay, a sibling's observation goes.

    Four rows on one source, seeded on both sides of the predicate:

      - a key-less inline run ``COMPLETE`` — kept (no scalar ``dataset_urn`` at all)
      - a ``datahub_sync`` mirror ``FAIL`` carrying an explicit JSON ``null``
        ``dataset_urn`` — kept (the *other* absence shape)
      - this dataset's own observation — kept
      - a **sibling** dataset's observation — dropped

    ``total_count == len(rows)`` is asserted as its own check: the predicate is applied to
    the shared base select, so a version that narrowed the page query but not the count
    over its subquery would return three rows and report four, and every paginating caller
    would then see a phantom page.

    spec: feature/BACKEND.md §Querying Events — "a row qualifies when its
      ``detail.dataset_urn`` is this URN **or is absent** … Absence covers both shapes: a
      missing key and an explicit JSON ``null``. The predicate belongs to the shared base
      select, so the page query and its ``total_count`` cannot diverge."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    mine = _urn("timeline-mine")
    sibling = _urn("timeline-sibling")
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-timeline-predicate")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"run_id": "run-inline", "platform": "postgres"},
            occurred_at=now - timedelta(hours=4),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="failure",
            detail={
                "source": "datahub_sync",
                "execution_request_urn": "urn:li:dataHubExecutionRequest:explicit-null",
                "dataset_urn": None,
            },
            occurred_at=now - timedelta(hours=3),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"source": "last_ingested_observation", "dataset_urn": mine},
            occurred_at=now - timedelta(hours=2),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"source": "last_ingested_observation", "dataset_urn": sibling},
            occurred_at=now - timedelta(hours=1),
        )

        unfiltered, unfiltered_total = await service.get_events_for_source(
            source, offset=0, limit=50
        )
        assert unfiltered_total == 4, (
            f"backstop: all four rows must exist unfiltered, or the exclusions below "
            f"prove nothing; got {unfiltered_total}."
        )
        assert len(unfiltered) == 4

        rows, total = await service.get_events_for_source(
            source, offset=0, limit=50, dataset_urn=mine
        )

        urns = [(r.get("detail") or {}).get("dataset_urn") for r in rows]
        assert sibling not in urns, (
            f"a sibling dataset's observation must not appear on this dataset's timeline; "
            f"got {urns!r}. spec: feature/BACKEND.md §Querying Events."
        )
        assert mine in urns, (
            f"this dataset's own observation must be kept; got {urns!r}."
        )
        run_ids = {(r.get("detail") or {}).get("run_id") for r in rows}
        assert "run-inline" in run_ids, (
            "the key-less inline run record must be kept — an equality-only predicate "
            "would delete precisely the run rows from every dataset timeline. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert any(r["event_type"] == "INGESTION.FAIL" for r in rows), (
            "the mirror FAIL carrying an explicit JSON null dataset_urn must be kept — "
            "`detail->>'…'` covers both absence shapes. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert len(rows) == 3
        assert total == len(rows), (
            f"total_count must equal the narrowed row count; got total={total} with "
            f"{len(rows)} rows. A predicate applied to only one of the page query and the "
            "count over its subquery produces exactly this divergence. "
            "spec: feature/BACKEND.md §Querying Events."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_the_timeline_predicate_composes_with_the_wrapper_union_and_time_range(
    async_session: AsyncSession,
) -> None:
    """The dataset predicate composes with the wrapper union and the ``from``/``to`` range.

    Three orthogonal filters applied at once, each with a row that only it excludes:

      - a sibling's observation on the **wrapper** — excluded by the dataset predicate,
        and it is on the wrapper so it proves the two compose rather than one shadowing
        the other
      - this dataset's observation on the wrapper, **inside** the window — kept, and
        flagged ``wrapper``
      - this dataset's observation **before** the window — excluded by ``from``

    spec: feature/BACKEND.md §Querying Events — the per-dataset timeline is "filtered by
      ``from``/``to``" over the source-and-wrappers union.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    mine = _urn("compose-mine")
    sibling = _urn("compose-sibling")
    source_ids: list[str] = []
    try:
        parent = await _seed_source(
            async_session, name="spot-timeline-compose-parent", mode="DATAHUB_MANAGED"
        )
        wrapper = await _seed_source(
            async_session,
            name="[CLI] spot-timeline-compose-parent",
            mode="DATAHUB_MANAGED",
            parent_source_id=parent,
        )
        source_ids = [wrapper, parent]
        now = datetime.now(tz=UTC)
        window_start = now - timedelta(hours=2)
        kept_at = now - timedelta(hours=1)

        await _seed_event(
            async_session,
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"source": "last_ingested_observation", "dataset_urn": sibling},
            occurred_at=now - timedelta(minutes=30),
        )
        await _seed_event(
            async_session,
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"source": "last_ingested_observation", "dataset_urn": mine},
            occurred_at=kept_at,
        )
        await _seed_event(
            async_session,
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={"source": "last_ingested_observation", "dataset_urn": mine},
            occurred_at=now - timedelta(hours=6),
        )

        rows, total = await service.get_events_for_source(
            parent,
            offset=0,
            limit=50,
            from_dt=window_start,
            dataset_urn=mine,
        )

        assert total == 1, (
            f"exactly the in-window row for this dataset survives all three filters; got "
            f"total={total} with rows at "
            f"{[r['occurred_at'] for r in rows]!r}. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert len(rows) == 1
        assert rows[0]["occurred_at"] == kept_at
        assert rows[0]["detail"]["dataset_urn"] == mine
        assert rows[0]["wrapper"] is True, (
            "the surviving row was booked on the wrapper, so it must carry the wrapper "
            "flag — proving the dataset predicate did not replace the union."
        )
    finally:
        await _cleanup(async_session, source_ids)


@pytest.mark.asyncio
async def test_an_unnarrowed_feed_still_shows_every_producer(
    async_session: AsyncSession,
) -> None:
    """Without ``dataset_urn`` the per-source feed shows every producer, unfiltered.

    The per-source ``event/…`` timeline is deliberately *not* run-outcome filtered — that
    filtering belongs to ``latest_run`` alone — so this is the negative control for the
    blacklist tests above: the very rows ``get_latest_run_event`` excludes are the rows
    this read must keep.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "The per-source ``event/…``
      timeline is deliberately **not** filtered this way: it shows every producer."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-feed-unfiltered")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.SOURCE_UPDATE",
            status="success",
            detail={"operation": "PATCH", "fields_changed": ["recipe"]},
            occurred_at=now - timedelta(minutes=45),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            status="success",
            detail={
                "source": "passive_observation",
                "dataset_urn": _urn("unfiltered"),
                "operation_type": "UPDATE",
            },
            occurred_at=now - timedelta(minutes=30),
        )
        await _seed_event(
            async_session,
            entity_id=source,
            event_type="INGESTION.FAIL",
            status="error",
            detail={"run_id": "run-unfiltered"},
            occurred_at=now - timedelta(minutes=15),
        )

        rows, total = await service.get_events_for_source(source, offset=0, limit=50)

        assert total == 3, (
            f"the unnarrowed feed must carry all three producers; got {total}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert {r["event_type"] for r in rows} == {
            "INGESTION.SOURCE_UPDATE",
            "INGESTION.COMPLETE",
            "INGESTION.FAIL",
        }
    finally:
        await _cleanup(async_session, source_ids)
