"""Metadata Generation sub-resource handlers: /data/{dataset_urn}/attr/metagen/*
   and siblings: /data/{dataset_urn}/event/metagen

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Data Resource — metagen rows (lines 294–301).
"""

from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import AuthContext, require_writer
from src.api.dependencies import get_db, get_metagen_service
from src.api.routers.spoke._metagen_mappers import (
    event_list,
    to_candidate,
    to_item_detail,
    to_item_summary,
)
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse
from src.api.schemas.metagen import (
    MetagenBoundaryPatchRequest,
    MetagenBoundaryPutRequest,
    MetagenBoundaryResponse,
    MetagenCandidate,
    MetagenItemDetailResponse,
    MetagenItemListResponse,
    MetagenReviewRequest,
)
from src.backend.metagen.service import MetagenService
from src.shared.db.models import Event, MetagenItem
from src.shared.events import METAGEN_PREFIX

sub_router = APIRouter()

_AllowedKind = Literal["dataset.description", "column.description"]


def _boundary_response(dto: Any) -> MetagenBoundaryResponse:
    return MetagenBoundaryResponse(
        dataset_urn=dto.dataset_urn,
        is_enabled=dto.is_enabled,
        allowed=cast(list[_AllowedKind], dto.allowed),
        owner=dto.owner,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ── Boundary CRUD ─────────────────────────────────────────────────────────────


@sub_router.get(
    "/{dataset_urn}/attr/metagen/boundary",
    response_model=MetagenBoundaryResponse | None,
)
async def get_data_metagen_boundary(
    dataset_urn: DatasetUrnPath,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenBoundaryResponse | None:
    dto = await service.get_boundary(dataset_urn)
    return _boundary_response(dto) if dto is not None else None


@sub_router.put(
    "/{dataset_urn}/attr/metagen/boundary",
    response_model=MetagenBoundaryResponse,
)
async def put_data_metagen_boundary(
    dataset_urn: DatasetUrnPath,
    body: MetagenBoundaryPutRequest,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenBoundaryResponse:
    dto = await service.put_boundary(dataset_urn, body.model_dump())
    return _boundary_response(dto)


@sub_router.patch(
    "/{dataset_urn}/attr/metagen/boundary",
    response_model=MetagenBoundaryResponse,
)
async def patch_data_metagen_boundary(
    dataset_urn: DatasetUrnPath,
    body: MetagenBoundaryPatchRequest,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenBoundaryResponse:
    dto = await service.patch_boundary(dataset_urn, body.model_dump(exclude_unset=True))
    return _boundary_response(dto)


@sub_router.delete(
    "/{dataset_urn}/attr/metagen/boundary",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_data_metagen_boundary(
    dataset_urn: DatasetUrnPath,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    await service.delete_boundary(dataset_urn)


# ── Per-dataset items ─────────────────────────────────────────────────────────


@sub_router.get(
    "/{dataset_urn}/attr/metagen/item",
    response_model=MetagenItemListResponse,
)
async def get_data_metagen_items(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenItemListResponse:
    """Per-dataset metagen items (paginated; sortable by created_at, updated_at)."""
    order_by = parse_sort(
        sort,
        {"created_at": MetagenItem.created_at, "updated_at": MetagenItem.updated_at},
        None,
    )
    dtos, total = await service.list_items_for_dataset(
        dataset_urn, offset=offset, limit=limit, order_by=order_by
    )
    return MetagenItemListResponse(
        items=[to_item_summary(d) for d in dtos],
        total_count=total,
        offset=offset,
        limit=limit,
    )


@sub_router.get(
    "/{dataset_urn}/attr/metagen/item/{item_id}",
    response_model=MetagenItemDetailResponse,
)
async def get_data_metagen_item(
    dataset_urn: DatasetUrnPath,
    item_id: str,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenItemDetailResponse:
    dto = await service.get_item_for_dataset(dataset_urn, item_id)
    return to_item_detail(dto)


# ── Candidate review ──────────────────────────────────────────────────────────


@sub_router.post(
    "/{dataset_urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review",
    response_model=MetagenCandidate,
)
async def post_data_metagen_item_candidate_review(
    dataset_urn: DatasetUrnPath,
    item_id: str,
    candidate_id: str,
    body: MetagenReviewRequest,
    service: MetagenService = Depends(get_metagen_service),
    ctx: AuthContext = Depends(require_writer),
) -> MetagenCandidate:
    """Review a candidate — approve or reject.

    409 METAGEN_CANNOT_REJECT_APPROVED if rejecting an approved candidate.
    422 METAGEN_DATASET_NOT_IN_BOUNDARY if no is_enabled=true boundary.
    """
    reviewer_id: str | None = str(ctx.user.id)
    dto = await service.review_candidate(
        dataset_urn=dataset_urn,
        item_id=item_id,
        candidate_id=candidate_id,
        verdict=body.verdict,
        reason=body.reason,
        reviewer_id=reviewer_id,
    )
    return to_candidate(dto)


# ── Per-dataset metagen events ────────────────────────────────────────────────


@sub_router.get(
    "/{dataset_urn}/event/metagen",
    response_model=EventListResponse,
)
async def get_data_metagen_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """Per-dataset metagen events (METAGEN.CANDIDATE_APPROVE, METAGEN.CANDIDATE_REJECT).

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    base = select(Event).where(
        Event.entity_type == "dataset",
        Event.entity_id == dataset_urn,
        Event.event_type.startswith(METAGEN_PREFIX),
    )
    if from_time is not None:
        base = base.where(Event.occurred_at >= from_time)
    if to_time is not None:
        base = base.where(Event.occurred_at <= to_time)

    count_q = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, Event.occurred_at.desc())
    rows_q = base.order_by(order_by).offset(offset).limit(limit)
    rows = (await db.execute(rows_q)).scalars().all()

    return event_list(
        [
            {
                "id": str(r.id),
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "event_type": r.event_type,
                "status": r.status,
                "detail": dict(r.detail) if r.detail else {},
                "occurred_at": r.occurred_at,
            }
            for r in rows
        ],
        int(total),
        offset,
        limit,
    )
