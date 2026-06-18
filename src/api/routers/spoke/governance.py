"""Governance metrics router — /spoke/governance/metric/...

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Metric (/spoke/governance/metric).
"""

from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Path, Query, Response, status

from src.api.auth.dependencies import AuthContext, require_authenticated, require_writer
from src.api.dependencies import get_airflow_client, get_metrics_service
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.metrics import (
    CreateMetricConfigRequest,
    MetricAttrResponse,
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    MetricResultListResponse,
    MetricResultResponse,
    MetricRunResultResponse,
    PatchMetricConfigRequest,
    ReplaceMetricConfigRequest,
)
from src.backend.metrics.service import MetricDefinitionRecord, MetricsService
from src.shared.db.models import Event, MetricDefinition, MetricResult
from src.shared.exceptions import ConflictError, NotImplementedAPIError
from src.shared.settings import settings
from src.workflows.airflow.client import AirflowClient

_METRIC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$"
MetricIdParam = Annotated[str, Path(pattern=_METRIC_ID_PATTERN)]

router = APIRouter(
    prefix="/metric",
    tags=["governance/metric"],
    dependencies=[Depends(require_authenticated)],
)


def _definition_response(m: "MetricDefinitionRecord") -> MetricDefinitionResponse:
    return MetricDefinitionResponse(
        id=m.id,
        mode=m.mode,
        is_enabled=m.is_enabled,
        metric_type=m.metric_type,
        title=m.title,
        description=m.description,
        metrics=m.metrics,
        metric_conf=m.metric_conf,
        schedule_tier=m.schedule_tier,
        dataset_filter=m.dataset_filter,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.post("", response_model=MetricDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def post_metric(
    body: CreateMetricConfigRequest,
    response: Response,
    service: MetricsService = Depends(get_metrics_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetricDefinitionResponse:
    """Create a new metric definition; ``metric_id`` is supplied in the request body.

    Returns 409 METRIC_EXISTS when the id already exists.
    Returns 501 NOT_IMPLEMENTED when ``mode`` is 'passive'.
    """
    if body.mode == "passive":
        raise NotImplementedAPIError("Passive mode is reserved for a future release")

    metric = await service.create_metric_config(
        metric_id=body.metric_id,
        mode=body.mode,
        metric_type=body.metric_type,
        title=body.title,
        description=body.description,
        metrics=body.metrics,
        metric_conf=body.metric_conf,
        dataset_filter=body.dataset_filter,
        schedule_tier=body.schedule_tier,
        is_enabled=body.is_enabled,
    )
    response.status_code = status.HTTP_201_CREATED
    return _definition_response(metric)


@router.get("", response_model=MetricDefinitionListResponse)
async def get_metric_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    metric_type: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionListResponse:
    """List metric definitions with optional metric_type, mode, and is_enabled filters."""
    order_by = parse_sort(
        sort,
        {
            "created_at": MetricDefinition.created_at,
            "updated_at": MetricDefinition.updated_at,
            "title": MetricDefinition.title,
        },
        None,
    )
    metrics, total_count = await service.list_metrics(
        offset=offset,
        limit=limit,
        metric_type_filter=metric_type,
        mode_filter=mode,
        is_enabled_filter=is_enabled,
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
    metric_id: MetricIdParam,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Get metric summary (identity, mode, metric_type, enabled status)."""
    metric = await service.get_metric(metric_id)
    return _definition_response(metric)


@router.get("/{metric_id}/attr", response_model=MetricAttrResponse)
async def get_metric_attr(
    metric_id: MetricIdParam,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricAttrResponse:
    """Get metric attributes overview (mode, metric_type, schedule_tier, enabled, latest values)."""
    attr = await service.get_metric_attr(metric_id)
    return MetricAttrResponse(**attr)


@router.get("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def get_metric_conf(
    metric_id: MetricIdParam,
    service: MetricsService = Depends(get_metrics_service),
) -> MetricDefinitionResponse:
    """Get full metric definition."""
    metric = await service.get_metric_config(metric_id)
    return _definition_response(metric)


@router.put("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def put_metric_conf(
    metric_id: MetricIdParam,
    body: ReplaceMetricConfigRequest,
    service: MetricsService = Depends(get_metrics_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetricDefinitionResponse:
    """Replace an existing metric definition.

    Returns 404 METRIC_NOT_FOUND when the id is absent (use POST /spoke/governance/metric
    to create).
    Returns 501 NOT_IMPLEMENTED when mode is 'passive'.
    """
    if body.mode == "passive":
        raise NotImplementedAPIError("Passive mode is reserved for a future release")

    metric = await service.replace_metric_config(
        metric_id=metric_id,
        mode=body.mode,
        metric_type=body.metric_type,
        title=body.title,
        description=body.description,
        metrics=body.metrics,
        metric_conf=body.metric_conf,
        dataset_filter=body.dataset_filter,
        schedule_tier=body.schedule_tier,
        is_enabled=body.is_enabled,
    )
    return _definition_response(metric)


@router.patch("/{metric_id}/attr/conf", response_model=MetricDefinitionResponse)
async def patch_metric_conf(
    metric_id: MetricIdParam,
    body: PatchMetricConfigRequest,
    service: MetricsService = Depends(get_metrics_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetricDefinitionResponse:
    """Update metric definition fields.

    Returns 501 NOT_IMPLEMENTED when the patch sets mode to 'passive'.
    """
    if body.mode == "passive":
        raise NotImplementedAPIError("Passive mode is reserved for a future release")

    patch = body.model_dump(exclude_unset=True)
    metric = await service.patch_metric_config(metric_id, patch)
    return _definition_response(metric)


@router.delete("/{metric_id}/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric_conf(
    metric_id: MetricIdParam,
    service: MetricsService = Depends(get_metrics_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    """Remove a metric definition and its configuration."""
    await service.delete_metric_config(metric_id)


@router.get("/{metric_id}/attr/result", response_model=MetricResultListResponse)
async def get_metric_results(
    metric_id: MetricIdParam,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: MetricsService = Depends(get_metrics_service),
) -> MetricResultListResponse:
    """Get measurement results (dict-valued timeseries; ?from/to for time range)."""
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
                values=r.values,
                breakdown=r.breakdown,
                measured_at=r.measured_at,
            )
            for r in results
        ],
    )


@router.post("/{metric_id}/method/run", response_model=MetricRunResultResponse)
async def post_metric_run(
    metric_id: MetricIdParam,
    dry_run: bool = Query(default=False),
    airflow: AirflowClient = Depends(get_airflow_client),
    service: MetricsService = Depends(get_metrics_service),
    _writer: AuthContext = Depends(require_writer),
) -> MetricRunResultResponse:
    """Trigger a metric measurement run.

    Pass ``?dry_run=true`` to simulate the measurement without persisting results.
    Concurrent runs return 409 METRIC_RUNNING.
    Returns 409 METRIC_DISABLED when the metric is disabled and ``dry_run`` is not true.
    """
    definition = await service.get_metric(metric_id)
    if not definition.is_enabled and not dry_run:
        raise ConflictError(
            "METRIC_DISABLED",
            f"Metric {metric_id} is disabled; only dry-run is permitted",
        )

    workflow_id = f"metrics-{metric_id}"
    await airflow.check_no_duplicate("metrics", "workflow_id", workflow_id, "METRIC_RUNNING")
    try:
        dag_run = await airflow.trigger_and_wait(
            "metrics",
            conf={
                "callback_base_url": settings.airflow_callback_base_url,
                "metric_id": metric_id,
                "dry_run": str(dry_run).lower(),
                "workflow_id": workflow_id,
            },
        )
    except httpx.HTTPStatusError as exc:
        # check_no_duplicate is non-atomic; concurrent callers race past it and
        # Airflow rejects all but one with 409 "DAG run already exists".
        if exc.response.status_code == 409:
            raise ConflictError(
                "METRIC_RUNNING",
                f"Metric measurement is already running for {metric_id}",
            ) from exc
        raise
    xcom_value = await airflow.fetch_task_xcom(
        dag_id="metrics",
        dag_run_id=dag_run.dag_run_id,
        task_id="run_metric",
    )
    if (
        isinstance(xcom_value, dict)
        and xcom_value.get("status") == "error"
        and xcom_value.get("detail", {}).get("error_code") == "METRIC_RUNNING"
    ):
        raw_msg = xcom_value["detail"].get(
            "message", f"Metric measurement is already running for {metric_id}"
        )
        msg = (raw_msg[:200] + "…") if len(raw_msg) > 200 else raw_msg
        raise ConflictError("METRIC_RUNNING", msg)
    if isinstance(xcom_value, dict):
        return MetricRunResultResponse(
            run_id=xcom_value.get("run_id", dag_run.dag_run_id),
            status=xcom_value.get("status", dag_run.state.value),
            detail=xcom_value.get("detail", {}),
        )
    return MetricRunResultResponse(
        run_id=dag_run.dag_run_id,
        status=dag_run.state.value,
        detail={},
    )


@router.get("/{metric_id}/event", response_model=EventListResponse)
async def get_metric_events(
    metric_id: MetricIdParam,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
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
