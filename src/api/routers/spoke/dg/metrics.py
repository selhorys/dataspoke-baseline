from datetime import datetime

from fastapi import APIRouter, Depends, Query, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from src.api.auth.dependencies import require_dg
from src.api.auth.ws import ws_authenticate
from src.api.dependencies import get_metrics_service, get_redis
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.metrics import (
    MetricAttrResponse,
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    MetricResultListResponse,
    MetricResultResponse,
    MetricRunResultResponse,
    PatchMetricConfigRequest,
    RunMetricRequest,
    UpsertMetricConfigRequest,
)
from src.backend.metrics.service import MetricsService

router = APIRouter(
    prefix="/metric",
    tags=["dg/metric"],
    dependencies=[Depends(require_dg)],
)


def _definition_response(m) -> MetricDefinitionResponse:  # noqa: ANN001
    return MetricDefinitionResponse(
        id=m.id,
        title=m.title,
        description=m.description,
        theme=m.theme,
        measurement_query=m.measurement_query,
        schedule=m.schedule,
        alarm_enabled=m.alarm_enabled,
        alarm_threshold=m.alarm_threshold,
        active=m.active,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.get("", response_model=MetricDefinitionListResponse)
async def list_metrics(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    theme: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionListResponse:
    metrics, total_count = await service.list_metrics(
        offset=offset, limit=limit, theme_filter=theme, active_filter=active
    )
    return MetricDefinitionListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        metrics=[_definition_response(m) for m in metrics],
    )


@router.get("/{metric_id}", response_model=MetricDefinitionResponse)
async def get_metric(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    metric = await service.get_metric(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/attr", response_model=MetricAttrResponse)
async def get_metric_attr(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricAttrResponse:
    attr = await service.get_metric_attr(metric_id)
    return MetricAttrResponse(**attr)


@router.get("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def get_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    metric = await service.get_metric_config(metric_id)
    return _definition_response(metric)


@router.put("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def put_metric_conf(
    metric_id: str,
    body: UpsertMetricConfigRequest,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    metric = await service.upsert_metric_config(
        metric_id=metric_id,
        title=body.title,
        description=body.description,
        theme=body.theme,
        measurement_query=body.measurement_query,
        schedule=body.schedule,
        alarm_enabled=body.alarm_enabled,
        alarm_threshold=body.alarm_threshold,
        active=body.active,
    )
    return _definition_response(metric)


@router.patch("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def patch_metric_conf(
    metric_id: str,
    body: PatchMetricConfigRequest,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    patch = body.model_dump(exclude_unset=True)
    metric = await service.patch_metric_config(metric_id, patch)
    return _definition_response(metric)


@router.delete("/{metric_id}/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> None:
    await service.delete_metric_config(metric_id)


@router.get("/{metric_id}/attr/result", response_model=MetricResultListResponse)
async def get_metric_result(
    metric_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricResultListResponse:
    results, total_count = await service.get_results(
        metric_id, from_dt=from_time, to_dt=to_time, offset=offset, limit=limit
    )
    return MetricResultListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        results=[
            MetricResultResponse(
                id=r.id,
                metric_id=r.metric_id,
                value=r.value,
                breakdown=r.breakdown,
                alarm_triggered=r.alarm_triggered,
                run_id=r.run_id,
                measured_at=r.measured_at,
            )
            for r in results
        ],
    )


@router.post("/{metric_id}/method/run", response_model=MetricRunResultResponse)
async def run_metric(
    metric_id: str,
    body: RunMetricRequest,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricRunResultResponse:
    result = await service.run(metric_id, dry_run=body.dry_run)
    return MetricRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.post("/{metric_id}/method/activate", response_model=MetricDefinitionResponse)
async def activate_metric(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    metric = await service.activate(metric_id)
    return _definition_response(metric)


@router.post("/{metric_id}/method/deactivate", response_model=MetricDefinitionResponse)
async def deactivate_metric(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    metric = await service.deactivate(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/event", response_model=EventListResponse)
async def get_metric_events(
    metric_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> EventListResponse:
    events, total_count = await service.get_events(
        metric_id, offset=offset, limit=limit, from_dt=from_time, to_dt=to_time
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


# ── WebSocket: metric update stream ───────────────────────────────────────────

# Separate router without HTTP auth dependencies — WebSocket routes handle
# authentication via the message-based handshake inside the handler.
ws_router = APIRouter(prefix="/metric", tags=["dg/metric"])


@ws_router.websocket("/stream")
async def stream_metrics(websocket: WebSocket) -> None:
    """Stream metric updates via Redis pub/sub.

    Protocol:
    1. Client sends ``{"type": "auth", "token": "<jwt>"}``
    2. Server replies ``{"type": "auth_ok"}`` then forwards Redis messages
    3. Connection stays open until the client disconnects
    """

    await websocket.accept()

    if not await ws_authenticate(websocket):
        return

    cache = get_redis()
    try:
        async for message in cache.subscribe("ws:metric:updates"):
            await websocket.send_text(message)
    except WebSocketDisconnect:
        pass
    finally:
        await cache.close()
        await websocket.close()
