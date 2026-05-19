"""Measurer: ingestion-freshness — counts datasets ingested within the time window.

A dataset is counted as *ingested in time* if its latest ``INGESTION.COMPLETE``
event occurred within ``metric_conf["time_window_sec"]`` seconds of now.
Datasets with no event in that window (or no event at all) are stale.

Spec: spec/feature/BACKEND.md §Metrics Service — built-in active metric types
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event
from src.shared.events import INGESTION_COMPLETE


@register_measurer("ingestion-freshness")
async def measure(
    datasets: list[str],
    metric_conf: dict[str, Any],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return ingestion-freshness values and a stale-dataset breakdown.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    metric_conf:
        Must contain ``time_window_sec`` (positive int).
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying the ``events`` table.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``ingested_in_time``; breakdown lists only stale datasets.
    """
    time_window_sec = int(metric_conf["time_window_sec"])
    cutoff: datetime = datetime.now(tz=UTC) - timedelta(seconds=time_window_sec)

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

    total = len(datasets)
    ingested_in_time = 0
    stale_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        last_event_at = latest.get(urn)
        if last_event_at is not None and last_event_at > cutoff:
            ingested_in_time += 1
        else:
            stale_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "last_event_at": last_event_at.isoformat() if last_event_at else None,
                    },
                }
            )

    values: dict[str, float] = {
        "total": float(total),
        "ingested_in_time": float(ingested_in_time),
    }
    breakdown: dict[str, Any] = {
        "dataset_count": total,
        "datasets": stale_datasets,
    }
    return values, breakdown
