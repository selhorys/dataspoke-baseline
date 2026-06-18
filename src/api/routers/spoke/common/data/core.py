"""Dataset-level handlers: summary, attributes, and dataset-level events."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import get_dataset_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse, QualityScoreResponse
from src.api.schemas.events import EventListResponse, EventResponse
from src.backend.dataset.service import DatasetService
from src.shared.db.models import Event

sub_router = APIRouter()


@sub_router.get("/{dataset_urn}", response_model=DatasetResponse)
async def get_data(
    dataset_urn: DatasetUrnPath,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetResponse:
    """Retrieve a dataset summary by URN."""
    summary = await service.get_summary(dataset_urn)
    return DatasetResponse.model_validate(summary)


@sub_router.get("/{dataset_urn}/attr", response_model=DatasetAttributesResponse)
async def get_data_attr(
    dataset_urn: DatasetUrnPath,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetAttributesResponse:
    """Retrieve extended attributes (columns, quality score, owners) for a dataset."""
    attrs = await service.get_attributes(dataset_urn)
    quality = None
    if attrs.quality_score is not None:
        quality = QualityScoreResponse(
            overall_score=attrs.quality_score.overall_score,
            dimensions=attrs.quality_score.dimensions,
            dimension_details=attrs.quality_score.dimension_details,
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


@sub_router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_data_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: DatasetService = Depends(get_dataset_service),
) -> EventListResponse:
    """List events for a dataset with time range and pagination."""
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
