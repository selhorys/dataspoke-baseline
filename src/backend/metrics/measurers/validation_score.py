"""Measurer: validation-score — sums per-dataset validation scores within the metric's window.

The window is ``metric_conf["time_window_sec"]``, applied uniformly to every dataset in
the run: it is the recency SLO the governance lead declares, not a quantity read off a
dataset's own validation cadence.

The score counted is the latest ``ValidationResult`` row's ``score`` IFF its
``data_time`` falls within that window. The boundary is inclusive — a ``data_time`` of
exactly one window before the measurement instant (this measurer's own clock reading,
taken once per run) is in window, the same rule the ingestion-freshness measurer
applies. A dataset with no qualifying row contributes 0.0 and appears in the breakdown.

Spec: spec/feature/BACKEND.md §Metrics Service — Measurement window
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import DatasetVerdict, register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import ValidationResult


@register_measurer("validation-score")
async def measure(
    datasets: list[str],
    metric_conf: dict[str, Any],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[dict[str, float], list[DatasetVerdict]]:
    """Return validation-score values and one verdict per dataset in scope.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    metric_conf:
        Must contain ``time_window_sec`` — the measurement window in seconds,
        applied to every dataset. The write boundary admits only a positive int
        up to :data:`src.shared.metric_conf.MAX_TIME_WINDOW_SEC`; this measurer
        holds no copy of that bound and trusts ``metric_conf`` by contract.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``validation_results``.

    Returns
    -------
    tuple[dict[str, float], list[DatasetVerdict]]
        ``(values, verdicts)`` where values has keys ``total`` and
        ``validation_score_sum``. ``met`` is false for a dataset scoring < 1.0,
        holding no validation result at all, or whose latest result falls outside
        the window. ``evidence_at`` is the **counted** result's ``data_time`` and
        ``None`` when nothing was counted — no result at all, or a latest result
        outside the window. ``last_check_at`` then falls back to the run's
        ``measured_at``, which is the honest reading: this metric checked the
        dataset just now and found nothing inside its window. The stale
        validation date stays available in ``detail.latest_data_time``.
    """
    if not datasets:
        return ({"total": 0.0, "validation_score_sum": 0.0}, [])

    window_sec = int(metric_conf["time_window_sec"])
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(seconds=window_sec)

    # ── 1. Fetch the latest ValidationResult row per dataset ─────────────────
    # A single query: row_number() partitioned by dataset_urn ordered by data_time
    # desc, filtered to rn == 1 — one round trip for the whole dataset list.
    sub = (
        select(
            ValidationResult.dataset_urn,
            ValidationResult.data_time,
            ValidationResult.score,
            func.row_number()
            .over(
                partition_by=ValidationResult.dataset_urn,
                order_by=ValidationResult.data_time.desc(),
            )
            .label("rn"),
        )
        .where(ValidationResult.dataset_urn.in_(datasets))
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

    # ── 2. Evaluate per dataset ───────────────────────────────────────────────
    total = len(datasets)
    score_sum = 0.0
    verdicts: list[DatasetVerdict] = []

    for urn in datasets:
        latest_row = latest.get(urn)

        if latest_row is None:
            # No validation result at all — no evidence, so no evidence_at.
            verdicts.append(
                DatasetVerdict(
                    urn=urn,
                    met=False,
                    evidence_at=None,
                    detail={
                        "latest_data_time": None,
                        "score": None,
                        "time_window_sec": window_sec,
                    },
                )
            )
            continue

        latest_data_time, latest_score = latest_row
        in_window = latest_data_time >= cutoff
        if in_window:
            # In-window row found: accumulate score.
            score_sum += latest_score

        verdicts.append(
            DatasetVerdict(
                urn=urn,
                met=in_window and latest_score >= 1.0,
                # Only a counted result dates the check. An out-of-window row is
                # not evidence this window, so evidence_at stays None and
                # last_check_at falls back to the run's measured_at.
                evidence_at=latest_data_time if in_window else None,
                detail={
                    "latest_data_time": latest_data_time.isoformat(),
                    "score": latest_score,
                    "time_window_sec": window_sec,
                },
            )
        )

    values: dict[str, float] = {
        "total": float(total),
        "validation_score_sum": float(score_sum),
    }
    return values, verdicts
