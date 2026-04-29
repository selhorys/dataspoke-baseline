"""Metrics workflow — parameter models and schedule tier helpers.

Manual on-demand runs trigger the 'metrics' Airflow DAG via AirflowClient.
Periodic metric execution is handled by static Airflow DAGs keyed by
schedule tier (hourly, daily, weekly). The list-active activity endpoint
queries enabled metric definitions for a given tier and passes them to the DAG.

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class MetricRunParams(BaseModel):
    """Parameters for a single metric measurement run."""

    metric_id: str


async def get_metrics_for_tier(db: Any, tier: str) -> list[str]:
    """Return metric IDs with is_enabled=True and schedule_tier matching the given tier.

    Called by the /internal/activities/metrics/list-active endpoint so that
    Airflow DAGs can discover which metrics to run for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import MetricDefinition

    result = await db.execute(
        select(MetricDefinition.id).where(
            MetricDefinition.is_enabled == True,  # noqa: E712
            MetricDefinition.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
