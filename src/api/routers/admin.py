"""Admin endpoints — system configuration and operational tasks.

Accessible to users with the ``admin`` group claim via ``/api/v1/admin/…``.
Also mounted as ``/internal/admin/…`` (no auth) for scripts and automation.
"""

import logging
import time

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_admin
from src.api.dependencies import get_kestra_client
from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.registry import _STARTUP_FLOWS, register_all_flows

logger = logging.getLogger(__name__)

_WARMUP_FLOW_YAML = """\
id: test-noop
namespace: {namespace}
tasks:
  - id: noop
    type: io.kestra.plugin.core.log.Log
    message: warm-up
"""

_WARMUP_ITERATIONS = 5


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

internal_router = APIRouter(
    prefix="/internal/admin",
    tags=["internal/admin"],
)


# ── Flow init ─────────────────────────────────────────────────────────────────

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


# ── Kestra JVM warm-up ────────────────────────────────────────────────────────

async def _warm_up_kestra(kestra: KestraClient) -> dict:
    flow_yaml = _WARMUP_FLOW_YAML.format(namespace=kestra.namespace)
    flow_id = "test-noop"
    start = time.monotonic()

    await kestra.create_or_update_flow(flow_yaml)
    logger.info("Kestra warm-up: created test-noop flow")

    for i in range(1, _WARMUP_ITERATIONS + 1):
        iter_start = time.monotonic()
        execution = await kestra.trigger_execution(flow_id)
        result = await kestra.wait_for_execution(
            execution.id, flow_id=flow_id, timeout_seconds=60,
        )
        elapsed_ms = int((time.monotonic() - iter_start) * 1000)
        logger.info(
            "Kestra warm-up: iteration %d/%d %s (%dms)",
            i, _WARMUP_ITERATIONS, result.status.value, elapsed_ms,
        )

    await kestra.delete_flow(flow_id)

    total_ms = int((time.monotonic() - start) * 1000)
    logger.info("Kestra warm-up complete (%dms total)", total_ms)
    return {"status": "ok", "iterations": _WARMUP_ITERATIONS, "elapsed_ms": total_ms}


@router.post("/flows/warm_up_kestra")
async def warm_up_kestra(
    kestra: KestraClient = Depends(get_kestra_client),
) -> dict:
    """Warm up Kestra JVM via repeated noop flow executions."""
    return await _warm_up_kestra(kestra)


@internal_router.post("/flows/warm_up_kestra")
async def internal_warm_up_kestra(
    kestra: KestraClient = Depends(get_kestra_client),
) -> dict:
    """Warm up Kestra JVM via repeated noop flow executions (internal, no auth)."""
    return await _warm_up_kestra(kestra)
