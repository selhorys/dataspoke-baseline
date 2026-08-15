"""Unit tests for the validation-score measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'validation-score'.
    - Emits {'total': float, 'validation_score_sum': float}.
    - "`total` = count of datasets matched by `dataset_filter`;
      `validation_score_sum` = sum of each dataset's latest validation `score` whose
      `data_time` falls within `metric_conf.time_window_sec` of the measurement. The
      contribution is 0.0 when there is no validation result inside the window".
    - "`time_window_sec` for `ingestion-freshness` and `validation-score` — **the**
      measurement window (positive int seconds … factory default `172800`) … the same
      for every dataset the metric scans".
  spec/feature/BACKEND.md §Metrics Service §Measurement window:
    - "the window is `metric_conf.time_window_sec`, applied uniformly to every dataset
      in the run. It is a declared SLO the governance lead owns, not a quantity derived
      from a per-dataset fact such as an owning source's registered schedule, a
      sync-loop cadence, or a dataset's observed validation inter-arrival gap".
    - "`validation-score`: the score counted is the latest result whose `data_time` is
      inside `time_window_sec`; a dataset with no result in the window contributes
      `0.0`."
    - "Each run records the window it applied in the breakdown's
      `detail.time_window_sec`."
  spec/feature/BACKEND.md §Metrics Service §Verdict contract:
    - The measurer returns (values, verdicts); verdicts cover EVERY dataset in scope,
      one entry per dataset carrying urn, met, evidence_at, detail.
    - "`validation-score` → the counted result's `data_time`" is evidence_at.
    - The failures-only metric_results.breakdown is DERIVED from the verdicts by
      MetricsService, not built here.
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - A dataset is failed when its latest in-window score is < 1.0, or it has no
      result inside the window.
    - detail for validation-score: {latest_data_time, score, time_window_sec}.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers import validation_score  # noqa: F401 — triggers registration
from src.shared.datahub.client import DataHubClient


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("validation-score")
    assert fn is not None, "validation-score measurer must be registered"
    return fn


def _verdict(verdicts, urn):
    """The one verdict for *urn* — verdicts cover every dataset exactly once.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract.
    """
    matches = [v for v in verdicts if v.urn == urn]
    assert len(matches) == 1, f"expected exactly one verdict for {urn}; got {matches!r}"
    return matches[0]


def _failed(verdicts):
    """The `met = false` subset — the entries the derived breakdown lists."""
    return [v for v in verdicts if not v.met]


def _row(urn: str, data_time: datetime, score: float) -> MagicMock:
    """One latest-per-dataset ``validation_results`` row as the measurer reads it."""
    row = MagicMock()
    row.dataset_urn = urn
    row.data_time = data_time
    row.score = score
    return row


def _db(rows: list) -> AsyncMock:
    """Mock session returning *rows* for the latest-per-dataset query.

    ``spec=AsyncSession`` so a renamed or mistyped session method fails loud instead of
    returning a fresh auto-mock (spec/TESTING.md §Unit Testing).
    """
    result = MagicMock()
    result.all.return_value = rows
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock(return_value=result)
    return db


def _datahub() -> MagicMock:
    """A spec'd DataHubClient stand-in.

    The measurer accepts one for signature uniformity and makes no DataHub call — it is
    contractually "pure-aggregation and DataSpoke-DB-side" (spec/feature/BACKEND.md
    §Metrics Service §Measurement window). This exists only to fail loudly if it ever
    starts making one: ``spec=`` turns a call to a method DataHubClient does not have
    into an ``AttributeError`` instead of a silently successful auto-mock.
    """
    return MagicMock(spec=DataHubClient)


def _freeze_now(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> None:
    """Pin the measurer's ``datetime.now`` so cutoff boundaries are exact."""
    import src.backend.metrics.measurers.validation_score as _mod

    class _FixedDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return fixed_now

    monkeypatch.setattr(_mod, "datetime", _FixedDatetime)


# ── Registration ──────────────────────────────────────────────────────────────


def test_registered_under_correct_key() -> None:
    """Measurer is registered under 'validation-score'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_type
          value is 'validation-score'.
    """
    fn = _get_measurer()
    assert fn is not None


# ── Empty datasets list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zeros_without_querying() -> None:
    """measure([]) returns total=0.0, validation_score_sum=0.0 and issues no query.

    ``total`` is the count of datasets matched by ``dataset_filter``, which is zero
    here, so there is nothing to look up — the ``db.execute`` assertion is what makes
    "no dataset" distinguishable from "every dataset scored 0.0".

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``total`` = count of
          datasets matched by ``dataset_filter``".
    """
    measure = _get_measurer()
    db = _db([])

    values, verdicts = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=db,
    )

    assert values == {"total": 0.0, "validation_score_sum": 0.0}
    assert verdicts == []
    db.execute.assert_not_awaited()


# ── All passing (score=1.0) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_datasets_score_one_meet_the_criterion() -> None:
    """Datasets with in-window score=1.0 are met, and each still carries a verdict.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          validation-score: latest score in window < 1.0 → failed.
    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — verdicts cover
          every dataset, "not only the failing ones".
    """
    measure = _get_measurer()
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"

    now = datetime.now(tz=UTC)

    values, verdicts = await measure(
        datasets=[urn1, urn2],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            [
                _row(urn1, now - timedelta(hours=1), 1.0),
                _row(urn2, now - timedelta(hours=2), 1.0),
            ]
        ),
    )

    assert values["total"] == 2.0
    assert values["validation_score_sum"] == 2.0
    assert [(v.urn, v.met) for v in verdicts] == [(urn1, True), (urn2, True)], (
        "Both datasets are in scope and both passed, so both carry a met verdict. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
    )
    assert _failed(verdicts) == []


# ── Three datasets: two passing, one partial ──────────────────────────────────


@pytest.mark.asyncio
async def test_three_datasets_two_full_one_partial() -> None:
    """Three datasets with scores [1.0, 1.0, 0.7]: total=3, sum=2.7, breakdown=1 entry.

    The partial dataset is counted in ``validation_score_sum`` *and* listed as failed —
    an in-window result below 1.0 does both, so summing and flagging are independent.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``validation_score_sum``
          = sum of each dataset's latest validation ``score`` whose ``data_time`` falls
          within ``metric_conf.time_window_sec`` of the measurement".
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when the "latest validation ``score`` inside the window is ``< 1.0``".
    """
    measure = _get_measurer()
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.full1,DEV)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.full2,DEV)"
    urn3 = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.partial,DEV)"

    now = datetime.now(tz=UTC)
    partial_data_time = now - timedelta(hours=3)

    values, verdicts = await measure(
        datasets=[urn1, urn2, urn3],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            [
                _row(urn1, now - timedelta(hours=1), 1.0),
                _row(urn2, now - timedelta(hours=2), 1.0),
                _row(urn3, partial_data_time, 0.7),
            ]
        ),
    )

    assert values["total"] == 3.0
    assert abs(values["validation_score_sum"] - 2.7) < 1e-6, (
        "the in-window 0.7 must accumulate alongside the two 1.0 scores. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert len(verdicts) == 3, "every dataset in scope carries a verdict"
    assert [v.urn for v in _failed(verdicts)] == [urn3], (
        "only the sub-1.0 dataset is failed. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    partial = _verdict(verdicts, urn3)
    assert partial.detail["score"] == 0.7
    assert partial.detail["latest_data_time"] == partial_data_time.isoformat()
    assert partial.evidence_at == partial_data_time, (
        "an in-window row was counted, so it dates the check. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "
        "'`validation-score` → the counted result's `data_time`'."
    )


# ── Dataset with out-of-window result ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_out_of_window_result_contributes_zero() -> None:
    """A dataset whose latest result predates the window contributes 0.0 and is failed.

    The latest row's ``score`` is 0.9, so a measurer that ignored ``data_time`` would
    report 0.9 — the sum assertion is what proves the window gate ran.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window —
          "``validation-score``: the score counted is the latest result whose
          ``data_time`` is inside ``time_window_sec``; a dataset with no result in the
          window contributes ``0.0``".
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when it has "no result inside the window".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.old,DEV)"
    time_window_sec = 86400
    outside = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec + 3600)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_db([_row(urn, outside, 0.9)]),
    )

    assert values["total"] == 1.0
    assert values["validation_score_sum"] == 0.0, (
        "A row whose data_time is older than the window must not be counted. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["latest_data_time"] == outside.isoformat()
    assert verdict.detail["score"] == 0.9, (
        "the out-of-window row is still reported so the reader can see how stale it is. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
    )
    assert verdict.evidence_at is None, (
        "nothing was counted this window, so there is no evidence timestamp and "
        "last_check_at falls back to the run's measured_at. "
        "Spec: spec/API.md §Metric — '`last_check_at` is the per-dataset evidence "
        "timestamp […] falling back to the run's `measured_at`'."
    )


# ── Dataset with no validation result ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_with_no_result_contributes_zero_and_fails_its_verdict() -> None:
    """Dataset with no validation result at all contributes 0.0 and fails its verdict.

    Its detail carries a null ``latest_data_time`` and ``score``: there is no result to
    report, which is a different fact from a result that scored zero.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "a dataset with
          no result in the window contributes ``0.0``".
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when it has "no result inside the window"; detail carries
          ``latest_data_time`` + ``score``.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.novalidation,DEV)"

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db([]),
    )

    assert values["validation_score_sum"] == 0.0
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["latest_data_time"] is None
    assert verdict.detail["score"] is None
    assert verdict.evidence_at is None


# ── Verdict field set ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_carries_exactly_the_four_contract_fields() -> None:
    """A verdict is {urn, met, evidence_at, detail} — no classification field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "one entry per
          dataset carrying `urn`, `met: bool`, `evidence_at: datetime | None`, and a
          type-specific `detail`".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocat,DEV)"

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db([]),
    )

    from dataclasses import fields

    verdict = _verdict(verdicts, urn)
    assert {f.name for f in fields(verdict)} == {"urn", "met", "evidence_at", "detail"}
    assert verdict.met is False, "backstop: this dataset must actually have failed"


# ── The declared window gates in both directions within one run ──────────────


@pytest.mark.asyncio
async def test_one_declared_window_splits_two_datasets_in_the_same_run() -> None:
    """A single run's window admits one dataset and excludes the other.

    One latest result is 30 hours old and the other 2 hours old against a declared
    86400s window, so the run splits: the older contributes 0.0 and is failed, the
    newer contributes its score and is not. Both directions of the gate are exercised
    in one call, so neither verdict can be a constant.

    **Scope**: this does *not* establish cross-dataset window *uniformity*. With one row
    per dataset the fixture cannot tell "one window applied to both" from "two
    per-dataset windows that happen to coincide" — a per-dataset window needs several
    rows to derive anything from. Uniformity for validation-score is carried by
    ``tests/integration/spot/test_metrics.py``
    ``::test_validation_score_declared_window_gates_the_latest_row``, which seeds four
    rows per dataset and needs real PostgreSQL for the ``row_number()`` latest-per-dataset
    pick that the fake session here cannot execute.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the score
          counted is the latest result whose ``data_time`` is inside ``time_window_sec``;
          a dataset with no result in the window contributes ``0.0``".
    """
    measure = _get_measurer()
    outside_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.window.outside,DEV)"
    inside_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.window.inside,DEV)"

    now = datetime.now(tz=UTC)

    values, verdicts = await measure(
        datasets=[outside_urn, inside_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            [
                _row(outside_urn, now - timedelta(hours=30), 1.0),
                _row(inside_urn, now - timedelta(hours=2), 1.0),
            ]
        ),
    )

    assert values["total"] == 2.0
    assert values["validation_score_sum"] == 1.0, (
        "only the dataset whose latest result is inside the declared 86400s window "
        "contributes. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert [v.urn for v in _failed(verdicts)] == [outside_urn], (
        "the 30-hour-old result is outside the declared window and the 2-hour-old one "
        "is inside it. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert _verdict(verdicts, outside_urn).detail["time_window_sec"] == 86400


# ── Deterministic clock boundary ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_row_one_second_inside_window_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result at now - time_window_sec + 1s is inside the window and is counted.

    One second inside the window is inside it on any reading, so this is the boundary
    side the spec does settle.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window —
          "``validation-score``: the score counted is the latest result whose
          ``data_time`` is inside ``time_window_sec``".
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.inside,DEV)"
    one_sec_inside = fixed_now - timedelta(seconds=time_window_sec - 1)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_db([_row(urn, one_sec_inside, 1.0)]),
    )

    assert values["validation_score_sum"] == 1.0, (
        "a result one second inside the window is inside it and must be counted. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert _verdict(verdicts, urn).met is True


@pytest.mark.asyncio
async def test_row_one_second_outside_window_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result at now - time_window_sec - 1s is outside the window and contributes 0.0.

    The mirror of the case above: one second the other side of the cutoff is outside
    the window on any reading. The pair brackets the cutoff from both directions, so a
    cutoff computed one second off in either direction moves one of them.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "a dataset with
          no result in the window contributes ``0.0``".
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when it has "no result inside the window".
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.outside,DEV)"
    one_sec_outside = fixed_now - timedelta(seconds=time_window_sec + 1)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_db([_row(urn, one_sec_outside, 1.0)]),
    )

    assert values["validation_score_sum"] == 0.0, (
        "a result one second outside the window must not be counted. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["latest_data_time"] == one_sec_outside.isoformat(), (
        "backstop: the seeded out-of-window row must be what the verdict rests on."
    )


@pytest.mark.asyncio
async def test_row_exactly_at_cutoff_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result at exactly measurement instant - time_window_sec is counted.

    The boundary instant is settled by spec: spec/feature/BACKEND.md §Metrics Service
    §Measurement window — "**Boundary is inclusive**, for both measurers: evidence whose
    instant is exactly one window before the measurement instant is *in* window — the
    comparison is ``instant >= cutoff``, never ``>``." The same section names the clock
    reading the cutoff hangs off: "The measurement instant is the run's clock reading
    taken once at measurer entry … The ``measured_at`` persisted with the result is a
    later reading … it dates the result and does not define the window." The row below is
    therefore dated off the frozen entry-time reading, not off ``measured_at``.

    The spec also explains why this rule needs a test rather than a run to hold it: "The
    boundary direction is not observable in practice — the reading is
    microsecond-resolution, so a stored timestamp landing on it exactly is a measure-zero
    event." This test and
    ``test_ingestion_freshness.py::test_event_exactly_at_cutoff_is_fresh`` are the only
    two that exercise the comparison at the instant which distinguishes ``>=`` from ``>``.

    The wider sides of the boundary are
    ``test_row_one_second_inside_window_is_counted`` and
    ``test_row_one_second_outside_window_is_not_counted``.
    """
    time_window_sec = 3600
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, fixed_now)

    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.boundary.exact,DEV)"
    exact_cutoff = fixed_now - timedelta(seconds=time_window_sec)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": time_window_sec},
        datahub=_datahub(),
        db=_db([_row(urn, exact_cutoff, 1.0)]),
    )

    assert values["validation_score_sum"] == 1.0
    assert _verdict(verdicts, urn).met is True


# ── The measurer holds no copy of the write boundary's window bound ──────────


@pytest.mark.asyncio
async def test_an_out_of_range_stored_window_fails_the_run_rather_than_being_clamped() -> None:
    """A window far past the write boundary's ceiling makes the run fail, not clamp.

    ``metric_conf`` is plain JSONB with no column constraint, so a row written by
    something other than the API can carry a window the write boundary would have
    rejected. The spec settles what a measurer does with one: it fails. A measurer that
    carried its own copy of the bound and clamped to it would return an ordinary-looking
    ``validation_score_sum`` here, computed against a window nobody declared — and no
    other test in this file would notice, because every one of them passes an admissible
    window.

    ``10**20`` seconds is past what the runtime's duration type can represent, so "fails"
    is observable as the ``OverflowError`` the arithmetic raises. The assertion is that it
    propagates. The dataset list is non-empty on purpose: the empty-list short circuit
    returns before the window is ever used.

    Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — "Measurers carry no
          second copy of the bound; they trust `metric_conf` by contract … a row carrying
          an out-of-range window (written by something other than the API) makes every run
          of that metric fail rather than being silently clamped".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unbounded.window,DEV)"

    with pytest.raises(OverflowError):
        await measure(
            datasets=[urn],
            metric_conf={"time_window_sec": 10**20},
            datahub=_datahub(),
            db=_db([_row(urn, datetime.now(tz=UTC), 1.0)]),
        )


# ── Verdict detail shape ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_detail_keys_are_exactly_the_three_spec_fields() -> None:
    """Verdict detail carries exactly latest_data_time, score, time_window_sec.

    The key set is asserted as an equality so a detail key naming a *derived* window
    provenance — a quantity the spec says the window is not — cannot reappear unnoticed.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          "``ingestion-freshness`` and ``validation-score`` record the window applied at
          run time in ``time_window_sec`` … alongside ``last_event_at`` (freshness) or
          ``latest_data_time`` + ``score`` (validation-score)".
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "Each run
          records the window it applied in the breakdown's ``detail.time_window_sec``".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detailshape,DEV)"

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db([]),
    )

    verdict = _verdict(verdicts, urn)
    assert verdict.met is False, "backstop: this dataset must actually have failed"
    detail = verdict.detail
    assert set(detail) == {"latest_data_time", "score", "time_window_sec"}, (
        "Failed detail keys must be exactly {latest_data_time, score, "
        "time_window_sec}; got " + f"{sorted(detail)}. "
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
