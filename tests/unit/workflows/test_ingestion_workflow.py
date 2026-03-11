"""Unit tests for IngestionWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.ingestion import IngestionParams, IngestionWorkflow


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_happy_path(env: WorkflowEnvironment):
    @activity.defn(name="run_ingestion_activity")
    async def mock_run(dataset_urn: str, dry_run: bool) -> dict:
        return {"run_id": "r-001", "status": "success", "detail": {"dry_run": dry_run}}

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestionWorkflow], activities=[mock_run]
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"),
            id="ingestion-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"
    assert result["run_id"] == "r-001"


async def test_dry_run_flag_propagated(env: WorkflowEnvironment):
    captured_dry_run = None

    @activity.defn(name="run_ingestion_activity")
    async def mock_run(dataset_urn: str, dry_run: bool) -> dict:
        nonlocal captured_dry_run
        captured_dry_run = dry_run
        return {"run_id": "r-002", "status": "success", "detail": {"dry_run": dry_run}}

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestionWorkflow], activities=[mock_run]
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(
                dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)",
                dry_run=True,
            ),
            id="ingestion-test-2",
            task_queue=TASK_QUEUE,
        )
    assert result["detail"]["dry_run"] is True
    assert captured_dry_run is True


async def test_activity_failure_raises(env: WorkflowEnvironment):
    @activity.defn(name="run_ingestion_activity")
    async def mock_run(dataset_urn: str, dry_run: bool) -> dict:
        raise RuntimeError("Connection refused")

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IngestionWorkflow], activities=[mock_run]
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                IngestionWorkflow.run,
                IngestionParams(
                    dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"
                ),
                id="ingestion-test-3",
                task_queue=TASK_QUEUE,
            )
        assert "Connection refused" in str(exc_info.value.cause.cause)
