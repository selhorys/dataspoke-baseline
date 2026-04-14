"""Metrics workflow — schedule tier helpers for Airflow-based periodic metrics runs.

Manual runs trigger the 'metrics' Airflow DAG via AirflowClient.
Periodic metric execution is handled by static Airflow DAGs keyed by
schedule tier (hourly, daily, weekly). The list-periodic activity endpoint
queries active metric definitions for a given tier and passes them to the DAG.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

FLOW_ID = "metrics"
PERIODIC_FLOW_PREFIX = "metrics-periodic-"


def schedule_to_flow_id(schedule: str) -> str:
    """Return a stable DAG ID fragment for a given schedule string.

    Uses the first 8 hex chars of the MD5 hash of the schedule for a
    stable, human-readable short identifier.
    """
    digest = hashlib.md5(schedule.encode()).hexdigest()[:8]  # noqa: S324
    return f"{PERIODIC_FLOW_PREFIX}{digest}"


async def get_metrics_for_tier(db: object, tier: str) -> list[str]:
    """Return metric IDs with active definitions matching the given schedule tier.

    Called by the /internal/activities/metrics/list-periodic endpoint so that
    Airflow DAGs can discover which metrics to run for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import MetricDefinition

    result = await db.execute(  # type: ignore[union-attr]
        select(MetricDefinition.id).where(
            MetricDefinition.is_active == True,  # noqa: E712
            MetricDefinition.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
