"""Validation workflow — schedule tier helpers for Airflow-based periodic validation.

Periodic validation is handled by static Airflow DAGs keyed by schedule tier
(hourly, daily, weekly). The list-periodic activity endpoint queries active
datasets for a given tier and passes them to the DAG.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def get_datasets_for_tier(db: object, tier: str) -> list[str]:
    """Return dataset URNs with active validation configs matching the given schedule tier.

    Called by the /internal/activities/validation/list-periodic endpoint so that
    Airflow DAGs can discover which datasets to validate for a given tier
    (hourly, daily, weekly).
    """
    from sqlalchemy import select

    from src.shared.db.models import ValidationConfig

    result = await db.execute(  # type: ignore[union-attr]
        select(ValidationConfig.dataset_urn).where(
            ValidationConfig.is_active == True,  # noqa: E712
            ValidationConfig.schedule_tier == tier,
        )
    )
    return [row[0] for row in result.all()]
