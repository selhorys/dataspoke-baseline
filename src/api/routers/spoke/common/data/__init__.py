"""Data package — composes the canonical /data/{dataset_urn}/… route surface.

``router``    — HTTP routes (auth-guarded via require_common dependency)
``ws_router`` — WebSocket routes (no HTTP auth; handshake-based inside the handler)
"""

from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_common

from . import core, generation, ingestion, validation, ws

router = APIRouter(
    prefix="/data",
    tags=["common/data"],
    dependencies=[Depends(require_common)],
)
router.include_router(core.sub_router)
router.include_router(ingestion.sub_router)
router.include_router(validation.sub_router)
router.include_router(generation.sub_router)

# WebSocket router — no HTTP auth dependency; authentication is handled via
# the message-based handshake inside the ws handler itself.
ws_router = APIRouter(prefix="/data", tags=["common/data"])
ws_router.include_router(ws.sub_router)
