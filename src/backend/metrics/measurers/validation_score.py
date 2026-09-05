"""Measurer: validation-score — counts datasets whose current validation state is passing.

Two counts: ``valid_confd`` (of the datasets ``dataset_filter`` matched, the ones carrying a
``validation_configs`` row) and ``valid_in_time`` (of *those*, the ones that pass the
per-dataset test below). Neither is a score sum, so ``valid_in_time / valid_confd`` reads as a
pass rate over the configured estate. The scanned-dataset count (every dataset matched by
``dataset_filter``, configured or not) is not carried in ``values`` for this metric type — it
is available independently via ``breakdown.dataset_count``.

**The per-dataset test**: take the dataset's single latest validation result overall — the
newest row by ``data_time`` (ties broken by ``ingestion_time`` descending, so a re-posted
result for the same ``data_time`` supersedes the one it replaced), chosen without regard to
any window — then ask whether *that* result lies inside the dataset's cadence-anchored window
and scored ``>= 1.0``. Both
conditions must hold. Searching backwards for a qualifying row instead would let a
superseded result stand in for the newest one, so a dataset could pass on evidence it has
itself already replaced.

**The window** is ``metric_conf["time_window_sec"]`` wide — the SLO the governance lead
declares, the same for every dataset in the run — but anchored per dataset on that
dataset's own declared arrival lag, read from ``validation_configs.attribute``::

    upper_bound = now - (cadence_offset * cadence_unit)
    lower_bound = upper_bound - time_window_sec
    in_window   = lower_bound <= data_time <= upper_bound

Both bounds are inclusive. The shift exists because a dataset whose D-8 partition is the
freshest one that can exist is not stale for lacking a D-1 result; anchoring on the
measurement instant alone would mark every lagged dataset failing at exactly its declared
lag.

A dataset with **no** validation config is not evaluated at all: it gets no verdict, which
reads as ``met = "unknown"`` on ``GET .../dataset``. It has declared no cadence and promised
no result, so counting it as failing would let a filter widen its way to a falling metric
with nothing having changed about the datasets that do participate.

Spec: spec/feature/BACKEND.md §Metrics Service — Measurement window, Cadence-anchored
window, `validation-score` counts and the unconfigured set
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import DatasetVerdict, register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DEFAULT_VALIDATION_ATTRIBUTE,
    ValidationConfig,
    ValidationResult,
)


def _window_bounds(
    now: datetime, attribute: dict[str, Any], window_sec: int
) -> tuple[datetime, datetime, int, int]:
    """Return ``(lower_bound, upper_bound, cadence_unit, cadence_offset)``.

    The stored ``attribute`` is a complete object by column contract; the
    per-field defaults are applied anyway, so a row **missing** a key still yields
    a window. That is the whole of the guarantee: a default fills an absent key,
    it does not repair a present one.

    The shift's bound — ``cadence_offset * cadence_unit <=``
    :data:`~src.shared.metric_conf.MAX_TIME_WINDOW_SEC` — is enforced at the write
    boundary only, by :class:`src.api.schemas.validation.ValidationAttribute`.
    ``validation_configs.attribute`` is plain JSONB with no column constraint, the
    same as ``metric_conf``, so a row carrying an out-of-range or wrong-typed
    cadence (written by something other than the API) makes every run of the
    metric fail rather than being silently clamped; it is repaired by ``PUT``/
    ``PATCH``ing a valid ``attribute``, not by a migration. This function carries
    no second copy of the bound — a duplicated bound would be free to diverge from
    the one the write boundary enforces.

    Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds;
    spec/feature/VALIDATION.md §Rule Configuration.
    """
    cadence_unit = int(attribute.get("cadence_unit", DEFAULT_VALIDATION_ATTRIBUTE["cadence_unit"]))
    cadence_offset = int(
        attribute.get("cadence_offset", DEFAULT_VALIDATION_ATTRIBUTE["cadence_offset"])
    )
    upper_bound = now - timedelta(seconds=cadence_offset * cadence_unit)
    lower_bound = upper_bound - timedelta(seconds=window_sec)
    return lower_bound, upper_bound, cadence_unit, cadence_offset


@register_measurer("validation-score")
async def measure(
    datasets: list[str],
    metric_conf: dict[str, Any],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
    now: datetime,
) -> tuple[dict[str, float], list[DatasetVerdict]]:
    """Return validation-score values and one verdict per **configured** dataset.

    Parameters
    ----------
    datasets:
        Dataset URNs in the metric's scope.
    metric_conf:
        Must contain ``time_window_sec`` — the window *width* in seconds, applied
        to every dataset. The write boundary admits only a positive int up to
        :data:`src.shared.metric_conf.MAX_TIME_WINDOW_SEC`; this measurer holds no
        copy of that bound and trusts ``metric_conf`` by contract.
    datahub:
        DataHubClient — accepted for signature uniformity; this measurer stays
        DataSpoke-DB-side and makes no DataHub call.
    db:
        Async SQLAlchemy session for querying ``validation_configs`` and
        ``validation_results``.
    now:
        The run's measurement instant, from which each dataset's window is
        anchored back by its own declared arrival lag.

    Returns
    -------
    tuple[dict[str, float], list[DatasetVerdict]]
        ``(values, verdicts)`` where values has keys ``valid_confd`` and
        ``valid_in_time``. Verdicts cover the **configured** subset only — a
        dataset with no ``validation_configs`` row is left verdict-less so it
        reads ``unknown`` rather than failing. ``evidence_at`` is the **counted**
        result's ``data_time`` and ``None`` when nothing was counted (no result at
        all, or a latest result outside the window), so ``last_check_at`` falls
        back to the run's ``measured_at`` — the honest reading: this metric
        checked the dataset just now and found nothing inside its window. The
        stale validation date stays available in ``detail.latest_data_time``.
    """
    if not datasets:
        return ({"valid_confd": 0.0, "valid_in_time": 0.0}, [])

    window_sec = int(metric_conf["time_window_sec"])

    # ── 1. Which datasets carry a validation conf, and with what cadence ──────
    # The conf row is both the participation gate and the window anchor, so the
    # attribute travels with it in one query.
    conf_q = select(ValidationConfig.dataset_urn, ValidationConfig.attribute).where(
        ValidationConfig.dataset_urn.in_(datasets)
    )
    attribute_by_urn: dict[str, dict[str, Any]] = {
        row.dataset_urn: dict(row.attribute or DEFAULT_VALIDATION_ATTRIBUTE)
        for row in (await db.execute(conf_q)).all()
    }
    # Iterate the scope, not the map, so verdict order follows the scan order.
    configured = [urn for urn in datasets if urn in attribute_by_urn]
    if not configured:
        return ({"valid_confd": 0.0, "valid_in_time": 0.0}, [])

    # ── 2. Fetch the latest ValidationResult row per configured dataset ───────
    # No window predicate here on purpose: the test is about the dataset's newest
    # result, and filtering first would let an older in-window row stand in for
    # it. One query — row_number() partitioned by dataset_urn, ordered by
    # data_time desc then ingestion_time desc (last-write-wins on a re-posted
    # data_time), filtered to rn == 1.
    sub = (
        select(
            ValidationResult.dataset_urn,
            ValidationResult.data_time,
            ValidationResult.score,
            func.row_number()
            .over(
                partition_by=ValidationResult.dataset_urn,
                order_by=(
                    ValidationResult.data_time.desc(),
                    ValidationResult.ingestion_time.desc(),
                ),
            )
            .label("rn"),
        )
        .where(ValidationResult.dataset_urn.in_(configured))
        .subquery()
    )
    rows_q = select(
        sub.c.dataset_urn,
        sub.c.data_time,
        sub.c.score,
    ).where(sub.c.rn == 1)
    rows = (await db.execute(rows_q)).all()

    latest: dict[str, tuple[datetime, float]] = {
        row.dataset_urn: (row.data_time, row.score) for row in rows
    }

    # ── 3. Evaluate the configured subset ─────────────────────────────────────
    valid_in_time = 0
    verdicts: list[DatasetVerdict] = []

    for urn in configured:
        lower_bound, upper_bound, cadence_unit, cadence_offset = _window_bounds(
            now, attribute_by_urn[urn], window_sec
        )
        base_detail: dict[str, Any] = {
            "time_window_sec": window_sec,
            "cadence_unit": cadence_unit,
            "cadence_offset": cadence_offset,
            "lower_bound": lower_bound.isoformat(),
            "upper_bound": upper_bound.isoformat(),
        }

        latest_row = latest.get(urn)
        if latest_row is None:
            # Configured but has never reported — evaluated and failing, with no
            # evidence to date the check by.
            verdicts.append(
                DatasetVerdict(
                    urn=urn,
                    met=False,
                    evidence_at=None,
                    detail={**base_detail, "latest_data_time": None, "score": None},
                )
            )
            continue

        latest_data_time, latest_score = latest_row
        # Both bounds inclusive: evidence landing exactly on either bound is in
        # window (spec/feature/BACKEND.md §Boundary is inclusive).
        in_window = lower_bound <= latest_data_time <= upper_bound
        met = in_window and latest_score >= 1.0
        if met:
            valid_in_time += 1

        verdicts.append(
            DatasetVerdict(
                urn=urn,
                met=met,
                # Only a counted result dates the check. An out-of-window row is
                # not evidence this window, so evidence_at stays None and
                # last_check_at falls back to the run's measured_at.
                evidence_at=latest_data_time if in_window else None,
                detail={
                    **base_detail,
                    # Always the latest result's data_time, in window or not, so
                    # the stale validation date stays readable on a failing row.
                    "latest_data_time": latest_data_time.isoformat(),
                    "score": latest_score,
                },
            )
        )

    values: dict[str, float] = {
        "valid_confd": float(len(configured)),
        "valid_in_time": float(valid_in_time),
    }
    return values, verdicts
