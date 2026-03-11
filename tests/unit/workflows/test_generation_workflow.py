"""Unit tests for GenerationWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.generation import GenerationParams, GenerationWorkflow


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_happy_path(env: WorkflowEnvironment):
    @activity.defn(name="run_generation_activity")
    async def mock_run(dataset_urn: str) -> dict:
        return {
            "run_id": "g-001",
            "status": "success",
            "detail": {"proposals": 3, "applied": 0},
        }

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[GenerationWorkflow], activities=[mock_run]
    ):
        result = await env.client.execute_workflow(
            GenerationWorkflow.run,
            GenerationParams(dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"),
            id="generation-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"
    assert result["detail"]["proposals"] == 3


async def test_activity_failure_raises(env: WorkflowEnvironment):
    @activity.defn(name="run_generation_activity")
    async def mock_run(dataset_urn: str) -> dict:
        raise RuntimeError("LLM API error")

    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[GenerationWorkflow], activities=[mock_run]
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                GenerationWorkflow.run,
                GenerationParams(
                    dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"
                ),
                id="generation-test-2",
                task_queue=TASK_QUEUE,
            )
        assert "LLM API error" in str(exc_info.value.cause.cause)
