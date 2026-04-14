"""Ingestion workflow — schedule tier helpers for Airflow-based periodic ingestion.

Manual runs call IngestionService.run() directly via the API route.
Periodic ingestion is handled by static Airflow DAGs keyed by schedule tier
(hourly, daily, weekly). The list-periodic activity endpoint queries active
datasets for a given tier and passes them to the DAG.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

FLOW_ID = "ingestion"
PERIODIC_FLOW_PREFIX = "ingestion-periodic-"


def schedule_to_flow_id(schedule: str) -> str:
    """Return a stable DAG ID fragment for a given schedule string.

    Uses the first 8 hex chars of the MD5 hash of the schedule for a
    stable, human-readable short identifier.
    """
    digest = hashlib.md5(schedule.encode()).hexdigest()[:8]  # noqa: S324
    return f"{PERIODIC_FLOW_PREFIX}{digest}"


async def get_datasets_for_tier(db: object, tier: str) -> list[str]:
    """Return dataset URNs with active ingestion configs matching the given schedule tier.

    Called by the /internal/activities/ingestion/list-periodic endpoint so that
    Airflow DAGs can discover which datasets to process for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import IngestionConfig

    result = await db.execute(  # type: ignore[union-attr]
        select(IngestionConfig.dataset_urn).where(
            IngestionConfig.is_active == True,  # noqa: E712
            IngestionConfig.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
