from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_ingestion_service
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    IngestionConfigListResponse,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/ingestion",
    tags=["common/ingestion"],
    dependencies=[Depends(require_common)],
)


def _config_response(c) -> IngestionConfigResponse:  # noqa: ANN001
    return IngestionConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),
        dataset_urn=c.dataset_urn,
        sources=c.sources,
        deep_spec_enabled=c.deep_spec_enabled,
        schedule=c.schedule,
        status=c.status,
        owner=c.owner,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=IngestionConfigListResponse)
async def list_ingestion_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigListResponse:
    configs, total = await service.list_configs(offset, limit, status_filter)
    return IngestionConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        configs=[_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=IngestionConfigResponse)
async def get_ingestion_config(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return _config_response(config)


@router.get("/{dataset_urn}/attr", response_model=IngestionConfigResponse)
async def get_ingestion_config_attr(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return _config_response(config)


@router.patch("/{dataset_urn}/attr", response_model=IngestionConfigResponse)
async def patch_ingestion_config_attr(
    dataset_urn: str,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _config_response(config)


@router.post("/{dataset_urn}/method/run", response_model=RunResultResponse)
async def run_ingestion(
    dataset_urn: str,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> RunResultResponse:
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_ingestion_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    events, total_count = await service.get_events(dataset_urn, offset, limit, from_time, to_time)
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=e["id"],
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e["detail"],
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )
