"""Ingestion sub-resource handlers: /data/{dataset_urn}/attr/ingestion/*"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_ingestion_service, get_redis
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.api.schemas.mappers import ingestion_config_response
from src.backend.ingestion.service import IngestionService
from src.shared.cache.client import RedisClient
from src.shared.db.models import Event
from src.shared.exceptions import EntityNotFoundError

sub_router = APIRouter()


@sub_router.get("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def get_data_ingestion_conf(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Retrieve the ingestion config embedded within the dataset resource."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return ingestion_config_response(config)


@sub_router.put("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def put_data_ingestion_conf(
    dataset_urn: str,
    body: CreateIngestionConfigRequest,
    response: Response,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Create or replace the ingestion config for the dataset (upsert)."""
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        platform=body.platform,
        locator=body.locator,
        identifier=body.identifier,
        auth=body.auth,
        is_active=body.is_active,
        schedule_tier=body.schedule_tier,
        enrichment_sources=body.enrichment_sources,
        custom_extractors=body.custom_extractors,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return ingestion_config_response(config)


@sub_router.patch("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def patch_data_ingestion_conf(
    dataset_urn: str,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Partially update the ingestion config for the dataset."""
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return ingestion_config_response(config)


@sub_router.delete("/{dataset_urn}/attr/ingestion/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_data_ingestion_conf(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> None:
    """Delete the ingestion config for the dataset."""
    await service.delete_config(dataset_urn)


@sub_router.post("/{dataset_urn}/attr/ingestion/method/run", response_model=RunResultResponse)
async def post_data_ingestion_run(
    dataset_urn: str,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
    cache: RedisClient = Depends(get_redis),
) -> RunResultResponse:
    """Trigger an ingestion run for the dataset via the data sub-resource."""
    from src.backend.ingestion.service import run_ingestion_with_lock

    result = await run_ingestion_with_lock(service, cache, dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(run_id=result.run_id, status=result.status, detail=result.detail)


@sub_router.get("/{dataset_urn}/attr/ingestion/event", response_model=EventListResponse)
async def get_data_ingestion_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    """List ingestion events for the dataset with time range and pagination."""
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events(
        dataset_urn, offset, limit, from_time, to_time, order_by=order_by
    )
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
