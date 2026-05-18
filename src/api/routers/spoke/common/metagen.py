"""Metadata Generation router — /spoke/common/metagen/...

Routes:
  singleton conf CRUD   GET/PUT/PATCH/DELETE /metagen/attr/conf
  run                   POST /metagen/method/run
  global events         GET /metagen/event
  item list             GET /metagen/item
  item detail           GET /metagen/item/{composite_id}

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: require_common (de/da/dg/admin groups).
Spec: API.md §Metadata Generation (/spoke/common/metagen).
"""

from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_db, get_metagen_service
from src.api.routers.spoke.common._metagen_mappers import (
    event_list,
    to_item_detail,
    to_item_summary,
)
from src.api.schemas.events import EventListResponse
from src.api.schemas.metagen import (
    MetagenGlobalConfPatchRequest,
    MetagenGlobalConfPutRequest,
    MetagenGlobalConfResponse,
    MetagenItemDetailResponse,
    MetagenItemListResponse,
    MetagenRunRequest,
    MetagenRunResponse,
)
from src.backend.metagen.service import MetagenService
from src.shared.db.models import Event
from src.shared.events import METAGEN_PREFIX
from src.shared.exceptions import PreconditionFailedError

router = APIRouter(
    prefix="/metagen",
    tags=["common/metagen"],
    dependencies=[Depends(require_common)],
)


_ScheduleTier = Literal["hourly", "daily", "weekly"]


def _conf_response(dto: Any) -> MetagenGlobalConfResponse:
    return MetagenGlobalConfResponse(
        is_enabled=dto.is_enabled,
        schedule_tier=cast(_ScheduleTier | None, dto.schedule_tier),
        dataset_filter=dto.dataset_filter,
        result_limit=dto.result_limit,
        overwrite_pending=dto.overwrite_pending,
        updated_at=dto.updated_at,
    )


# ── Singleton conf ────────────────────────────────────────────────────────────


@router.get("/attr/conf", response_model=MetagenGlobalConfResponse | None)
async def get_metagen_conf(
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenGlobalConfResponse | None:
    dto = await service.get_global_conf()
    return _conf_response(dto) if dto is not None else None


@router.put("/attr/conf", response_model=MetagenGlobalConfResponse)
async def put_metagen_conf(
    body: MetagenGlobalConfPutRequest,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenGlobalConfResponse:
    dto = await service.put_global_conf(body.model_dump())
    return _conf_response(dto)


@router.patch("/attr/conf", response_model=MetagenGlobalConfResponse)
async def patch_metagen_conf(
    body: MetagenGlobalConfPatchRequest,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenGlobalConfResponse:
    dto = await service.patch_global_conf(body.model_dump(exclude_unset=True))
    return _conf_response(dto)


@router.delete("/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metagen_conf(
    service: MetagenService = Depends(get_metagen_service),
) -> None:
    await service.delete_global_conf()


# ── Run ───────────────────────────────────────────────────────────────────────


_DebateOutcome = Literal["accept", "turns_exhausted", "cycle_detected"]
_RunStatus = Literal["success", "failure"]


@router.post("/method/run", response_model=MetagenRunResponse)
async def post_metagen_run(
    body: MetagenRunRequest = Body(default_factory=MetagenRunRequest),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenRunResponse:
    """Trigger a manual metagen generation run.

    Concurrent runs return 409 METAGEN_RUNNING.
    Rejected with 409 METAGEN_DISABLED when conf is disabled and dry_run is not true.
    """
    result = await service.run(
        dataset_urns=body.dataset_urns,
        dry_run=body.dry_run,
    )
    return MetagenRunResponse(
        run_id=result.run_id,
        status=cast(_RunStatus, result.status),
        dry_run=result.dry_run,
        unresolved_urns=result.unresolved_urns,
        counts=result.counts,
        producer_iterations=result.producer_iterations,
        debate_outcome=cast(_DebateOutcome | None, result.debate_outcome),
    )


# ── Global events ─────────────────────────────────────────────────────────────


@router.get("/event", response_model=EventListResponse)
async def get_metagen_events(
    event_type: str | None = Query(default=None),
    after: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """Global metagen run event history (METAGEN.RUN_COMPLETE, METAGEN.RUN_FAILED)."""
    base = select(Event).where(
        Event.entity_type == "metagen",
        Event.event_type.startswith(METAGEN_PREFIX),
    )
    if event_type is not None:
        base = base.where(Event.event_type == event_type)
    if after is not None:
        base = base.where(Event.occurred_at > after)

    count_q = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    rows_q = base.order_by(Event.occurred_at.desc()).offset(offset).limit(limit)
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


# ── Item list (cross-dataset) ─────────────────────────────────────────────────


@router.get("/item", response_model=MetagenItemListResponse)
async def get_metagen_items(
    dataset_urn: str | None = Query(default=None),
    kind: Literal["dataset.description", "column.description"] | None = Query(default=None),
    status_filter: Literal["pending", "llm_approved", "approved"] | None = Query(
        default=None, alias="status"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenItemListResponse:
    dtos, total = await service.list_items(
        dataset_urn=dataset_urn,
        kind=kind,
        status=status_filter,
        offset=offset,
        limit=limit,
    )
    return MetagenItemListResponse(
        items=[to_item_summary(d) for d in dtos],
        total_count=total,
        offset=offset,
        limit=limit,
    )


# ── Item detail by composite id ───────────────────────────────────────────────


@router.get("/item/{composite_id:path}", response_model=MetagenItemDetailResponse)
async def get_metagen_item_by_composite_id(
    composite_id: str,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenItemDetailResponse:
    """Get item detail by composite id `{dataset_urn}::{item_id}`."""
    sep = "::"
    idx = composite_id.find(sep)
    if idx == -1:
        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            "composite_id must be in the form '{dataset_urn}::{item_id}'",
        )
    dataset_urn = composite_id[:idx]
    item_id = composite_id[idx + len(sep):]
    dto = await service.get_item(dataset_urn, item_id)
    return to_item_detail(dto)
