"""Admin endpoints — system configuration and operational tasks.

Accessible to users with the ``admin`` group claim via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` for scripts and automation (requires ``X-Internal-Token`` shared-secret header via ``require_internal_token``).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_admin
from src.api.auth.internal import require_internal_token
from src.api.dependencies import get_airflow_client, get_datahub, get_db
from src.shared.datahub.client import DataHubClient
from src.shared.db.registry import sync_with_datahub
from src.workflows.airflow.client import AirflowClient

logger = logging.getLogger(__name__)

# Expected DAG IDs that must be loaded in Airflow for DataSpoke to function.
_ON_DEMAND_DAGS = ("generation", "metrics", "embedding-sync", "ontology-rebuild")
_PERIODIC_TIERS = ("hourly", "daily", "weekly")
_PERIODIC_DAGS = tuple(
    f"{domain}-periodic-{tier}"
    for domain in ("ingestion", "metrics", "validation")
    for tier in _PERIODIC_TIERS
)
_SYNC_DAGS = ("datahub-sync-daily",)
_EXPECTED_DAGS = frozenset(_ON_DEMAND_DAGS + _PERIODIC_DAGS + _SYNC_DAGS)

DatasetUrn = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^urn:li:dataset:")]


class DatahubSyncRequest(BaseModel):
    dataset_urns: Annotated[list[DatasetUrn], Field(max_length=10_000)] | None = None


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

internal_router = APIRouter(
    prefix="/internal/admin",
    tags=["internal/admin"],
    dependencies=[Depends(require_internal_token)],
)


async def _verify_dags(airflow: AirflowClient) -> dict:
    dags = await airflow.list_dags()
    loaded_ids = {d.get("dag_id") for d in dags}
    missing = sorted(_EXPECTED_DAGS - loaded_ids)
    found = sorted(_EXPECTED_DAGS & loaded_ids)
    logger.info(
        "DAG verification: found %d/%d expected DAGs, missing=%s",
        len(found),
        len(_EXPECTED_DAGS),
        missing,
    )
    return {
        "found": found,
        "missing": missing,
        "total_expected": len(_EXPECTED_DAGS),
    }


@router.post("/dags/verify")
async def verify_dags(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> dict:
    """Verify that all expected Airflow DAGs are loaded and visible."""
    return await _verify_dags(airflow)


@internal_router.post("/dags/verify")
async def internal_verify_dags(
    airflow: AirflowClient = Depends(get_airflow_client),
) -> dict:
    """Verify that all expected Airflow DAGs are loaded (internal — requires X-Internal-Token)."""
    return await _verify_dags(airflow)


@internal_router.post("/datahub/sync")
async def internal_datahub_sync(
    body: DatahubSyncRequest | None = None,
    db: AsyncSession = Depends(get_db),
    datahub: DataHubClient = Depends(get_datahub),
) -> dict:
    """Reconcile dataset_registry.datahub_registered against DataHub.

    Internal-only — requires ``X-Internal-Token`` header.
    Body is optional. When omitted or ``dataset_urns`` is null, all registry rows are
    checked. When ``dataset_urns`` is provided, only those URNs are reconciled.
    """
    dataset_urns = body.dataset_urns if body else None
    result = await sync_with_datahub(db, datahub, dataset_urns=dataset_urns)
    await db.commit()
    logger.info(
        "datahub_sync completed: checked=%d flipped_true=%d flipped_false=%d "
        "unchanged=%d not_found=%d",
        result["checked"],
        result["flipped_true"],
        result["flipped_false"],
        result["unchanged"],
        result["not_found"],
    )
    return result
