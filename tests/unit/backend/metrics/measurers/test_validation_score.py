"""Unit tests for the validation-score measurer.

Spec sources (quoted text is verbatim; ``…`` marks an elision):

  spec/feature/BACKEND.md §Metrics Service — "`validation-score` counts and the
  unconfigured set":
    - "the measurer emits three counts — `total` (datasets matched by `dataset_filter`),
      `valid_confd` (of those, the ones carrying a `validation_configs` row), and
      `valid_in_time` (of `valid_confd`, the ones that pass the per-dataset test defined
      above). All three are counts; none is a score sum".
    - "A dataset with **no** validation config gets **no verdict row** — it is neither
      `met = true` nor `met = false`."
    - "`validation-score`'s verdict list is a **subset** of its scan."

  spec/feature/BACKEND.md §Metrics Service — "The `validation-score` per-dataset test":
    - "take the dataset's **single latest validation result overall** — the newest row by
      `data_time`, chosen without regard to any window — then ask whether *that* result
      lies inside the dataset's cadence-anchored window and scored `>= 1.0`. Both
      conditions must hold."
    - "A dataset therefore fails when its latest result's `data_time` falls outside the
      window, or falls inside it but scored `< 1.0`, or it has no validation result at
      all."

  spec/feature/BACKEND.md §Metrics Service — "Cadence-anchored window":
    - ``upper_bound = instant - (cadence_offset * cadence_unit)``;
      ``lower_bound = upper_bound - time_window_sec``;
      ``in_window = lower_bound <= data_time <= upper_bound``.
    - "With `offset = 0` this is the plain trailing window
      `[instant - time_window_sec, instant]`; with `unit = 86400, offset = 1` it is
      `[instant - 259200, instant - 86400]` at the factory `time_window_sec` of `172800`."

  spec/feature/BACKEND.md §Metrics Service — "Boundary is inclusive": "evidence whose
  instant is exactly one window width from the bound is *in* window — the comparisons are
  `>= lower_bound` and `<= upper_bound`, never strict."

  spec/feature/BACKEND.md §Metrics Service — "Measurement instant": "one clock reading per
  run, computed by the service before it dispatches and passed to every measurer". The
  tests pass it in as ``now=`` rather than patching a clock.

  spec/feature/BACKEND.md §Metrics Service — Verdict contract: verdicts carry
  ``urn``/``met``/``evidence_at``/``detail``; for `validation-score` the evaluated set "is
  the configured subset, and the unconfigured remainder is deliberately left verdict-less
  so it reads `unknown`".

  spec/API.md §Metric: "`last_check_at` is the per-dataset evidence timestamp
  (… `validation-score`: the counted result's `data_time`), falling back to the run's
  `measured_at` … a `validation-score` dataset whose latest result fell outside its window
  counted nothing, so it reports the run time too."

  spec/feature/VALIDATION.md §Rule Configuration: `attribute` defaults are
  ``{"cadence_unit": 86400, "cadence_offset": 0}``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers import validation_score  # noqa: F401 — triggers registration
from src.shared.datahub.client import DataHubClient
from tests.unit.conftest import route_db_execute

#: The run's measurement instant, handed to the measurer as ``now=``. Every seeded
#: ``data_time`` below is an offset from this instant, so each window bound lands on an
#: exact second and the inclusive-boundary tests can sit on it.
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: The factory default of ``validation_configs.attribute`` — daily data with no lag
#: (spec/feature/VALIDATION.md §Rule Configuration).
_DAILY_NO_LAG = {"cadence_unit": 86400, "cadence_offset": 0}


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("validation-score")
    assert fn is not None, "validation-score measurer must be registered"
    return fn


def _verdict(verdicts, urn):
    """The one verdict for *urn* — an evaluated dataset carries exactly one.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract.
    """
    matches = [v for v in verdicts if v.urn == urn]
    assert len(matches) == 1, f"expected exactly one verdict for {urn}; got {matches!r}"
    return matches[0]


def _failed(verdicts):
    """The `met = false` subset — the entries the derived breakdown lists."""
    return [v for v in verdicts if not v.met]


def _result_row(urn: str, data_time: datetime, score: float) -> MagicMock:
    """One latest-per-dataset ``validation_results`` row as the measurer reads it."""
    row = MagicMock()
    row.dataset_urn = urn
    row.data_time = data_time
    row.score = score
    return row


def _conf_row(urn: str, attribute: dict[str, Any] | None) -> MagicMock:
    """One ``validation_configs`` row as the measurer reads it (urn + attribute)."""
    row = MagicMock()
    row.dataset_urn = urn
    row.attribute = attribute
    return row


def _db(
    *,
    confs: list[MagicMock] | None = None,
    results: list[MagicMock] | None = None,
) -> AsyncMock:
    """Mock session routing the measurer's two reads **by the table each names**.

    The measurer issues a ``validation_configs`` read (participation gate + window
    anchor) and a ``validation_results`` read (latest row per configured dataset). They
    are routed by their compiled SQL, never by call position, so an added, reordered or
    short-circuited query fails loudly instead of silently returning the other query's
    rows (spec/TESTING.md §Unit Testing → Mocking rules).

    ``spec=AsyncSession`` so a renamed or mistyped session method fails loud instead of
    returning a fresh auto-mock.
    """
    conf_result = MagicMock()
    conf_result.all.return_value = list(confs or [])
    results_result = MagicMock()
    results_result.all.return_value = list(results or [])

    db = AsyncMock(spec=AsyncSession)
    route_db_execute(
        db,
        [
            ("validation_configs", conf_result),
            ("validation_results", results_result),
        ],
    )
    return db


def _datahub() -> MagicMock:
    """A spec'd DataHubClient stand-in.

    The measurer accepts one for signature uniformity and makes no DataHub call — it is
    contractually "pure-aggregation and DataSpoke-DB-side" (spec/feature/BACKEND.md
    §Metrics Service). This exists only to fail loudly if it ever starts making one:
    ``spec=`` turns a call to a method DataHubClient does not have into an
    ``AttributeError`` instead of a silently successful auto-mock.
    """
    return MagicMock(spec=DataHubClient)


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
async def test_empty_datasets_returns_zero_counts_without_querying() -> None:
    """measure([]) returns all three counts at 0.0 and issues no query.

    ``total`` is the count of datasets matched by ``dataset_filter``, which is zero here,
    so there is nothing to look up — the ``db.execute`` assertion is what makes "no
    dataset in scope" distinguishable from "every dataset failed".

    Spec: spec/feature/BACKEND.md §Metrics Service — "`validation-score` counts and the
          unconfigured set": the three emitted counts are ``total``, ``valid_confd``
          and ``valid_in_time``.
    """
    measure = _get_measurer()
    db = _db()

    values, verdicts = await measure(
        datasets=[],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=db,
        now=_NOW,
    )

    assert values == {"total": 0.0, "valid_confd": 0.0, "valid_in_time": 0.0}
    assert verdicts == []
    db.execute.assert_not_awaited()


# ── The three counts over a mixed scope ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_three_counts_are_nested_dataset_counts_over_a_mixed_scope() -> None:
    """total / valid_confd / valid_in_time are counts of datasets, not a score sum.

    The scope mixes every state the spec distinguishes, so no single wrong rule survives
    it: an unconfigured dataset (in ``total`` only, and *verdict-less*), a configured
    dataset whose latest result is in window and passing, one in window but scoring
    ``0.7``, one whose only result predates the window, and one that has never reported.

    The 0.7 dataset is the discriminator against the old score-sum reading: a sum would
    make ``valid_in_time`` 1.7, while the count is 1.

    Spec: spec/feature/BACKEND.md §Metrics Service — "`validation-score` counts and the
          unconfigured set": "`total` (datasets matched by `dataset_filter`),
          `valid_confd` (of those, the ones carrying a `validation_configs` row), and
          `valid_in_time` (of `valid_confd`, the ones that pass the per-dataset test
          defined above). All three are counts; none is a score sum".
    Spec: §"The `validation-score` per-dataset test" — a dataset fails when its latest
          result "falls outside the window, or falls inside it but scored `< 1.0`, or it
          has no validation result at all".
    """
    measure = _get_measurer()
    unconfigured = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unconfigured,DEV)"
    passing = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.passing,DEV)"
    low_score = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.low_score,DEV)"
    stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.stale,DEV)"
    never_reported = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.never_reported,DEV)"

    values, verdicts = await measure(
        datasets=[unconfigured, passing, low_score, stale, never_reported],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_db(
            confs=[
                _conf_row(passing, _DAILY_NO_LAG),
                _conf_row(low_score, _DAILY_NO_LAG),
                _conf_row(stale, _DAILY_NO_LAG),
                _conf_row(never_reported, _DAILY_NO_LAG),
            ],
            results=[
                _result_row(passing, _NOW - timedelta(hours=1), 1.0),
                _result_row(low_score, _NOW - timedelta(hours=2), 0.7),
                # 172800s window, offset 0 → lower bound is _NOW - 48h.
                _result_row(stale, _NOW - timedelta(hours=60), 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values == {"total": 5.0, "valid_confd": 4.0, "valid_in_time": 1.0}, (
        "five datasets scanned, four configured, one passing the per-dataset test. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — `validation-score` counts."
    )
    # Membership, not order: the spec fixes which datasets carry a verdict, not the
    # sequence they come back in. len() alongside the set rules out a duplicate URN
    # collapsing into the set unnoticed.
    assert len(verdicts) == 4, f"one verdict per configured dataset; got {len(verdicts)}"
    assert {v.urn for v in verdicts} == {passing, low_score, stale, never_reported}, (
        "verdicts cover the configured subset and leave the unconfigured "
        f"dataset out entirely; got {[v.urn for v in verdicts]!r}"
    )
    assert len(_failed(verdicts)) == 3
    assert {v.urn for v in _failed(verdicts)} == {low_score, stale, never_reported}
    assert _verdict(verdicts, passing).met is True


@pytest.mark.asyncio
async def test_an_unconfigured_dataset_gets_no_verdict_at_all() -> None:
    """A dataset with no validation config is neither met nor failed — it has no verdict.

    Both halves are asserted: it counts toward ``total`` (so it was genuinely scanned and
    not dropped from scope) yet contributes no verdict row, which is what makes it read
    ``met = "unknown"`` on ``GET .../dataset`` rather than failing for a cadence it never
    declared. The configured sibling is the backstop: it proves the measurer does emit
    verdicts on this call, so the absence above is about the unconfigured dataset and not
    about a measurer that returned nothing.

    Spec: spec/feature/BACKEND.md §Metrics Service — "A dataset with **no** validation
          config gets **no verdict row** — it is neither `met = true` nor `met = false`.
          … The absent verdict surfaces through the existing `unknown` path".
    """
    measure = _get_measurer()
    unconfigured = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.no_conf,DEV)"
    configured = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.has_conf,DEV)"

    values, verdicts = await measure(
        datasets=[unconfigured, configured],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(configured, _DAILY_NO_LAG)],
            results=[_result_row(configured, _NOW - timedelta(hours=1), 1.0)],
        ),
        now=_NOW,
    )

    assert values["total"] == 2.0, "the unconfigured dataset is still in scope"
    assert values["valid_confd"] == 1.0, "…but not in the configured subset"
    assert [v.urn for v in verdicts] == [configured], (
        "backstop + assertion: the configured dataset carries a verdict and the "
        f"unconfigured one carries none; got {[v.urn for v in verdicts]!r}"
    )


@pytest.mark.asyncio
async def test_a_scope_with_no_configured_dataset_reports_total_only() -> None:
    """With nothing configured, `total` still counts the scan and no verdict is emitted.

    The count that matters is ``valid_confd = 0`` against a non-zero ``total``: that pair
    is what surfaces "the estate has zero validation coverage" rather than "the estate is
    empty".

    Spec: spec/feature/BACKEND.md §Metrics Service — "`valid_confd` is what makes that
          bucket's size a first-class value on the result row."
    """
    measure = _get_measurer()
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"

    values, verdicts = await measure(
        datasets=[urn_a, urn_b],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        # A result row exists but no conf does: the conf row is the participation gate,
        # so the dataset is still not evaluated.
        db=_db(confs=[], results=[_result_row(urn_a, _NOW, 1.0)]),
        now=_NOW,
    )

    assert values == {"total": 2.0, "valid_confd": 0.0, "valid_in_time": 0.0}
    assert verdicts == []


# ── The per-dataset test: latest result overall, then window + score ──────────


@pytest.mark.asyncio
async def test_a_configured_dataset_with_no_result_is_evaluated_and_failing() -> None:
    """A configured dataset that has never reported fails, with no evidence to date it by.

    Its detail carries a null ``latest_data_time`` and ``score``: there is no result to
    report, which is a different fact from a result that scored zero.

    Spec: spec/feature/BACKEND.md §Metrics Service — "A dataset therefore fails when …
          it has no validation result at all."
    Spec: spec/API.md §Metric — `last_check_at` "falls back to the run's `measured_at`"
          when nothing was counted, which is what ``evidence_at = None`` expresses.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.novalidation,DEV)"

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(confs=[_conf_row(urn, _DAILY_NO_LAG)], results=[]),
        now=_NOW,
    )

    assert values == {"total": 1.0, "valid_confd": 1.0, "valid_in_time": 0.0}
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["latest_data_time"] is None
    assert verdict.detail["score"] is None
    assert verdict.evidence_at is None


@pytest.mark.asyncio
async def test_an_in_window_result_below_one_fails_but_still_dates_the_check() -> None:
    """An in-window result scoring < 1.0 fails the criterion yet counts as evidence.

    The two halves are independent: ``met`` turns on the score while ``evidence_at``
    turns on the window, so a measurer that conflated them would either count the 0.7 as
    passing or drop its timestamp.

    Spec: spec/feature/BACKEND.md §Metrics Service — a dataset fails when its latest
          result "falls inside it but scored `< 1.0`".
    Spec: spec/API.md §Metric — `last_check_at` for `validation-score` is "the counted
          result's `data_time`".
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.partial,DEV)"
    data_time = _NOW - timedelta(hours=3)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(urn, _DAILY_NO_LAG)],
            results=[_result_row(urn, data_time, 0.7)],
        ),
        now=_NOW,
    )

    assert values["valid_confd"] == 1.0
    assert values["valid_in_time"] == 0.0, (
        "0.7 is inside the window but below 1.0, so the dataset does not count. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — the per-dataset test."
    )
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["score"] == 0.7
    assert verdict.detail["latest_data_time"] == data_time.isoformat()
    assert verdict.evidence_at == data_time, (
        "an in-window row was counted as evidence even though it failed the criterion. "
        "Spec: spec/API.md §Metric — '`validation-score`: the counted result's "
        "`data_time`'."
    )


@pytest.mark.asyncio
async def test_an_out_of_window_result_fails_and_leaves_evidence_at_unset() -> None:
    """A dataset whose latest result predates the window fails with no evidence instant.

    The latest row's ``score`` is 0.9, so a measurer that ignored ``data_time`` would
    report it as in-window evidence — the ``evidence_at`` assertion is what proves the
    window gate ran, and ``detail.latest_data_time`` still surfaces how stale it is.

    Spec: spec/feature/BACKEND.md §Metrics Service — a dataset fails when its latest
          result's "`data_time` falls outside the window".
    Spec: spec/API.md §Metric — "a `validation-score` dataset whose latest result fell
          outside its window counted nothing, so it reports the run time too."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.old,DEV)"
    outside = _NOW - timedelta(seconds=86400 + 3600)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(urn, _DAILY_NO_LAG)],
            results=[_result_row(urn, outside, 0.9)],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 0.0
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["latest_data_time"] == outside.isoformat(), (
        "the out-of-window row is still reported so the reader can see how stale it is."
    )
    assert verdict.detail["score"] == 0.9
    assert verdict.evidence_at is None, (
        "nothing was counted this window, so there is no evidence timestamp and "
        "last_check_at falls back to the run's measured_at. "
        "Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_one_declared_width_splits_two_datasets_in_the_same_run() -> None:
    """A single run's window admits one dataset and excludes the other.

    Both directions of the gate are exercised in one call against one declared width and
    one shared cadence, so neither verdict can be a constant.

    **Scope**: this does *not* establish that the measurer picks the *latest* row per
    dataset. With one row per dataset the fixture cannot tell "the newest row was
    chosen" from "some row was chosen"; that pick needs several rows and real PostgreSQL
    ``row_number()``, so it is carried by
    ``tests/integration/spot/test_metrics.py``.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — the window's
          width "is `metric_conf.time_window_sec`, the same for every dataset in the run".
    """
    measure = _get_measurer()
    outside_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.window.outside,DEV)"
    inside_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.window.inside,DEV)"

    values, verdicts = await measure(
        datasets=[outside_urn, inside_urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[
                _conf_row(outside_urn, _DAILY_NO_LAG),
                _conf_row(inside_urn, _DAILY_NO_LAG),
            ],
            results=[
                _result_row(outside_urn, _NOW - timedelta(hours=30), 1.0),
                _result_row(inside_urn, _NOW - timedelta(hours=2), 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["total"] == 2.0
    assert values["valid_in_time"] == 1.0, (
        "only the dataset whose latest result is inside the declared 86400s window "
        "counts. Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
    )
    assert [v.urn for v in _failed(verdicts)] == [outside_urn]
    assert _verdict(verdicts, outside_urn).detail["time_window_sec"] == 86400


# ── Cadence-anchored window: the spec's two worked examples ──────────────────


@pytest.mark.asyncio
async def test_offset_zero_is_the_plain_trailing_window() -> None:
    """`unit=86400, offset=0` with `time_window_sec=172800` gives `[-172800, 0]`.

    Reproduces the spec's first worked example. Three rows bracket it: one just inside
    the lower bound, one at the instant itself (the upper bound), and one just outside
    the lower bound. The reported bounds are asserted too, so a window computed with the
    right *width* but the wrong anchor still fails.

    Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window: "With
          `offset = 0` this is the plain trailing window
          `[instant - time_window_sec, instant]`".
    """
    measure = _get_measurer()
    inside = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad0.inside,DEV)"
    at_now = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad0.at_now,DEV)"
    too_old = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad0.too_old,DEV)"
    attribute = {"cadence_unit": 86400, "cadence_offset": 0}

    values, verdicts = await measure(
        datasets=[inside, at_now, too_old],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_db(
            confs=[
                _conf_row(inside, attribute),
                _conf_row(at_now, attribute),
                _conf_row(too_old, attribute),
            ],
            results=[
                _result_row(inside, _NOW - timedelta(seconds=172799), 1.0),
                _result_row(at_now, _NOW, 1.0),
                _result_row(too_old, _NOW - timedelta(seconds=172801), 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 2.0
    assert [v.urn for v in _failed(verdicts)] == [too_old], (
        "only the row past the lower bound falls out of the trailing window"
    )
    detail = _verdict(verdicts, inside).detail
    assert detail["lower_bound"] == (_NOW - timedelta(seconds=172800)).isoformat()
    assert detail["upper_bound"] == _NOW.isoformat(), (
        "with offset 0 the upper bound is the measurement instant itself. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window."
    )


@pytest.mark.asyncio
async def test_offset_one_shifts_the_window_back_by_one_cadence_unit() -> None:
    """`unit=86400, offset=1` with `time_window_sec=172800` gives `[-259200, -86400]`.

    Reproduces the spec's second worked example, and this is the case that separates a
    shifted window from a merely wider one: a result 1 hour old — comfortably inside the
    *unshifted* window — is now **too new** and falls out, while a 48-hour-old result
    that a trailing window would also have admitted stays in. Only a genuine shift
    produces that pair.

    Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window: "with
          `unit = 86400, offset = 1` it is `[instant - 259200, instant - 86400]` at the
          factory `time_window_sec` of `172800`."
    """
    measure = _get_measurer()
    too_new = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad1.too_new,DEV)"
    in_shifted = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad1.in_shifted,DEV)"
    too_old = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.cad1.too_old,DEV)"
    attribute = {"cadence_unit": 86400, "cadence_offset": 1}

    values, verdicts = await measure(
        datasets=[too_new, in_shifted, too_old],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_db(
            confs=[
                _conf_row(too_new, attribute),
                _conf_row(in_shifted, attribute),
                _conf_row(too_old, attribute),
            ],
            results=[
                _result_row(too_new, _NOW - timedelta(hours=1), 1.0),
                _result_row(in_shifted, _NOW - timedelta(hours=48), 1.0),
                _result_row(too_old, _NOW - timedelta(seconds=259201), 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 1.0
    # Membership, not order — the spec fixes which datasets fail, not their sequence.
    assert len(_failed(verdicts)) == 2
    assert {v.urn for v in _failed(verdicts)} == {too_new, too_old}, (
        "the shifted window excludes results that are too *new* as well as too old — "
        "the property a merely wider trailing window cannot produce. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window."
    )
    detail = _verdict(verdicts, in_shifted).detail
    assert detail["lower_bound"] == (_NOW - timedelta(seconds=259200)).isoformat()
    assert detail["upper_bound"] == (_NOW - timedelta(seconds=86400)).isoformat()


@pytest.mark.asyncio
async def test_two_cadences_in_one_run_get_two_different_windows() -> None:
    """One run's declared width anchors per dataset on each dataset's own cadence.

    Both datasets carry the same 24-hour-old result and the same declared width; only
    their cadences differ, so the split can come from nothing but the per-dataset anchor.
    This is what a single run-wide window cannot reproduce.

    Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window: "the
          window's upper bound is shifted back by the dataset's own declared arrival lag,
          read from `validation_configs.attribute`"; the shift exists so "a dataset whose
          D-8 partition is the freshest one that can exist is not stale for lacking a D-1
          result".
    """
    measure = _get_measurer()
    daily = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.daily,DEV)"
    lagged = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.lagged_d8,DEV)"
    data_time = _NOW - timedelta(hours=24)

    values, verdicts = await measure(
        datasets=[daily, lagged],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[
                _conf_row(daily, {"cadence_unit": 86400, "cadence_offset": 0}),
                # D-8 data: the freshest result that can exist is seven days old.
                _conf_row(lagged, {"cadence_unit": 86400, "cadence_offset": 7}),
            ],
            results=[
                _result_row(daily, data_time, 1.0),
                _result_row(lagged, data_time, 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 1.0
    assert _verdict(verdicts, daily).met is True, (
        "a 24h-old result sits on the daily dataset's trailing 86400s window"
    )
    assert _verdict(verdicts, lagged).met is False, (
        "the same result is far newer than the D-8 dataset's shifted window, whose "
        "upper bound is seven days back. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window."
    )
    lagged_detail = _verdict(verdicts, lagged).detail
    assert lagged_detail["upper_bound"] == (_NOW - timedelta(days=7)).isoformat()
    assert lagged_detail["lower_bound"] == (
        _NOW - timedelta(days=7) - timedelta(seconds=86400)
    ).isoformat()


@pytest.mark.asyncio
async def test_data_time_exactly_on_either_bound_is_in_window() -> None:
    """Both window bounds are inclusive — a result on either one is in window.

    The boundary is settled by spec rather than observable from a run: "the reading is
    microsecond-resolution, so a stored timestamp landing on it exactly is a measure-zero
    event". This test and ``test_ingestion_freshness.py::test_event_exactly_at_cutoff_is_fresh``
    are the only places the comparison is exercised at the instant that distinguishes
    ``>=``/``<=`` from ``>``/``<``. The one-second-outside pair below brackets it.

    Spec: spec/feature/BACKEND.md §Metrics Service — "Boundary is inclusive, for both
          measurers: evidence whose instant is exactly one window width from the bound is
          *in* window — the comparisons are `>= lower_bound` and `<= upper_bound`, never
          strict."
    """
    measure = _get_measurer()
    on_lower = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bound.lower,DEV)"
    on_upper = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bound.upper,DEV)"
    attribute = {"cadence_unit": 3600, "cadence_offset": 1}
    # upper_bound = _NOW - 3600; lower_bound = upper_bound - 7200.
    upper_bound = _NOW - timedelta(seconds=3600)
    lower_bound = upper_bound - timedelta(seconds=7200)

    values, verdicts = await measure(
        datasets=[on_lower, on_upper],
        metric_conf={"time_window_sec": 7200},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(on_lower, attribute), _conf_row(on_upper, attribute)],
            results=[
                _result_row(on_lower, lower_bound, 1.0),
                _result_row(on_upper, upper_bound, 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 2.0, (
        "a result landing exactly on either bound is in window. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — Boundary is inclusive."
    )
    assert _failed(verdicts) == []
    assert _verdict(verdicts, on_lower).evidence_at == lower_bound, (
        "backstop: the passing verdict must rest on the row seeded on the bound"
    )
    assert _verdict(verdicts, on_upper).evidence_at == upper_bound


@pytest.mark.asyncio
async def test_data_time_one_second_past_either_bound_is_out_of_window() -> None:
    """One second beyond either bound is outside the window, on any reading.

    The mirror of the inclusive-boundary test: the pair straddles each bound at
    one-second granularity, so a bound computed one second off in either direction moves
    one of the four datasets.

    Spec: spec/feature/BACKEND.md §Metrics Service — Cadence-anchored window:
          "in_window = lower_bound <= data_time <= upper_bound".
    """
    measure = _get_measurer()
    just_below = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bound.below,DEV)"
    just_above = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bound.above,DEV)"
    attribute = {"cadence_unit": 3600, "cadence_offset": 1}
    upper_bound = _NOW - timedelta(seconds=3600)
    lower_bound = upper_bound - timedelta(seconds=7200)

    values, verdicts = await measure(
        datasets=[just_below, just_above],
        metric_conf={"time_window_sec": 7200},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(just_below, attribute), _conf_row(just_above, attribute)],
            results=[
                _result_row(just_below, lower_bound - timedelta(seconds=1), 1.0),
                _result_row(just_above, upper_bound + timedelta(seconds=1), 1.0),
            ],
        ),
        now=_NOW,
    )

    assert values["valid_in_time"] == 0.0, (
        "neither a result one second before the lower bound nor one a second after the "
        "upper bound is in window."
    )
    # Membership, not order — the spec fixes which datasets fail, not their sequence.
    assert len(_failed(verdicts)) == 2
    assert {v.urn for v in _failed(verdicts)} == {just_below, just_above}
    assert _verdict(verdicts, just_above).detail["latest_data_time"] == (
        upper_bound + timedelta(seconds=1)
    ).isoformat(), "backstop: the seeded out-of-window row must be what the verdict rests on"


@pytest.mark.asyncio
async def test_a_stored_attribute_missing_a_key_falls_back_to_that_key_s_default() -> None:
    """A conf row missing a cadence key still yields a window, via the field's default.

    The column contract makes ``attribute`` complete, so this is about a row written
    outside the API. The default fills the *absent* key only — asserted by the reported
    bounds, which are the ones a `{cadence_unit: 86400, cadence_offset: 0}` object gives.

    Spec: spec/feature/VALIDATION.md §Rule Configuration — the `attribute` defaults are
          `cadence_unit = 86400` and `cadence_offset = 0`.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.partial_attribute,DEV)"

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 172800},
        datahub=_datahub(),
        db=_db(
            # cadence_offset absent — the row predates the column or was hand-written.
            confs=[_conf_row(urn, {"cadence_unit": 86400})],
            results=[_result_row(urn, _NOW - timedelta(hours=1), 1.0)],
        ),
        now=_NOW,
    )

    verdict = _verdict(verdicts, urn)
    assert verdict.met is True, "backstop: the dataset must actually have been evaluated"
    assert verdict.detail["cadence_unit"] == 86400
    assert verdict.detail["cadence_offset"] == 0
    assert verdict.detail["upper_bound"] == _NOW.isoformat(), (
        "the defaulted offset of 0 leaves the upper bound at the measurement instant"
    )


# ── Verdict shape ─────────────────────────────────────────────────────────────


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
        db=_db(confs=[_conf_row(urn, _DAILY_NO_LAG)], results=[]),
        now=_NOW,
    )

    from dataclasses import fields

    verdict = _verdict(verdicts, urn)
    assert {f.name for f in fields(verdict)} == {"urn", "met", "evidence_at", "detail"}
    assert verdict.met is False, "backstop: this dataset must actually have failed"


@pytest.mark.asyncio
async def test_verdict_detail_reports_the_window_and_the_cadence_that_shaped_it() -> None:
    """Verdict detail names the width, the cadence, both bounds, and the latest result.

    The key set is asserted as an equality so a reader can always reconstruct *why* a
    dataset was judged the way it was: the width alone no longer determines the window,
    so a detail carrying only ``time_window_sec`` would leave the verdict unexplainable.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — "`detail` is
          optional, type-specific metadata. `ingestion-freshness` and `validation-score`
          record the window width applied at run time in `time_window_sec` … alongside
          `last_event_at` (freshness) or `latest_data_time` + `score`
          (validation-score). `validation-score` additionally records the **resolved**
          window it evaluated the dataset against — `cadence_unit`, `cadence_offset`,
          and the computed `lower_bound` / `upper_bound` — because the width alone does
          not determine it". Those seven names are the whole enumerated set, which is
          why the assertion below is an equality.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.detailshape,DEV)"
    data_time = _NOW - timedelta(hours=2)

    _values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(urn, {"cadence_unit": 3600, "cadence_offset": 2})],
            results=[_result_row(urn, data_time, 0.5)],
        ),
        now=_NOW,
    )

    verdict = _verdict(verdicts, urn)
    assert verdict.met is False, "backstop: this dataset must actually have been judged"
    detail = verdict.detail
    assert set(detail) == {
        "time_window_sec",
        "cadence_unit",
        "cadence_offset",
        "lower_bound",
        "upper_bound",
        "latest_data_time",
        "score",
    }, f"unexpected detail key set: {sorted(detail)}"
    assert detail["time_window_sec"] == 86400
    assert detail["cadence_unit"] == 3600
    assert detail["cadence_offset"] == 2
    assert detail["upper_bound"] == (_NOW - timedelta(seconds=7200)).isoformat()
    assert detail["lower_bound"] == (_NOW - timedelta(seconds=7200 + 86400)).isoformat()
    assert detail["latest_data_time"] == data_time.isoformat()
    assert detail["score"] == 0.5


# ── The measurer holds no copy of the write boundary's window bound ──────────


@pytest.mark.asyncio
async def test_an_out_of_range_stored_window_fails_the_run_rather_than_being_clamped() -> None:
    """A window far past the write boundary's ceiling makes the run fail, not clamp.

    ``metric_conf`` is plain JSONB with no column constraint, so a row written by
    something other than the API can carry a window the write boundary would have
    rejected. The spec settles what a measurer does with one: it fails. A measurer that
    carried its own copy of the bound and clamped to it would return ordinary-looking
    counts here, computed against a window nobody declared — and no other test in this
    file would notice, because every one of them passes an admissible window.

    ``10**20`` seconds is past what the runtime's duration type can represent, so "fails"
    is observable as the ``OverflowError`` the arithmetic raises. The dataset list is
    non-empty and configured on purpose: both the empty-scope and the no-conf short
    circuits return before the window is ever used.

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
            db=_db(
                confs=[_conf_row(urn, _DAILY_NO_LAG)],
                results=[_result_row(urn, _NOW, 1.0)],
            ),
            now=_NOW,
        )


@pytest.mark.asyncio
async def test_an_out_of_range_stored_cadence_fails_the_run_rather_than_being_clamped() -> None:
    """An `attribute` past the write boundary's product bound fails the run too.

    ``validation_configs.attribute`` is plain JSONB with no ``CHECK``, exactly as
    ``metric_conf`` is, and the product bound (`cadence_offset * cadence_unit <=
    315_360_000`) lives at the API schema layer alone. The measurer holds no second copy,
    so a row written around the API surfaces as a failed run rather than a silently
    clamped window.

    Spec: spec/feature/VALIDATION.md §Rule Configuration — "`cadence_offset *
          cadence_unit` MUST be `<= 315,360,000` — the same product bound the
          `validation-score` window arithmetic applies to `time_window_sec`, so an
          accepted `attribute` can never make a governed window's arithmetic overflow."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unbounded.cadence,DEV)"

    with pytest.raises(OverflowError):
        await measure(
            datasets=[urn],
            metric_conf={"time_window_sec": 86400},
            datahub=_datahub(),
            db=_db(
                confs=[_conf_row(urn, {"cadence_unit": 10**18, "cadence_offset": 10**3})],
                results=[_result_row(urn, _NOW, 1.0)],
            ),
            now=_NOW,
        )


# ── The instant is the caller's, not the measurer's own clock ────────────────


@pytest.mark.asyncio
async def test_the_window_anchors_on_the_supplied_instant_not_wall_clock() -> None:
    """The window hangs off ``now=``, so a backlogged run measures its own interval.

    The instant is a year behind wall-clock and the result is dated relative to *it*. A
    measurer reading its own clock would compute a window a year later, in which this
    result sits far outside — so the pass is only reachable if the supplied instant is
    what anchored the window. The reported bounds pin it exactly.

    Spec: spec/feature/BACKEND.md §Metrics Service — Measurement instant: "A scheduled run
          therefore measures the interval it is *for*, not the interval it happened to
          execute in: a retried or backlogged `@daily` run yields the same verdicts as an
          on-time one."
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.backlogged,DEV)"
    backlogged_instant = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={"time_window_sec": 86400},
        datahub=_datahub(),
        db=_db(
            confs=[_conf_row(urn, _DAILY_NO_LAG)],
            results=[_result_row(urn, backlogged_instant - timedelta(hours=1), 1.0)],
        ),
        now=backlogged_instant,
    )

    assert values["valid_in_time"] == 1.0, (
        "the window must anchor on the supplied instant; against wall-clock this result "
        "would be a year stale."
    )
    detail = _verdict(verdicts, urn).detail
    assert detail["upper_bound"] == backlogged_instant.isoformat()
    assert detail["lower_bound"] == (
        backlogged_instant - timedelta(seconds=86400)
    ).isoformat()
