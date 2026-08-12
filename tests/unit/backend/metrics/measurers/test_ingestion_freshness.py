"""Unit tests for the ingestion-freshness measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types (the row for
  `ingestion-freshness`, quoted verbatim):
    - "`total` = count of datasets matched by `dataset_filter`; `ingested_in_time` =
      count whose latest `INGESTION.COMPLETE` falls within a **per-dataset freshness
      window**."
    - "a scheduled source (`ACTIVE_CUSTOM_MANAGED`/`DATAHUB_MANAGED`) → twice its
      `schedule_tier` period (`hourly`→7200s, `daily`→172800s, `weekly`→1209600s); a
      `PASSIVE` source → twice the DataHub-sync cadence (hourly → 7200s); a dataset
      mapped to no source (or a source with no derivable schedule) falls back to
      `metric_conf.time_window_sec`."
    - Registered under the `metric_type` value 'ingestion-freshness'; emits
      {'total': float, 'ingested_in_time': float}.

  Note on the boundary: "falls within" fixes the *window*, not the behaviour at the
  exact-cutoff instant — the spec says nothing about an event whose timestamp equals
  `now - window`. Where a test below turns on that instant it says so and names the
  strict comparison as the implementation's chosen tie-break, not a spec requirement.

  spec/feature/BACKEND.md §Metrics Service §Time windows:
    - "every `INGESTION.*` event is booked on a source (entity_type="ingestion_source",
      entity_id=source_id …) and never on the dataset, so the measurer resolves each
      dataset's **owning source** first. It then reads that source's feed in **two tiers
      of evidence**, per-dataset first and source-level as fallback. The same resolution
      supplies the window."
    - Tier 1 (preferred): "max(occurred_at) over the observation events the owning source
      booked **for that dataset**"; tier 2 (fallback): "max(occurred_at) over **every**
      INGESTION.COMPLETE booked on the owning source — no producer filter, **excluding
      dry runs**", applying only to "datasets with no observation evidence yet".
    - "Owning source is what IngestionService.reverse_lookup returns — or, over a
      whole dataset list at once, its batched single-winner sibling
      reverse_lookup_batch, which the measurer calls".

  Which tier each test exercises: every test that seeds only ``events=`` exercises
  **tier 2** (a source-level COMPLETE, the only evidence available), which is what the
  window, wrapper-union and owning-source tests are about — they are unchanged in
  substance by the two-tier split. The tests under §Evidence tiers seed
  ``observations=`` and exercise **tier 1** and the preference between them.
    - "if the sort winner is itself a wrapper it resolves up to its regular parent —
      a wrapper is never the owning source."
    - "The owning source's **CLI-wrapper runs count as its own** … a source's events
      are the union of its own and its wrappers'."
    - Window: ACTIVE_CUSTOM_MANAGED / DATAHUB_MANAGED with a schedule →
      SCHEDULE_TIER_SECONDS[schedule_tier] × 2; PASSIVE → PASSIVE_SYNC_PERIOD_SEC × 2;
      "a dataset mapped to no source, or a source with no derivable schedule →
      metric_conf.time_window_sec".
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (stale datasets).
    - Entry shape is {"urn": …, "detail": {…}} — no 'category' field.
    - detail for ingestion-freshness: {last_event_at, time_window_sec, window_source,
      evidence_tier} with window_source in {"managed:<tier>", "passive", "default"} and
      evidence_tier in {"observation", "source_level", null}.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers import ingestion_freshness  # noqa: F401 — triggers registration
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import IngestionSource
from tests.unit.conftest import route_db_execute

# A quoted UUID or uuid-shaped string as ``literal_binds`` renders it into an IN list.
_QUOTED_ID = re.compile(r"'([0-9a-fA-F-]{36})'")


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("ingestion-freshness")
    assert fn is not None, "ingestion-freshness measurer must be registered"
    return fn


def _datahub() -> MagicMock:
    """A spec'd DataHubClient stand-in.

    The measurer accepts one for signature uniformity and makes no DataHub call, so this
    exists only to fail loudly if it ever starts making one: ``spec=`` turns a call to a
    method DataHubClient does not have into an ``AttributeError`` instead of a silently
    successful auto-mock.
    """
    return MagicMock(spec=DataHubClient)


# ── Fake DB: the four queries the measurer's two service helpers issue ────────


@dataclass(frozen=True)
class _MappingRow:
    """One ``ingestion_source_dataset ⋈ ingestion_source`` row of the batch lookup.

    Mirrors the exact column tuple ``reverse_lookup_batch``'s first query selects.
    """

    dataset_urn: str
    derivation: str
    last_seen_at: datetime
    id: uuid.UUID  # the covering source's id
    parent_source_id: uuid.UUID | None  # non-NULL ⇒ that source is a CLI wrapper


def _source(
    *,
    mode: str,
    schedule_tier: str | None = None,
    parent_source_id: uuid.UUID | None = None,
    name: str = "unit-source",
    source_id: uuid.UUID | None = None,
) -> IngestionSource:
    """Build a detached ``ingestion_source`` ORM row (no session, no DB)."""
    now = datetime.now(tz=UTC)
    return IngestionSource(
        id=source_id or uuid.uuid4(),
        mode=mode,
        name=name,
        platform="postgres",
        recipe={"source": {"type": "postgres", "config": {}}},
        schedule=None,
        schedule_tier=schedule_tier,
        datahub_source_urn=None,
        parent_source_id=parent_source_id,
        status="OK",
        created_at=now,
        updated_at=now,
    )


def _fake_measurer_db(
    *,
    mappings: list[_MappingRow] = (),
    sources: list[IngestionSource] = (),
    wrappers: list[tuple[uuid.UUID, uuid.UUID]] = (),
    events: list[tuple[str, datetime]] = (),
    observations: list[tuple[str, str, datetime]] = (),
) -> AsyncMock:
    """Query-routing fake session for the measurer's DB reads.

    The measurer itself issues no SQL: it calls three ``IngestionService`` helpers,
    which between them issue exactly these queries. Each is routed by the SQL
    it compiles to (never by call position), so an added, reordered or
    short-circuited query cannot silently shift a result:

    ==  ==============================================================  ==============
    #   Query (issuer)                                                  Stub argument
    ==  ==============================================================  ==============
    1   ``ingestion_source_dataset ⋈ ingestion_source`` covering-source
        rows for the requested URNs (``reverse_lookup_batch`` step 1)    ``mappings``
    2   ``SELECT ingestion_source WHERE id IN (…)`` loading the ranked
        winners and their parents (``reverse_lookup_batch`` step 2)      ``sources``
    3   ``SELECT id, parent_source_id WHERE parent_source_id IN (…)``
        resolving each owner's CLI wrappers (``_owner_by_entity_id``,
        issued once per evidence helper)                                 ``wrappers``
    4   ``SELECT entity_id, detail->>'dataset_urn', max(occurred_at)
        … GROUP BY entity_id, detail->>'dataset_urn'`` — **tier 1**
        (``latest_ingestion_observed_by_dataset``)                       ``observations``
    5   ``SELECT entity_id, max(occurred_at) … GROUP BY entity_id`` —
        **tier 2** (``latest_ingestion_complete_by_source``)             ``events``
    ==  ==============================================================  ==============

    Queries 4 and 5 both aggregate ``max(occurred_at)`` over ``events``; they are told
    apart by the ``detail->>'dataset_urn'`` grouping key, which only tier 1 carries.

    **Modelled**, because the behaviour under test depends on it: the ``IN`` list of
    queries 3, 4 and 5. Each returns only rows whose key appears in the id list that
    query actually asked for, read out of the compiled SQL. Two reasons, and the second
    is the important one:

    - Queries 4 and 5 must filter or the helpers' ``owner_by_entity[entity_id]`` lookup
      raises ``KeyError`` on a row production could never return.
    - Query 3 must filter or a mutation to the *owning-source resolution* surfaces as a
      ``KeyError`` deep inside the evidence helper instead of as the
      window assertion the test names as its discriminator. A fake that hands back every
      seeded wrapper regardless of who was asked about hides which step broke.

    **Not modelled**, and covered against real PostgreSQL instead — a fake cannot prove a
    ``WHERE`` clause it reimplements:

    - Query 5's ``event_type`` **and** ``entity_type`` predicates —
      ``tests/integration/spot/test_ingestion_owning_source.py``
      ``::test_only_completed_source_keyed_runs_are_read``: one ``entity_id`` carrying a
      ``COMPLETE``/``ingestion_source`` row plus *newer* ``INGESTION.FAIL`` and
      ``entity_type='dataset'`` decoys, so either predicate leaking changes the answer.
      ``tests/integration/spot/test_metrics.py`` covers the ``entity_type`` half again
      through a whole metric run (dataset-keyed decoys either side of the window).
    - Query 5's **dry-run exclusion**, and query 4's ``detail->>'source'`` producer
      filter — ``tests/integration/spot/test_ingestion_freshness_evidence.py``, against
      real JSONB. Here, ``observations`` and ``events`` are separate stub arguments, so a
      test seeds a row into whichever tier it means to exercise; the tier *preference* is
      what these tests judge.
    - Query 1's ``dataset_urn IN`` predicate —
      ``tests/integration/spot/test_ingestion_owning_source.py``
      ``::test_every_input_urn_is_a_key_and_unclaimed_urns_map_to_none`` (a claimed and an
      unclaimed URN in one call: the unclaimed one must come back as a key with no owner)
      and ``::test_batch_resolves_each_urn_independently`` (two URNs with different owners
      in one call, so a predicate that ignored the URN would give both the same winner).
    """
    mapping_result = MagicMock()
    mapping_result.all.return_value = list(mappings)

    source_result = MagicMock()
    source_result.scalars.return_value.all.return_value = list(sources)

    # The ids each of queries 3–5 asked about, captured from the compiled SQL when the
    # route matches and read back when the result's `.all()` is called (which happens
    # after, inside the code under test).
    requested: dict[str, set[str]] = {
        "parents": set(),
        "entities": set(),
        "obs_entities": set(),
    }

    def _capture(sql: str, marker: str, slot: str) -> bool:
        if marker not in sql:
            return False
        requested[slot] = set(_QUOTED_ID.findall(sql.split(marker, 1)[1]))
        return True

    wrapper_result = MagicMock()
    wrapper_result.all.side_effect = lambda: [
        (child, parent) for child, parent in wrappers if str(parent) in requested["parents"]
    ]

    observation_result = MagicMock()
    observation_result.all.side_effect = lambda: [
        (entity_id, dataset_urn, occurred_at)
        for entity_id, dataset_urn, occurred_at in observations
        if entity_id in requested["obs_entities"]
    ]

    event_result = MagicMock()
    event_result.all.side_effect = lambda: [
        (entity_id, occurred_at)
        for entity_id, occurred_at in events
        if entity_id in requested["entities"]
    ]

    db = AsyncMock(spec=AsyncSession)
    route_db_execute(
        db,
        [
            ("ingestion_source_dataset", mapping_result),
            (
                lambda sql: _capture(sql, "parent_source_id in", "parents"),
                wrapper_result,
            ),
            (lambda sql: "ingestion_source.id in" in sql, source_result),
            (
                lambda sql: "max(dataspoke.events.occurred_at)" in sql
                and "'dataset_urn'" in sql
                and _capture(sql, "entity_id in", "obs_entities"),
                observation_result,
            ),
            (
                lambda sql: "max(dataspoke.events.occurred_at)" in sql
                and "'dataset_urn'" not in sql
                and _capture(sql, "entity_id in", "entities"),
                event_result,
            ),
        ],
    )
    return db


def _mapped(
    urn: str,
    source: IngestionSource,
    *,
    derivation: str = "emitted",
    last_seen_at: datetime | None = None,
) -> _MappingRow:
    """A covering-source row linking *urn* to *source*."""
    return _MappingRow(
        dataset_urn=urn,
        derivation=derivation,
        last_seen_at=last_seen_at or datetime.now(tz=UTC),
        id=source.id,
        parent_source_id=source.parent_source_id,
    )


def _freeze_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> None:
    """Pin the measurer's ``datetime.now`` so cutoff boundaries are exact."""
    import src.backend.metrics.measurers.ingestion_freshness as _mod

    class _FixedDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return fixed_now

    monkeypatch.setattr(_mod, "datetime", _FixedDatetime)


# ── Registration ──────────────────────────────────────────────────────────────


def test_registered_under_correct_key() -> None:
    """Measurer is registered under 'ingestion-freshness'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_type value
          is 'ingestion-freshness'.
    """
    fn = _get_measurer()
    assert fn is not None


# ── Empty datasets list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zeros() -> None:
    """measure([]) returns total=0.0, ingested_in_time=0.0 with empty datasets list.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — total = count of
          datasets matched by dataset_filter.
    """
    measure = _get_measurer()

    values, breakdown = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(),
    )

    assert values == {"total": 0.0, "ingested_in_time": 0.0}
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


# ── Fresh / stale against the metric_conf fallback window ─────────────────────


@pytest.mark.asyncio
async def test_fresh_dataset_not_in_breakdown() -> None:
    """A dataset whose owning source ran inside the window is NOT in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] carries only failed entries (stale datasets).
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
    # No schedule_tier ⇒ the metric_conf fallback window applies.
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    recent = datetime.now(tz=UTC) - timedelta(hours=1)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), recent)],
        ),
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 1.0
    assert breakdown["datasets"] == [], (
        "Fresh dataset must NOT appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert breakdown["dataset_count"] == 1


@pytest.mark.asyncio
async def test_dataset_with_no_event_in_breakdown_with_none_last_event() -> None:
    """A mapped dataset whose source has never completed a run is stale, last_event_at=None.

    The source is present in the mapping and loaded as an entity — only its
    INGESTION.COMPLETE event is missing, so the source is **absent** from the
    helper's result dict rather than mapped to None.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when its latest INGESTION.COMPLETE "is older than the dataset's
          freshness window … or absent"; detail carries last_event_at.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.editions,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(mappings=[_mapped(urn, src)], sources=[src], events=[]),
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert entry["detail"]["last_event_at"] is None


@pytest.mark.asyncio
async def test_dataset_with_stale_event_in_breakdown() -> None:
    """A source run older than the cutoff puts its dataset in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          ingestion-freshness: "the resolved ingestion evidence (tier 1 or tier 2)
          is older than the dataset's freshness window, or absent on both tiers".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders.fulfillment,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90000)  # older than 86400s

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert entry["detail"]["last_event_at"] == stale_ts.isoformat()


@pytest.mark.asyncio
async def test_event_well_inside_window_is_ingested_in_time() -> None:
    """Event well inside the window (half the window ago) counts as ingested_in_time.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``ingested_in_time`` =
          count whose latest ``INGESTION.COMPLETE`` falls within a **per-dataset freshness
          window**". Half a window ago is unambiguously within it.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.test,DEV)"
    time_window_sec = 3600
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    inside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec // 2)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), inside_window)],
        ),
    )

    assert values["ingested_in_time"] == 1.0
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_event_well_outside_window_is_stale() -> None:
    """Event well outside the window (2x window ago) is stale.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — only a latest
          ``INGESTION.COMPLETE`` that "falls within a **per-dataset freshness window**"
          counts; twice the window ago falls outside it.
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is failed
          when "the resolved ingestion evidence (tier 1 or tier 2) is older than the
          dataset's freshness window, or absent on both tiers".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary2.test,DEV)"
    time_window_sec = 3600
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    outside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec * 2)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), outside_window)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1


# ── Breakdown entry shape ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_entries_have_no_category_field() -> None:
    """Breakdown entries must not carry a 'category' field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] entries are {"urn": "...", "detail": {...}} only.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocategory.test,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)

    _values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(mappings=[_mapped(urn, src)], sources=[src], events=[]),
    )

    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert "category" not in entry, (
        "Breakdown entry must not carry 'category'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert set(entry) == {"urn", "detail"}


@pytest.mark.asyncio
async def test_stale_breakdown_detail_includes_the_window_and_the_evidence_tier() -> None:
    """Stale detail carries last_event_at, time_window_sec, window_source, evidence_tier.

    ``evidence_tier`` is ``None`` here because neither tier produced evidence: the dataset
    is mapped to no source at all. The two tiers make different claims, so a stale verdict
    without it is not diagnosable.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          "ingestion-freshness and validation-score report the applied window via
          time_window_sec (the resolved per-dataset value) and window_source …
          alongside last_event_at (freshness)"; "``ingestion-freshness`` additionally
          names **which tier supplied ``last_event_at``** in ``evidence_tier``
          (``"observation"`` for tier 1, ``"source_level"`` for tier 2, ``null`` when
          neither tier produced evidence)".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detail-check,DEV)"

    # No mapping at all → the 'default' window branch, no event → stale.
    _values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(),
    )

    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert set(detail) == {
        "last_event_at",
        "time_window_sec",
        "window_source",
        "evidence_tier",
    }, (
        "Stale detail keys must be exactly {last_event_at, time_window_sec, "
        "window_source, evidence_tier}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert isinstance(detail["time_window_sec"], int)
    assert isinstance(detail["window_source"], str)
    assert detail["evidence_tier"] is None, (
        f"with no owning source neither tier produced evidence, so evidence_tier must be "
        f"null; got {detail['evidence_tier']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


# ── Mixed dataset set: each dataset reads its OWN owning source ───────────────


@pytest.mark.asyncio
async def test_mixed_fresh_and_stale_counts_correctly() -> None:
    """Three datasets on three sources: two fresh, one stale.

    One source per dataset, each carrying its own INGESTION.COMPLETE — the shape
    the sweep produces. Sharing one source across all three would give all three
    the same freshness and the fresh/stale contrast would collapse.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "the measurer
          resolves each dataset's owning source first. It then reads that source's
          feed in two tiers of evidence, per-dataset first and source-level as
          fallback."
    """
    measure = _get_measurer()
    urn_fresh1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn_fresh2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,DEV)"

    now = datetime.now(tz=UTC)
    time_window_sec = 86400
    src_fresh1 = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="a")
    src_fresh2 = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="b")
    src_stale = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="c")

    values, breakdown = await measure(
        datasets=[urn_fresh1, urn_fresh2, urn_stale],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[
                _mapped(urn_fresh1, src_fresh1),
                _mapped(urn_fresh2, src_fresh2),
                _mapped(urn_stale, src_stale),
            ],
            sources=[src_fresh1, src_fresh2, src_stale],
            events=[
                (str(src_fresh1.id), now - timedelta(hours=1)),
                (str(src_fresh2.id), now - timedelta(hours=2)),
                (str(src_stale.id), now - timedelta(seconds=time_window_sec + 3600)),
            ],
        ),
    )

    assert values["total"] == 3.0
    assert values["ingested_in_time"] == 2.0
    assert breakdown["dataset_count"] == 3
    assert [e["urn"] for e in breakdown["datasets"]] == [urn_stale]


@pytest.mark.asyncio
async def test_two_datasets_sharing_a_source_share_its_tier_2_evidence() -> None:
    """On **tier 2**, two datasets covered by one source get the same verdict.

    Neither dataset has an observation of its own, so both fall back to the source-level
    maximum and cannot split — which is precisely the approximation tier 2 admits ("an
    event booked on a source genuinely cannot say which dataset it touched"). The
    contrast is ``test_a_dataset_reads_its_own_observation_and_not_a_siblings``: the same
    two-datasets-one-source shape *does* split once each carries tier-1 evidence.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 2 is
          "``max(occurred_at)`` over **every** ``INGESTION.COMPLETE`` booked on the owning
          source", applying to "datasets with no observation evidence yet".
    """
    measure = _get_measurer()
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.shared.a,DEV)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.shared.b,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="shared")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90000)

    values, breakdown = await measure(
        datasets=[urn_a, urn_b],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn_a, src), _mapped(urn_b, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert {e["urn"] for e in breakdown["datasets"]} == {urn_a, urn_b}
    for entry in breakdown["datasets"]:
        assert entry["detail"]["last_event_at"] == stale_ts.isoformat()


# ── Deterministic clock boundary (strict >) ──────────────────────────────────


@pytest.mark.asyncio
async def test_event_exactly_at_cutoff_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event at exactly now - time_window_sec is STALE.

    **This assertion has no spec basis.** The spec says only that ``ingested_in_time``
    counts a latest ``INGESTION.COMPLETE`` that "falls within a **per-dataset freshness
    window**" (spec/USE_CASE_en.md §UC5 §Built-in active metric types); it does not fix
    the behaviour at the exact-cutoff instant, and "falls within" arguably reads inclusive
    there. Treating the boundary instant as stale is the **implementation's chosen
    tie-break** (a strict ``>`` against the cutoff), pinned here so a silent flip is
    visible in review rather than shipped — not because the spec requires it.

    The spec-grounded side of the boundary is
    ``test_event_one_second_inside_window_is_fresh``.
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.exact,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    exact_cutoff = fixed_now - timedelta(seconds=time_window_sec)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), exact_cutoff)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn


@pytest.mark.asyncio
async def test_event_one_second_inside_window_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event at now - time_window_sec + 1s is FRESH.

    One second inside the window is inside it on any reading, so this is the boundary side
    the spec does settle.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``ingested_in_time`` =
          count whose latest ``INGESTION.COMPLETE`` falls within a **per-dataset freshness
          window**".
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.inside,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    one_sec_inside = fixed_now - timedelta(seconds=time_window_sec - 1)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), one_sec_inside)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "A run one second inside the window 'falls within' it and must be FRESH. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert breakdown["datasets"] == []


# ── Per-dataset window: managed modes with a schedule tier ───────────────────


@pytest.mark.asyncio
async def test_active_custom_daily_window_is_twice_daily_period() -> None:
    """ACTIVE_CUSTOM_MANAGED schedule_tier='daily' uses a 172800s window (2 × 86400).

    A source run 130000s ago (< 172800) is ingested_in_time=1 even though the
    metric_conf fallback (86400s) would have called it stale.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "a scheduled source
          (``ACTIVE_CUSTOM_MANAGED``/``DATAHUB_MANAGED``) → twice its ``schedule_tier``
          period (… ``daily``→172800s …)".
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows —
          "SCHEDULE_TIER_SECONDS[schedule_tier] × 2".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.daily,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="daily")
    recent = datetime.now(tz=UTC) - timedelta(seconds=130000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), recent)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "managed daily: a run 130000s ago must be in-time (window=172800s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — daily→172800s."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_active_custom_daily_stale_outside_window() -> None:
    """ACTIVE_CUSTOM_MANAGED daily: run 200000s ago is stale; detail names the window.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          active-custom daily window = 172800s; event outside → stale.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — detail carries
          time_window_sec and window_source ("managed:<tier>").
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.daily-stale,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="daily")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=200000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT override the tier
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn

    detail = entry["detail"]
    # Spec literal: active-custom daily → 86400 × 2 = 172800s.
    assert detail["time_window_sec"] == 172800, (
        "detail.time_window_sec must be 172800 (managed daily = 86400 × 2). "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert detail["window_source"] == "managed:daily", (
        "detail.window_source must be 'managed:daily' for ACTIVE_CUSTOM_MANAGED daily. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


@pytest.mark.asyncio
async def test_datahub_managed_daily_window_matches_active_custom() -> None:
    """DATAHUB_MANAGED daily derives the same 172800s window as ACTIVE_CUSTOM_MANAGED.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "ACTIVE_CUSTOM_MANAGED
          / DATAHUB_MANAGED with a schedule → SCHEDULE_TIER_SECONDS[schedule_tier] × 2".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.managed-daily,DEV)"
    src = _source(mode="DATAHUB_MANAGED", schedule_tier="daily")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=200000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    assert detail["time_window_sec"] == 172800
    assert detail["window_source"] == "managed:daily"


@pytest.mark.asyncio
async def test_active_custom_hourly_window_is_7200s() -> None:
    """schedule_tier='hourly' uses a 7200s window (2 × 3600).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "twice its
          ``schedule_tier`` period (``hourly``→7200s …)".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.hourly,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="hourly")
    # 8000s ago — past the 7200s hourly window, inside the 86400s fallback.
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=8000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0, (
        "managed hourly: a run 8000s ago must be stale (window=7200s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — hourly→7200s."
    )
    # Spec literal: hourly → 3600 × 2 = 7200s.
    assert breakdown["datasets"][0]["detail"]["time_window_sec"] == 7200
    assert breakdown["datasets"][0]["detail"]["window_source"] == "managed:hourly"


@pytest.mark.asyncio
async def test_active_custom_weekly_window_is_1209600s() -> None:
    """schedule_tier='weekly' uses a 1209600s window (2 × 604800).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "twice its
          ``schedule_tier`` period (… ``weekly``→1209600s)".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.weekly,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="weekly")
    # 600000s ago — within the 1209600s weekly window, past the 3600s fallback.
    fresh_ts = datetime.now(tz=UTC) - timedelta(seconds=600000)

    values, _breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # fallback — must NOT be used
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), fresh_ts)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "managed weekly: a run 600000s ago must be in-time (window=1209600s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — weekly→1209600s."
    )


# ── Per-dataset window: passive ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passive_window_is_7200s() -> None:
    """A PASSIVE source uses a 7200s window (2 × the hourly sync cadence).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "a ``PASSIVE``
          source → twice the DataHub-sync cadence (hourly → 7200s)".
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "PASSIVE (no
          schedule) → PASSIVE_SYNC_PERIOD_SEC × 2"; window_source='passive'.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive,DEV)"
    src = _source(mode="PASSIVE", schedule_tier=None)
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=8000)  # past 7200s

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — must NOT be used for passive
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src, derivation="matched")],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    # Spec literal: passive → twice the hourly sync cadence = 3600 × 2 = 7200s.
    assert detail["time_window_sec"] == 7200, (
        "Passive dataset must use window 7200s (2 × hourly sync cadence). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert detail["window_source"] == "passive", (
        "Passive dataset detail.window_source must be 'passive'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


@pytest.mark.asyncio
async def test_passive_in_window_fresh() -> None:
    """A PASSIVE source that ran 3600s ago (< 7200s) is in-time.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "a ``PASSIVE``
          source → twice the DataHub-sync cadence (hourly → 7200s)".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive-fresh,DEV)"
    src = _source(mode="PASSIVE", schedule_tier=None)
    fresh_ts = datetime.now(tz=UTC) - timedelta(seconds=3600)

    values, _breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 600},  # fallback — must NOT override passive
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src, derivation="matched")],
            sources=[src],
            events=[(str(src.id), fresh_ts)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "Passive dataset with event 3600s ago must be in-time (passive window=7200s). "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


# ── Per-dataset window: the metric_conf fallback ─────────────────────────────


@pytest.mark.asyncio
async def test_unmapped_dataset_falls_back_to_metric_conf_time_window() -> None:
    """A dataset covered by no source uses metric_conf.time_window_sec, window_source='default'.

    With no owning source there is no source-keyed run to read either, so the
    dataset is stale with last_event_at=None.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "a dataset mapped
          to no source, or a source with no derivable schedule →
          metric_conf.time_window_sec".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.noconfig,DEV)"

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},
        datahub=_datahub(),
        db=_fake_measurer_db(),  # no mapping rows at all
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    assert detail["time_window_sec"] == 3600
    assert detail["window_source"] == "default", (
        "A dataset mapped to no source must produce window_source='default'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert detail["last_event_at"] is None


@pytest.mark.asyncio
async def test_managed_null_schedule_tier_falls_back_to_metric_conf() -> None:
    """A managed source with no schedule_tier falls back to metric_conf.time_window_sec.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "a source with no
          derivable schedule → metric_conf.time_window_sec".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.activenull,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None)
    # 50000s ago: inside the 86400s fallback, outside every tier window below it.
    fresh_ts = datetime.now(tz=UTC) - timedelta(seconds=50000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), fresh_ts)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "managed null tier with fallback 86400s, event 50000s ago → in-time. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_managed_unrecognised_schedule_tier_falls_back_to_metric_conf() -> None:
    """A managed source whose schedule_tier is not a known tier falls back to the default.

    'monthly' is not one of the tiers ``SCHEDULE_TIER_SECONDS`` defines, so no
    per-dataset window can be derived from it.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "a source with no
          derivable schedule → metric_conf.time_window_sec". Tier→seconds "live in
          src/shared/schedule.py".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unknown-tier,DEV)"
    src = _source(mode="DATAHUB_MANAGED", schedule_tier="monthly")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=5000)

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},  # 5000s > 3600s → stale
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    assert detail["time_window_sec"] == 3600
    assert detail["window_source"] == "default", (
        "An unrecognised schedule_tier is not a derivable schedule, so the window "
        "source must be 'default'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )


# ── CLI-wrapper runs count as the owning source's own ────────────────────────


@pytest.mark.asyncio
async def test_run_booked_on_a_wrapper_only_counts_for_the_owning_parent() -> None:
    """A run booked on the CLI wrapper makes the parent's dataset fresh.

    Injection is asymmetric on purpose: the parent source id carries **no** event
    row at all, only the wrapper's does. If the measurer read the registered source
    id alone the dataset would be stale, which is exactly the defect this covers.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "The owning
          source's **CLI-wrapper runs count as its own** — DataHub books a managed
          source's executions on an auto-created wrapper rather than on the
          registered source, so a source's events are the union of its own and its
          wrappers'."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.wrapper-only,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", schedule_tier="daily", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        schedule_tier=None,
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    wrapper_run = datetime.now(tz=UTC) - timedelta(seconds=130000)  # inside 172800s

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 60},  # fallback — must NOT be used
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, parent)],
            sources=[parent],
            wrappers=[(wrapper.id, parent.id)],
            events=[(str(wrapper.id), wrapper_run)],  # only the wrapper ran
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "A run booked on the CLI wrapper must count as the owning parent's own run. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_newest_run_across_parent_and_wrapper_wins() -> None:
    """With runs on both the parent and its wrapper, the newest of the union is used.

    Both rows are seeded and the wrapper's is the newer one, so a reader that took
    only the parent's own event would report the older timestamp and call the
    dataset stale.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "a source's events
          are the union of its own and its wrappers'".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.parent-and-wrapper,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", schedule_tier="hourly", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        schedule_tier=None,
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    now = datetime.now(tz=UTC)
    parent_run = now - timedelta(seconds=8000)  # outside the 7200s hourly window
    wrapper_run = now - timedelta(seconds=600)  # inside it, and newer

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 60},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, parent)],
            sources=[parent],
            wrappers=[(wrapper.id, parent.id)],
            events=[(str(parent.id), parent_run), (str(wrapper.id), wrapper_run)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "The newest run across the source and its wrappers must decide freshness; the "
        "wrapper's 600s-old run is inside the 7200s hourly window. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_owning_source_is_the_regular_parent_of_a_claiming_wrapper() -> None:
    """When a wrapper is the ranked winner, the window comes from its regular parent.

    The wrapper claims the dataset at derivation 'emitted' while its parent only
    'matched' it, so the wrapper outranks the parent and the tie-break never runs —
    only the explicit resolve-up step can produce the parent here. The parent
    carries schedule_tier='daily' and the wrapper carries none, so the resolved
    window ('managed:daily') is what proves which row was used.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "if the sort
          winner is itself a wrapper it resolves up to its regular parent — a wrapper
          is never the owning source. The second step … also fires when a wrapper
          claims a dataset at a *higher* derivation rank than its parent, where the
          tie-break never runs."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.wrapper-claims,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", schedule_tier="daily", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        schedule_tier=None,
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=200000)  # outside 172800s

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},  # fallback — the wrapper has no tier
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[
                _mapped(urn, wrapper, derivation="emitted"),
                _mapped(urn, parent, derivation="matched"),
            ],
            sources=[parent, wrapper],
            wrappers=[(wrapper.id, parent.id)],
            events=[(str(parent.id), stale_ts)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    detail = breakdown["datasets"][0]["detail"]
    assert detail["window_source"] == "managed:daily", (
        "A winning wrapper must resolve up to its regular parent, so the window comes "
        f"from the parent's daily tier; got {detail['window_source']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert detail["time_window_sec"] == 172800
    assert detail["last_event_at"] == stale_ts.isoformat()


# ── Evidence tiers: per-dataset observation preferred, source-level as fallback ──
#
# spec/feature/BACKEND.md §Metrics Service §Time windows — the two-tier table, and
# "Tier 1 exists because a run-level COMPLETE is a claim about a *run*, not about a
# dataset"; "Tier 2 … applies only where nothing better exists".


@pytest.mark.asyncio
async def test_a_dataset_with_its_own_observation_reads_that_instant_not_the_source_max() -> None:
    """Tier 1 wins over tier 2 even when the source's newest run is newer.

    The discriminating shape: the dataset's own observation is STALE while its owning
    source's newest ``COMPLETE`` is FRESH. A measurer that still preferred the
    source-level maximum would call the dataset ingested-in-time, which is exactly the
    claim the two-tier rule retires — a run-level COMPLETE says a *run* finished, not that
    this dataset was written.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 1 is
          "(preferred)"; tier 2 "applies only where nothing better exists".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.own,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="tier1-own")
    now = datetime.now(tz=UTC)
    own_observation = now - timedelta(seconds=90_000)  # stale against the 86400s window
    source_run = now - timedelta(hours=1)  # fresh — must NOT be what answers

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            observations=[(str(src.id), urn, own_observation)],
            events=[(str(src.id), source_run)],
        ),
    )

    assert values["ingested_in_time"] == 0.0, (
        "the dataset's own observation is the evidence, and it is outside the window, so "
        "the fresh source-level run must not make it in-time. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 1 preferred."
    )
    detail = breakdown["datasets"][0]["detail"]
    assert detail["last_event_at"] == own_observation.isoformat(), (
        f"last_event_at must be the dataset's own observation instant; got "
        f"{detail['last_event_at']!r}, expected {own_observation.isoformat()!r}."
    )
    assert detail["evidence_tier"] == "observation", (
        f"evidence_tier must name tier 1 as the answer; got {detail['evidence_tier']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


@pytest.mark.asyncio
async def test_a_dataset_reads_its_own_observation_and_not_a_siblings() -> None:
    """Two datasets on one source read their own observations, not each other's.

    Both are covered by the same source, so under the old source-grained rule they were
    necessarily the same verdict. With per-dataset evidence they split: the sibling's
    observation is fresh and this dataset's is stale, so a lookup keyed on the source
    alone would report both fresh.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 1 is
          "max(occurred_at) over the observation events the owning source booked **for
          that dataset**".
    """
    measure = _get_measurer()
    stale_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.stale,DEV)"
    fresh_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.fresh,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="tier1-shared")
    now = datetime.now(tz=UTC)

    values, breakdown = await measure(
        datasets=[stale_urn, fresh_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(stale_urn, src), _mapped(fresh_urn, src)],
            sources=[src],
            observations=[
                (str(src.id), stale_urn, now - timedelta(seconds=90_000)),
                (str(src.id), fresh_urn, now - timedelta(hours=1)),
            ],
        ),
    )

    assert values["total"] == 2.0
    assert values["ingested_in_time"] == 1.0, (
        "two datasets on one source must split on their own observations. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 1."
    )
    assert [e["urn"] for e in breakdown["datasets"]] == [stale_urn], (
        f"only the dataset whose own observation is outside the window is stale; got "
        f"{[e['urn'] for e in breakdown['datasets']]}."
    )


@pytest.mark.asyncio
async def test_a_dataset_with_no_observation_falls_back_to_the_source_level_maximum() -> None:
    """Tier 2 answers for a dataset that has no observation of its own.

    Both sides are seeded in one call: the sibling carries an observation and this dataset
    does not, so a measurer that had dropped the fallback entirely would report this one
    stale with ``last_event_at=None``.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 2 applies to
          "datasets with no observation evidence yet"; it is "source-grained, not
          producer-filtered: any COMPLETE on the owning source qualifies, so a sibling
          dataset's observation can stand in for a dataset that has none of its own".
    """
    measure = _get_measurer()
    observed_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier2.observed,DEV)"
    bare_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier2.bare,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="tier2-fallback")
    now = datetime.now(tz=UTC)
    source_run = now - timedelta(hours=2)

    values, breakdown = await measure(
        datasets=[observed_urn, bare_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(observed_urn, src), _mapped(bare_urn, src)],
            sources=[src],
            observations=[(str(src.id), observed_urn, now - timedelta(hours=1))],
            events=[(str(src.id), source_run)],
        ),
    )

    assert values["ingested_in_time"] == 2.0, (
        "the dataset with no observation of its own must still read the source-level "
        "maximum and count as in-time. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — tier 2."
    )
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_the_fallback_names_the_source_level_tier_in_the_breakdown() -> None:
    """A stale dataset answered by tier 2 reports ``evidence_tier='source_level'``.

    The label names the *grain*, not a producer: tier 2 admits observations too, so a
    label naming a producer would be wrong wherever a sibling's observation supplied it.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — "``"source_level"``
          for tier 2 … Tier 2's label names the *grain*, not a producer: it is the newest
          ``COMPLETE`` on the owning source whatever wrote it."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier2.stale,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier=None, name="tier2-stale")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90_000)

    _values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert len(breakdown["datasets"]) == 1
    detail = breakdown["datasets"][0]["detail"]
    assert detail["last_event_at"] == stale_ts.isoformat(), (
        "backstop: tier 2 must actually have supplied the instant, or the label below is "
        "attached to nothing."
    )
    assert detail["evidence_tier"] == "source_level", (
        f"evidence_tier must name tier 2 as the answer; got {detail['evidence_tier']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )


@pytest.mark.asyncio
async def test_an_observation_on_a_wrapper_counts_for_the_owning_parent() -> None:
    """A tier-1 observation booked on a CLI wrapper answers for the owning parent.

    The parent carries no observation of its own, only the wrapper does, so a lookup by
    the registered source id alone would fall through to tier 2 (or to nothing). The
    wrapper union is the same rule tier 2 already applies, and it has to hold on tier 1
    too, or a `DATAHUB_MANAGED` source's per-dataset evidence would be invisible.

    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "The owning source's
          **CLI-wrapper runs count as its own** … a source's events are the union of its
          own and its wrappers'."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.wrapper,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", schedule_tier="daily", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        schedule_tier=None,
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    observed = datetime.now(tz=UTC) - timedelta(seconds=130_000)  # inside 172800s

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 60},  # fallback — must NOT be used
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, parent)],
            sources=[parent],
            wrappers=[(wrapper.id, parent.id)],
            observations=[(str(wrapper.id), urn, observed)],  # only the wrapper booked it
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "an observation booked on the CLI wrapper must count as the owning parent's own. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
    )
    assert breakdown["datasets"] == []
