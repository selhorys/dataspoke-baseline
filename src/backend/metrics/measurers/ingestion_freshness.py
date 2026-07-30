"""Measurer: ingestion-freshness — counts datasets ingested within a per-dataset window.

A dataset's ingestion recency **is the recency of its owning source's runs**:
``INGESTION.COMPLETE`` is booked on the source (``entity_type='ingestion_source'``)
and never on the dataset. So the measurer resolves each dataset's owning source
first (``IngestionService.reverse_lookup_batch`` — the same priority rule the
per-dataset event timeline uses) and reads that source's runs, counting the
source's CLI-wrapper runs as its own
(``IngestionService.latest_ingestion_complete_by_source``).

That same owning source supplies the window:

- ACTIVE_CUSTOM_MANAGED / DATAHUB_MANAGED with a known schedule_tier
  → SCHEDULE_TIER_SECONDS[tier] × LATE_INGESTION_FACTOR
- PASSIVE → PASSIVE_SYNC_PERIOD_SEC × LATE_INGESTION_FACTOR
- dataset mapped to no source, or source with no derivable schedule
  → metric_conf["time_window_sec"]

Spec: spec/feature/BACKEND.md §Metrics Service — Time windows
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService, IngestionSourceRecord
from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient
from src.shared.models.ingestion import Mode
from src.shared.schedule import (
    LATE_INGESTION_FACTOR,
    PASSIVE_SYNC_PERIOD_SEC,
    SCHEDULE_TIER_SECONDS,
)

_MANAGED_MODES = frozenset({
    Mode.ACTIVE_CUSTOM_MANAGED.value,
    Mode.DATAHUB_MANAGED.value,
})
_PASSIVE_MODES = frozenset({
    Mode.PASSIVE.value,
})


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
        DataHubClient — accepted for signature uniformity; this measurer stays
        DataSpoke-DB-side and makes no DataHub call.
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
    ingestion = IngestionService(datahub=datahub, db=db)

    # ── 1. Resolve each dataset's owning source ───────────────────────────────
    # reverse_lookup_batch owns the priority rule (emitted > pipeline_name >
    # matched, regular parent over its wrapper, then most recent last_seen_at)
    # and resolves a winning wrapper up to its regular parent. Every URN is a
    # key; the value is None when no source claims it.
    owners: dict[str, IngestionSourceRecord | None] = await ingestion.reverse_lookup_batch(datasets)

    def _resolve_window(owner: IngestionSourceRecord | None) -> tuple[int, str]:
        """Return (window_sec, window_source) for a dataset's owning source."""
        if owner is None:
            return default_window_sec, "default"
        tier = owner.schedule_tier
        if owner.mode in _MANAGED_MODES and tier and tier in SCHEDULE_TIER_SECONDS:
            return SCHEDULE_TIER_SECONDS[tier] * LATE_INGESTION_FACTOR, f"managed:{tier}"
        if owner.mode in _PASSIVE_MODES:
            return PASSIVE_SYNC_PERIOD_SEC * LATE_INGESTION_FACTOR, "passive"
        return default_window_sec, "default"

    # ── 2. Fetch the latest INGESTION.COMPLETE per owning source ──────────────
    # The helper unions each source's own events with its CLI wrappers' — DataHub
    # books a managed source's executions on the wrapper — and is absent for a
    # source that has never completed a run.
    source_ids = sorted({owner.id for owner in owners.values() if owner is not None})
    latest_by_source: dict[str, datetime] = await ingestion.latest_ingestion_complete_by_source(
        source_ids
    )

    # ── 3. Evaluate freshness per dataset ─────────────────────────────────────
    now = datetime.now(tz=UTC)
    total = len(datasets)
    ingested_in_time = 0
    stale_datasets: list[dict[str, Any]] = []

    for urn in datasets:
        owner = owners.get(urn)
        window_sec, window_source = _resolve_window(owner)
        cutoff = now - timedelta(seconds=window_sec)
        last_event_at = latest_by_source.get(owner.id) if owner is not None else None

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
