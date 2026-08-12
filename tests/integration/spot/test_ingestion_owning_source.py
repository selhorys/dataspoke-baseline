"""Spot tests — the two owning-source helpers the freshness measurer depends on.

``IngestionService.reverse_lookup_batch`` and
``IngestionService.latest_ingestion_complete_by_source`` are the pair that turn a
dataset URN list into "the recency of each dataset's owning source's runs". Neither
can be covered at the unit tier: both are pure DB queries, and the two properties
that matter most — the ``entity_type`` / ``event_type`` predicates on ``events``, and
the wrapper→parent union — are exactly what a fake session would have to reimplement
in order to "prove".

Spot is therefore the right layer, and raw ORM/SQL seeding the right span: the states
under test (a CLI wrapper claiming a dataset at a *higher* derivation rank than its
parent; a run booked only on the wrapper; a source with wrappers but no runs) are
states the api-wired pipeline cannot naturally reach.
spec: TESTING.md §Spot integration tests §Boundary — "a spot test may call dataspoke
Python directly (e.g., a backend service or a workflow stub)".
spec: feedback_spot_vs_api_wired_principle — spot for raw-SQL/ORM-seeded state.

Spec: spec/feature/BACKEND.md §Metrics Service §Time windows —
  - "Owning source is what IngestionService.reverse_lookup returns — or, over a whole
    dataset list at once, its batched single-winner sibling reverse_lookup_batch,
    which the measurer calls and which resolves the identical rule in two queries
    rather than one round trip per URN."
  - "derivation rank emitted > pipeline_name > matched; at equal rank a regular parent
    beats its CLI wrapper; remaining ties go to the most recent last_seen_at. Then, if
    the sort winner is itself a wrapper it resolves up to its regular parent — a
    wrapper is never the owning source. The second step is not the tie-break restated:
    it also fires when a wrapper claims a dataset at a higher derivation rank than its
    parent, where the tie-break never runs."
  - "The owning source's CLI-wrapper runs count as its own — DataHub books a managed
    source's executions on an auto-created wrapper rather than on the registered
    source, so a source's events are the union of its own and its wrappers'."
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — a row is a CLI wrapper iff
  parent_source_id IS NOT NULL.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService

# No dummy-data constants: every row this module reads is one it seeded itself, so no
# Imazon dataset needs to exist in DataHub or PostgreSQL.
# spec: TESTING.md §Per-Module Dummy-Data Reset — modules with no constants are no-ops.

_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,spot_owning_source."


def _urn(name: str) -> str:
    """A dataset URN unique to this module, so no other test's rows can collide."""
    return f"{_URN_PREFIX}{name},DEV)"


async def _seed_source(
    db: AsyncSession,
    *,
    mode: str = "DATAHUB_MANAGED",
    schedule_tier: str | None = None,
    parent_source_id: str | None = None,
    name: str,
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
            " NULL, :tier, :urn, :parent, 'OK')"
        ),
        {
            "id": source_id,
            "mode": mode,
            "name": name,
            "recipe": json.dumps({"source": {"type": "postgres", "config": {}}}),
            "tier": schedule_tier,
            "urn": "urn:li:dataHubIngestionSource:" + source_id,
            "parent": parent_source_id,
        },
    )
    await db.commit()
    return source_id


async def _seed_mapping(
    db: AsyncSession,
    *,
    source_id: str,
    dataset_urn: str,
    derivation: str,
    last_seen_at: datetime | None = None,
) -> None:
    """Insert one ``ingestion_source_dataset`` covering row."""
    await db.execute(
        text(
            "INSERT INTO dataspoke.ingestion_source_dataset "
            "(source_id, dataset_urn, derivation, first_seen_at, last_seen_at) "
            "VALUES (:sid, :urn, :derivation, :seen, :seen)"
        ),
        {
            "sid": source_id,
            "urn": dataset_urn,
            "derivation": derivation,
            "seen": last_seen_at or datetime.now(tz=UTC),
        },
    )
    await db.commit()


async def _seed_event(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    occurred_at: datetime,
) -> None:
    """Insert one ``events`` row with a controlled ``occurred_at``."""
    await db.execute(
        text(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), :etype, :eid, :evtype, 'success', "
            " CAST('{}' AS jsonb), :at)"
        ),
        {
            "etype": entity_type,
            "eid": entity_id,
            "evtype": event_type,
            "at": occurred_at,
        },
    )
    await db.commit()


async def _cleanup(db: AsyncSession, source_ids: list[str], urns: list[str]) -> None:
    """Delete only the rows this module seeded — scoped, so concurrent state survives."""
    await db.rollback()
    if urns:
        await db.execute(
            text(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE dataset_urn = ANY(CAST(:urns AS text[]))"
            ),
            {"urns": urns},
        )
    if source_ids:
        await db.execute(
            text(
                "DELETE FROM dataspoke.events WHERE entity_id = ANY(CAST(:ids AS text[]))"
            ),
            {"ids": source_ids},
        )
        # Wrappers cascade with their parent, so children first is not required; the
        # ANY() list carries both and the FK is ON DELETE CASCADE either way.
        await db.execute(
            text("DELETE FROM dataspoke.ingestion_source WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": source_ids},
        )
    await db.commit()


# ── reverse_lookup_batch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_agrees_with_reverse_lookup_on_a_single_urn(
    async_session: AsyncSession,
) -> None:
    """``reverse_lookup_batch`` returns the same owner ``reverse_lookup`` does.

    The batched sibling exists only to save round trips, so the two must never
    disagree. The estate is deliberately one where the rule has work to do: three
    covering sources at three different derivations, so a winner picked by anything
    other than the derivation rank would differ.

    spec: feature/BACKEND.md §Metrics Service §Time windows — reverse_lookup_batch is
    the "batched single-winner sibling" of reverse_lookup which "resolves the identical
    rule in two queries rather than one round trip per URN".
    """
    urn = _urn("agree")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        emitted = await _seed_source(async_session, name="spot-owning-emitted")
        pipeline = await _seed_source(async_session, name="spot-owning-pipeline")
        matched = await _seed_source(async_session, name="spot-owning-matched")
        source_ids = [emitted, pipeline, matched]
        # Seeded worst-first so a naive "first row wins" would pick the wrong one.
        await _seed_mapping(
            async_session, source_id=matched, dataset_urn=urn, derivation="matched"
        )
        await _seed_mapping(
            async_session, source_id=pipeline, dataset_urn=urn, derivation="pipeline_name"
        )
        await _seed_mapping(
            async_session, source_id=emitted, dataset_urn=urn, derivation="emitted"
        )

        single = await service.reverse_lookup(urn)
        batched = await service.reverse_lookup_batch([urn])

        assert single is not None, (
            "Backstop: reverse_lookup must find an owner for a mapped URN, or the "
            "agreement below is agreement on None."
        )
        assert single.id == emitted, (
            f"the 'emitted' source must win the derivation rank; got {single.id}. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert batched[urn] is not None
        assert batched[urn].id == single.id, (
            f"batch and single reverse lookup must agree on the owning source: "
            f"batch={batched[urn].id}, single={single.id}. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows — the batch "
            "'resolves the identical rule'."
        )
        assert batched[urn].model_dump() == single.model_dump(), (
            "the two lookups must return the same record, field for field."
        )
    finally:
        await _cleanup(async_session, source_ids, [urn])


@pytest.mark.asyncio
async def test_every_input_urn_is_a_key_and_unclaimed_urns_map_to_none(
    async_session: AsyncSession,
) -> None:
    """Each requested URN is present as a key; an unclaimed URN maps to ``None``.

    Both sides are seeded: one URN with a covering source and one with none, in the
    same call. Callers read every URN unconditionally, so a missing key would be a
    ``KeyError`` in the measurer rather than a "no owner" reading.

    spec: feature/BACKEND.md §Metrics Service §Time windows — "a dataset mapped to no
    source … → metric_conf.time_window_sec", which the measurer can only apply if the
    unmapped URN comes back as a key with no owner.
    """
    claimed = _urn("claimed")
    unclaimed = _urn("unclaimed")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        owner = await _seed_source(async_session, name="spot-owning-claimed")
        source_ids = [owner]
        await _seed_mapping(
            async_session, source_id=owner, dataset_urn=claimed, derivation="emitted"
        )

        result = await service.reverse_lookup_batch([claimed, unclaimed])

        assert set(result) == {claimed, unclaimed}, (
            f"every input URN must be a key of the result; got {sorted(result)}."
        )
        assert result[claimed] is not None and result[claimed].id == owner, (
            "the mapped URN must resolve to its covering source — the positive leg that "
            "makes the None below meaningful."
        )
        assert result[unclaimed] is None, (
            "a URN no source claims must map to None, not be omitted. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
    finally:
        await _cleanup(async_session, source_ids, [claimed, unclaimed])


@pytest.mark.asyncio
async def test_a_wrapper_outranking_its_parent_still_resolves_up_to_the_parent(
    async_session: AsyncSession,
) -> None:
    """A wrapper claiming a dataset at a *higher* derivation rank resolves to its parent.

    This is the case the derivation rank's wrapper term cannot express: that term only
    breaks ties at *equal* rank, and here the wrapper's ``emitted`` outranks the
    parent's ``matched`` outright. Only the explicit resolve-up step can produce the
    parent, so this test fails if that step is removed — where a same-rank fixture
    would still pass on the tie-break alone.

    The parent carries schedule_tier='daily' and the wrapper none, so the returned
    record's ``schedule_tier`` independently confirms which row came back.

    spec: feature/BACKEND.md §Metrics Service §Time windows — "if the sort winner is
    itself a wrapper it resolves up to its regular parent — a wrapper is never the
    owning source. The second step is not the tie-break restated: it also fires when a
    wrapper claims a dataset at a higher derivation rank than its parent, where the
    tie-break never runs."
    """
    urn = _urn("wrapper-outranks")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        parent = await _seed_source(
            async_session, schedule_tier="daily", name="spot-owning-parent"
        )
        wrapper = await _seed_source(
            async_session,
            schedule_tier=None,
            parent_source_id=parent,
            name="[CLI] spot-owning-wrapper",
        )
        source_ids = [wrapper, parent]
        await _seed_mapping(
            async_session, source_id=parent, dataset_urn=urn, derivation="matched"
        )
        await _seed_mapping(
            async_session, source_id=wrapper, dataset_urn=urn, derivation="emitted"
        )

        result = await service.reverse_lookup_batch([urn])
        owner = result[urn]

        assert owner is not None, "the mapped URN must resolve to an owning source."
        assert owner.id == parent, (
            f"the winning wrapper must resolve up to its regular parent; got {owner.id} "
            f"(wrapper={wrapper}, parent={parent}). "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert owner.parent_source_id is None, (
            f"the owning source is always a regular source; got parent_source_id="
            f"{owner.parent_source_id!r}. spec: feature/BACKEND_SCHEMA.md "
            "§ingestion_source — a wrapper is a row with parent_source_id IS NOT NULL."
        )
        assert owner.schedule_tier == "daily", (
            "the returned record must be the parent's row, whose tier is the one the "
            f"freshness window is derived from; got {owner.schedule_tier!r}."
        )

        # And the single-URN sibling agrees, so the measurer and the per-dataset event
        # timeline cannot disagree about who owns this dataset.
        single = await service.reverse_lookup(urn)
        assert single is not None and single.id == parent
    finally:
        await _cleanup(async_session, source_ids, [urn])


@pytest.mark.asyncio
async def test_a_regular_parent_beats_its_wrapper_at_equal_derivation_rank(
    async_session: AsyncSession,
) -> None:
    """At equal rank the regular parent wins the ranking outright.

    The complement of the test above, and the leg the wrapper term of the rank *does*
    decide. The wrapper's mapping is the more recent one, so a lookup that skipped the
    wrapper term and fell through to the ``last_seen_at`` tie-break would pick the
    wrapper.

    spec: feature/BACKEND.md §Metrics Service §Time windows — "at equal rank a regular
    parent beats its CLI wrapper; remaining ties go to the most recent last_seen_at."
    """
    urn = _urn("parent-wins-tie")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    now = datetime.now(tz=UTC)
    try:
        parent = await _seed_source(
            async_session, schedule_tier="hourly", name="spot-owning-tie-parent"
        )
        wrapper = await _seed_source(
            async_session,
            parent_source_id=parent,
            name="[CLI] spot-owning-tie-wrapper",
        )
        source_ids = [wrapper, parent]
        await _seed_mapping(
            async_session,
            source_id=parent,
            dataset_urn=urn,
            derivation="pipeline_name",
            last_seen_at=now - timedelta(hours=2),
        )
        await _seed_mapping(
            async_session,
            source_id=wrapper,
            dataset_urn=urn,
            derivation="pipeline_name",
            last_seen_at=now,  # newer — would win a pure last_seen_at tie-break
        )

        owner = (await service.reverse_lookup_batch([urn]))[urn]

        assert owner is not None
        assert owner.id == parent, (
            f"at equal derivation rank the regular parent must beat its wrapper even "
            f"when the wrapper's mapping is newer; got {owner.id}. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
    finally:
        await _cleanup(async_session, source_ids, [urn])


@pytest.mark.asyncio
async def test_the_most_recent_mapping_wins_a_tie_between_two_regular_sources(
    async_session: AsyncSession,
) -> None:
    """Two regular sources at equal derivation rank: the newer ``last_seen_at`` wins.

    This is the third and last term of the rank, and the only fixture shape that can
    reach it. Both covering sources are **regular** (``parent_source_id IS NULL``), so
    the wrapper term is equal for both, and both claim the dataset at
    ``pipeline_name``, so the derivation term is equal too — leaving ``last_seen_at`` as
    the sole discriminator. A fixture pitting a parent against its wrapper cannot
    substitute: the wrapper term decides there before ``last_seen_at`` is consulted.

    The older mapping is inserted first, so an implementation that dropped the
    ``last_seen_at`` term altogether would keep the input order and return the older
    source. Inverting the term returns the older source as well. Each source carries a
    distinct ``schedule_tier``, so the returned record independently names which row came
    back — and that is the field the freshness measurer derives its window from, which is
    what makes this term load-bearing rather than cosmetic.

    Both call sites of the shared rank are asserted, because they must not disagree about
    who owns a dataset: ``reverse_lookup_batch`` (what the measurer calls) and
    ``reverse_lookup`` (what the per-dataset ingestion views call).

    spec: feature/BACKEND.md §Metrics Service §Time windows — "derivation rank emitted >
    pipeline_name > matched; at equal rank a regular parent beats its CLI wrapper;
    remaining ties go to the most recent last_seen_at."
    """
    urn = _urn("recency-tie")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    now = datetime.now(tz=UTC)
    try:
        older = await _seed_source(
            async_session, schedule_tier="daily", name="spot-owning-recency-older"
        )
        newer = await _seed_source(
            async_session, schedule_tier="hourly", name="spot-owning-recency-newer"
        )
        source_ids = [older, newer]
        # Seeded older-first so a lookup that ignored last_seen_at would answer `older`.
        await _seed_mapping(
            async_session,
            source_id=older,
            dataset_urn=urn,
            derivation="pipeline_name",
            last_seen_at=now - timedelta(hours=3),
        )
        await _seed_mapping(
            async_session,
            source_id=newer,
            dataset_urn=urn,
            derivation="pipeline_name",
            last_seen_at=now,
        )

        batched = (await service.reverse_lookup_batch([urn]))[urn]
        single = await service.reverse_lookup(urn)

        assert batched is not None and single is not None, (
            "Backstop: both lookups must find an owner for a mapped URN, or the "
            "comparisons below are comparisons against None."
        )
        assert batched.id == newer, (
            f"the most recently seen mapping must win a tie between two regular sources; "
            f"got {batched.id} (older={older}, newer={newer}). "
            "spec: feature/BACKEND.md §Metrics Service §Time windows — 'remaining ties go "
            "to the most recent last_seen_at'."
        )
        assert batched.schedule_tier == "hourly", (
            "the returned record must be the newer source's row, whose tier is the one "
            f"the freshness window is derived from; got {batched.schedule_tier!r}."
        )
        assert single.id == newer, (
            f"the single-URN sibling must break the tie the same way; got {single.id}. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows — the batch "
            "'resolves the identical rule'."
        )
    finally:
        await _cleanup(async_session, source_ids, [urn])


@pytest.mark.asyncio
async def test_batch_resolves_each_urn_independently(
    async_session: AsyncSession,
) -> None:
    """Two URNs with different owners in one call each get their own winner.

    The batch groups one join's rows per URN, so a grouping bug would let the
    strongest row in the whole result set win for every URN. Injected here as an
    asymmetry: URN A's owner claims it at ``matched`` (the weakest rank) while URN B's
    claims it at ``emitted``, and A's owner must still be A's.

    spec: feature/BACKEND.md §Metrics Service §Time windows — the resolution is "a sort
    over *the dataset's* covering sources".
    """
    urn_a = _urn("independent-a")
    urn_b = _urn("independent-b")
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        owner_a = await _seed_source(async_session, name="spot-owning-indep-a")
        owner_b = await _seed_source(async_session, name="spot-owning-indep-b")
        source_ids = [owner_a, owner_b]
        await _seed_mapping(
            async_session, source_id=owner_a, dataset_urn=urn_a, derivation="matched"
        )
        await _seed_mapping(
            async_session, source_id=owner_b, dataset_urn=urn_b, derivation="emitted"
        )

        result = await service.reverse_lookup_batch([urn_a, urn_b])

        assert result[urn_a] is not None and result[urn_a].id == owner_a, (
            f"urn_a must resolve to its own covering source even though another URN in "
            f"the same call has a stronger derivation; got "
            f"{result[urn_a].id if result[urn_a] else None}."
        )
        assert result[urn_b] is not None and result[urn_b].id == owner_b
    finally:
        await _cleanup(async_session, source_ids, [urn_a, urn_b])


@pytest.mark.asyncio
async def test_batch_over_an_empty_urn_list_returns_an_empty_mapping(
    async_session: AsyncSession,
) -> None:
    """An empty URN list returns ``{}`` without querying.

    The measurer calls this unconditionally, including for a metric whose
    ``dataset_filter`` resolved to nothing.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    assert await service.reverse_lookup_batch([]) == {}


# ── latest_ingestion_complete_by_source ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_booked_only_on_a_wrapper_is_attributed_to_its_owner(
    async_session: AsyncSession,
) -> None:
    """A wrapper's INGESTION.COMPLETE is returned under the *parent's* id.

    DataHub books a managed source's executions on the auto-created wrapper, so the
    parent's own id carries no event row here at all — a lookup by the registered id
    alone would return nothing, which is exactly the freshness defect this covers. The
    wrapper is deliberately absent from the requested id list: the caller knows only
    the owning source.

    spec: feature/BACKEND.md §Metrics Service §Time windows — "The owning source's
    CLI-wrapper runs count as its own … a source's events are the union of its own and
    its wrappers'."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        parent = await _seed_source(async_session, name="spot-owning-run-parent")
        wrapper = await _seed_source(
            async_session, parent_source_id=parent, name="[CLI] spot-owning-run-wrapper"
        )
        source_ids = [wrapper, parent]
        wrapper_run = datetime.now(tz=UTC) - timedelta(minutes=30)
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            occurred_at=wrapper_run,
        )

        latest = await service.latest_ingestion_complete_by_source([parent])

        assert parent in latest, (
            "a run booked on the CLI wrapper must surface under the owning parent's id; "
            f"got keys {sorted(latest)}. spec: feature/BACKEND.md §Metrics Service "
            "§Time windows."
        )
        assert latest[parent] == wrapper_run
        assert wrapper not in latest, (
            "the result is keyed by the *given* source ids; the wrapper was not asked "
            f"about and must not get a key of its own. Got keys {sorted(latest)}."
        )
    finally:
        await _cleanup(async_session, source_ids, [])


@pytest.mark.asyncio
async def test_newest_run_across_the_source_and_its_wrapper_wins(
    async_session: AsyncSession,
) -> None:
    """With runs on both the parent and its wrapper, the newest of the union is returned.

    Both rows are seeded and the wrapper's is the newer, so a reader that took only
    the parent's own event would return the older timestamp. The parent's own row is
    what makes this a *union* test rather than a repeat of the wrapper-only case.

    spec: feature/BACKEND.md §Metrics Service §Time windows — "a source's events are
    the union of its own and its wrappers'."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        parent = await _seed_source(async_session, name="spot-owning-union-parent")
        wrapper = await _seed_source(
            async_session, parent_source_id=parent, name="[CLI] spot-owning-union-wrapper"
        )
        source_ids = [wrapper, parent]
        now = datetime.now(tz=UTC)
        older_on_parent = now - timedelta(hours=6)
        newer_on_wrapper = now - timedelta(minutes=10)
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=parent,
            event_type="INGESTION.COMPLETE",
            occurred_at=older_on_parent,
        )
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=wrapper,
            event_type="INGESTION.COMPLETE",
            occurred_at=newer_on_wrapper,
        )

        latest = await service.latest_ingestion_complete_by_source([parent])

        assert latest.get(parent) == newer_on_wrapper, (
            f"the newest run across the source and its wrappers must be returned; got "
            f"{latest.get(parent)!r} (parent's own run was {older_on_parent!r}). "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
    finally:
        await _cleanup(async_session, source_ids, [])


@pytest.mark.asyncio
async def test_only_completed_source_keyed_runs_are_read(
    async_session: AsyncSession,
) -> None:
    """INGESTION.FAIL and dataset-keyed rows are excluded; the COMPLETE row is returned.

    All three rows share one ``entity_id``, and the two that must be excluded are the
    *newer* ones, so any predicate that leaked would change the answer:

      - ``INGESTION.COMPLETE`` / ``entity_type='ingestion_source'`` — 6h ago, the answer
      - ``INGESTION.FAIL`` / ``entity_type='ingestion_source'`` — 10 min ago, excluded
        by the event_type predicate
      - ``INGESTION.COMPLETE`` / ``entity_type='dataset'`` — 1 min ago, excluded by the
        entity_type predicate

    spec: feature/BACKEND.md §Metrics Service §Breakdown format — a dataset fails when
    "the resolved ingestion evidence (tier 1 or tier 2) is older than the dataset's "
    "freshness window, or absent on both tiers",
    so a failed run must not refresh it.
    spec: feature/BACKEND.md §Metrics Service §Time windows — runs are booked with
    entity_type="ingestion_source".
    spec: TESTING.md §Assertion Discipline — "Filter/query/matching tests seed both
    sides."
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-owning-predicates")
        source_ids = [source]
        now = datetime.now(tz=UTC)
        completed = now - timedelta(hours=6)
        for entity_type, event_type, occurred_at in (
            ("ingestion_source", "INGESTION.COMPLETE", completed),
            ("ingestion_source", "INGESTION.FAIL", now - timedelta(minutes=10)),
            ("dataset", "INGESTION.COMPLETE", now - timedelta(minutes=1)),
        ):
            await _seed_event(
                async_session,
                entity_type=entity_type,
                entity_id=source,
                event_type=event_type,
                occurred_at=occurred_at,
            )

        latest = await service.latest_ingestion_complete_by_source([source])

        assert latest.get(source) == completed, (
            f"only the source-keyed INGESTION.COMPLETE may be read; got "
            f"{latest.get(source)!r}, expected {completed!r}. A newer INGESTION.FAIL or "
            "a dataset-keyed row must not refresh the reading. "
            "spec: feature/BACKEND.md §Metrics Service §Time windows."
        )
    finally:
        await _cleanup(async_session, source_ids, [])


@pytest.mark.asyncio
async def test_a_source_with_wrappers_but_no_runs_is_absent_from_the_result(
    async_session: AsyncSession,
) -> None:
    """A source that has never completed a run gets no key at all.

    Absence, not ``None``: the caller distinguishes "never ran" from "ran at time T"
    by key membership. The estate injects everything *except* an
    ``INGESTION.COMPLETE`` — a linked wrapper, and an ``INGESTION.FAIL`` on it — so the
    absence is a filtered result rather than an empty table. A second source in the
    same call *does* carry a run, proving the query returns keys at all.

    spec: feature/BACKEND.md §Metrics Service §Breakdown format — a dataset whose
    latest INGESTION.COMPLETE is "absent" is failed, which requires the helper to
    report absence rather than a timestamp.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        never_ran = await _seed_source(async_session, name="spot-owning-never-ran")
        wrapper = await _seed_source(
            async_session, parent_source_id=never_ran, name="[CLI] spot-owning-never-ran"
        )
        did_run = await _seed_source(async_session, name="spot-owning-did-run")
        source_ids = [wrapper, never_ran, did_run]
        run_at = datetime.now(tz=UTC) - timedelta(minutes=5)
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=wrapper,
            event_type="INGESTION.FAIL",
            occurred_at=datetime.now(tz=UTC) - timedelta(minutes=2),
        )
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=did_run,
            event_type="INGESTION.COMPLETE",
            occurred_at=run_at,
        )

        latest = await service.latest_ingestion_complete_by_source([never_ran, did_run])

        assert latest.get(did_run) == run_at, (
            "Backstop: the source that did complete a run must be keyed, or the absence "
            "below only proves the query returned nothing at all."
        )
        assert never_ran not in latest, (
            f"a source with no INGESTION.COMPLETE must be absent from the dict, not "
            f"mapped to None; got keys {sorted(latest)}."
        )
    finally:
        await _cleanup(async_session, source_ids, [])


@pytest.mark.asyncio
async def test_latest_run_over_an_empty_source_list_returns_an_empty_mapping(
    async_session: AsyncSession,
) -> None:
    """An empty source-id list returns ``{}``.

    The measurer reaches this whenever no dataset in scope has an owning source.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    assert await service.latest_ingestion_complete_by_source([]) == {}


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_uuid_simply_has_no_runs(
    async_session: AsyncSession,
) -> None:
    """A non-UUID source id yields no key rather than a database error.

    ``ingestion_source.id`` is a UUID column, so a non-UUID id can match no stored
    row; passing it to the query would raise instead. The second, well-formed id in
    the same call must still be answered — otherwise "no key" could just as well mean
    the whole call was abandoned.
    """
    service = IngestionService(datahub=None, db=async_session)  # type: ignore[arg-type]
    source_ids: list[str] = []
    try:
        source = await _seed_source(async_session, name="spot-owning-bad-id-neighbour")
        source_ids = [source]
        run_at = datetime.now(tz=UTC) - timedelta(minutes=5)
        await _seed_event(
            async_session,
            entity_type="ingestion_source",
            entity_id=source,
            event_type="INGESTION.COMPLETE",
            occurred_at=run_at,
        )

        latest = await service.latest_ingestion_complete_by_source(["not-a-uuid", source])

        assert latest.get(source) == run_at, (
            "the well-formed id in the same call must still be answered."
        )
        assert "not-a-uuid" not in latest
    finally:
        await _cleanup(async_session, source_ids, [])
