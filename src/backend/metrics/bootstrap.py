"""Metrics bootstrap — idempotent factory-default seed for built-in metric types.

On API startup, one ``metric_definitions`` row is inserted for each built-in
metric type if the row does not already exist. Seeds ship with ``is_enabled=false``
so scheduled DAG runs are a no-op until the governance lead opts in via PATCH.
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import MetricDefinition

logger = logging.getLogger(__name__)

_FACTORY_DEFAULTS: list[dict[str, Any]] = [
    {
        "id": "ingestion-freshness",
        "mode": "active",
        "metric_type": "ingestion-freshness",
        "title": "Ingestion freshness",
        "description": "Daily count of datasets ingested within the configured time window",
        "metrics": ["total", "ingested_in_time"],
        "metric_conf": {"time_window_sec": 86400},
        "dataset_filter": {},
        "schedule_tier": "daily",
        "is_enabled": False,
    },
    {
        "id": "validation-score",
        "mode": "active",
        "metric_type": "validation-score",
        "title": "Validation score",
        "description": "Daily sum of dataset validation scores within the configured time window",
        "metrics": ["total", "validation_score_sum"],
        "metric_conf": {"time_window_sec": 86400},
        "dataset_filter": {},
        "schedule_tier": "daily",
        "is_enabled": False,
    },
    {
        "id": "doc-health",
        "mode": "active",
        "metric_type": "doc-health",
        "title": "Documentation health",
        "description": "Daily count of fully documented datasets (table + every column)",
        "metrics": ["total", "doc_health"],
        "metric_conf": {},
        "dataset_filter": {},
        "schedule_tier": "daily",
        "is_enabled": False,
    },
]


async def seed_factory_defaults(db: AsyncSession) -> None:
    """Insert factory-default metric rows for each built-in type if absent.

    Idempotent — rows that already exist are left unchanged.
    """
    for defaults in _FACTORY_DEFAULTS:
        metric_id = defaults["id"]
        result = await db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            continue

        row = MetricDefinition(
            id=metric_id,
            mode=defaults["mode"],
            metric_type=defaults["metric_type"],
            title=defaults["title"],
            description=defaults["description"],
            metrics=defaults["metrics"],
            metric_conf=defaults["metric_conf"],
            dataset_filter=defaults["dataset_filter"],
            schedule_tier=defaults["schedule_tier"],
            is_enabled=defaults["is_enabled"],
        )
        db.add(row)
        logger.info("metrics_bootstrap_seeded", extra={"metric_id": metric_id})

    await db.commit()
