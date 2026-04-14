"""Unit tests for AirflowClient with mocked httpx."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.airflow.client import AirflowClient
from src.workflows.airflow.errors import AirflowExecutionFailedError, AirflowTimeoutError
from src.workflows.airflow.models import DagRunResponse, DagRunState


@pytest.fixture
def client():
    return AirflowClient(base_url="http://airflow:8080", username="admin", password="admin")


def _make_dag_run(state: str = "success", dag_run_id: str = "run-1", dag_id: str = "ingestion") -> dict:
    return {
        "dag_run_id": dag_run_id,
        "dag_id": dag_id,
        "state": state,
        "conf": {},
        "logical_date": None,
        "start_date": None,
        "end_date": None,
    }


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response with sync json() and raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response
        resp.raise_for_status.side_effect = HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── DagRunResponse model ─────────────────────────────────────────────────────


async def test_dag_run_response_model():
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.success)
    assert resp.state == DagRunState.success
    assert resp.is_terminal


async def test_dag_run_response_non_terminal():
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.running)
    assert not resp.is_terminal


async def test_dag_run_response_queued_non_terminal():
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.queued)
    assert not resp.is_terminal


async def test_dag_run_response_failed_is_terminal():
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.failed)
    assert resp.is_terminal


async def test_dag_run_response_status_alias():
    """status property is an alias for state."""
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.success)
    assert resp.status == DagRunState.success


async def test_dag_run_response_default_conf():
    resp = DagRunResponse(dag_run_id="run-1", dag_id="ingestion", state=DagRunState.running)
    assert resp.conf == {}


# ── trigger_dag_run ──────────────────────────────────────────────────────────


async def test_trigger_dag_run_posts_to_correct_url(client: AirflowClient):
    client._client.post = AsyncMock(
        return_value=_mock_response(_make_dag_run("running"))
    )

    result = await client.trigger_dag_run("ingestion", conf={"dataset_urn": "urn:test"})
    assert result.dag_id == "ingestion"
    assert result.state == DagRunState.running

    client._client.post.assert_called_once()
    call_args = client._client.post.call_args
    assert call_args[0][0] == "/api/v1/dags/ingestion/dagRuns"
    assert call_args[1]["json"] == {"conf": {"dataset_urn": "urn:test"}}


async def test_trigger_dag_run_with_no_conf_defaults_to_empty(client: AirflowClient):
    client._client.post = AsyncMock(
        return_value=_mock_response(_make_dag_run("running"))
    )

    await client.trigger_dag_run("ingestion")
    call_args = client._client.post.call_args
    assert call_args[1]["json"] == {"conf": {}}


async def test_trigger_dag_run_parses_response(client: AirflowClient):
    payload = _make_dag_run("running", dag_run_id="manual__2024-01-01T00:00:00+00:00")
    client._client.post = AsyncMock(return_value=_mock_response(payload))

    result = await client.trigger_dag_run("ingestion")
    assert result.dag_run_id == "manual__2024-01-01T00:00:00+00:00"


# ── get_dag_run ──────────────────────────────────────────────────────────────


async def test_get_dag_run_success(client: AirflowClient):
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("success"))
    )

    result = await client.get_dag_run("ingestion", "run-1")
    assert result.state == DagRunState.success
    assert result.is_terminal


async def test_get_dag_run_calls_correct_url(client: AirflowClient):
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("running"))
    )

    await client.get_dag_run("metrics", "run-abc")
    call_args = client._client.get.call_args
    assert call_args[0][0] == "/api/v1/dags/metrics/dagRuns/run-abc"


# ── wait_for_dag_run ─────────────────────────────────────────────────────────


async def test_wait_for_dag_run_success_immediately(client: AirflowClient):
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("success"))
    )

    result = await client.wait_for_dag_run("ingestion", "run-1", timeout_seconds=5)
    assert result.state == DagRunState.success


async def test_wait_for_dag_run_failure_raises(client: AirflowClient):
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("failed"))
    )

    with pytest.raises(AirflowExecutionFailedError) as exc_info:
        await client.wait_for_dag_run("ingestion", "run-1", timeout_seconds=5)
    assert exc_info.value.dag_id == "ingestion"
    assert exc_info.value.dag_run_id == "run-1"


async def test_wait_for_dag_run_timeout_raises(client: AirflowClient):
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("running"))
    )

    fake_time = 0.0

    def advance_time(*_args):
        nonlocal fake_time
        fake_time += 10.0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("asyncio.sleep", AsyncMock(side_effect=advance_time))
        mp.setattr(
            "asyncio.get_event_loop",
            lambda: type("L", (), {"time": lambda self: fake_time})(),
        )
        with pytest.raises(AirflowTimeoutError) as exc_info:
            await client.wait_for_dag_run(
                "ingestion", "run-1", timeout_seconds=5, poll_interval=5
            )
    assert exc_info.value.dag_id == "ingestion"
    assert exc_info.value.dag_run_id == "run-1"


async def test_wait_for_dag_run_polls_until_terminal(client: AirflowClient):
    """Should poll multiple times until state transitions to terminal."""
    responses = [
        _mock_response(_make_dag_run("running")),
        _mock_response(_make_dag_run("running")),
        _mock_response(_make_dag_run("success")),
    ]
    client._client.get = AsyncMock(side_effect=responses)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("asyncio.sleep", AsyncMock())
        result = await client.wait_for_dag_run(
            "ingestion", "run-1", timeout_seconds=300, poll_interval=5
        )
    assert result.state == DagRunState.success
    assert client._client.get.call_count == 3


# ── trigger_and_wait ─────────────────────────────────────────────────────────


async def test_trigger_and_wait_combined(client: AirflowClient):
    """trigger_and_wait should trigger then poll until terminal."""
    client._client.post = AsyncMock(
        return_value=_mock_response(_make_dag_run("running", dag_run_id="run-123"))
    )
    client._client.get = AsyncMock(
        return_value=_mock_response(_make_dag_run("success", dag_run_id="run-123"))
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("asyncio.sleep", AsyncMock())
        result = await client.trigger_and_wait("ingestion", conf={"key": "val"})

    assert result.state == DagRunState.success
    client._client.post.assert_called_once()
    client._client.get.assert_called_once()


# ── find_running_dag_runs ─────────────────────────────────────────────────────


async def test_find_running_dag_runs_returns_list(client: AirflowClient):
    body = {"dag_runs": [_make_dag_run("running", dag_run_id="run-1")]}
    client._client.get = AsyncMock(return_value=_mock_response(body))

    running = await client.find_running_dag_runs("ingestion")
    assert len(running) == 1
    assert running[0].state == DagRunState.running


async def test_find_running_dag_runs_empty(client: AirflowClient):
    client._client.get = AsyncMock(return_value=_mock_response({"dag_runs": []}))

    running = await client.find_running_dag_runs("ingestion")
    assert running == []


async def test_find_running_dag_runs_404_returns_empty(client: AirflowClient):
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    resp.raise_for_status.return_value = None  # 404 is handled before raise_for_status
    client._client.get = AsyncMock(return_value=resp)

    running = await client.find_running_dag_runs("nonexistent")
    assert running == []


async def test_find_running_dag_runs_uses_state_filter(client: AirflowClient):
    client._client.get = AsyncMock(return_value=_mock_response({"dag_runs": []}))

    await client.find_running_dag_runs("ingestion")
    call_args = client._client.get.call_args
    assert call_args[1]["params"]["state"] == "running"


# ── check_no_duplicate ────────────────────────────────────────────────────────


async def test_check_no_duplicate_raises_conflict(client: AirflowClient):
    running = _make_dag_run("running")
    running["conf"] = {"dataset_urn": "urn:test"}
    client._client.get = AsyncMock(return_value=_mock_response({"dag_runs": [running]}))

    from src.shared.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await client.check_no_duplicate(
            "ingestion", "dataset_urn", "urn:test", "INGESTION_RUNNING"
        )


async def test_check_no_duplicate_passes_when_no_running(client: AirflowClient):
    client._client.get = AsyncMock(return_value=_mock_response({"dag_runs": []}))

    # Should not raise
    await client.check_no_duplicate(
        "ingestion", "dataset_urn", "urn:test", "INGESTION_RUNNING"
    )


async def test_check_no_duplicate_passes_when_conf_key_differs(client: AirflowClient):
    """Running DAG run with different conf value should not raise."""
    running = _make_dag_run("running")
    running["conf"] = {"dataset_urn": "urn:other-dataset"}
    client._client.get = AsyncMock(return_value=_mock_response({"dag_runs": [running]}))

    # Should not raise — conf value doesn't match
    await client.check_no_duplicate(
        "ingestion", "dataset_urn", "urn:test", "INGESTION_RUNNING"
    )


# ── kill_dag_run ─────────────────────────────────────────────────────────────


async def test_kill_dag_run_success(client: AirflowClient):
    client._client.patch = AsyncMock(return_value=_mock_response({}))

    await client.kill_dag_run("ingestion", "run-1")
    client._client.patch.assert_called_once()
    call_args = client._client.patch.call_args
    assert "/api/v1/dags/ingestion/dagRuns/run-1" in call_args[0][0]
    assert call_args[1]["json"] == {"state": "failed"}


async def test_kill_dag_run_not_found_no_raise(client: AirflowClient):
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    resp.raise_for_status.return_value = None
    client._client.patch = AsyncMock(return_value=resp)

    await client.kill_dag_run("ingestion", "nonexistent")


async def test_kill_dag_run_already_terminal_no_raise(client: AirflowClient):
    resp = _mock_response({}, status_code=409)
    resp.status_code = 409
    resp.raise_for_status.return_value = None
    client._client.patch = AsyncMock(return_value=resp)

    await client.kill_dag_run("ingestion", "already-done")


# ── list_dags ────────────────────────────────────────────────────────────────


async def test_list_dags_returns_all(client: AirflowClient):
    body = {"dags": [{"dag_id": "ingestion"}, {"dag_id": "metrics"}]}
    client._client.get = AsyncMock(return_value=_mock_response(body))

    dags = await client.list_dags()
    assert len(dags) == 2
    dag_ids = {d["dag_id"] for d in dags}
    assert "ingestion" in dag_ids
    assert "metrics" in dag_ids


async def test_list_dags_with_prefix_filter(client: AirflowClient):
    body = {
        "dags": [
            {"dag_id": "ingestion"},
            {"dag_id": "ingestion-hourly"},
            {"dag_id": "metrics"},
        ]
    }
    client._client.get = AsyncMock(return_value=_mock_response(body))

    dags = await client.list_dags(prefix="ingestion")
    assert len(dags) == 2
    assert all(d["dag_id"].startswith("ingestion") for d in dags)


async def test_list_dags_404_returns_empty(client: AirflowClient):
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    resp.raise_for_status.return_value = None
    client._client.get = AsyncMock(return_value=resp)

    dags = await client.list_dags()
    assert dags == []


async def test_list_dags_passes_prefix_as_pattern_param(client: AirflowClient):
    client._client.get = AsyncMock(return_value=_mock_response({"dags": []}))

    await client.list_dags(prefix="ingest")
    call_args = client._client.get.call_args
    assert call_args[1]["params"]["dag_id_pattern"] == "ingest"


async def test_list_dags_without_prefix_no_pattern_param(client: AirflowClient):
    client._client.get = AsyncMock(return_value=_mock_response({"dags": []}))

    await client.list_dags()
    call_args = client._client.get.call_args
    assert "dag_id_pattern" not in call_args[1]["params"]


# ── delete_dag_run ───────────────────────────────────────────────────────────


async def test_delete_dag_run_success(client: AirflowClient):
    client._client.delete = AsyncMock(return_value=_mock_response({}))

    await client.delete_dag_run("ingestion", "run-1")
    client._client.delete.assert_called_once()
    call_args = client._client.delete.call_args
    assert "run-1" in call_args[0][0]


async def test_delete_dag_run_not_found_no_raise(client: AirflowClient):
    resp = _mock_response({}, status_code=404)
    resp.status_code = 404
    resp.raise_for_status.return_value = None
    client._client.delete = AsyncMock(return_value=resp)

    await client.delete_dag_run("ingestion", "nonexistent")
