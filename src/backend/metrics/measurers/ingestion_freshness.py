"""Measurer: ingestion-freshness — counts datasets ingested within a per-dataset window.

A dataset is counted as *ingested in time* if its latest ``INGESTION.COMPLETE``
event occurred within its resolved freshness window. The window is derived
per dataset from ``ingestion_configs``:

- active-custom with a known schedule_tier → SCHEDULE_TIER_SECONDS[tier] × LATE_INGESTION_FACTOR
- passive → PASSIVE_SYNC_PERIOD_SEC × LATE_INGESTION_FACTOR
- no config row, or active-custom with an unknown/null tier → metric_conf["time_window_sec"]

Spec: spec/feature/BACKEND.md §Metrics Service — Time windows
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, IngestionConfig
from src.shared.events import INGESTION_COMPLETE
from src.shared.schedule import (
    LATE_INGESTION_FACTOR,
    PASSIVE_SYNC_PERIOD_SEC,
    SCHEDULE_TIER_SECONDS,
)


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
        Must contain ``time_window_sec`` (positive int) used as the fallback
        window when no per-dataset config can be resolved.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``events`` and ``ingestion_configs``.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``ingested_in_time``; breakdown lists only stale datasets with
        ``last_event_at``, ``time_window_sec``, and ``window_source`` in detail.
    """
    default_window_sec = int(metric_conf["time_window_sec"])

    # ── 1. Resolve per-dataset window from ingestion_configs ──────────────────
    configs_q = select(
        IngestionConfig.dataset_urn,
        IngestionConfig.mode,
        IngestionConfig.schedule_tier,
    ).where(IngestionConfig.dataset_urn.in_(datasets))
    config_rows = (await db.execute(configs_q)).all()
    config_map: dict[str, tuple[str, str | None]] = {
        row.dataset_urn: (row.mode, row.schedule_tier) for row in config_rows
    }

    def _resolve_window(urn: str) -> tuple[int, str]:
        """Return (window_sec, window_source) for the given URN."""
        if urn not in config_map:
            return default_window_sec, "default"
        mode, tier = config_map[urn]
        if mode == "active-custom" and tier in SCHEDULE_TIER_SECONDS:
            return SCHEDULE_TIER_SECONDS[tier] * LATE_INGESTION_FACTOR, f"active-custom:{tier}"
        if mode == "passive":
            return PASSIVE_SYNC_PERIOD_SEC * LATE_INGESTION_FACTOR, "passive"
        return default_window_sec, "default"

    # ── 2. Fetch latest INGESTION.COMPLETE event per dataset ──────────────────
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

    # ── 3. Evaluate freshness per dataset ─────────────────────────────────────
    now = datetime.now(tz=UTC)
    total = len(datasets)
    ingested_in_time = 0
    stale_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        window_sec, window_source = _resolve_window(urn)
        cutoff = now - timedelta(seconds=window_sec)
        last_event_at = latest.get(urn)

        if last_event_at is not None and last_event_at > cutoff:
            ingested_in_time += 1
        else:
            stale_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "last_event_at": last_event_at.isoformat() if last_event_at else None,
                        "time_window_sec": window_sec,
                        "window_source": window_source,
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
