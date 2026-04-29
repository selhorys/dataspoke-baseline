"""Ingestion workflow — parameter models and schedule tier helpers.

Manual runs call IngestionService.run() directly via the API route.
Periodic active ingestion is handled by static Airflow DAGs keyed by schedule
tier (hourly, daily, weekly). The list-active activity endpoint queries
active datasets for a given tier and passes them to the DAG.
Passive sync is handled by the ingestion-passive-hourly DAG, which calls
POST /internal/activities/ingestion/passive-sync once per hour.

Spec: spec/feature/BACKEND.md §Ingestion Workflow, §DAG Catalogue
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class IngestionRunParams(BaseModel):
    """Parameters for a single active ingestion run."""

    dataset_urn: str
    dry_run: bool = False


class IngestionPassiveSyncParams(BaseModel):
    """Parameters for the passive-sync activity (no inputs required)."""

    pass


async def get_datasets_for_tier(db: Any, tier: str) -> list[str]:
    """Return dataset URNs with active-mode ingestion configs matching the given schedule tier.

    Called by the /internal/activities/ingestion/list-active endpoint so that
    Airflow DAGs can discover which datasets to process for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import IngestionConfig

    result = await db.execute(
        select(IngestionConfig.dataset_urn).where(
            IngestionConfig.is_enabled == True,  # noqa: E712
            IngestionConfig.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
