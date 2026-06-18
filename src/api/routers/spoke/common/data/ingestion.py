"""Ingestion sub-resource handlers for the per-dataset surface.

Read-only: the source that covers a dataset plus its event history.
All ingestion configuration and mutation is at /spoke/ingestion/sources/...

Spec: API.md §Data Resource
  GET /data/{dataset_urn}/attr/ingestion  — reverse-lookup (read-only)
  GET /data/{dataset_urn}/event/ingestion — ingestion events for this dataset

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated (reads only).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import get_ingestion_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import IngestionLatestRunSummary, IngestionReverseLookupResponse
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import Event
from src.shared.models.ingestion import Mode

sub_router = APIRouter()


@sub_router.get(
    "/{dataset_urn}/attr/ingestion",
    response_model=IngestionReverseLookupResponse,
)
async def get_data_ingestion(
    dataset_urn: DatasetUrnPath,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionReverseLookupResponse:
    """Reverse-lookup: the ingestion source that covers this dataset.

    Returns the owning source's ``source_id``, ``mode``, ``name``, and the
    latest run summary. Returns nulls (but HTTP 200) when no source claims this
    dataset.

    Ingestion is configured per-source under ``/spoke/ingestion/sources``.
    This endpoint is read-only.
    """
    source = await service.reverse_lookup(dataset_urn)
    if source is None:
        return IngestionReverseLookupResponse(
            dataset_urn=dataset_urn,
            source_id=None,
            mode=None,
            name=None,
            latest_run=None,
        )

    # Fetch the most recent INGESTION.COMPLETE or INGESTION.FAIL event.
    latest_run: IngestionLatestRunSummary | None = None
    events, _ = await service.get_events_for_source(source_id=source.id, limit=1)
    if events:
        ev = events[0]
        detail = ev.get("detail") or {}
        latest_run = IngestionLatestRunSummary(
            run_id=detail.get("run_id"),
            status=ev["status"],
            occurred_at=ev["occurred_at"],
        )

    return IngestionReverseLookupResponse(
        dataset_urn=dataset_urn,
        source_id=source.id,
        mode=Mode(source.mode),
        name=source.name,
        latest_run=latest_run,
    )


@sub_router.get(
    "/{dataset_urn}/event/ingestion",
    response_model=EventListResponse,
)
async def get_data_ingestion_event(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    """Ingestion event reports for this dataset.

    Events are mirrored from the owning source's run history. When no source
    covers this dataset, an empty event list is returned (HTTP 200).
    """
    # Resolve the owning source; return empty list if unmapped.
    source = await service.reverse_lookup(dataset_urn)
    if source is None:
        return EventListResponse(
            offset=offset,
            limit=limit,
            total_count=0,
            events=[],
        )

    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events_for_source(
        source_id=source.id,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
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
