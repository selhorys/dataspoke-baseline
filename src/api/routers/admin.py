"""Admin endpoints — system configuration and operational tasks.

Accessible to users with the ``admin`` group claim via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` (no auth) for scripts and automation.
"""

import logging

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_admin
from src.api.dependencies import get_airflow_client
from src.workflows.airflow.client import AirflowClient

logger = logging.getLogger(__name__)

# Expected DAG IDs that must be loaded in Airflow for DataSpoke to function.
_EXPECTED_DAGS = frozenset({
    "ingestion",
    "generation",
    "metrics",
    "embedding-sync",
    "ontology-rebuild",
})

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

internal_router = APIRouter(
    prefix="/internal/admin",
    tags=["internal/admin"],
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
    """Verify that all expected Airflow DAGs are loaded (internal, no auth)."""
    return await _verify_dags(airflow)
