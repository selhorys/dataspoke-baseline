"""Validation workflow — parameter models and schedule tier helpers.

Ad-hoc validation runs execute directly (no orchestrator detour).
Periodic/scheduled validation runs are handled by static Airflow DAGs
keyed by schedule tier (hourly, daily, weekly).

The list-active activity endpoint queries active datasets for a given tier
and passes them to the DAG.

Spec: spec/feature/BACKEND.md §DAG Catalogue
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)




class ValidationRunParams(BaseModel):
    """Parameters for a single validation run."""

    dataset_urn: str
    partition: dict[str, Any] | None = None
    dry_run: bool = False


async def get_datasets_for_tier(db: Any, tier: str) -> list[str]:
    """Return dataset URNs with is_enabled validation configs matching the given tier.

    Called by the /internal/activities/validation/list-active endpoint so that
    Airflow DAGs can discover which datasets to validate for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import ValidationConfig

    result = await db.execute(
        select(ValidationConfig.dataset_urn).where(
            ValidationConfig.is_enabled == True,  # noqa: E712
            ValidationConfig.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
