"""Ingestion workflow — schedule tier helpers for Airflow-based periodic ingestion.

Manual runs call IngestionService.run() directly via the API route.
Periodic ingestion is handled by static Airflow DAGs keyed by schedule tier
(hourly, daily, weekly). The list-periodic activity endpoint queries active
datasets for a given tier and passes them to the DAG.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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
