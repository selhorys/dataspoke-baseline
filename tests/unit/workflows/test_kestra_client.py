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
        "inputs": {},
        "outputs": {"run_id": "r-001", "status": "success", "detail": {}},
    }


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response with sync json() and raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


async def test_trigger_execution(client: KestraClient):
    client._client.post = AsyncMock(return_value=_mock_response(_make_execution("RUNNING")))

    execution = await client.trigger_execution("ingestion", inputs={"dataset_urn": "urn:test"})
    assert execution.id == "exec-1"
    assert execution.status == ExecutionStatus.RUNNING


async def test_get_execution(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("SUCCESS")))

    execution = await client.get_execution("exec-1")
    assert execution.status == ExecutionStatus.SUCCESS
    assert execution.is_terminal


async def test_wait_for_execution_success(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("SUCCESS")))

    execution = await client.wait_for_execution("exec-1", flow_id="test", timeout_seconds=5)
    assert execution.status == ExecutionStatus.SUCCESS


async def test_wait_for_execution_failure(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("FAILED")))

    with pytest.raises(KestraExecutionFailedError):
        await client.wait_for_execution("exec-1", flow_id="test", timeout_seconds=5)


async def test_wait_for_execution_timeout(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response(_make_execution("RUNNING")))

    with pytest.raises(KestraTimeoutError):
        await client.wait_for_execution(
            "exec-1", flow_id="test", timeout_seconds=0.1, poll_interval=0.05
        )


async def test_find_running_executions(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("RUNNING")]})
    )

    running = await client.find_running_executions("ingestion", "workflow_id", "ingestion-abc123")
    assert len(running) == 1
    assert running[0].status == ExecutionStatus.RUNNING


async def test_check_no_duplicate_raises_conflict(client: KestraClient):
    client._client.get = AsyncMock(
        return_value=_mock_response({"results": [_make_execution("RUNNING")]})
    )

    from src.shared.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await client.check_no_duplicate("ingestion", "workflow_id", "ingestion-abc", "INGESTION_RUNNING")


async def test_check_no_duplicate_passes_when_no_running(client: KestraClient):
    client._client.get = AsyncMock(return_value=_mock_response({"results": []}))

    # Should not raise
    await client.check_no_duplicate("ingestion", "workflow_id", "ingestion-abc", "INGESTION_RUNNING")


async def test_create_or_update_flow(client: KestraClient):
    client._client.post = AsyncMock(
        return_value=_mock_response({"id": "ingestion", "namespace": "dataspoke"})
    )

    result = await client.create_or_update_flow("id: ingestion\nnamespace: dataspoke")
    assert result["id"] == "ingestion"


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
