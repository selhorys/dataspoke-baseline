"""Ingestion sub-resource handlers: /data/{dataset_urn}/attr/ingestion/*
   and siblings: /data/{dataset_urn}/method/ingestion/run
                  /data/{dataset_urn}/event/ingestion

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Data Resource (lines 233–238).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_ingestion_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import Event
from src.shared.exceptions import EntityNotFoundError

sub_router = APIRouter()


# ── Conf CRUD ─────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def get_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Retrieve the ingestion config for a dataset."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return IngestionConfigResponse.model_validate(config)


@sub_router.put("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def put_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    body: CreateIngestionConfigRequest,
    response: Response,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Create or replace the ingestion config for a dataset (upsert)."""
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        mode=body.mode,
        platform=body.platform,
        locator=body.locator,
        identifier=body.identifier,
        auth=body.auth,
        is_enabled=body.is_enabled,
        schedule_tier=body.schedule_tier,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return IngestionConfigResponse.model_validate(config)


@sub_router.patch("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def patch_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Partially update the ingestion config for a dataset."""
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return IngestionConfigResponse.model_validate(config)


@sub_router.delete(
    "/{dataset_urn}/attr/ingestion/conf", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    service: IngestionService = Depends(get_ingestion_service),
) -> None:
    """Delete the ingestion config for a dataset."""
    await service.delete_config(dataset_urn)


# ── Run (sibling path) ────────────────────────────────────────────────────────


@sub_router.post("/{dataset_urn}/method/ingestion/run", response_model=RunResultResponse)
async def post_data_ingestion_run(
    dataset_urn: DatasetUrnPath,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> RunResultResponse:
    """Execute the ingestion pipeline for a dataset.

    ?dry_run=true (in body) runs the extractor without emitting to DataHub.
    Concurrent runs return 409 INGESTION_RUNNING.
    """
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


# ── Events (sibling path) ─────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/event/ingestion", response_model=EventListResponse)
async def get_data_ingestion_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    """Ingestion event reports for a dataset (INGESTION.COMPLETE, INGESTION.FAIL)."""
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
                id=str(e["id"]),
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e.get("detail", {}),
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )
