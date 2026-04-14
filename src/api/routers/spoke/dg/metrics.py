from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from src.api.auth.dependencies import require_dg
from src.api.auth.ws import ws_authenticate
from src.api.dependencies import get_airflow_client, get_metrics_service, get_redis
from src.api.schemas.common import parse_sort
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
from src.shared.db.models import Event, MetricDefinition, MetricResult
from src.shared.settings import settings
from src.workflows.airflow.client import AirflowClient

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
        schedule_tier=m.schedule_tier,
        is_active=m.is_active,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.get("", response_model=MetricDefinitionListResponse)
async def get_metrics(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    theme: str | None = Query(default=None),
    is_active_filter: bool | None = Query(default=None, alias="is_active"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionListResponse:
    """List metric definitions with optional theme and active filters."""
    order_by = parse_sort(sort, {"created_at": MetricDefinition.created_at}, None)
    metrics, total_count = await service.list_metrics(
        offset=offset,
        limit=limit,
        theme_filter=theme,
        is_active_filter=is_active_filter,
        order_by=order_by,
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
    """Retrieve a single metric definition by ID."""
    metric = await service.get_metric(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/attr", response_model=MetricAttrResponse)
async def get_metric_attr(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricAttrResponse:
    """Retrieve the aggregated attribute sub-resource for a metric."""
    attr = await service.get_metric_attr(metric_id)
    return MetricAttrResponse(**attr)


@router.get("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def get_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Retrieve the configuration for a metric definition."""
    metric = await service.get_metric_config(metric_id)
    return _definition_response(metric)


@router.put("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def put_metric_conf(
    metric_id: str,
    body: UpsertMetricConfigRequest,
    response: Response,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Create or replace a metric definition configuration (upsert)."""
    metric, created = await service.upsert_metric_config(
        metric_id=metric_id,
        title=body.title,
        description=body.description,
        theme=body.theme,
        measurement_query=body.measurement_query,
        schedule_tier=body.schedule_tier,
        is_active=body.is_active,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return _definition_response(metric)


@router.patch("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def patch_metric_conf(
    metric_id: str,
    body: PatchMetricConfigRequest,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Partially update a metric definition's configuration."""
    patch = body.model_dump(exclude_unset=True)
    metric = await service.patch_metric_config(metric_id, patch)
    return _definition_response(metric)


@router.delete("/{metric_id}/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> None:
    """Delete a metric definition and its configuration."""
    await service.delete_metric_config(metric_id)


@router.get("/{metric_id}/attr/result", response_model=MetricResultListResponse)
async def get_metric_result(
    metric_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricResultListResponse:
    """List metric measurement results with optional time range and pagination."""
    order_by = parse_sort(sort, {"measured_at": MetricResult.measured_at}, None)
    results, total_count = await service.get_results(
        metric_id,
        from_dt=from_time,
        to_dt=to_time,
        offset=offset,
        limit=limit,
        order_by=order_by,
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
                measured_at=r.measured_at,
            )
            for r in results
        ],
    )


@router.post("/{metric_id}/method/run", response_model=MetricRunResultResponse)
async def post_metric_run(
    metric_id: str,
    body: RunMetricRequest,
    airflow: AirflowClient = Depends(get_airflow_client),
) -> MetricRunResultResponse:
    """Trigger a metric measurement run via Airflow."""
    workflow_id = f"metrics-{metric_id}"
    await airflow.check_no_duplicate(
        "metrics", "workflow_id", workflow_id, "METRIC_RUNNING"
    )
    dag_run = await airflow.trigger_and_wait(
        "metrics",
        conf={
            "callback_base_url": settings.airflow_callback_base_url,
            "metric_id": metric_id,
            "dry_run": str(body.dry_run).lower(),
            "workflow_id": workflow_id,
        },
    )
    conf_out = dag_run.conf or {}
    return MetricRunResultResponse(
        run_id=conf_out.get("run_id", dag_run.dag_run_id),
        status=conf_out.get("status", dag_run.state.value),
        detail=conf_out.get("detail", {}),
    )


@router.post("/{metric_id}/method/activate", response_model=MetricDefinitionResponse)
async def post_metric_activate(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Activate a metric definition to enable scheduled measurement runs."""
    metric = await service.activate(metric_id)
    return _definition_response(metric)


@router.post("/{metric_id}/method/deactivate", response_model=MetricDefinitionResponse)
async def post_metric_deactivate(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Deactivate a metric definition to pause scheduled measurement runs."""
    metric = await service.deactivate(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/event", response_model=EventListResponse)
async def get_metric_events(
    metric_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> EventListResponse:
    """List events for a metric with time range and pagination."""
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events(
        metric_id,
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
