from fastapi import APIRouter, Depends, HTTPException, WebSocket, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/data",
    tags=["common/data"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


# ── Dataset resource ───────────────────────────────────────────────────────────


@router.get("/{dataset_urn}")
async def get_dataset(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr")
async def get_dataset_attr(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/event")
async def get_dataset_events(dataset_urn: str) -> None:
    raise _501


# ── Ingestion ─────────────────────────────────────────────────────────────────


@router.get("/{dataset_urn}/attr/ingestion/conf")
async def get_ingestion_conf(dataset_urn: str) -> None:
    raise _501


@router.put("/{dataset_urn}/attr/ingestion/conf")
async def put_ingestion_conf(dataset_urn: str) -> None:
    raise _501


@router.patch("/{dataset_urn}/attr/ingestion/conf")
async def patch_ingestion_conf(dataset_urn: str) -> None:
    raise _501


@router.delete("/{dataset_urn}/attr/ingestion/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ingestion_conf(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/attr/ingestion/method/run")
async def run_ingestion(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/ingestion/event")
async def get_ingestion_events(dataset_urn: str) -> None:
    raise _501


# ── Validation ────────────────────────────────────────────────────────────────


@router.get("/{dataset_urn}/attr/validation/conf")
async def get_validation_conf(dataset_urn: str) -> None:
    raise _501


@router.put("/{dataset_urn}/attr/validation/conf")
async def put_validation_conf(dataset_urn: str) -> None:
    raise _501


@router.patch("/{dataset_urn}/attr/validation/conf")
async def patch_validation_conf(dataset_urn: str) -> None:
    raise _501


@router.delete("/{dataset_urn}/attr/validation/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_validation_conf(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/validation/result")
async def get_validation_result(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/attr/validation/method/run")
async def run_validation(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/validation/event")
async def get_validation_events(dataset_urn: str) -> None:
    raise _501


# ── Generation ────────────────────────────────────────────────────────────────


@router.get("/{dataset_urn}/attr/gen/conf")
async def get_gen_conf(dataset_urn: str) -> None:
    raise _501


@router.put("/{dataset_urn}/attr/gen/conf")
async def put_gen_conf(dataset_urn: str) -> None:
    raise _501


@router.patch("/{dataset_urn}/attr/gen/conf")
async def patch_gen_conf(dataset_urn: str) -> None:
    raise _501


@router.delete("/{dataset_urn}/attr/gen/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gen_conf(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/gen/result")
async def get_gen_result(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/attr/gen/method/generate")
async def run_gen(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/attr/gen/method/apply")
async def apply_gen(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/gen/event")
async def get_gen_events(dataset_urn: str) -> None:
    raise _501


# ── WebSocket: validation progress stream ─────────────────────────────────────


@router.websocket("/{dataset_urn}/stream/validation")
async def stream_validation(dataset_urn: str, websocket: WebSocket) -> None:
    """Stub WebSocket — immediately closes with 1011 (internal error / not implemented)."""
    await websocket.accept()
    await websocket.close(code=1011, reason="not implemented")
