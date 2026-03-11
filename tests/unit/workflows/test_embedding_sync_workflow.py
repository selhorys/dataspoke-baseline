"""Unit tests for EmbeddingSyncWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.embedding_sync import EmbeddingSyncParams, EmbeddingSyncWorkflow

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_full_sync_batches(env: WorkflowEnvironment):
    reindex_calls = []

    @activity.defn(name="enumerate_datasets_activity")
    async def mock_enumerate() -> list[str]:
        return [f"urn:li:dataset:d{i}" for i in range(5)]

    @activity.defn(name="reindex_batch_activity")
    async def mock_reindex(dataset_urns: list[str]) -> dict:
        reindex_calls.append(dataset_urns)
        return {"indexed": len(dataset_urns), "errors": []}

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[EmbeddingSyncWorkflow],
        activities=[mock_enumerate, mock_reindex],
    ):
        result = await env.client.execute_workflow(
            EmbeddingSyncWorkflow.run,
            EmbeddingSyncParams(mode="full"),
            id="embedding-sync-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "ok"
    assert result["mode"] == "full"
    assert result["indexed"] == 5
    assert len(reindex_calls) >= 1


async def test_single_dataset_reindex(env: WorkflowEnvironment):
    enumerate_called = False

    @activity.defn(name="enumerate_datasets_activity")
    async def mock_enumerate() -> list[str]:
        nonlocal enumerate_called
        enumerate_called = True
        return []

    @activity.defn(name="reindex_batch_activity")
    async def mock_reindex(dataset_urns: list[str]) -> dict:
        assert dataset_urns == [_DATASET_URN]
        return {"indexed": 1, "errors": []}

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[EmbeddingSyncWorkflow],
        activities=[mock_enumerate, mock_reindex],
    ):
        result = await env.client.execute_workflow(
            EmbeddingSyncWorkflow.run,
            EmbeddingSyncParams(mode="single", dataset_urn=_DATASET_URN),
            id="embedding-sync-test-2",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "ok"
    assert result["mode"] == "single"
    assert not enumerate_called
