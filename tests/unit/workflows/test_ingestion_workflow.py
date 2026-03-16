"""Unit tests for IngestionWorkflow using Temporal test framework."""

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.ingestion import (
    IngestionParams,
    IngestionWorkflow,
    emit_to_datahub_activity,
    extract_metadata_activity,
    record_ingestion_event_activity,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"

_EXTRACT_RESULT = {
    "run_id": "r-001",
    "metadata": [{"metadata_type": "description", "content": {"title": "T"}, "source_ref": "s"}],
    "errors": [],
    "sources_processed": 1,
    "deep_spec_enabled": False,
}

_EMIT_RESULT = {
    "status": "success",
    "detail": {
        "run_id": "r-001",
        "sources_processed": 1,
        "metadata_extracted": 1,
        "dry_run": False,
    },
}

_RECORD_RESULT = {"run_id": "r-001", "status": "success", "detail": _EMIT_RESULT["detail"]}


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _make_activities(
    extract_fn=None,
    emit_fn=None,
    record_fn=None,
):
    """Return a list of three mock activities, using no-op defaults when omitted."""

    @activity.defn(name="extract_metadata_activity")
    async def default_extract(dataset_urn: str, run_id: str) -> dict:
        return {**_EXTRACT_RESULT, "run_id": run_id}

    @activity.defn(name="emit_to_datahub_activity")
    async def default_emit(dataset_urn: str, extract_result: dict) -> dict:
        return _EMIT_RESULT

    @activity.defn(name="record_ingestion_event_activity")
    async def default_record(dataset_urn: str, run_id: str, status: str, detail: dict) -> dict:
        return {"run_id": run_id, "status": status, "detail": detail}

    return [
        extract_fn or default_extract,
        emit_fn or default_emit,
        record_fn or default_record,
    ]


async def test_happy_path(env: WorkflowEnvironment):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(),
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN),
            id="test-ingestion-happy-path",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"
    assert "run_id" in result


async def test_dry_run_skips_emit(env: WorkflowEnvironment):
    emit_called = False

    @activity.defn(name="emit_to_datahub_activity")
    async def track_emit(dataset_urn: str, extract_result: dict) -> dict:
        nonlocal emit_called
        emit_called = True
        return _EMIT_RESULT

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(emit_fn=track_emit),
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN, dry_run=True),
            id="test-ingestion-dry-run",
            task_queue=TASK_QUEUE,
        )

    assert not emit_called, "emit_to_datahub_activity must not be called during a dry run"
    assert result["detail"]["dry_run"] is True


async def test_dry_run_propagates_status_success(env: WorkflowEnvironment):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(),
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN, dry_run=True),
            id="test-ingestion-dry-run-status",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "success"


async def test_dry_run_partial_when_extractor_errors(env: WorkflowEnvironment):
    @activity.defn(name="extract_metadata_activity")
    async def partial_extract(dataset_urn: str, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "metadata": [{"metadata_type": "description", "content": {}, "source_ref": "s"}],
            "errors": ["sql_log: parse failed"],
            "sources_processed": 2,
            "deep_spec_enabled": False,
        }

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(extract_fn=partial_extract),
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN, dry_run=True),
            id="test-ingestion-dry-run-partial",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "partial"
    assert result["detail"]["extractor_errors"] == ["sql_log: parse failed"]


async def test_dry_run_error_when_all_extractors_fail(env: WorkflowEnvironment):
    @activity.defn(name="extract_metadata_activity")
    async def all_fail_extract(dataset_urn: str, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "metadata": [],
            "errors": ["confluence: 401"],
            "sources_processed": 1,
            "deep_spec_enabled": False,
        }

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(extract_fn=all_fail_extract),
    ):
        result = await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN, dry_run=True),
            id="test-ingestion-dry-run-error",
            task_queue=TASK_QUEUE,
        )
    assert result["status"] == "error"


async def test_extract_failure_raises_workflow_error(env: WorkflowEnvironment):
    @activity.defn(name="extract_metadata_activity")
    async def failing_extract(dataset_urn: str, run_id: str) -> dict:
        raise RuntimeError("DB connection refused")

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(extract_fn=failing_extract),
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                IngestionWorkflow.run,
                IngestionParams(dataset_urn=_DATASET_URN),
                id="test-ingestion-extract-failure",
                task_queue=TASK_QUEUE,
            )
    assert "DB connection refused" in str(exc_info.value.cause.cause)


async def test_emit_failure_raises_workflow_error(env: WorkflowEnvironment):
    @activity.defn(name="emit_to_datahub_activity")
    async def failing_emit(dataset_urn: str, extract_result: dict) -> dict:
        raise RuntimeError("DataHub unreachable")

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(emit_fn=failing_emit),
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                IngestionWorkflow.run,
                IngestionParams(dataset_urn=_DATASET_URN),
                id="test-ingestion-emit-failure",
                task_queue=TASK_QUEUE,
            )
    assert "DataHub unreachable" in str(exc_info.value.cause.cause)


async def test_record_receives_emit_status(env: WorkflowEnvironment):
    recorded_status = None

    @activity.defn(name="record_ingestion_event_activity")
    async def capture_record(dataset_urn: str, run_id: str, status: str, detail: dict) -> dict:
        nonlocal recorded_status
        recorded_status = status
        return {"run_id": run_id, "status": status, "detail": detail}

    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IngestionWorkflow],
        activities=_make_activities(record_fn=capture_record),
    ):
        await env.client.execute_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_DATASET_URN),
            id="test-ingestion-record-status",
            task_queue=TASK_QUEUE,
        )
    assert recorded_status == "success"
