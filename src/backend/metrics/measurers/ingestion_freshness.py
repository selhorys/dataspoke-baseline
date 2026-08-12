"""Measurer: ingestion-freshness — counts datasets ingested within a per-dataset window.

Every ``INGESTION.*`` event is booked on a source (``entity_type='ingestion_source'``)
and never on the dataset, so the measurer resolves each dataset's owning source first
(``IngestionService.reverse_lookup_batch`` — the same priority rule the per-dataset
event timeline uses) and reads that source's feed, counting the source's CLI-wrapper
rows as its own. It reads that feed in **two tiers of evidence**:

1. ``observation`` — ``max(occurred_at)`` over the per-dataset observation events the
   owning source booked **for this dataset**
   (``IngestionService.latest_ingestion_observed_by_dataset``). Exact.
2. ``source_level`` — ``max(occurred_at)`` over every non-dry-run
   ``INGESTION.COMPLETE`` on the owning source
   (``IngestionService.latest_ingestion_complete_by_source``), whatever wrote it.

Tier 1 is preferred because a run-level ``COMPLETE`` is a claim about a *run*, not
about a dataset: partial emission still reads ``COMPLETE``, a ``DATAHUB_MANAGED``
``SUCCESS`` is not a per-table claim, and a ``matched`` mapping is recipe-*pattern*
derived, so a dataset the source merely could cover would inherit its freshness.
Tier 2 keeps those approximations by construction and applies only where nothing
better exists — a source-booked event genuinely cannot say which dataset it touched.
It is source-grained rather than producer-filtered on purpose: blacklisting
observations there would leave every ``PASSIVE`` dataset without one of its own with
no evidence at all.

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
        ``last_event_at``, ``time_window_sec``, ``window_source`` and
        ``evidence_tier`` in detail.
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

    # ── 2. Fetch both evidence tiers, keyed by owning source ──────────────────
    # Both helpers union each source's own events with its CLI wrappers' — DataHub
    # books a managed source's executions on the wrapper. Both bind only the source
    # ids: the dataset list can be the whole estate, and an IN list of URNs would
    # walk into asyncpg's 32767-bind-parameter ceiling. A source (tier 2) or a
    # (source, dataset) pair (tier 1) with no qualifying event is simply absent.
    source_ids = sorted({owner.id for owner in owners.values() if owner is not None})
    observed_by_dataset: dict[
        tuple[str, str], datetime
    ] = await ingestion.latest_ingestion_observed_by_dataset(source_ids)
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

        # Tier 1 (this dataset's own observations) preferred; tier 2 (any COMPLETE
        # on the owning source) only where tier 1 has nothing. evidence_tier names
        # which one answered, so a stale verdict is diagnosable — the two tiers make
        # different claims. Tier 2's label names the *grain*, not a producer.
        last_event_at: datetime | None = None
        evidence_tier: str | None = None
        if owner is not None:
            last_event_at = observed_by_dataset.get((owner.id, urn))
            if last_event_at is not None:
                evidence_tier = "observation"
            else:
                last_event_at = latest_by_source.get(owner.id)
                if last_event_at is not None:
                    evidence_tier = "source_level"

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
                        "evidence_tier": evidence_tier,
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
