"""Measurer: ingestion-freshness — counts datasets ingested within a per-dataset window.

A dataset is counted as *ingested in time* if its latest ``INGESTION.COMPLETE``
event occurred within its resolved freshness window. The window is derived
per dataset from ``ingestion_source`` / ``ingestion_source_dataset``:

- ACTIVE_CUSTOM_MANAGED / DATAHUB_MANAGED with a known schedule_tier
  → SCHEDULE_TIER_SECONDS[tier] × LATE_INGESTION_FACTOR
- PASSIVE → PASSIVE_SYNC_PERIOD_SEC × LATE_INGESTION_FACTOR
- dataset mapped to no source, or source with no derivable schedule
  → metric_conf["time_window_sec"]

Spec: spec/feature/BACKEND.md §Metrics Service — Time windows
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, IngestionSource, IngestionSourceDataset
from src.shared.events import INGESTION_COMPLETE
from src.shared.models.ingestion import Mode
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
        window when no per-dataset source can be resolved.
    datahub:
        DataHubClient — accepted for signature uniformity, not used here.
    db:
        Async SQLAlchemy session for querying ``events``,
        ``ingestion_source_dataset``, and ``ingestion_source``.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``ingested_in_time``; breakdown lists only stale datasets with
        ``last_event_at``, ``time_window_sec``, and ``window_source`` in detail.
    """
    default_window_sec = int(metric_conf["time_window_sec"])

    # ── 1. Resolve per-dataset window from ingestion_source_dataset + ingestion_source ──
    #
    # Join ingestion_source_dataset -> ingestion_source to obtain (mode, schedule_tier)
    # for each dataset URN.  When a dataset appears under multiple sources, use
    # the highest-priority origin (emitted > pipeline_name > matcher) and then the
    # most-recent last_seen_at to pick one source row per dataset.
    _ORIGIN_PRIORITY = {"emitted": 0, "pipeline_name": 1, "matcher": 2}

    mapping_q = (
        select(
            IngestionSourceDataset.dataset_urn,
            IngestionSourceDataset.origin,
            IngestionSourceDataset.last_seen_at,
            IngestionSource.mode,
            IngestionSource.schedule_tier,
        )
        .join(IngestionSource, IngestionSourceDataset.source_id == IngestionSource.id)
        .where(IngestionSourceDataset.dataset_urn.in_(datasets))
    )
    mapping_rows = (await db.execute(mapping_q)).all()

    # Group by dataset_urn and pick the best row per dataset.
    _best: dict[str, tuple[str, str | None]] = {}  # urn -> (mode, schedule_tier)
    _best_priority: dict[str, tuple[int, float]] = {}  # urn -> (origin_prio, -ts)

    for row in mapping_rows:
        urn = row.dataset_urn
        origin = getattr(row, "origin", "matcher")
        prio = _ORIGIN_PRIORITY.get(origin, 99)
        # last_seen_at may be absent in test mocks; default to epoch so it
        # sorts last within the same priority level.
        last_seen = getattr(row, "last_seen_at", None)
        try:
            neg_ts: float = -last_seen.timestamp() if last_seen is not None else 0.0
        except (AttributeError, TypeError):
            neg_ts = 0.0
        current = _best_priority.get(urn)
        if current is None or (prio, neg_ts) < current:
            _best_priority[urn] = (prio, neg_ts)
            _best[urn] = (row.mode, row.schedule_tier)

    _MANAGED_MODES = frozenset({
        Mode.ACTIVE_CUSTOM_MANAGED.value,
        Mode.DATAHUB_MANAGED.value,
    })
    _PASSIVE_MODES = frozenset({
        Mode.PASSIVE.value,
    })

    def _resolve_window(urn: str) -> tuple[int, str]:
        """Return (window_sec, window_source) for the given URN."""
        if urn not in _best:
            return default_window_sec, "default"
        mode, tier = _best[urn]
        if mode in _MANAGED_MODES:
            if tier and tier in SCHEDULE_TIER_SECONDS:
                return (
                    SCHEDULE_TIER_SECONDS[tier] * LATE_INGESTION_FACTOR,
                    f"managed:{tier}",
                )
        if mode in _PASSIVE_MODES:
            return PASSIVE_SYNC_PERIOD_SEC * LATE_INGESTION_FACTOR, "passive"
        return default_window_sec, "default"

    # ── 2. Fetch latest INGESTION.COMPLETE event per dataset ──────────────────
    #
    # The freshness measurer queries dataset-entity events keyed by
    # entity_id = dataset_urn (entity_type='dataset').  The sync sweep (Phase 2b)
    # mirrors INGESTION.COMPLETE events with entity_type='dataset' /
    # entity_id=dataset_urn; until that is implemented, datasets with only
    # source-level events are counted as stale (conservative / safe default).
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
            Event.entity_type == "dataset",
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
