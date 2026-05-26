"""Data package — composes the canonical /data/{dataset_urn}/… route surface.

``router`` — HTTP routes (auth-guarded via require_common dependency).
No WebSocket router: streaming surface is not exposed in the baseline
(clients poll event/... and attr/.../result per spec/feature/FRONTEND_BASIC.md).
"""

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_authenticated

from . import core, ingestion, metagen, validation

router = APIRouter(
    prefix="/data",
    tags=["common/data"],
    dependencies=[Depends(require_authenticated)],
)
router.include_router(core.sub_router)
router.include_router(ingestion.sub_router)
router.include_router(validation.sub_router)
router.include_router(metagen.sub_router)
