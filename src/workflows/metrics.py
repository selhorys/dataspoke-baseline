"""Metrics collection workflow — run metric, aggregate health, publish updates."""

import json
from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from src.backend.metrics.aggregator import aggregate_health_scores
    from src.backend.metrics.service import MetricsService
    from src.shared.db.session import SessionLocal
    from src.shared.exceptions import DataSpokeError
    from src.shared.notifications.service import NotificationService
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_cache,
        make_datahub,
    )


@dataclass
class MetricsParams:
    metric_id: str
    aggregate: bool = False


@activity.defn
async def run_metric_activity(metric_id: str) -> dict:
    """Run a single metric measurement."""
    datahub = make_datahub()
    cache = make_cache()
    notification = NotificationService()
    try:
        async with SessionLocal() as db:
            service = MetricsService(datahub=datahub, db=db, cache=cache, notification=notification)
            result = await service.run(metric_id)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@activity.defn
async def aggregate_health_activity() -> dict:
    """Aggregate health scores across all departments."""
    datahub = make_datahub()
    cache = make_cache()
    async with SessionLocal() as db:
        health_map = await aggregate_health_scores(datahub=datahub, db=db, cache=cache)
        return {
            dept: {
                "department": h.department,
                "avg_score": h.avg_score,
                "dataset_count": h.dataset_count,
                "worst_datasets": h.worst_datasets,
            }
            for dept, h in health_map.items()
        }


@activity.defn
async def publish_metric_update_activity(result: dict) -> None:
    """Publish metric run result to Redis for WebSocket consumers."""
    cache = make_cache()
    await cache.publish("ws:metric:updates", json.dumps(result))


@workflow.defn
class MetricsCollectionWorkflow:
    """Orchestrate metric collection, health aggregation, and update publishing.

    Workflow ID convention: ``metrics-{metric_id}``
    """

    @workflow.run
    async def run(self, params: MetricsParams) -> dict:
        result = await workflow.execute_activity(
            run_metric_activity,
            args=[params.metric_id],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )

        if params.aggregate:
            await workflow.execute_activity(
                aggregate_health_activity,
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=default_retry_policy(),
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            )

        await workflow.execute_activity(
            publish_metric_update_activity,
            args=[result],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        return result
