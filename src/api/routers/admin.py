"""Admin endpoints — system configuration and operational tasks.

Accessible to users with the ``admin`` group claim via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` (no auth) for scripts and automation.
"""

import logging

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_admin
from src.api.dependencies import get_kestra_client
from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.registry import _STARTUP_FLOWS, register_all_flows

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

internal_router = APIRouter(
    prefix="/internal/admin",
    tags=["internal/admin"],
)


async def _init_flows(kestra: KestraClient) -> dict:
    count = await register_all_flows(kestra)
    total = len(_STARTUP_FLOWS)
    logger.info("Flow init: registered %d/%d flows", count, total)
    return {"registered": count, "total": total}


@router.post("/flows/init")
async def init_flows(
    kestra: KestraClient = Depends(get_kestra_client),
) -> dict:
    """Register or update all startup Kestra flows."""
    return await _init_flows(kestra)


@internal_router.post("/flows/init")
async def internal_init_flows(
    kestra: KestraClient = Depends(get_kestra_client),
) -> dict:
    """Register or update all startup Kestra flows (internal, no auth)."""
    return await _init_flows(kestra)
