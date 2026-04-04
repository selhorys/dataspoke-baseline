"""Dataset registry — tracks whether dataset URNs are known to DataHub."""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.datahub.client import DataHubClient
from src.shared.db.models import DatasetRegistry
from src.shared.exceptions import PreconditionError

logger = logging.getLogger(__name__)


async def ensure_dataset_registered(
    db: AsyncSession,
    datahub: DataHubClient,
    dataset_urn: str,
    require_in_datahub: bool = False,
) -> bool:
    """Look up or create a dataset_registry row. Returns datahub_registered.

    1. SELECT from dataset_registry
    2. If no row: check DataHub via get_aspect(DatasetPropertiesClass),
       INSERT row with result (handles concurrent INSERT race)
    3. If require_in_datahub and datahub_registered is False: raise PreconditionError
    4. Return datahub_registered

    Does NOT commit — the caller's commit covers both the registry
    insert and the subsequent config upsert atomically.
    """
    result = await db.execute(
        select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()

    if row is not None:
        registered = row.datahub_registered
    else:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        aspect = await datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        registered = aspect is not None

        new_row = DatasetRegistry(
            dataset_urn=dataset_urn,
            datahub_registered=registered,
        )
        db.add(new_row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
            )
            row = result.scalar_one_or_none()
            registered = row.datahub_registered if row is not None else False

    if require_in_datahub and not registered:
        raise PreconditionError(
            "DATASET_NOT_IN_DATAHUB",
            f"Dataset '{dataset_urn}' is not registered in DataHub",
        )

    return registered
