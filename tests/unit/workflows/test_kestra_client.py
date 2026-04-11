"""Unit tests for KestraClient with mocked httpx."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.errors import KestraExecutionFailedError, KestraTimeoutError
from src.workflows.kestra.models import ExecutionResponse, ExecutionStatus


@pytest.fixture
def client():
    return KestraClient(base_url="http://kestra:8080", namespace="dataspoke")


def _make_execution(status: str = "SUCCESS", execution_id: str = "exec-1") -> dict:
    return {
        "id": execution_id,
        "namespace": "dataspoke",
        "flowId": "ingestion",
        "state": {"current": status},
        "inputs": {"dataset_urn": "urn:test", "run_id": "r-001"},
        "outputs": {"run_id": "r-001", "status": "success", "detail": {}},
    }


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response with sync json() and raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


# ── trigger_execution ────────────────────────────────────────────────────────


async def test_trigger_execution(client: KestraClient):
    client._client.post = AsyncMock(return_value=_mock_response(_make_execution("RUNNING")))

    execution = await client.trigger_execution("ingestion", inputs={"dataset_urn": "urn:test"})
    assert execution.id == "exec-1"
    assert execution.status == ExecutionStatus.RUNNING


async def test_trigger_execution_with_labels(client: KestraClient):
    """trigger_execution should call _set_labels when labels are provided."""
    client._client.post = AsyncMock(return_value=_mock_response(_make_execution("RUNNING")))
    client._client.put = AsyncMock(return_value=_mock_response({}))

    execution = await client.trigger_execution(
        "ingestion",
        inputs={"dataset_urn": "urn:test"},
        labels={"workflow_id": "ingestion-abc"},
    )
    assert execution.id == "exec-1"

    # _set_labels should have been called via PUT
    client._client.put.assert_called_once()
    call_args = client._client.put.call_args
    assert "/labels" in call_args[0][0]


# ── get_execution ────────────────────────────────────────────────────────────


async def test_get_execution(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("SUCCESS")))

    execution = await client.get_execution("exec-1")
    assert execution.status == ExecutionStatus.SUCCESS
    assert execution.is_terminal


# ── wait_for_execution ───────────────────────────────────────────────────────


async def test_wait_for_execution_success(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("SUCCESS")))

    execution = await client.wait_for_execution("exec-1", flow_id="test", timeout_seconds=5)
    assert execution.status == ExecutionStatus.SUCCESS


async def test_wait_for_execution_failure(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("FAILED")))

    with pytest.raises(KestraExecutionFailedError) as exc_info:
        await client.wait_for_execution("exec-1", flow_id="test", timeout_seconds=5)
    assert exc_info.value.flow_id == "test"
    assert exc_info.value.execution_id == "exec-1"


async def test_wait_for_execution_timeout(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("RUNNING")))

    # Simulate elapsed time via a fake clock so the timeout fires deterministically
    # without depending on real wall-clock delays.
    fake_time = 0.0

    def advance_time(*_args):
        nonlocal fake_time
        fake_time += 10.0  # jump past the deadline on each sleep

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("asyncio.sleep", AsyncMock(side_effect=advance_time))
        mp.setattr("asyncio.get_event_loop", lambda: type("L", (), {"time": lambda self: fake_time})())
        with pytest.raises(KestraTimeoutError) as exc_info:
            await client.wait_for_execution(
                "exec-1", flow_id="test", timeout_seconds=5, poll_interval=5
            )
    assert exc_info.value.flow_id == "test"
    assert exc_info.value.execution_id == "exec-1"


async def test_wait_for_execution_polls_until_terminal(client: KestraClient):
    """Should poll multiple times until status transitions to terminal."""
    responses = [
        _mock_response(_make_execution("RUNNING")),
        _mock_response(_make_execution("RUNNING")),
        _mock_response(_make_execution("SUCCESS")),
    ]
    client._client.get = AsyncMock(side_effect=responses)

    # Patch asyncio.sleep to be instant so the 5-second poll_interval floor
    # in wait_for_execution doesn't cause real delays or timeouts.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("asyncio.sleep", AsyncMock())
        execution = await client.wait_for_execution(
            "exec-1", flow_id="test", timeout_seconds=300, poll_interval=5
        )
    assert execution.status == ExecutionStatus.SUCCESS
    assert client._client.get.call_count == 3


# ── find_running_executions ──────────────────────────────────────────────────


async def test_find_running_executions(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("RUNNING")]})
    )

    running = await client.find_running_executions("ingestion", "workflow_id", "ingestion-abc123")
    assert len(running) == 1
    assert running[0].status == ExecutionStatus.RUNNING


async def test_find_running_executions_empty(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response({"results": []}))

    running = await client.find_running_executions("ingestion")
    assert running == []


# ── check_no_duplicate ───────────────────────────────────────────────────────


async def test_check_no_duplicate_raises_conflict(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("RUNNING")]})
    )

    from src.shared.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await client.check_no_duplicate(
            "ingestion", "workflow_id", "ingestion-abc", "INGESTION_RUNNING"
        )


async def test_check_no_duplicate_passes_when_no_running(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response({"results": []}))

    # Should not raise
    await client.check_no_duplicate(
        "ingestion", "workflow_id", "ingestion-abc", "INGESTION_RUNNING"
    )


# ── create_or_update_flow ────────────────────────────────────────────────────


async def test_create_or_update_flow_update_path(client: KestraClient):
    """PUT succeeds on first try (flow already exists)."""
    client._client.put = AsyncMock(
        return_value=_mock_response({"id": "ingestion", "namespace": "dataspoke", "revision": 2})
    )
    client._client.post = AsyncMock()

    result = await client.create_or_update_flow("id: ingestion\nnamespace: dataspoke")
    assert result["id"] == "ingestion"
    assert result["revision"] == 2
    # POST should not be called since PUT succeeded
    client._client.post.assert_not_called()


async def test_create_or_update_flow_create_path(client: KestraClient):
    """PUT returns 404 — falls back to POST (create)."""
    put_resp = _mock_response({}, status_code=404)
    put_resp.status_code = 404
    post_resp = _mock_response({"id": "ingestion", "namespace": "dataspoke", "revision": 1})

    client._client.put = AsyncMock(return_value=put_resp)
    client._client.post = AsyncMock(return_value=post_resp)

    result = await client.create_or_update_flow("id: ingestion\nnamespace: dataspoke")
    assert result["id"] == "ingestion"
    client._client.post.assert_called_once()


# ── get_flow ─────────────────────────────────────────────────────────────────


async def test_get_flow_found(client: KestraClient):
    flow_data = {"id": "ingestion", "namespace": "dataspoke", "revision": 1, "tasks": []}
    client._client.get = AsyncMock(return_value=_mock_response(flow_data))

    flow = await client.get_flow("ingestion")
    assert flow is not None
    assert flow["id"] == "ingestion"


async def test_get_flow_not_found(client: KestraClient):
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    client._client.get = AsyncMock(return_value=resp)

    flow = await client.get_flow("nonexistent")
    assert flow is None


# ── kill_execution ───────────────────────────────────────────────────────────


async def test_kill_execution_success(client: KestraClient):
    client._client.post = AsyncMock(return_value=_mock_response({}))

    await client.kill_execution("exec-1")
    client._client.post.assert_called_once_with("/api/v1/executions/exec-1/kill")


async def test_kill_execution_not_found(client: KestraClient):
    """kill_execution should not raise on 404."""
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    client._client.post = AsyncMock(return_value=resp)

    await client.kill_execution("nonexistent")


async def test_kill_execution_already_terminal(client: KestraClient):
    """kill_execution should not raise on 409 (already terminal)."""
    resp = _mock_response({}, status_code=409)
    resp.status_code = 409
    client._client.post = AsyncMock(return_value=resp)

    await client.kill_execution("already-done")


# ── delete_execution ─────────────────────────────────────────────────────────


async def test_delete_execution_success(client: KestraClient):
    client._client.delete = AsyncMock(return_value=_mock_response({}))

    await client.delete_execution("exec-1")
    client._client.delete.assert_called_once()
    call_args = client._client.delete.call_args
    assert "exec-1" in call_args[0][0]


async def test_delete_execution_not_found(client: KestraClient):
    """delete_execution should not raise on 404."""
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    client._client.delete = AsyncMock(return_value=resp)

    await client.delete_execution("nonexistent")


async def test_delete_execution_with_logs(client: KestraClient):
    """delete_execution should pass cleanup params."""
    client._client.delete = AsyncMock(return_value=_mock_response({}))

    await client.delete_execution("exec-1", delete_logs=True)
    call_args = client._client.delete.call_args
    params = call_args[1]["params"]
    assert params["deleteLogs"] == "true"
    assert params["deleteMetrics"] == "true"
    assert params["deleteStorage"] == "true"


# ── find_executions ──────────────────────────────────────────────────────────


async def test_find_executions_by_flow(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("SUCCESS")]})
    )

    results = await client.find_executions(flow_id="ingestion")
    assert len(results) == 1
    assert results[0].flowId == "ingestion"


async def test_find_executions_by_state(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("FAILED")]})
    )

    results = await client.find_executions(flow_id="ingestion", state="FAILED")
    assert len(results) == 1
    assert results[0].status == ExecutionStatus.FAILED

    # Verify correct params were passed
    call_args = client._client.get.call_args
    params = call_args[1]["params"]
    assert params["state"] == "FAILED"
    assert params["flowId"] == "ingestion"


async def test_find_executions_with_label(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response({"results": []}))

    results = await client.find_executions(
        flow_id="ingestion", label_key="workflow_id", label_value="ingestion-abc"
    )
    assert results == []

    call_args = client._client.get.call_args
    params = call_args[1]["params"]
    assert params["labels"] == "workflow_id:ingestion-abc"


async def test_find_executions_empty(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response({"results": []}))

    results = await client.find_executions()
    assert results == []


# ── delete_flow ──────────────────────────────────────────────────────────────


async def test_delete_flow_success(client: KestraClient):
    client._client.delete = AsyncMock(return_value=_mock_response({}))

    await client.delete_flow("ingestion")
    client._client.delete.assert_called_once_with("/api/v1/flows/dataspoke/ingestion")


async def test_delete_flow_not_found(client: KestraClient):
    """delete_flow should not raise on 404."""
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    client._client.delete = AsyncMock(return_value=resp)

    await client.delete_flow("nonexistent")


# ── _set_labels ──────────────────────────────────────────────────────────────


async def test_set_labels_uses_put(client: KestraClient):
    """_set_labels should use PUT method per Kestra API."""
    client._client.put = AsyncMock(return_value=_mock_response({}))

    await client._set_labels("exec-1", {"workflow_id": "test-123"})
    client._client.put.assert_called_once()
    call_args = client._client.put.call_args
    assert "/api/v1/executions/exec-1/labels" in call_args[0][0]
    assert call_args[1]["json"] == [{"key": "workflow_id", "value": "test-123"}]


# ── ExecutionResponse model ──────────────────────────────────────────────────


async def test_execution_response_model():
    resp = ExecutionResponse(
        id="e-1",
        namespace="dataspoke",
        flowId="ingestion",
        state={"current": "SUCCESS"},
    )
    assert resp.status == ExecutionStatus.SUCCESS
    assert resp.is_terminal

    resp2 = ExecutionResponse(
        id="e-2",
        namespace="dataspoke",
        flowId="ingestion",
        state={"current": "RUNNING"},
    )
    assert resp2.status == ExecutionStatus.RUNNING
    assert not resp2.is_terminal


async def test_execution_response_all_terminal_states():
    """All documented terminal states should report is_terminal=True."""
    for status in ("SUCCESS", "WARNING", "FAILED", "KILLED"):
        resp = ExecutionResponse(
            id="e", namespace="dataspoke", flowId="test", state={"current": status}
        )
        assert resp.is_terminal, f"{status} should be terminal"


async def test_execution_response_non_terminal_states():
    """Non-terminal states should report is_terminal=False."""
    for status in ("CREATED", "RUNNING", "PAUSED", "QUEUED", "RETRYING", "KILLING"):
        resp = ExecutionResponse(
            id="e", namespace="dataspoke", flowId="test", state={"current": status}
        )
        assert not resp.is_terminal, f"{status} should NOT be terminal"


async def test_execution_response_default_inputs_outputs():
    """inputs and outputs should default to None."""
    resp = ExecutionResponse(
        id="e", namespace="dataspoke", flowId="test", state={"current": "CREATED"}
    )
    assert resp.inputs is None
    assert resp.outputs is None
