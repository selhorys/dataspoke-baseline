"""Unit tests for ValidationWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.validation import ValidationParams, ValidationWorkflow


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_happy_path(env: WorkflowEnvironment):
    @activity.defn(name="run_validation_activity")
    async def mock_run(dataset_urn: str, config_id: str | None, dry_run: bool) -> dict:
        return {
            "run_id": "v-001",
            "status": "success",
            "detail": {"quality_score": 85.5, "issues": []},
        }

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[ValidationWorkflow], activities=[mock_run]
    ):
        result = await env.client.execute_workflow(
            ValidationWorkflow.run,
            ValidationParams(dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"),
            id="validation-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"
    assert result["detail"]["quality_score"] == 85.5


async def test_dry_run_propagated(env: WorkflowEnvironment):
    captured_dry_run = None

    @activity.defn(name="run_validation_activity")
    async def mock_run(dataset_urn: str, config_id: str | None, dry_run: bool) -> dict:
        nonlocal captured_dry_run
        captured_dry_run = dry_run
        return {"run_id": "v-002", "status": "success", "detail": {"dry_run": dry_run}}

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[ValidationWorkflow], activities=[mock_run]
    ):
        await env.client.execute_workflow(
            ValidationWorkflow.run,
            ValidationParams(
                dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)",
                dry_run=True,
            ),
            id="validation-test-2",
            task_queue=TASK_QUEUE,
        )
    assert captured_dry_run is True


async def test_activity_failure_raises(env: WorkflowEnvironment):
    @activity.defn(name="run_validation_activity")
    async def mock_run(dataset_urn: str, config_id: str | None, dry_run: bool) -> dict:
        raise RuntimeError("DB unavailable")

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[ValidationWorkflow], activities=[mock_run]
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                ValidationWorkflow.run,
                ValidationParams(
                    dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"
                ),
                id="validation-test-3",
                task_queue=TASK_QUEUE,
            )
        assert "DB unavailable" in str(exc_info.value.cause.cause)
