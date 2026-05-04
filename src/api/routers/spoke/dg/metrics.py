"""Governance metrics router — /spoke/dg/metric/...

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: require_dg.
Spec: API.md §Metric (/spoke/dg/metric).

Changes vs legacy:
- Dropped method/activate and method/deactivate POST routes (use is_enabled on PUT/PATCH).
- is_active → is_enabled everywhere.
- Handler names follow convention: get_metric_list, get_metric, get_metric_attr,
  get_metric_conf, put_metric_conf, patch_metric_conf, delete_metric_conf,
  get_metric_results, post_metric_run, get_metric_events.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

import secrets

from src.api.auth.dependencies import require_dg
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
from src.backend.metrics.service import MetricDefinitionRecord, MetricsService
from src.shared.cache.client import RedisClient
from src.shared.db.models import Event, MetricDefinition, MetricResult
from src.shared.exceptions import ConflictError
from src.shared.settings import settings
from src.workflows.airflow.client import AirflowClient

router = APIRouter(
    prefix="/metric",
    tags=["dg/metric"],
    dependencies=[Depends(require_dg)],
)


def _definition_response(m: "MetricDefinitionRecord") -> MetricDefinitionResponse:
    return MetricDefinitionResponse(
        id=m.id,
        title=m.title,
        description=m.description,
        theme=m.theme,
        measurement_query=m.measurement_query,
        schedule_tier=m.schedule_tier,
        is_enabled=m.is_enabled,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.get("", response_model=MetricDefinitionListResponse)
async def get_metric_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    theme: str | None = Query(default=None),
    is_enabled_filter: bool | None = Query(default=None, alias="status"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionListResponse:
    """List metric definitions with optional theme and enabled filters."""
    order_by = parse_sort(sort, {"created_at": MetricDefinition.created_at}, None)
    metrics, total_count = await service.list_metrics(
        offset=offset,
        limit=limit,
        theme_filter=theme,
        is_enabled_filter=is_enabled_filter,
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
    """Get metric summary (identity, theme, enabled status)."""
    metric = await service.get_metric(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/attr", response_model=MetricAttrResponse)
async def get_metric_attr(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricAttrResponse:
    """Get metric attributes overview (theme, schedule_tier, enabled status)."""
    attr = await service.get_metric_attr(metric_id)
    return MetricAttrResponse(**attr)


@router.get("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def get_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Get full metric definition (title, theme, measurement_query, schedule_tier, enabled)."""
    metric = await service.get_metric_config(metric_id)
    return _definition_response(metric)


@router.put("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def put_metric_conf(
    metric_id: str,
    body: UpsertMetricConfigRequest,
    response: Response,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Create or replace a metric definition (upsert).

    Use is_enabled field to enable/disable the metric's scheduled measurement.
    """
    metric, created = await service.upsert_metric_config(
        metric_id=metric_id,
        title=body.title,
        description=body.description,
        theme=body.theme,
        measurement_query=body.measurement_query,
        schedule_tier=body.schedule_tier,
        is_enabled=body.is_enabled,
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
    """Update metric definition fields.

    Set is_enabled=true/false to enable or disable scheduled measurement.
    """
    patch = body.model_dump(exclude_unset=True)
    metric = await service.patch_metric_config(metric_id, patch)
    return _definition_response(metric)


@router.delete("/{metric_id}/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric_conf(
    metric_id: str,
    service: MetricsService = Depends(get_metrics_service),
) -> None:
    """Remove a metric definition and its configuration."""
    await service.delete_metric_config(metric_id)


@router.get("/{metric_id}/attr/result", response_model=MetricResultListResponse)
async def get_metric_results(
    metric_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricResultListResponse:
    """Get measurement results (numeric timeseries; ?from/to for time range)."""
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
    cache: RedisClient = Depends(get_redis),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricRunResultResponse:
    """Trigger a metric measurement run; concurrent runs return 409 METRIC_RUNNING."""
    definition = await service.get_metric(metric_id)
    if not definition.is_enabled and not body.dry_run:
        raise ConflictError(
            "METRIC_DISABLED",
            f"Metric {metric_id} is disabled; only dry-run is permitted",
        )

    workflow_id = f"metrics-{metric_id}"
    lock_key = f"metrics:running:{metric_id}"
    lock_token = secrets.token_urlsafe(16)
    acquired = await cache.set_nx(lock_key, lock_token, ttl_seconds=3600)
    if not acquired:
        raise ConflictError(
            "METRIC_RUNNING",
            f"A metrics DAG run is already running for {metric_id}",
        )
    try:
        await airflow.check_no_duplicate("metrics", "workflow_id", workflow_id, "METRIC_RUNNING")
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
    finally:
        await cache.delete_if_value(lock_key, lock_token)


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
    """Metric run events (run completions, definition changes)."""
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
