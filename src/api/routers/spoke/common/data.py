from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_dataset_service
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse, QualityScoreResponse
from src.api.schemas.events import EventListResponse, EventResponse
from src.backend.dataset.service import DatasetService

router = APIRouter(
    prefix="/data",
    tags=["common/data"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


# ── Dataset resource ───────────────────────────────────────────────────────────


@router.get("/{dataset_urn}", response_model=DatasetResponse)
async def get_dataset(
    dataset_urn: str,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetResponse:
    summary = await service.get_summary(dataset_urn)
    return DatasetResponse(
        urn=summary.urn,
        name=summary.name,
        platform=summary.platform,
        description=summary.description,
        owners=summary.owners,
        tags=summary.tags,
    )


@router.get("/{dataset_urn}/attr", response_model=DatasetAttributesResponse)
async def get_dataset_attr(
    dataset_urn: str,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetAttributesResponse:
    attrs = await service.get_attributes(dataset_urn)
    quality = None
    if attrs.quality_score is not None:
        quality = QualityScoreResponse(
            overall_score=attrs.quality_score.overall_score,
            dimensions=attrs.quality_score.dimensions,
        )
    return DatasetAttributesResponse(
        urn=attrs.urn,
        column_count=attrs.column_count,
        fields=attrs.fields,
        owners=attrs.owners,
        tags=attrs.tags,
        description=attrs.description,
        quality_score=quality,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_dataset_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: DatasetService = Depends(get_dataset_service),
) -> EventListResponse:
    events, total_count = await service.get_events(dataset_urn, offset, limit, from_time, to_time)
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=e.id,
                entity_type=e.entity_type,
                entity_id=e.entity_id,
                event_type=e.event_type,
                status=e.status,
                detail=e.detail,
                occurred_at=e.occurred_at,
            )
            for e in events
        ],
    )


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
