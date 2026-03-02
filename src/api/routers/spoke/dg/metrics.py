from fastapi import APIRouter, Depends, HTTPException, WebSocket, status

from src.api.auth.dependencies import require_dg

router = APIRouter(
    prefix="/metric",
    tags=["dg/metric"],
    dependencies=[Depends(require_dg)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def list_metrics() -> None:
    raise _501


@router.get("/{metric_id}")
async def get_metric(metric_id: str) -> None:
    raise _501


@router.get("/{metric_id}/attr")
async def get_metric_attr(metric_id: str) -> None:
    raise _501


@router.get("/{metric_id}/attr/conf")
async def get_metric_conf(metric_id: str) -> None:
    raise _501


@router.put("/{metric_id}/attr/conf")
async def put_metric_conf(metric_id: str) -> None:
    raise _501


@router.patch("/{metric_id}/attr/conf")
async def patch_metric_conf(metric_id: str) -> None:
    raise _501


@router.delete("/{metric_id}/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric_conf(metric_id: str) -> None:
    raise _501


@router.get("/{metric_id}/attr/result")
async def get_metric_result(metric_id: str) -> None:
    raise _501


@router.post("/{metric_id}/method/run")
async def run_metric(metric_id: str) -> None:
    raise _501


@router.post("/{metric_id}/method/activate")
async def activate_metric(metric_id: str) -> None:
    raise _501


@router.post("/{metric_id}/method/deactivate")
async def deactivate_metric(metric_id: str) -> None:
    raise _501


@router.get("/{metric_id}/event")
async def get_metric_events(metric_id: str) -> None:
    raise _501


# ── WebSocket: metric update stream ───────────────────────────────────────────


@router.websocket("/stream")
async def stream_metrics(websocket: WebSocket) -> None:
    """Stub WebSocket — immediately closes with 1011 (internal error / not implemented)."""
    await websocket.accept()
    await websocket.close(code=1011, reason="not implemented")
