"""Dataset-level handlers: summary, attributes, and dataset-level events."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import get_dataset_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.dataset import (
    DatasetAttributesResponse,
    DatasetListItem,
    DatasetListResponse,
    DatasetResponse,
    QualityScoreResponse,
)
from src.api.schemas.events import EventListResponse, EventResponse
from src.backend.dataset.service import DatasetService
from src.shared.db.models import DatasetRegistry, Event
from src.shared.events import INGESTION_PREFIX, METAGEN_PREFIX, VALIDATION_PREFIX

sub_router = APIRouter()

# Maps the public ``event_major_type`` filter values to the event-type prefixes
# used by the unified dataset timeline.
_MAJOR_TYPE_PREFIX = {
    "INGESTION": INGESTION_PREFIX,
    "VALIDATION": VALIDATION_PREFIX,
    "METAGEN": METAGEN_PREFIX,
}


@sub_router.get("", response_model=DatasetListResponse)
async def get_data_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetListResponse:
    """List all registered datasets with cross-feature coverage (paginated).

    The base set is the DataHub-registered ``dataset_registry`` (same as
    ``/ingestion/unmanaged`` and ``/metagen/uncovered``). Each row carries its
    owning ingestion source (``ingestion``, null when uncovered) and the enabled
    metadata-generation confs whose filter matches it (``metagen``, possibly
    empty). Sortable by ``dataset_urn`` (default: ``dataset_urn_asc``).
    """
    order_by = parse_sort(
        sort,
        {"dataset_urn": DatasetRegistry.dataset_urn},
        DatasetRegistry.dataset_urn,
    )
    items, total_count = await service.list_datasets(
        offset=offset,
        limit=limit,
        order_by=order_by,
    )
    return DatasetListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        datasets=[DatasetListItem.model_validate(row) for row in items],
    )


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
    event_major_type: list[str] = Query(default=[]),
    service: DatasetService = Depends(get_dataset_service),
) -> EventListResponse:
    """List the unified per-dataset event timeline with filtering and pagination.

    The timeline unions the covering source's ingestion runs with the dataset's
    validation and metagen events. ``event_major_type`` is a repeatable filter
    (``INGESTION`` / ``VALIDATION`` / ``METAGEN``); omitted means all.
    """
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    prefixes = {
        _MAJOR_TYPE_PREFIX[m] for m in event_major_type if m in _MAJOR_TYPE_PREFIX
    }
    events, total_count = await service.get_events(
        dataset_urn,
        offset,
        limit,
        from_time,
        to_time,
        order_by=order_by,
        event_type_prefixes=prefixes or None,
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
                wrapper=e.wrapper,
            )
            for e in events
        ],
    )
