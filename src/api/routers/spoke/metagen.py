"""Metadata Generation router — /spoke/metagen/...

Routes:
  conf collection       GET/POST /metagen/conf
  conf item             GET/PUT/PATCH/DELETE /metagen/conf/{conf_id}
  run                   POST /metagen/conf/{conf_id}/method/run
  per-conf events       GET /metagen/conf/{conf_id}/event
  uncovered view        GET /metagen/uncovered
  global events         GET /metagen/event
  item list             GET /metagen/item
  item detail           GET /metagen/item/{composite_id}

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Metadata Generation (/spoke/metagen).
"""

from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import AuthContext, require_authenticated, require_writer
from src.api.dependencies import get_db, get_metagen_service
from src.api.routers.spoke._metagen_mappers import (
    event_list,
    to_item_detail,
    to_item_summary,
)
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse
from src.api.schemas.metagen import (
    MetagenConfCreateRequest,
    MetagenConfListResponse,
    MetagenConfPatchRequest,
    MetagenConfPutRequest,
    MetagenConfResponse,
    MetagenItemDetailResponse,
    MetagenItemListResponse,
    MetagenRunRequest,
    MetagenRunResponse,
    MetagenUncoveredResponse,
    MetagenUncoveredRow,
)
from src.backend.metagen.service import MetagenService
from src.shared.db.models import (
    DatasetRegistry,
    Event,
    MetagenConfig,
    MetagenItem,
)
from src.shared.events import METAGEN_PREFIX
from src.shared.exceptions import PreconditionFailedError

router = APIRouter(
    prefix="/metagen",
    tags=["metagen"],
    dependencies=[Depends(require_authenticated)],
)


_ScheduleTier = Literal["hourly", "daily", "weekly"]
_DebateOutcome = Literal["accept", "turns_exhausted", "cycle_detected"]
_RunStatus = Literal["success", "failure"]
_UncoveredReason = Literal["no_conf_match", "boundary_blocked"]


def _conf_response(dto: Any) -> MetagenConfResponse:
    return MetagenConfResponse(
        id=dto.id,
        name=dto.name,
        is_enabled=dto.is_enabled,
        schedule_tier=cast(_ScheduleTier | None, dto.schedule_tier),
        dataset_filter=dto.dataset_filter,
        result_limit=dto.result_limit,
        overwrite_pending=dto.overwrite_pending,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ── Conf collection ───────────────────────────────────────────────────────────


@router.get("/conf", response_model=MetagenConfListResponse)
async def get_metagen_confs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenConfListResponse:
    """List metagen confs (paginated; sortable by created_at, updated_at)."""
    order_by = parse_sort(
        sort,
        {"created_at": MetagenConfig.created_at, "updated_at": MetagenConfig.updated_at},
        None,
    )
    dtos, total = await service.list_confs(offset=offset, limit=limit, order_by=order_by)
    return MetagenConfListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        confs=[_conf_response(d) for d in dtos],
    )


@router.post("/conf", response_model=MetagenConfResponse, status_code=status.HTTP_201_CREATED)
async def post_metagen_conf(
    body: MetagenConfCreateRequest,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenConfResponse:
    """Create a metagen conf. Returns ``409 METAGEN_CONF_EXISTS`` on duplicate name."""
    dto = await service.create_conf(body.model_dump())
    return _conf_response(dto)


@router.get("/conf/{conf_id}", response_model=MetagenConfResponse)
async def get_metagen_conf(
    conf_id: str,
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenConfResponse:
    """Get one conf. Returns ``404 METAGEN_CONF_NOT_FOUND`` when absent."""
    dto = await service.get_conf(conf_id)
    return _conf_response(dto)


@router.put("/conf/{conf_id}", response_model=MetagenConfResponse)
async def put_metagen_conf(
    conf_id: str,
    body: MetagenConfPutRequest,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenConfResponse:
    """Replace a conf. ``404 METAGEN_CONF_NOT_FOUND`` when absent, ``409 METAGEN_CONF_EXISTS``
    on a colliding name."""
    dto = await service.put_conf(conf_id, body.model_dump())
    return _conf_response(dto)


@router.patch("/conf/{conf_id}", response_model=MetagenConfResponse)
async def patch_metagen_conf(
    conf_id: str,
    body: MetagenConfPatchRequest,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenConfResponse:
    dto = await service.patch_conf(conf_id, body.model_dump(exclude_unset=True))
    return _conf_response(dto)


@router.delete("/conf/{conf_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metagen_conf(
    conf_id: str,
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    """Delete a conf — drops its non-approved candidates and SET NULLs conf_id on its
    approved (already-emitted) candidates."""
    await service.delete_conf(conf_id)


# ── Run ───────────────────────────────────────────────────────────────────────


@router.post("/conf/{conf_id}/method/run", response_model=MetagenRunResponse)
async def post_metagen_conf_run(
    conf_id: str,
    body: MetagenRunRequest = Body(default_factory=MetagenRunRequest),
    dry_run: bool = Query(default=False),
    service: MetagenService = Depends(get_metagen_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetagenRunResponse:
    """Trigger a manual generation run for this conf.

    Pass ``?dry_run=true`` to simulate without persisting. Optional ``{dataset_urns}``
    body narrows the scope. Concurrent runs of the same conf return 409 METAGEN_RUNNING.
    Rejected with 409 METAGEN_DISABLED when the conf is disabled and ``dry_run`` is not true.
    """
    result = await service.run(
        conf_id,
        dataset_urns=body.dataset_urns,
        dry_run=dry_run,
    )
    return MetagenRunResponse(
        run_id=result.run_id,
        conf_id=result.conf_id,
        status=cast(_RunStatus, result.status),
        dry_run=result.dry_run,
        unresolved_urns=result.unresolved_urns,
        counts=result.counts,
        producer_iterations=result.producer_iterations,
        debate_outcome=cast(_DebateOutcome | None, result.debate_outcome),
    )


# ── Per-conf events ─────────────────────────────────────────────────────────────


@router.get("/conf/{conf_id}/event", response_model=EventListResponse)
async def get_metagen_conf_event(
    conf_id: str,
    event_type: str | None = Query(default=None),
    after: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """Per-conf generation-run event history (METAGEN.RUN_COMPLETE, METAGEN.RUN_FAILED).

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    base = select(Event).where(
        Event.entity_type == "metagen",
        Event.entity_id == conf_id,
        Event.event_type.startswith(METAGEN_PREFIX),
    )
    if event_type is not None:
        base = base.where(Event.event_type == event_type)
    if after is not None:
        base = base.where(Event.occurred_at > after)

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


# ── Uncovered view ──────────────────────────────────────────────────────────────


@router.get("/uncovered", response_model=MetagenUncoveredResponse)
async def get_metagen_uncovered(
    include_disallowed: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenUncoveredResponse:
    """Registered datasets reached by no enabled conf.

    With ``?include_disallowed=true`` also includes datasets matched by a conf but
    blocked by the boundary. Each row carries a ``reason``. Paginated; sortable by
    ``dataset_urn`` (default: ``dataset_urn`` ascending).
    """
    order_by = parse_sort(sort, {"dataset_urn": DatasetRegistry.dataset_urn}, None)
    rows, total = await service.list_uncovered(
        include_disallowed=include_disallowed,
        offset=offset,
        limit=limit,
        order_by=order_by,
    )
    return MetagenUncoveredResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        datasets=[
            MetagenUncoveredRow(
                dataset_urn=r.dataset_urn,
                reason=cast(_UncoveredReason, r.reason),
            )
            for r in rows
        ],
    )


# ── Global events (cross-conf union) ────────────────────────────────────────────


@router.get("/event", response_model=EventListResponse)
async def get_metagen_events(
    event_type: str | None = Query(default=None),
    after: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """Cross-conf union of all confs' generation-run events.

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
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


# ── Item list (cross-dataset, cross-conf) ───────────────────────────────────────


@router.get("/item", response_model=MetagenItemListResponse)
async def get_metagen_items(
    dataset_urn: str | None = Query(default=None),
    kind: Literal["dataset.description", "column.description"] | None = Query(default=None),
    status_filter: Literal["pending", "llm_approved", "approved"] | None = Query(
        default=None, alias="status"
    ),
    conf_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenItemListResponse:
    """List metagen items (paginated; sortable by created_at, updated_at)."""
    order_by = parse_sort(
        sort,
        {"created_at": MetagenItem.created_at, "updated_at": MetagenItem.updated_at},
        None,
    )
    dtos, total = await service.list_items(
        dataset_urn=dataset_urn,
        kind=kind,
        status=status_filter,
        conf_id=conf_id,
        offset=offset,
        limit=limit,
        order_by=order_by,
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
    item_id = composite_id[idx + len(sep) :]
    dto = await service.get_item(dataset_urn, item_id)
    return to_item_detail(dto)
