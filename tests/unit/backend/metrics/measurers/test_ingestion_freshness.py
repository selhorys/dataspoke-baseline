"""Unit tests for the ingestion-freshness measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types (the row for
  `ingestion-freshness`, quoted verbatim):
    - "`total` = count of datasets matched by `dataset_filter`; `ingested_in_time` =
      count whose latest ingestion evidence falls within `metric_conf.time_window_sec`
      of the measurement. The evidence is the owning ingestion source's per-dataset
      observation for that dataset where DataHub reports one, else that source's newest
      non-dry-run `INGESTION.COMPLETE`".
    - "`time_window_sec` for `ingestion-freshness` and `validation-score` — **the**
      measurement window (positive int seconds, factory default `172800`), the freshness
      SLO the governance lead declares and the same for every dataset the metric scans".
    - Registered under the `metric_type` value 'ingestion-freshness'; emits
      {'total': float, 'ingested_in_time': float}.

  Note on the boundary: USE_CASE's "falls within" fixes the *window*, not the behaviour
  at the exact-cutoff instant; spec/feature/BACKEND.md §Metrics Service §Measurement
  window does settle it — "**Boundary is inclusive**, for both measurers: evidence whose
  instant is exactly one window before the measurement instant is *in* window — the
  comparison is `instant >= cutoff`, never `>`". The one test below that turns on that
  instant asserts exactly that rule. The measurement instant is "the run's clock reading
  taken once at measurer entry", not the later `measured_at` stored with the result, so
  the tests freeze that entry-time reading rather than reasoning from `measured_at`.

  spec/feature/BACKEND.md §Metrics Service §Measurement window:
    - "the window is `metric_conf.time_window_sec`, applied uniformly to every dataset
      in the run. It is a declared SLO the governance lead owns, not a quantity derived
      from a per-dataset fact such as an owning source's registered schedule, a
      sync-loop cadence, or a dataset's observed validation inter-arrival gap".
    - "`ingestion-freshness`: a dataset counts toward `ingested_in_time` when its
      resolved ingestion evidence (below) is no older than `time_window_sec` at
      measurement time."
    - "Each run records the window it applied in the breakdown's
      `detail.time_window_sec`."

  spec/feature/BACKEND.md §Metrics Service §Ingestion evidence:
    - "every `INGESTION.*` event is booked on a source (entity_type="ingestion_source",
      entity_id=source_id …) and never on the dataset, so the measurer resolves each
      dataset's **owning source** first. It then reads that source's feed in **two tiers
      of evidence**, per-dataset first and source-level as fallback."
    - Tier 1 (preferred): "max(occurred_at) over the observation events the owning source
      booked **for that dataset**"; tier 2 (fallback): "max(occurred_at) over **every**
      INGESTION.COMPLETE booked on the owning source — no producer filter, **excluding
      dry runs**", applying only to "datasets with no observation evidence yet".
    - "Owning source is what IngestionService.reverse_lookup returns — or, over a
      whole dataset list at once, its batched single-winner sibling
      reverse_lookup_batch, which the measurer calls".
    - "if the sort winner is itself a wrapper it resolves up to its regular parent —
      a wrapper is never the owning source."
    - "The owning source's **CLI-wrapper runs count as its own** … a source's events
      are the union of its own and its wrappers'."

  Which tier each test exercises: every test that seeds only ``events=`` exercises
  **tier 2** (a source-level COMPLETE, the only evidence available), which is what the
  wrapper-union and owning-source tests are about. The tests under §Evidence tiers seed
  ``observations=`` and exercise **tier 1** and the preference between them.

  spec/feature/BACKEND.md §Metrics Service §Verdict contract:
    - The measurer returns (values, verdicts); verdicts cover EVERY dataset in scope,
      one entry per dataset carrying urn, met, evidence_at, detail.
    - "`ingestion-freshness` → the resolved ingestion evidence time" is evidence_at.
    - The failures-only metric_results.breakdown is DERIVED from the verdicts by
      MetricsService — `_failed()` below is the `met = false` subset it lists.

  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (stale datasets).
    - detail for ingestion-freshness: {last_event_at, time_window_sec, evidence_tier}
      with evidence_tier in {"observation", "source_level", null}.
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


def _verdict(verdicts, urn):
    """The one verdict for *urn* — verdicts cover every dataset exactly once.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "`verdicts`
    covers **every** dataset in scope, not only the failing ones".
    """
    matches = [v for v in verdicts if v.urn == urn]
    assert len(matches) == 1, f"expected exactly one verdict for {urn}; got {matches!r}"
    return matches[0]


def _failed(verdicts):
    """The `met = false` subset — the entries the derived breakdown lists."""
    return [v for v in verdicts if not v.met]


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
      evidence assertion the test names as its discriminator. A fake that hands back every
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

    values, verdicts = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(),
    )

    assert values == {"total": 0.0, "ingested_in_time": 0.0}
    assert len(verdicts) == 0
    assert _failed(verdicts) == []


# ── Fresh / stale against the declared metric_conf window ─────────────────────


@pytest.mark.asyncio
async def test_fresh_dataset_not_in_breakdown() -> None:
    """A dataset whose owning source ran inside the window is NOT in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] carries only failed entries (stale datasets).
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    recent = datetime.now(tz=UTC) - timedelta(hours=1)

    values, verdicts = await measure(
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
    assert _failed(verdicts) == [], (
        "Fresh dataset must NOT appear in breakdown. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert len(verdicts) == 1


@pytest.mark.asyncio
async def test_dataset_with_no_event_in_breakdown_with_none_last_event() -> None:
    """A mapped dataset whose source has never completed a run is stale, last_event_at=None.

    The source is present in the mapping and loaded as an entity — only its
    INGESTION.COMPLETE event is missing, so the source is **absent** from the
    helper's result dict rather than mapped to None.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when its resolved ingestion evidence "is older than
          ``metric_conf.time_window_sec``, or absent on both tiers"; detail carries
          ``last_event_at``.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.editions,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(mappings=[_mapped(urn, src)], sources=[src], events=[]),
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 0.0
    assert len(_failed(verdicts)) == 1
    entry = _failed(verdicts)[0]
    assert entry.urn == urn
    assert entry.detail["last_event_at"] is None


@pytest.mark.asyncio
async def test_dataset_with_stale_event_in_breakdown() -> None:
    """A source run older than the cutoff puts its dataset in breakdown.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          ingestion-freshness: "the resolved ingestion evidence (tier 1 or tier 2 — see
          **Ingestion evidence** above) is older than ``metric_conf.time_window_sec``, or
          absent on both tiers".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders.fulfillment,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90000)  # older than 86400s

    values, verdicts = await measure(
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
    assert len(_failed(verdicts)) == 1
    entry = _failed(verdicts)[0]
    assert entry.urn == urn
    assert entry.detail["last_event_at"] == stale_ts.isoformat()


@pytest.mark.asyncio
async def test_event_well_inside_window_is_ingested_in_time() -> None:
    """Event well inside the window (half the window ago) counts as ingested_in_time.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``ingested_in_time`` =
          count whose latest ingestion evidence falls within ``metric_conf.time_window_sec``
          of the measurement". Half a window ago is unambiguously within it.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.test,DEV)"
    time_window_sec = 3600
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    inside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec // 2)

    values, verdicts = await measure(
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
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_event_well_outside_window_is_stale() -> None:
    """Event well outside the window (2x window ago) is stale.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — only evidence that
          "falls within ``metric_conf.time_window_sec`` of the measurement" counts; twice
          the window ago falls outside it.
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is failed
          when "the resolved ingestion evidence (tier 1 or tier 2 — see **Ingestion
          evidence** above) is older than ``metric_conf.time_window_sec``, or absent on
          both tiers".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary2.test,DEV)"
    time_window_sec = 3600
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    outside_window = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec * 2)

    values, verdicts = await measure(
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
    assert len(_failed(verdicts)) == 1


# ── Verdict field set ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_carries_exactly_the_four_contract_fields() -> None:
    """A verdict is {urn, met, evidence_at, detail} — no classification field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "one entry per
          dataset carrying `urn`, `met: bool`, `evidence_at: datetime | None`, and a
          type-specific `detail`".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocategory.test,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(mappings=[_mapped(urn, src)], sources=[src], events=[]),
    )

    from dataclasses import fields

    verdict = _verdict(verdicts, urn)
    assert {f.name for f in fields(verdict)} == {"urn", "met", "evidence_at", "detail"}
    assert verdict.met is False, "backstop: this dataset must actually have failed"
    assert verdict.evidence_at is None, (
        "no evidence on either tier, so there is no evidence timestamp. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
    )


@pytest.mark.asyncio
async def test_stale_breakdown_detail_includes_the_window_and_the_evidence_tier() -> None:
    """Stale detail carries exactly last_event_at, time_window_sec, evidence_tier.

    ``evidence_tier`` is ``None`` here because neither tier produced evidence: the dataset
    is mapped to no source at all. The two tiers make different claims, so a stale verdict
    without it is not diagnosable. The key set is asserted as an equality so a detail key
    naming a *derived* window provenance — a quantity the spec says the window is not —
    cannot reappear unnoticed.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          "``ingestion-freshness`` and ``validation-score`` record the window applied at
          run time in ``time_window_sec`` … alongside ``last_event_at`` (freshness)";
          "``ingestion-freshness`` additionally names **which tier supplied
          ``last_event_at``** in ``evidence_tier`` (``"observation"`` for tier 1,
          ``"source_level"`` for tier 2, ``null`` when neither tier produced evidence)".
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the window is
          ``metric_conf.time_window_sec``, applied uniformly to every dataset in the run".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detail-check,DEV)"

    # No mapping at all, no event → stale with neither tier answering.
    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(),
    )

    assert len(_failed(verdicts)) == 1
    detail = _failed(verdicts)[0].detail
    assert set(detail) == {
        "last_event_at",
        "time_window_sec",
        "evidence_tier",
    }, (
        "Stale detail keys must be exactly {last_event_at, time_window_sec, "
        "evidence_tier}; got " + f"{sorted(detail)}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert "window_source" not in detail, (
        "detail must not name a window provenance: the window is always "
        "metric_conf.time_window_sec, so there is no provenance to report. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert detail["time_window_sec"] == 86400, (
        "detail.time_window_sec must be the metric's declared window. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
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

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "the measurer
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
    src_fresh1 = _source(mode="ACTIVE_CUSTOM_MANAGED", name="a")
    src_fresh2 = _source(mode="ACTIVE_CUSTOM_MANAGED", name="b")
    src_stale = _source(mode="ACTIVE_CUSTOM_MANAGED", name="c")

    values, verdicts = await measure(
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
    assert len(verdicts) == 3
    assert [v.urn for v in _failed(verdicts)] == [urn_stale]


@pytest.mark.asyncio
async def test_two_datasets_sharing_a_source_share_its_tier_2_evidence() -> None:
    """On **tier 2**, two datasets covered by one source get the same verdict.

    Neither dataset has an observation of its own, so both fall back to the source-level
    maximum and cannot split — which is precisely the approximation tier 2 admits ("an
    event booked on a source genuinely cannot say which dataset it touched"). The
    contrast is ``test_a_dataset_reads_its_own_observation_and_not_a_siblings``: the same
    two-datasets-one-source shape *does* split once each carries tier-1 evidence.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 2 is
          "``max(occurred_at)`` over **every** ``INGESTION.COMPLETE`` booked on the owning
          source", applying to "datasets with no observation evidence yet".
    """
    measure = _get_measurer()
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.shared.a,DEV)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.shared.b,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="shared")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90000)

    values, verdicts = await measure(
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
    assert {v.urn for v in _failed(verdicts)} == {urn_a, urn_b}
    for entry in _failed(verdicts):
        assert entry.detail["last_event_at"] == stale_ts.isoformat()


# ── Deterministic clock boundary (inclusive >=) ──────────────────────────────


@pytest.mark.asyncio
async def test_event_exactly_at_cutoff_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event at exactly measurement instant - time_window_sec is FRESH.

    The boundary instant is settled by spec, not by the implementation:
    spec/feature/BACKEND.md §Metrics Service §Measurement window — "**Boundary is
    inclusive**, for both measurers: evidence whose instant is exactly one window before
    the measurement instant is *in* window — the comparison is ``instant >= cutoff``,
    never ``>``." The same section fixes which clock reading the cutoff hangs off: "The
    measurement instant is the run's clock reading taken once at measurer entry, and
    ``cutoff`` is that reading minus ``time_window_sec``. The ``measured_at`` persisted
    with the result is a later reading … it dates the result and does not define the
    window." So the event here is dated off the frozen entry-time reading.

    The same section explains why a test has to carry this rather than a run: "The
    boundary direction is not observable in practice — the reading is
    microsecond-resolution, so a stored timestamp landing on it exactly is a measure-zero
    event — so it is fixed here rather than left to be inferred from a run." This test and
    ``test_validation_score.py::test_row_exactly_at_cutoff_is_counted`` are therefore the
    only places the ``>=`` is exercised at the instant that distinguishes it from ``>``.

    The wider sides of the boundary are ``test_event_one_second_inside_window_is_fresh``
    and ``test_event_well_outside_window_is_stale``.
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.exact,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    exact_cutoff = fixed_now - timedelta(seconds=time_window_sec)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), exact_cutoff)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "evidence exactly one window before the measurement instant is in window "
        "(instant >= cutoff). Spec: spec/feature/BACKEND.md §Metrics Service "
        "§Measurement window — 'Boundary is inclusive'."
    )
    assert _failed(verdicts) == []
    verdict = _verdict(verdicts, urn)
    assert verdict.met is True
    assert verdict.detail["last_event_at"] == exact_cutoff.isoformat(), (
        "backstop: the passing verdict must rest on the event seeded exactly on the "
        "cutoff, not on some other (or absent) evidence."
    )
    assert verdict.detail["time_window_sec"] == time_window_sec


@pytest.mark.asyncio
async def test_event_one_second_inside_window_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event at now - time_window_sec + 1s is FRESH.

    One second inside the window is inside it on any reading, so this is the boundary side
    the spec does settle.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``ingested_in_time`` =
          count whose latest ingestion evidence falls within
          ``metric_conf.time_window_sec`` of the measurement".
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.inside,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    one_sec_inside = fixed_now - timedelta(seconds=time_window_sec - 1)

    values, verdicts = await measure(
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
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_event_one_second_outside_window_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event at now - time_window_sec - 1s is STALE.

    The mirror of the case above, and the outer bracket of the cutoff: one second the far
    side of it is outside the window on any reading. The pair straddles the cutoff at
    one-second granularity, so a cutoff computed one second off in either direction moves
    one of the two — which the inclusive-boundary pair on its own no longer catches, since
    ``instant >= cutoff`` admits the cutoff instant itself.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — only evidence that
          "falls within ``metric_conf.time_window_sec`` of the measurement" counts.
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is failed
          when "the resolved ingestion evidence (tier 1 or tier 2 — see **Ingestion
          evidence** above) is older than ``metric_conf.time_window_sec``, or absent on
          both tiers".
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.outside,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")
    one_sec_outside = fixed_now - timedelta(seconds=time_window_sec + 1)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), one_sec_outside)],
        ),
    )

    assert values["ingested_in_time"] == 0.0, (
        "a run one second outside the window does not 'fall within' it and must be STALE. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert [v.urn for v in _failed(verdicts)] == [urn]
    assert _failed(verdicts)[0].detail["last_event_at"] == one_sec_outside.isoformat(), (
        "backstop: the seeded out-of-window run must be what the verdict rests on, not "
        "absent evidence."
    )


# ── The measurer holds no copy of the write boundary's window bound ──────────


@pytest.mark.asyncio
async def test_an_out_of_range_stored_window_fails_the_run_rather_than_being_clamped() -> None:
    """A window far past the write boundary's ceiling makes the run fail, not clamp.

    ``metric_conf`` is plain JSONB with no column constraint, so a row written by
    something other than the API can carry a window the write boundary would have
    rejected. The spec settles what a measurer does with one: it fails. A measurer that
    carried its own copy of the bound and clamped to it would return ordinary-looking
    values here, silently reporting freshness against a window nobody declared — and no
    other test in this file would notice, because every one of them passes an admissible
    window.

    ``10**20`` seconds is past what the runtime's duration type can represent, so "fails"
    is observable as the ``OverflowError`` the arithmetic raises. The assertion is that it
    propagates.

    Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — "Measurers carry no
          second copy of the bound; they trust `metric_conf` by contract … a row carrying
          an out-of-range window (written by something other than the API) makes every run
          of that metric fail rather than being silently clamped".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unbounded.window,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED")

    with pytest.raises(OverflowError):
        await measure(
            datasets=[urn],
            metric_conf={"time_window_sec": 10**20},
            datahub=_datahub(),
            db=_fake_measurer_db(
                mappings=[_mapped(urn, src)],
                sources=[src],
                events=[(str(src.id), datetime.now(tz=UTC))],
            ),
        )


# ── The window is the metric's declared config value, for every dataset ──────


@pytest.mark.asyncio
async def test_window_is_the_declared_config_value_for_a_passive_owned_dataset() -> None:
    """A PASSIVE-owned dataset applies the metric's declared window, not a sync cadence.

    Its only evidence is ~3 hours old and the metric declares a 172800s window, so the
    dataset is ingested_in_time. The owning source's mode, and the cadence at which
    DataSpoke polls DataHub on its behalf, state how often something is *expected* to
    happen — a different question from how recent the evidence must be to count — so
    neither narrows the window here.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the window is
          ``metric_conf.time_window_sec``, applied uniformly to every dataset in the run.
          It is a declared SLO the governance lead owns, not a quantity derived from a
          per-dataset fact such as an owning source's registered schedule, a sync-loop
          cadence, or a dataset's observed validation inter-arrival gap".
    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``time_window_sec``
          … **the** measurement window … the same for every dataset the metric scans".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive.declared,DEV)"
    src = _source(mode="PASSIVE", name="passive-declared")
    three_hours_ago = datetime.now(tz=UTC) - timedelta(hours=3)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src, derivation="matched")],
            sources=[src],
            events=[(str(src.id), three_hours_ago)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, (
        "evidence 3 hours old is inside the metric's declared 172800s window, so the "
        "dataset is in-time whatever mode owns it. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_a_passive_owned_dataset_outside_the_declared_window_is_stale() -> None:
    """The same PASSIVE-owned dataset is stale once its evidence outruns the window.

    Mirror of the case above: only the evidence age moves, from inside 172800s to past
    it, so the pair pins the declared window as the actual boundary rather than merely
    asserting freshness. ``detail.time_window_sec`` reports the window the run applied.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when "the resolved ingestion evidence (tier 1 or tier 2 …) is older
          than ``metric_conf.time_window_sec``, or absent on both tiers".
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "Each run
          records the window it applied in the breakdown's ``detail.time_window_sec``".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passive.declared,DEV)"
    src = _source(mode="PASSIVE", name="passive-declared")
    past_the_window = datetime.now(tz=UTC) - timedelta(seconds=172800 + 3600)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src, derivation="matched")],
            sources=[src],
            events=[(str(src.id), past_the_window)],
        ),
    )

    assert values["ingested_in_time"] == 0.0
    assert [v.urn for v in _failed(verdicts)] == [urn]
    detail = _failed(verdicts)[0].detail
    assert detail["last_event_at"] == past_the_window.isoformat(), (
        "backstop: the seeded evidence must be what the verdict rests on."
    )
    assert detail["time_window_sec"] == 172800, (
        f"detail.time_window_sec must be the declared window; got "
        f"{detail['time_window_sec']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )


@pytest.mark.asyncio
async def test_every_owning_mode_and_tier_reports_the_same_declared_window() -> None:
    """Four datasets on sources of differing mode/schedule share one window.

    The seeded sources span both managed modes with an hourly and a daily
    ``schedule_tier``, a managed source with none, and a ``PASSIVE`` source. Those are
    the per-dataset facts the spec names as things the window is *not* read off, and the
    fixture varies every one of them. All four carry evidence at the same instant, 8000s
    old, and the metric declares 86400s, so all four are in-time. A window scaled to any
    of the varied facts would split the four instead: 8000s is inside 86400s but outside
    the 7200s an hourly cadence would imply, so ``ingested_in_time`` drops below 4.0 the
    moment any per-dataset fact narrows the window.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — the window is
          "applied uniformly to every dataset in the run".
    """
    measure = _get_measurer()
    hourly_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.uniform.hourly,DEV)"
    daily_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.uniform.daily,DEV)"
    untiered_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.uniform.untiered,DEV)"
    passive_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.uniform.passive,DEV)"

    src_hourly = _source(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="hourly", name="h")
    src_daily = _source(mode="DATAHUB_MANAGED", schedule_tier="daily", name="d")
    src_untiered = _source(mode="ACTIVE_CUSTOM_MANAGED", name="u")
    src_passive = _source(mode="PASSIVE", name="p")

    evidence_at = datetime.now(tz=UTC) - timedelta(seconds=8000)

    values, verdicts = await measure(
        datasets=[hourly_urn, daily_urn, untiered_urn, passive_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[
                _mapped(hourly_urn, src_hourly),
                _mapped(daily_urn, src_daily),
                _mapped(untiered_urn, src_untiered),
                _mapped(passive_urn, src_passive, derivation="matched"),
            ],
            sources=[src_hourly, src_daily, src_untiered, src_passive],
            events=[
                (str(src_hourly.id), evidence_at),
                (str(src_daily.id), evidence_at),
                (str(src_untiered.id), evidence_at),
                (str(src_passive.id), evidence_at),
            ],
        ),
    )

    assert values["total"] == 4.0
    assert values["ingested_in_time"] == 4.0, (
        "one declared 86400s window applies to all four owning modes/tiers, and 8000s "
        "old evidence is inside it for every one of them; stale entries: "
        f"{[v.urn for v in _failed(verdicts)]}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_a_dataset_with_no_owning_source_uses_the_declared_window() -> None:
    """A dataset covered by no source still reports the declared window, and is stale.

    With no owning source there is no feed to read on either tier, so the dataset is
    stale with ``last_event_at=None`` — and ``detail.time_window_sec`` is still the
    metric's declared value, because the window does not depend on the source.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when its evidence is "absent on both tiers".
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — the window is
          ``metric_conf.time_window_sec``.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unclaimed,DEV)"

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},
        datahub=_datahub(),
        db=_fake_measurer_db(),  # no mapping rows at all
    )

    assert values["total"] == 1.0
    assert values["ingested_in_time"] == 0.0
    detail = _failed(verdicts)[0].detail
    assert detail["last_event_at"] is None
    assert detail["evidence_tier"] is None
    assert detail["time_window_sec"] == 3600, (
        f"detail.time_window_sec must be the declared window; got "
        f"{detail['time_window_sec']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )


# ── CLI-wrapper runs count as the owning source's own ────────────────────────


@pytest.mark.asyncio
async def test_run_booked_on_a_wrapper_only_counts_for_the_owning_parent() -> None:
    """A run booked on the CLI wrapper makes the parent's dataset fresh.

    Injection is asymmetric on purpose: the parent source id carries **no** event
    row at all, only the wrapper's does. If the measurer read the registered source
    id alone the dataset would be stale, which is exactly the defect this covers.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "The owning
          source's **CLI-wrapper runs count as its own** — DataHub books a managed
          source's executions on an auto-created wrapper rather than on the
          registered source, so a source's events are the union of its own and its
          wrappers'."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.wrapper-only,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    wrapper_run = datetime.now(tz=UTC) - timedelta(seconds=130000)  # inside 172800s

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 172800},
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
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
    )
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_newest_run_across_parent_and_wrapper_wins() -> None:
    """With runs on both the parent and its wrapper, the newest of the union is used.

    Both rows are seeded and the wrapper's is the newer one, so a reader that took
    only the parent's own event would report the older timestamp and call the
    dataset stale.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "a source's events
          are the union of its own and its wrappers'".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.parent-and-wrapper,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    now = datetime.now(tz=UTC)
    parent_run = now - timedelta(seconds=8000)  # outside the declared 3600s window
    wrapper_run = now - timedelta(seconds=600)  # inside it, and newer

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 3600},
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
        "wrapper's 600s-old run is inside the declared 3600s window while the parent's "
        "own 8000s-old run is not. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
    )
    assert _failed(verdicts) == []


@pytest.mark.asyncio
async def test_owning_source_is_the_regular_parent_of_a_claiming_wrapper() -> None:
    """When a wrapper is the ranked winner, the evidence feed is its regular parent's.

    The wrapper claims the dataset at derivation 'emitted' while its parent only
    'matched' it, so the wrapper outranks the parent and the tie-break never runs —
    only the explicit resolve-up step can produce the parent here. The discriminator is
    *whose feed answered*: the run is booked on the parent id alone, and a source's feed
    is the union of its own events and its wrappers' — a union that reaches the parent's
    row from the parent, but never reaches it from the wrapper. So an unresolved wrapper
    owner would read no evidence at all and report ``last_event_at=None``.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "if the sort
          winner is itself a wrapper it resolves up to its regular parent — a wrapper
          is never the owning source. The second step … also fires when a wrapper
          claims a dataset at a *higher* derivation rank than its parent, where the
          tie-break never runs."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.wrapper-claims,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=200000)  # outside 86400s

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
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
    detail = _failed(verdicts)[0].detail
    assert detail["last_event_at"] == stale_ts.isoformat(), (
        "A winning wrapper must resolve up to its regular parent, so the parent's own "
        f"run is the evidence read; got {detail['last_event_at']!r}, expected "
        f"{stale_ts.isoformat()!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
    )
    assert detail["evidence_tier"] == "source_level", (
        f"the parent's run-level COMPLETE is tier-2 evidence; got "
        f"{detail['evidence_tier']!r}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert detail["time_window_sec"] == 86400


# ── Evidence tiers: per-dataset observation preferred, source-level as fallback ──
#
# spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — the two-tier table, and
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

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1 is
          "(preferred)"; tier 2 "applies only where nothing better exists".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.own,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="tier1-own")
    now = datetime.now(tz=UTC)
    own_observation = now - timedelta(seconds=90_000)  # stale against the 86400s window
    source_run = now - timedelta(hours=1)  # fresh — must NOT be what answers

    values, verdicts = await measure(
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
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1 preferred."
    )
    detail = _failed(verdicts)[0].detail
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

    Both are covered by the same source, so a purely source-grained rule would force them
    to the same verdict. With per-dataset evidence they split: the sibling's
    observation is fresh and this dataset's is stale, so a lookup keyed on the source
    alone would report both fresh.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1 is
          "max(occurred_at) over the observation events the owning source booked **for
          that dataset**".
    """
    measure = _get_measurer()
    stale_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.stale,DEV)"
    fresh_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.fresh,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="tier1-shared")
    now = datetime.now(tz=UTC)

    values, verdicts = await measure(
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
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 1."
    )
    assert [v.urn for v in _failed(verdicts)] == [stale_urn], (
        f"only the dataset whose own observation is outside the window is stale; got "
        f"{[v.urn for v in _failed(verdicts)]}."
    )


@pytest.mark.asyncio
async def test_a_dataset_with_no_observation_falls_back_to_the_source_level_maximum() -> None:
    """Tier 2 answers for a dataset that has no observation of its own.

    Both sides are seeded in one call: the sibling carries an observation and this dataset
    does not, so a measurer that had dropped the fallback entirely would report this one
    stale with ``last_event_at=None``.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 2 applies to
          "datasets with no observation evidence yet"; it is "source-grained, not
          producer-filtered: any COMPLETE on the owning source qualifies, so a sibling
          dataset's observation can stand in for a dataset that has none of its own".
    """
    measure = _get_measurer()
    observed_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier2.observed,DEV)"
    bare_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier2.bare,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="tier2-fallback")
    now = datetime.now(tz=UTC)
    source_run = now - timedelta(hours=2)

    values, verdicts = await measure(
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
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — tier 2."
    )
    assert _failed(verdicts) == []


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
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="tier2-stale")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90_000)

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    assert len(_failed(verdicts)) == 1
    detail = _failed(verdicts)[0].detail
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

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "The owning source's
          **CLI-wrapper runs count as its own** … a source's events are the union of its
          own and its wrappers'."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.tier1.wrapper,DEV)"
    parent = _source(mode="DATAHUB_MANAGED", name="parent")
    wrapper = _source(
        mode="DATAHUB_MANAGED",
        parent_source_id=parent.id,
        name="[CLI] postgres",
    )
    observed = datetime.now(tz=UTC) - timedelta(seconds=130_000)  # inside 172800s

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 172800},
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
        "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
    )
    assert _failed(verdicts) == []


# ── evidence_at: the per-dataset timestamp the /dataset view dates a check by ──


@pytest.mark.asyncio
async def test_evidence_at_is_the_resolved_evidence_instant_for_a_fresh_dataset() -> None:
    """A met verdict carries the evidence instant that decided it.

    `GET /spoke/governance/metric/{id}/dataset` reports it as `last_check_at`, so a
    verdict that carried no timestamp would silently fall back to the run time and
    report a stale dataset as freshly checked.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "`evidence_at`
          per type: `ingestion-freshness` → the resolved ingestion evidence time".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.evidence.fresh,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="evidence-fresh")
    observed = datetime.now(tz=UTC) - timedelta(hours=1)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            observations=[(str(src.id), urn, observed)],
        ),
    )

    assert values["ingested_in_time"] == 1.0, "backstop: this dataset must be fresh"
    verdict = _verdict(verdicts, urn)
    assert verdict.met is True
    assert verdict.evidence_at == observed


@pytest.mark.asyncio
async def test_evidence_at_is_carried_on_a_stale_verdict_too() -> None:
    """A stale dataset still carries the instant its (old) evidence was booked.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — evidence_at is
          "the resolved ingestion evidence time", independent of the verdict.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.evidence.stale,DEV)"
    src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="evidence-stale")
    stale_ts = datetime.now(tz=UTC) - timedelta(seconds=90_000)

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(urn, src)],
            sources=[src],
            events=[(str(src.id), stale_ts)],
        ),
    )

    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.evidence_at == stale_ts
    assert verdict.detail["evidence_tier"] == "source_level"


@pytest.mark.asyncio
async def test_verdicts_cover_every_dataset_fresh_and_stale_alike() -> None:
    """Both a fresh and a stale dataset get a verdict, in scope order.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "Full coverage
          is what makes 'in scope but never evaluated' (`unknown`) distinguishable from
          'evaluated and passing'".
    """
    measure = _get_measurer()
    fresh_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cover.fresh,DEV)"
    stale_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cover.stale,DEV)"
    fresh_src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="cover-fresh")
    stale_src = _source(mode="ACTIVE_CUSTOM_MANAGED", name="cover-stale")
    now = datetime.now(tz=UTC)

    values, verdicts = await measure(
        datasets=[fresh_urn, stale_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_fake_measurer_db(
            mappings=[_mapped(fresh_urn, fresh_src), _mapped(stale_urn, stale_src)],
            sources=[fresh_src, stale_src],
            events=[
                (str(fresh_src.id), now - timedelta(hours=1)),
                (str(stale_src.id), now - timedelta(seconds=90_000)),
            ],
        ),
    )

    assert values == {"total": 2.0, "ingested_in_time": 1.0}
    assert [(v.urn, v.met) for v in verdicts] == [(fresh_urn, True), (stale_urn, False)]
