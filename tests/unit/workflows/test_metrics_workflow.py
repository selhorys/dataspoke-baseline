"""Unit tests for MetricsCollectionWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.metrics import MetricsCollectionWorkflow, MetricsParams


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_run_and_publish(env: WorkflowEnvironment):
    published = []

    @activity.defn(name="run_metric_activity")
    async def mock_run(metric_id: str) -> dict:
        return {"run_id": "m-001", "status": "success", "detail": {"value": 42.0}}

    @activity.defn(name="aggregate_health_activity")
    async def mock_aggregate() -> dict:
        return {}

    @activity.defn(name="publish_metric_update_activity")
    async def mock_publish(result: dict) -> None:
        published.append(result)

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[MetricsCollectionWorkflow],
        activities=[mock_run, mock_aggregate, mock_publish],
    ):
        result = await env.client.execute_workflow(
            MetricsCollectionWorkflow.run,
            MetricsParams(metric_id="metric-1"),
            id="metrics-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"
    assert len(published) == 1
    assert published[0]["run_id"] == "m-001"


async def test_aggregate_health(env: WorkflowEnvironment):
    aggregate_called = False

    @activity.defn(name="run_metric_activity")
    async def mock_run(metric_id: str) -> dict:
        return {"run_id": "m-002", "status": "success", "detail": {}}

    @activity.defn(name="aggregate_health_activity")
    async def mock_aggregate() -> dict:
        nonlocal aggregate_called
        aggregate_called = True
        return {"engineering": {"avg_score": 75.0}}

    @activity.defn(name="publish_metric_update_activity")
    async def mock_publish(result: dict) -> None:
        pass

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[MetricsCollectionWorkflow],
        activities=[mock_run, mock_aggregate, mock_publish],
    ):
        await env.client.execute_workflow(
            MetricsCollectionWorkflow.run,
            MetricsParams(metric_id="metric-1", aggregate=True),
            id="metrics-test-2",
            task_queue=TASK_QUEUE,
        )
    assert aggregate_called


async def test_no_aggregate_by_default(env: WorkflowEnvironment):
    aggregate_called = False

    @activity.defn(name="run_metric_activity")
    async def mock_run(metric_id: str) -> dict:
        return {"run_id": "m-003", "status": "success", "detail": {}}

    @activity.defn(name="aggregate_health_activity")
    async def mock_aggregate() -> dict:
        nonlocal aggregate_called
        aggregate_called = True
        return {}

    @activity.defn(name="publish_metric_update_activity")
    async def mock_publish(result: dict) -> None:
        pass

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[MetricsCollectionWorkflow],
        activities=[mock_run, mock_aggregate, mock_publish],
    ):
        await env.client.execute_workflow(
            MetricsCollectionWorkflow.run,
            MetricsParams(metric_id="metric-1"),
            id="metrics-test-3",
            task_queue=TASK_QUEUE,
        )
    assert not aggregate_called
