"""Metadata Generation sub-resource handlers: /data/{dataset_urn}/attr/metagen/*
   and siblings: /data/{dataset_urn}/method/metagen/run
                  /data/{dataset_urn}/event/metagen

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Data Resource (lines 246–253).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_metagen_service
from src.api.schemas._paths import DatasetUrnPath, UuidPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.metagen import (
    MetagenConfPatchRequest,
    MetagenConfPutRequest,
    MetagenConfResponse,
    MetagenResultListResponse,
    MetagenResultResponse,
    MetagenRunResponse,
    ReviewResultRequest,
    RunMetagenRequest,
)
from src.backend.metagen.service import MetagenService
from src.shared.db.models import Event

sub_router = APIRouter()


# ── Conf CRUD ─────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/attr/metagen/conf", response_model=MetagenConfResponse)
async def get_data_metagen_conf(
    dataset_urn: DatasetUrnPath,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenConfResponse:
    """Get the metadata generation config for a dataset."""
    from src.shared.exceptions import EntityNotFoundError

    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("metagen_config", dataset_urn)
    return MetagenConfResponse(
        id=config.id,
        dataset_urn=config.dataset_urn,
        targets=config.targets,
        code_refs=config.code_refs,
        is_enabled=config.is_enabled,
        schedule_tier=config.schedule_tier,
        status=config.status,
        owner=config.owner,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@sub_router.put("/{dataset_urn}/attr/metagen/conf", response_model=MetagenConfResponse)
async def put_data_metagen_conf(
    dataset_urn: DatasetUrnPath,
    body: MetagenConfPutRequest,
    response: Response,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenConfResponse:
    """Create or replace the metadata generation config for a dataset (upsert)."""
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        targets=body.targets,
        code_refs=body.code_refs,
        is_enabled=body.is_enabled,
        schedule_tier=body.schedule_tier,
        owner=body.owner,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return MetagenConfResponse(
        id=config.id,
        dataset_urn=config.dataset_urn,
        targets=config.targets,
        code_refs=config.code_refs,
        is_enabled=config.is_enabled,
        schedule_tier=config.schedule_tier,
        status=config.status,
        owner=config.owner,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@sub_router.patch("/{dataset_urn}/attr/metagen/conf", response_model=MetagenConfResponse)
async def patch_data_metagen_conf(
    dataset_urn: DatasetUrnPath,
    body: MetagenConfPatchRequest,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenConfResponse:
    """Partially update the metadata generation config for a dataset."""
    config = await service.patch_config(dataset_urn, body.model_dump(exclude_unset=True))
    return MetagenConfResponse(
        id=config.id,
        dataset_urn=config.dataset_urn,
        targets=config.targets,
        code_refs=config.code_refs,
        is_enabled=config.is_enabled,
        schedule_tier=config.schedule_tier,
        status=config.status,
        owner=config.owner,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@sub_router.delete(
    "/{dataset_urn}/attr/metagen/conf", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_data_metagen_conf(
    dataset_urn: DatasetUrnPath,
    service: MetagenService = Depends(get_metagen_service),
) -> None:
    """Delete the metadata generation config for a dataset."""
    await service.delete_config(dataset_urn)


# ── Results ───────────────────────────────────────────────────────────────────


@sub_router.get(
    "/{dataset_urn}/attr/metagen/result", response_model=MetagenResultListResponse
)
async def get_data_metagen_results(
    dataset_urn: DatasetUrnPath,
    latest: bool = Query(default=False),
    approved: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenResultListResponse:
    """List metadata proposals.

    Use ?latest=true for the most recent only; ?approved=true for approved only.
    """
    from src.shared.db.models import MetagenResult

    order_by = parse_sort(sort, {"generated_at": MetagenResult.generated_at}, None)
    results, total = await service.list_results(
        dataset_urn=dataset_urn,
        latest=latest,
        approved=approved,
        from_dt=from_time,
        to_dt=to_time,
        offset=offset,
        limit=limit,
        order_by=order_by,
    )
    return MetagenResultListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        results=[
            MetagenResultResponse(
                id=r.id,
                dataset_urn=r.dataset_urn,
                proposals=r.proposals,
                field_status=r.field_status,
                run_id=r.run_id,
                generated_at=r.generated_at,
                last_reviewed_at=r.last_reviewed_at,
            )
            for r in results
        ],
    )


@sub_router.patch(
    "/{dataset_urn}/attr/metagen/result/{result_id}", response_model=MetagenResultResponse
)
async def patch_data_metagen_result(
    dataset_urn: DatasetUrnPath,
    result_id: UuidPath,
    body: ReviewResultRequest,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenResultResponse:
    """Review a pending metadata proposal — approve (all or selected fields) or reject.

    On approval, DataSpoke writes approved field values to DataHub editable aspects.
    """
    record = await service.review_result(
        result_id=result_id,
        verdict=body.verdict,
        fields=body.fields,
        reason=body.reason,
    )
    return MetagenResultResponse(
        id=record.id,
        dataset_urn=record.dataset_urn,
        proposals=record.proposals,
        field_status=record.field_status,
        run_id=record.run_id,
        generated_at=record.generated_at,
        last_reviewed_at=record.last_reviewed_at,
    )


# ── Run ───────────────────────────────────────────────────────────────────────


@sub_router.post(
    "/{dataset_urn}/method/metagen/run", response_model=MetagenRunResponse
)
async def post_data_metagen_run(
    dataset_urn: DatasetUrnPath,
    body: RunMetagenRequest,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenRunResponse:
    """Trigger a metadata generation run for the dataset.

    Concurrent runs return 409 GENERATION_RUNNING (via Airflow conf-dedup).
    """
    record = await service.run(dataset_urn=dataset_urn, dry_run=body.dry_run)
    return MetagenRunResponse(
        id=record.id,
        dataset_urn=record.dataset_urn,
        proposals=record.proposals,
        field_status=record.field_status,
        run_id=record.run_id,
        generated_at=record.generated_at,
        last_reviewed_at=record.last_reviewed_at,
    )


# ── Events ────────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/event/metagen", response_model=EventListResponse)
async def get_data_metagen_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetagenService = Depends(get_metagen_service),
) -> EventListResponse:
    """Metadata generation event reports (METAGEN.COMPLETE, METAGEN.APPROVE, METAGEN.REJECT)."""
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total = await service.get_events(
        dataset_urn=dataset_urn,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
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
