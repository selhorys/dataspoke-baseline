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

# Series colors: the shared "total" baseline is slate, and each type's own key
# takes a distinct hue so two metrics on one dashboard stay tellable apart.
_TOTAL_COLOR = "#64748B"

_FACTORY_DEFAULTS: list[dict[str, Any]] = [
    {
        "id": "ingestion-freshness",
        "mode": "active",
        "metric_type": "ingestion-freshness",
        "title": "Ingestion freshness",
        "description": "Daily count of datasets ingested within the configured time window",
        "metrics": [
            {"name": "total", "color": _TOTAL_COLOR, "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
        "metric_conf": {"time_window_sec": 172800},
        "dataset_filter": "",
        "schedule_tier": "daily",
        "is_enabled": False,
    },
    {
        "id": "validation-score",
        "mode": "active",
        "metric_type": "validation-score",
        "title": "Validation score",
        "description": (
            "Daily count of datasets whose latest validation result is inside their "
            "cadence-anchored window and passing, against the configured estate"
        ),
        # seed_factory_defaults() only inserts this row when absent — an existing
        # dev/prod row predating the total-drop change keeps its old 3-series
        # `metrics` list (including "total"). Delete that row (letting bootstrap
        # re-insert this two-series default) or PATCH it to drop "total" from its
        # stored metrics, or a later PATCH to that row will 422 against the
        # narrower _EMITTED_KEYS allow-list.
        "metrics": [
            {"name": "valid_confd", "color": "#3B82F6", "idx": 1},
            {"name": "valid_in_time", "color": "#14B8A6", "idx": 2},
        ],
        "metric_conf": {"time_window_sec": 172800},
        "dataset_filter": "",
        "schedule_tier": "daily",
        "is_enabled": False,
    },
    {
        "id": "doc-health",
        "mode": "active",
        "metric_type": "doc-health",
        "title": "Documentation health",
        "description": "Daily count of fully documented datasets (table + every column)",
        "metrics": [
            {"name": "total", "color": _TOTAL_COLOR, "idx": 1},
            {"name": "doc_health", "color": "#A855F7", "idx": 2},
        ],
        "metric_conf": {},
        "dataset_filter": "",
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
