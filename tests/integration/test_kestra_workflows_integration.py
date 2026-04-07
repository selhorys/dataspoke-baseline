"""Integration tests for KestraClient against dev-env Kestra instance.

Verifies that src/workflows/kestra/client.py talks to Kestra's REST
API correctly: flow CRUD, execution lifecycle, label-based dedup,
status querying, and cleanup operations.

Uses a lightweight noop flow registered as a test fixture — no
DataSpoke activity callbacks, no production flows required.

Prerequisites:
- Kestra port-forwarded to localhost:9205

Run: uv run pytest tests/integration/test_kestra_workflows_integration.py -v
"""

import uuid

import pytest
import pytest_asyncio
import yaml

from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.errors import KestraTimeoutError
from src.workflows.kestra.models import ExecutionStatus
from tests.integration.util.kestra import (
    cleanup_test_executions,
    kill_running_executions,
    wait_for_execution_terminal,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

_TEST_LABEL_PREFIX = "test-kestra-"

_NOOP_FLOW_ID = "test-noop"
_NOOP_FLOW_YAML = yaml.dump({
    "id": _NOOP_FLOW_ID,
    "namespace": "dataspoke",
    "inputs": [
        {"id": "callback_base_url", "type": "STRING", "defaults": "http://localhost"},
        {"id": "dataset_urn", "type": "STRING", "defaults": "urn:test"},
        {"id": "dry_run", "type": "STRING", "defaults": "true"},
    ],
    "tasks": [
        {
            "id": "noop",
            "type": "io.kestra.plugin.core.log.Log",
            "message": "noop test execution",
        },
    ],
})


def _test_label(suffix: str) -> str:
    return f"{_TEST_LABEL_PREFIX}{suffix}-{uuid.uuid4().hex[:8]}"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _register_noop_flow(kestra_client: KestraClient):
    """Register the noop flow for the test module; clean up on teardown."""
    await kestra_client.create_or_update_flow(_NOOP_FLOW_YAML)
    yield
    await cleanup_test_executions(kestra_client, _NOOP_FLOW_ID, label_prefix=_TEST_LABEL_PREFIX)
    await kestra_client.delete_flow(_NOOP_FLOW_ID)


@pytest_asyncio.fixture(scope="module")
async def shared_noop_execution(kestra_client: KestraClient):
    """Single noop execution shared by multiple read-only tests."""
    label = _test_label("shared-noop")
    execution = await kestra_client.trigger_and_wait(
        _NOOP_FLOW_ID,
        labels={"workflow_id": label},
        timeout_seconds=30,
    )
    return execution


# ── Flow CRUD ────────────────────────────────────────────────────────────────


async def test_get_flow_returns_correct_structure(kestra_client: KestraClient):
    """get_flow should return the noop flow with expected fields."""
    flow = await kestra_client.get_flow(_NOOP_FLOW_ID)
    assert flow is not None
    assert flow["id"] == _NOOP_FLOW_ID
    assert flow["namespace"] == "dataspoke"
    assert "tasks" in flow
    assert "inputs" in flow


async def test_get_flow_not_found(kestra_client: KestraClient):
    """get_flow for nonexistent flow should return None."""
    flow = await kestra_client.get_flow("nonexistent-flow-id")
    assert flow is None


async def test_flow_has_expected_inputs(kestra_client: KestraClient):
    """Noop flow should have the declared input parameters."""
    flow = await kestra_client.get_flow(_NOOP_FLOW_ID)
    assert flow is not None
    input_ids = {inp["id"] for inp in flow["inputs"]}
    assert {"callback_base_url", "dataset_urn", "dry_run"} <= input_ids


async def test_flow_has_tasks(kestra_client: KestraClient):
    """Noop flow should define at least one task."""
    flow = await kestra_client.get_flow(_NOOP_FLOW_ID)
    assert flow is not None
    assert len(flow.get("tasks", [])) >= 1


async def test_create_update_flow_idempotent(kestra_client: KestraClient):
    """create_or_update_flow should be idempotent for an existing flow."""
    result = await kestra_client.create_or_update_flow(_NOOP_FLOW_YAML)
    assert result["id"] == _NOOP_FLOW_ID


# ── Execution Lifecycle ──────────────────────────────────────────────────────


async def test_noop_execution_succeeded(shared_noop_execution):
    """Noop execution should complete as SUCCESS."""
    assert shared_noop_execution.is_terminal
    assert shared_noop_execution.status == ExecutionStatus.SUCCESS
    assert shared_noop_execution.id
    assert shared_noop_execution.namespace == "dataspoke"
    assert shared_noop_execution.flowId == _NOOP_FLOW_ID


async def test_get_execution_status(kestra_client: KestraClient, shared_noop_execution):
    """get_execution should return correct status."""
    resp = await kestra_client.get_execution(shared_noop_execution.id)
    assert resp.id == shared_noop_execution.id
    assert resp.flowId == _NOOP_FLOW_ID
    assert resp.status == ExecutionStatus.SUCCESS


# ── Label-based Deduplication ────────────────────────────────────────────────


async def test_check_no_duplicate_passes_when_clean(kestra_client: KestraClient):
    """check_no_duplicate should not raise when no running execution has the label."""
    unique_label = _test_label("no-dup-clean")
    await kestra_client.check_no_duplicate(
        _NOOP_FLOW_ID, "workflow_id", unique_label, "NOOP_RUNNING"
    )


# ── Kill and Delete ──────────────────────────────────────────────────────────


async def test_kill_execution_idempotent(kestra_client: KestraClient, shared_noop_execution):
    """kill_execution should not raise for already-terminated executions."""
    await kestra_client.kill_execution(shared_noop_execution.id)


async def test_delete_execution(kestra_client: KestraClient):
    """delete_execution should remove a completed execution."""
    label = _test_label("delete")
    execution = await kestra_client.trigger_and_wait(
        _NOOP_FLOW_ID,
        labels={"workflow_id": label},
        timeout_seconds=30,
    )

    await kestra_client.delete_execution(execution.id)

    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        await kestra_client.get_execution(execution.id)


async def test_delete_execution_not_found(kestra_client: KestraClient):
    """delete_execution for nonexistent ID should not raise."""
    await kestra_client.delete_execution("nonexistent-execution-id")


# ── find_executions ──────────────────────────────────────────────────────────


async def test_find_executions_by_flow(kestra_client: KestraClient, shared_noop_execution):
    """find_executions should filter by flow ID."""
    results = await kestra_client.find_executions(flow_id=_NOOP_FLOW_ID)
    assert len(results) >= 1
    assert all(r.flowId == _NOOP_FLOW_ID for r in results)


async def test_find_executions_by_state(kestra_client: KestraClient, shared_noop_execution):
    """find_executions with state filter should return matching executions."""
    results = await kestra_client.find_executions(
        flow_id=_NOOP_FLOW_ID, state="SUCCESS"
    )
    assert len(results) >= 1
    for r in results:
        assert r.status == ExecutionStatus.SUCCESS


# ── Cleanup Utilities ────────────────────────────────────────────────────────


async def test_kill_running_executions_utility(kestra_client: KestraClient):
    """kill_running_executions utility should not raise."""
    killed = await kill_running_executions(kestra_client, _NOOP_FLOW_ID)
    assert isinstance(killed, int)
    assert killed >= 0


async def test_cleanup_test_executions_utility(kestra_client: KestraClient):
    """cleanup_test_executions utility should delete test-labeled executions."""
    label = f"{_TEST_LABEL_PREFIX}cleanup-util-{uuid.uuid4().hex[:8]}"
    await kestra_client.trigger_and_wait(
        _NOOP_FLOW_ID,
        labels={"workflow_id": label},
        timeout_seconds=30,
    )

    deleted = await cleanup_test_executions(
        kestra_client, _NOOP_FLOW_ID, label_prefix=_TEST_LABEL_PREFIX
    )
    assert isinstance(deleted, int)


# ── Timeout Handling ─────────────────────────────────────────────────────────


async def test_wait_for_execution_timeout(kestra_client: KestraClient):
    """wait_for_execution with very short timeout should raise KestraTimeoutError."""
    label = _test_label("timeout")
    execution = await kestra_client.trigger_execution(
        _NOOP_FLOW_ID,
        labels={"workflow_id": label},
    )

    try:
        with pytest.raises(KestraTimeoutError):
            await kestra_client.wait_for_execution(
                execution.id,
                flow_id=_NOOP_FLOW_ID,
                timeout_seconds=0,
                poll_interval=0.001,
            )
    finally:
        await kestra_client.kill_execution(execution.id)
        await wait_for_execution_terminal(
            kestra_client, execution.id, timeout_seconds=30
        )
