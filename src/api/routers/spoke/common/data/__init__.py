"""Data package — composes the canonical /data/{dataset_urn}/… route surface.

``router`` — HTTP routes (auth-guarded via require_authenticated dependency).
No WebSocket router: streaming surface is not exposed in the baseline
(clients poll event/... and attr/.../result per spec/feature/FRONTEND_BASIC.md).
"""

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_authenticated

from . import core, ingestion, metagen, validation

# The ``/data`` prefix is applied per-include (not on the package router) so the
# collection-root handler ``GET /data`` in ``core.sub_router`` — registered at the
# empty path — resolves to a non-empty path at include time. FastAPI rejects an
# empty include-prefix combined with an empty route path.
router = APIRouter(
    tags=["common/data"],
    dependencies=[Depends(require_authenticated)],
)
router.include_router(core.sub_router, prefix="/data")
router.include_router(ingestion.sub_router, prefix="/data")
router.include_router(validation.sub_router, prefix="/data")
router.include_router(metagen.sub_router, prefix="/data")
