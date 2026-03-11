"""Unit tests for OntologyRebuildWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.ontology import OntologyRebuildParams, OntologyRebuildWorkflow


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_full_rebuild(env: WorkflowEnvironment):
    activity_order = []

    @activity.defn(name="classify_datasets_activity")
    async def mock_classify() -> list[dict]:
        activity_order.append("classify")
        return [
            {"dataset_urn": "urn:d1", "category": "finance", "confidence": 0.9, "field_count": 10},
            {"dataset_urn": "urn:d2", "category": "hr", "confidence": 0.85, "field_count": 5},
        ]

    @activity.defn(name="build_hierarchy_activity")
    async def mock_hierarchy(classifications: list[dict]) -> list[dict]:
        activity_order.append("hierarchy")
        return [
            {"concept_id": "c1", "name": "finance", "dataset_count": 1, "dataset_urns": ["urn:d1"]},
            {"concept_id": "c2", "name": "hr", "dataset_count": 1, "dataset_urns": ["urn:d2"]},
        ]

    @activity.defn(name="infer_relationships_activity")
    async def mock_relationships(hierarchy: list[dict]) -> list[dict]:
        activity_order.append("relationships")
        return []

    @activity.defn(name="detect_drift_activity")
    async def mock_drift(current_hierarchy: list[dict]) -> list[dict]:
        activity_order.append("drift")
        return [{"type": "new_category", "name": "finance"}]

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[OntologyRebuildWorkflow],
        activities=[mock_classify, mock_hierarchy, mock_relationships, mock_drift],
    ):
        result = await env.client.execute_workflow(
            OntologyRebuildWorkflow.run,
            OntologyRebuildParams(),
            id="ontology-test-1",
            task_queue=TASK_QUEUE,
        )
    assert result["classifications"] == 2
    assert result["hierarchy_nodes"] == 2
    assert result["relationships"] == 0
    assert result["drift_detected"] is True
    assert activity_order == ["classify", "hierarchy", "relationships", "drift"]


async def test_no_drift_detected(env: WorkflowEnvironment):
    @activity.defn(name="classify_datasets_activity")
    async def mock_classify() -> list[dict]:
        return [{"dataset_urn": "urn:d1", "category": "sales", "confidence": 0.8, "field_count": 3}]

    @activity.defn(name="build_hierarchy_activity")
    async def mock_hierarchy(classifications: list[dict]) -> list[dict]:
        return [
            {"concept_id": "c1", "name": "sales", "dataset_count": 1, "dataset_urns": ["urn:d1"]}
        ]

    @activity.defn(name="infer_relationships_activity")
    async def mock_relationships(hierarchy: list[dict]) -> list[dict]:
        return []

    @activity.defn(name="detect_drift_activity")
    async def mock_drift(current_hierarchy: list[dict]) -> list[dict]:
        return []

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[OntologyRebuildWorkflow],
        activities=[mock_classify, mock_hierarchy, mock_relationships, mock_drift],
    ):
        result = await env.client.execute_workflow(
            OntologyRebuildWorkflow.run,
            OntologyRebuildParams(),
            id="ontology-test-2",
            task_queue=TASK_QUEUE,
        )
    assert result["drift_detected"] is False


async def test_classification_failure_raises(env: WorkflowEnvironment):
    @activity.defn(name="classify_datasets_activity")
    async def mock_classify() -> list[dict]:
        raise RuntimeError("DataHub unreachable")

    @activity.defn(name="build_hierarchy_activity")
    async def mock_hierarchy(classifications: list[dict]) -> list[dict]:
        return []

    @activity.defn(name="infer_relationships_activity")
    async def mock_relationships(hierarchy: list[dict]) -> list[dict]:
        return []

    @activity.defn(name="detect_drift_activity")
    async def mock_drift(current_hierarchy: list[dict]) -> list[dict]:
        return []

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[OntologyRebuildWorkflow],
        activities=[mock_classify, mock_hierarchy, mock_relationships, mock_drift],
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                OntologyRebuildWorkflow.run,
                OntologyRebuildParams(),
                id="ontology-test-3",
                task_queue=TASK_QUEUE,
            )
        assert "DataHub unreachable" in str(exc_info.value.cause.cause)
