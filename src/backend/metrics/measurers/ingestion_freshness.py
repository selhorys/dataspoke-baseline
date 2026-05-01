"""Measurer: pct_fresh — datasets categorised as fresh or stale.

A dataset is *fresh* if its latest ``INGESTION.COMPLETE`` event occurred within
the freshness window defined in the metric's ``measurement_query``
(``freshness_days``; default 1 day).  Otherwise it is *stale*.

Spec: spec/feature/BACKEND.md §Metrics Service — baseline measurers
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event
from src.shared.events import INGESTION_COMPLETE

_DEFAULT_FRESHNESS_DAYS = 1


@register_measurer("pct_fresh")
async def measure(
    datasets: list[str],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
    freshness_days: int = _DEFAULT_FRESHNESS_DAYS,
    **_kwargs: Any,
) -> tuple[float, dict[str, Any]]:
    """Return stale-dataset count and per-dataset freshness breakdown.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying the ``events`` table.
    freshness_days:
        Number of days within which a dataset must have a successful ingestion
        to be considered *fresh*.  Taken from ``measurement_query`` extras by
        the caller; defaults to 1.

    Returns
    -------
    tuple[float, dict]
        ``(stale_count, breakdown)`` where ``breakdown`` has keys
        ``dataset_count``, ``fresh_count``, ``stale_count``, and ``datasets``
        (list of per-dataset dicts with ``urn``, ``category``,
        ``last_ingested_at``).
    """
    cutoff: datetime = datetime.now(tz=UTC) - timedelta(days=freshness_days)

    # Fetch the latest INGESTION.COMPLETE event per dataset in one round-trip
    # using a window function (ROW_NUMBER) to get the most recent row per entity_id.

    # Build a subquery that picks the latest INGESTION.COMPLETE per entity_id.
    sub = (
        select(
            Event.entity_id,
            Event.occurred_at,
            func.row_number()
            .over(
                partition_by=Event.entity_id,
                order_by=Event.occurred_at.desc(),
            )
            .label("rn"),
        )
        .where(
            Event.event_type == INGESTION_COMPLETE,
            Event.entity_id.in_(datasets),
        )
        .subquery()
    )
    latest_q = select(sub.c.entity_id, sub.c.occurred_at).where(sub.c.rn == 1)
    rows = (await db.execute(latest_q)).all()
    latest: dict[str, datetime] = {row.entity_id: row.occurred_at for row in rows}

    per_dataset: list[dict[str, Any]] = []
    stale_count = 0

    for urn in datasets:
        last_ingested_at = latest.get(urn)
        if last_ingested_at is None or last_ingested_at < cutoff:
            category = "stale"
            stale_count += 1
        else:
            category = "fresh"

        # Fix #8: unified breakdown shape per spec BACKEND.md §Metrics Service
        per_dataset.append(
            {
                "urn": urn,
                "category": category,
                "detail": {
                    "last_event_at": last_ingested_at.isoformat()
                    if last_ingested_at
                    else None,
                },
            }
        )

    return float(stale_count), {
        "dataset_count": len(datasets),
        "fresh_count": len(datasets) - stale_count,
        "stale_count": stale_count,
        "datasets": per_dataset,
    }
