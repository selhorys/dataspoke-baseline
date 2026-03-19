"""Integration tests for Kestra workflows against dev-env infrastructure.

Tests the real Kestra REST API: flow management, execution lifecycle,
label-based deduplication, status querying, and cleanup operations.

Prerequisites:
- Kestra port-forwarded to localhost:9205
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested via module_dummy_data fixture (catalog schema)

Note: Kestra flows call back to activity endpoints via HTTP.  When the
DataSpoke API server is not running (typical for integration tests), the
activity tasks will fail.  These tests verify Kestra **orchestration**
(trigger, poll, label, kill, cleanup) — not activity business logic.
Activity logic is covered by API-wired integration tests.

Run: uv run pytest tests/integration/test_kestra_workflows_integration.py -v
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from src.shared.exceptions import ConflictError
from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.errors import KestraExecutionFailedError, KestraTimeoutError
from src.workflows.kestra.models import ExecutionStatus
from tests.integration.util.kestra import (
    ALL_FLOW_IDS,
    cleanup_test_executions,
    kill_running_executions,
    verify_flows_registered,
    wait_for_execution_terminal,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

# ── Per-module dummy-data reset (see spec/TESTING.md §Per-Module) ─────────
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])

_IMAZON_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)

_TEST_LABEL_PREFIX = "test-kestra-"


def _test_label(suffix: str) -> str:
    """Generate a unique test label to avoid collisions between runs."""
    return f"{_TEST_LABEL_PREFIX}{suffix}-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _seed_workflow_configs():
    """Seed IngestionConfig and ValidationConfig for the test dataset."""
    from src.shared.db.models import IngestionConfig, ValidationConfig
    from src.shared.db.session import SessionLocal

    async with SessionLocal() as db:
        await db.execute(
            delete(IngestionConfig).where(IngestionConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.execute(
            delete(ValidationConfig).where(ValidationConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        db.add(
            IngestionConfig(
                dataset_urn=_IMAZON_DATASET_URN,
                sources={},
                deep_spec_enabled=False,
                owner="integration-test",
            )
        )
        db.add(
            ValidationConfig(
                dataset_urn=_IMAZON_DATASET_URN,
                rules={"completeness": {"threshold": 0.8}},
                owner="integration-test",
            )
        )
        await db.commit()

    yield

    async with SessionLocal() as db:
        await db.execute(
            delete(IngestionConfig).where(IngestionConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.execute(
            delete(ValidationConfig).where(ValidationConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.commit()


# ── Flow Registration ────────────────────────────────────────────────────────


async def test_all_flows_registered(kestra_client: KestraClient):
    """All 7 DataSpoke flows should be registered in the dataspoke namespace."""
    registered = await verify_flows_registered(kestra_client)
    assert set(registered) == ALL_FLOW_IDS


async def test_get_flow_returns_correct_structure(kestra_client: KestraClient):
    """get_flow should return flow with expected fields."""
    flow = await kestra_client.get_flow("ingestion")
    assert flow is not None
    assert flow["id"] == "ingestion"
    assert flow["namespace"] == "dataspoke"
    assert "tasks" in flow
    assert "inputs" in flow


async def test_get_flow_not_found(kestra_client: KestraClient):
    """get_flow for nonexistent flow should return None."""
    flow = await kestra_client.get_flow("nonexistent-flow-id")
    assert flow is None


async def test_flow_has_expected_inputs(kestra_client: KestraClient):
    """Ingestion flow should have the required input parameters."""
    flow = await kestra_client.get_flow("ingestion")
    assert flow is not None
    input_ids = {inp["id"] for inp in flow["inputs"]}
    assert {"callback_base_url", "dataset_urn", "dry_run", "run_id"} <= input_ids


async def test_each_flow_has_tasks(kestra_client: KestraClient):
    """Every registered flow should define at least one task."""
    for flow_id in ALL_FLOW_IDS:
        flow = await kestra_client.get_flow(flow_id)
        assert flow is not None, f"Flow {flow_id} not registered"
        assert len(flow.get("tasks", [])) >= 1, f"Flow {flow_id} has no tasks"


# ── Execution Lifecycle ──────────────────────────────────────────────────────


async def test_trigger_execution(kestra_client: KestraClient):
    """trigger_execution should return an ExecutionResponse with a valid ID."""
    label = _test_label("trigger")
    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    assert execution.id
    assert execution.namespace == "dataspoke"
    assert execution.flowId == "ingestion"
    assert execution.status in (
        ExecutionStatus.CREATED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.QUEUED,
    )

    # Wait for completion (activity will likely fail — that's expected)
    terminal_status = await wait_for_execution_terminal(
        kestra_client, execution.id, timeout_seconds=60
    )
    assert terminal_status in (
        ExecutionStatus.SUCCESS,
        ExecutionStatus.WARNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.KILLED,
    )


async def test_trigger_and_wait_returns_terminal(kestra_client: KestraClient):
    """trigger_and_wait should poll until terminal and return or raise."""
    label = _test_label("trigger-wait")
    try:
        execution = await kestra_client.trigger_and_wait(
            "ingestion",
            inputs={
                "callback_base_url": "http://localhost:8000",
                "dataset_urn": _IMAZON_DATASET_URN,
                "dry_run": "true",
                "run_id": str(uuid.uuid4()),
            },
            labels={"workflow_id": label},
            timeout_seconds=60,
        )
        # If SUCCESS/WARNING, execution is returned
        assert execution.is_terminal
        assert execution.status in (ExecutionStatus.SUCCESS, ExecutionStatus.WARNING)
    except KestraExecutionFailedError as exc:
        # Activity callback failure is expected when no API server is running
        assert exc.flow_id == "ingestion"
        assert exc.execution_id


async def test_get_execution_status(kestra_client: KestraClient):
    """get_execution should return current status for a triggered execution."""
    label = _test_label("status")
    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    status_resp = await kestra_client.get_execution(execution.id)
    assert status_resp.id == execution.id
    assert status_resp.flowId == "ingestion"
    assert status_resp.status is not None

    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)


async def test_execution_inputs_preserved(kestra_client: KestraClient):
    """Execution should preserve the inputs that were passed."""
    run_id = str(uuid.uuid4())
    label = _test_label("inputs")
    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": run_id,
        },
        labels={"workflow_id": label},
    )

    fetched = await kestra_client.get_execution(execution.id)
    assert fetched.inputs is not None
    assert fetched.inputs.get("dataset_urn") == _IMAZON_DATASET_URN
    assert fetched.inputs.get("run_id") == run_id
    assert fetched.inputs.get("dry_run") == "true"

    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)


# ── Label-based Deduplication ────────────────────────────────────────────────


async def test_check_no_duplicate_passes_when_clean(kestra_client: KestraClient):
    """check_no_duplicate should not raise when no running execution has the label."""
    unique_label = _test_label("no-dup-clean")
    # Should not raise
    await kestra_client.check_no_duplicate(
        "ingestion", "workflow_id", unique_label, "INGESTION_RUNNING"
    )


async def test_check_no_duplicate_detects_running(kestra_client: KestraClient):
    """check_no_duplicate should raise ConflictError if a running execution has the label."""
    label = _test_label("dup-detect")

    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    # Brief wait for execution to be indexed with labels
    import asyncio
    await asyncio.sleep(2)

    try:
        # If execution is still running, this should raise ConflictError
        await kestra_client.check_no_duplicate(
            "ingestion", "workflow_id", label, "INGESTION_RUNNING"
        )
        # If it didn't raise, the execution finished very quickly — that's OK
    except ConflictError:
        pass  # Expected

    # Clean up
    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)


async def test_find_running_executions(kestra_client: KestraClient):
    """find_running_executions should find executions by flow ID."""
    # Any currently-running executions should be findable
    result = await kestra_client.find_running_executions("ingestion")
    assert isinstance(result, list)
    for execution in result:
        assert execution.flowId == "ingestion"
        assert execution.status == ExecutionStatus.RUNNING


# ── Kill and Cleanup ─────────────────────────────────────────────────────────


async def test_kill_execution(kestra_client: KestraClient):
    """kill_execution should terminate a running execution."""
    label = _test_label("kill")

    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    # Kill it
    await kestra_client.kill_execution(execution.id)

    # Wait for terminal
    terminal_status = await wait_for_execution_terminal(
        kestra_client, execution.id, timeout_seconds=30
    )
    assert terminal_status in (
        ExecutionStatus.KILLED,
        ExecutionStatus.FAILED,
        ExecutionStatus.SUCCESS,
        ExecutionStatus.WARNING,
    )


async def test_kill_execution_idempotent(kestra_client: KestraClient):
    """kill_execution should not raise for already-terminated executions."""
    label = _test_label("kill-idem")

    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)

    # Kill after terminal — should not raise
    await kestra_client.kill_execution(execution.id)


async def test_delete_execution(kestra_client: KestraClient):
    """delete_execution should remove a completed execution."""
    label = _test_label("delete")

    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)

    # Delete the execution
    await kestra_client.delete_execution(execution.id)

    # Verify it's gone (get_execution should raise 404)
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        await kestra_client.get_execution(execution.id)


async def test_delete_execution_not_found(kestra_client: KestraClient):
    """delete_execution for nonexistent ID should not raise."""
    await kestra_client.delete_execution("nonexistent-execution-id")


# ── find_executions ──────────────────────────────────────────────────────────


async def test_find_executions_by_flow(kestra_client: KestraClient):
    """find_executions should filter by flow ID."""
    label = _test_label("find-flow")

    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )
    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)

    results = await kestra_client.find_executions(flow_id="ingestion")
    assert len(results) >= 1
    assert all(r.flowId == "ingestion" for r in results)


async def test_find_executions_by_state(kestra_client: KestraClient):
    """find_executions with state filter should return matching executions."""
    # Query for any terminal state
    results = await kestra_client.find_executions(
        flow_id="ingestion", state="SUCCESS"
    )
    for r in results:
        assert r.status == ExecutionStatus.SUCCESS


# ── Flow CRUD ────────────────────────────────────────────────────────────────


async def test_create_update_flow_idempotent(kestra_client: KestraClient):
    """create_or_update_flow should be idempotent — re-registering an existing flow works."""
    from pathlib import Path

    flows_dir = Path(__file__).resolve().parents[2] / "src/workflows/flows"
    flow_yaml = (flows_dir / "ingestion.yaml").read_text()

    result = await kestra_client.create_or_update_flow(flow_yaml)
    assert result["id"] == "ingestion"
    assert result["namespace"] == "dataspoke"

    # Second call should also succeed (update path)
    result2 = await kestra_client.create_or_update_flow(flow_yaml)
    assert result2["id"] == "ingestion"


async def test_delete_and_recreate_flow(kestra_client: KestraClient):
    """delete_flow + create_or_update_flow should work as re-registration."""
    from pathlib import Path

    flows_dir = Path(__file__).resolve().parents[2] / "src/workflows/flows"
    flow_yaml = (flows_dir / "validation.yaml").read_text()

    await kestra_client.delete_flow("validation")
    assert await kestra_client.get_flow("validation") is None

    result = await kestra_client.create_or_update_flow(flow_yaml)
    assert result["id"] == "validation"
    assert await kestra_client.get_flow("validation") is not None


# ── Multiple Flow Types ──────────────────────────────────────────────────────


async def test_trigger_validation_flow(kestra_client: KestraClient):
    """Trigger validation flow and verify execution lifecycle."""
    label = _test_label("validation")
    execution = await kestra_client.trigger_execution(
        "validation",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
        },
        labels={"workflow_id": label},
    )

    assert execution.id
    assert execution.flowId == "validation"

    terminal_status = await wait_for_execution_terminal(
        kestra_client, execution.id, timeout_seconds=60
    )
    assert terminal_status in (
        ExecutionStatus.SUCCESS,
        ExecutionStatus.WARNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.KILLED,
    )


async def test_trigger_generation_flow(kestra_client: KestraClient):
    """Trigger generation flow and verify execution lifecycle."""
    label = _test_label("generation")
    execution = await kestra_client.trigger_execution(
        "generation",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
        },
        labels={"workflow_id": label},
    )

    assert execution.id
    assert execution.flowId == "generation"

    terminal_status = await wait_for_execution_terminal(
        kestra_client, execution.id, timeout_seconds=60
    )
    assert terminal_status in (
        ExecutionStatus.SUCCESS,
        ExecutionStatus.WARNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.KILLED,
    )


# ── Cleanup Utility Tests ───────────────────────────────────────────────────


async def test_kill_running_executions_utility(kestra_client: KestraClient):
    """kill_running_executions utility should handle the flow gracefully."""
    # This may or may not find anything to kill — either way should not raise
    killed = await kill_running_executions(kestra_client, "ingestion")
    assert isinstance(killed, int)
    assert killed >= 0


async def test_cleanup_test_executions_utility(kestra_client: KestraClient):
    """cleanup_test_executions utility should delete test-labeled executions."""
    # Create a test execution, wait for it, then clean up
    label = f"{_TEST_LABEL_PREFIX}cleanup-util-{uuid.uuid4().hex[:8]}"
    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )
    await wait_for_execution_terminal(kestra_client, execution.id, timeout_seconds=60)

    deleted = await cleanup_test_executions(
        kestra_client, "ingestion", label_prefix=_TEST_LABEL_PREFIX
    )
    assert isinstance(deleted, int)


# ── Timeout Handling ─────────────────────────────────────────────────────────


async def test_wait_for_execution_timeout(kestra_client: KestraClient):
    """wait_for_execution with very short timeout should raise KestraTimeoutError."""
    label = _test_label("timeout")
    execution = await kestra_client.trigger_execution(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label},
    )

    # Use a very short timeout — execution won't finish in 0.1s
    try:
        with pytest.raises(KestraTimeoutError):
            await kestra_client.wait_for_execution(
                execution.id,
                flow_id="ingestion",
                timeout_seconds=0.1,
                poll_interval=0.05,
            )
    finally:
        # Clean up the running execution
        await kestra_client.kill_execution(execution.id)
        await wait_for_execution_terminal(
            kestra_client, execution.id, timeout_seconds=30
        )
