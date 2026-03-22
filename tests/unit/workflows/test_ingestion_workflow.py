"""Unit tests for the ingestion workflow module.

Tests cover:
- schedule_to_flow_id() hashing stability and prefix
- generate_periodic_flow_yaml() output structure
- sync_periodic_ingestion_flows() create/delete/unchanged logic
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.workflows.ingestion import (
    FLOW_ID,
    PERIODIC_FLOW_PREFIX,
    generate_periodic_flow_yaml,
    schedule_to_flow_id,
    sync_periodic_ingestion_flows,
)


# ── Constants ──────────────────────────────────────────────────────────────────


def test_flow_id_constant():
    assert FLOW_ID == "ingestion"


def test_periodic_flow_prefix():
    assert PERIODIC_FLOW_PREFIX == "ingestion-periodic-"


# ── schedule_to_flow_id ────────────────────────────────────────────────────────


def test_schedule_to_flow_id_has_prefix():
    flow_id = schedule_to_flow_id("0 2 * * *")
    assert flow_id.startswith(PERIODIC_FLOW_PREFIX)


def test_schedule_to_flow_id_length():
    # prefix (18) + 8 hex chars = 26
    flow_id = schedule_to_flow_id("0 2 * * *")
    assert len(flow_id) == len(PERIODIC_FLOW_PREFIX) + 8


def test_schedule_to_flow_id_stable():
    """Same schedule always produces the same ID."""
    assert schedule_to_flow_id("0 2 * * *") == schedule_to_flow_id("0 2 * * *")


def test_schedule_to_flow_id_different_schedules():
    """Different schedules produce different IDs."""
    assert schedule_to_flow_id("0 2 * * *") != schedule_to_flow_id("0 6 * * *")


def test_schedule_to_flow_id_known_hash():
    """Regression test — MD5 of '0 2 * * *' first 8 chars."""
    import hashlib

    expected = "ingestion-periodic-" + hashlib.md5(b"0 2 * * *").hexdigest()[:8]
    assert schedule_to_flow_id("0 2 * * *") == expected


# ── generate_periodic_flow_yaml ────────────────────────────────────────────────


@pytest.fixture()
def sample_yaml() -> dict:
    schedule = "0 2 * * *"
    callback = "http://localhost:8000"
    raw = generate_periodic_flow_yaml(schedule, callback)
    return yaml.safe_load(raw)


def test_generated_yaml_id(sample_yaml):
    assert sample_yaml["id"] == schedule_to_flow_id("0 2 * * *")


def test_generated_yaml_namespace(sample_yaml):
    assert sample_yaml["namespace"] == "dataspoke"


def test_generated_yaml_has_cron_trigger(sample_yaml):
    triggers = sample_yaml["triggers"]
    assert len(triggers) == 1
    assert triggers[0]["type"] == "io.kestra.plugin.core.trigger.Schedule"
    assert triggers[0]["cron"] == "0 2 * * *"


def test_generated_yaml_has_list_datasets_task(sample_yaml):
    tasks = sample_yaml["tasks"]
    list_task = next(t for t in tasks if t["id"] == "list_datasets")
    assert list_task["type"] == "io.kestra.plugin.core.http.Request"
    assert list_task["method"] == "POST"
    assert "/internal/activities/list-periodic-datasets" in list_task["uri"]


def test_generated_yaml_list_datasets_body_contains_schedule(sample_yaml):
    tasks = sample_yaml["tasks"]
    list_task = next(t for t in tasks if t["id"] == "list_datasets")
    assert "0 2 * * *" in list_task["body"]


def test_generated_yaml_has_each_sequential_task(sample_yaml):
    tasks = sample_yaml["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    assert each_task["type"] == "io.kestra.plugin.core.flow.EachSequential"


def test_generated_yaml_each_sequential_has_run_ingestion(sample_yaml):
    tasks = sample_yaml["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    subtask = next(t for t in each_task["tasks"] if t["id"] == "run_ingestion")
    assert subtask["type"] == "io.kestra.plugin.core.http.Request"
    assert subtask["method"] == "POST"


def test_generated_yaml_run_ingestion_has_retry(sample_yaml):
    tasks = sample_yaml["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    subtask = next(t for t in each_task["tasks"] if t["id"] == "run_ingestion")
    assert subtask["retry"]["maxAttempt"] == 3
    assert subtask["retry"]["interval"] == "PT10S"


def test_generated_yaml_callback_base_url_default(sample_yaml):
    inputs = sample_yaml["inputs"]
    cb_input = next(i for i in inputs if i["id"] == "callback_base_url")
    assert cb_input["defaults"] == "http://localhost:8000"


def test_generated_yaml_kestra_expressions_preserved():
    """Kestra {{ }} expressions must survive Python string formatting."""
    raw = generate_periodic_flow_yaml("*/5 * * * *", "http://localhost:8000")
    # These Kestra expressions must appear verbatim
    assert "{{ inputs.callback_base_url }}" in raw
    assert "{{ outputs.list_datasets.body }}" in raw
    assert "{{ taskrun.value }}" in raw


# ── sync_periodic_ingestion_flows ──────────────────────────────────────────────


def _make_row(schedule: str):
    """Simulate a SQLAlchemy Row with index access."""
    row = MagicMock()
    row.__getitem__ = lambda self, idx: schedule
    return row


@pytest.fixture()
def mock_db():
    """AsyncSession mock with configurable schedule rows."""
    db = AsyncMock()
    return db


@pytest.fixture()
def mock_kestra():
    client = AsyncMock()
    client.list_flows = AsyncMock(return_value=[])
    client.create_or_update_flow = AsyncMock(return_value={})
    client.delete_flow = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_sync_creates_flows_for_active_schedules(mock_db, mock_kestra):
    schedule = "0 2 * * *"

    result_mock = MagicMock()
    result_mock.all.return_value = [(schedule,)]
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_ingestion_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert schedule_to_flow_id(schedule) in result["created"]
    mock_kestra.create_or_update_flow.assert_called_once()


@pytest.mark.asyncio
async def test_sync_deletes_stale_flows(mock_db, mock_kestra):
    stale_id = schedule_to_flow_id("0 3 * * *")

    # No active schedules in DB
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    # Kestra has a stale flow
    mock_kestra.list_flows = AsyncMock(return_value=[{"id": stale_id}])

    result = await sync_periodic_ingestion_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert stale_id in result["deleted"]
    mock_kestra.delete_flow.assert_called_once_with(stale_id)


@pytest.mark.asyncio
async def test_sync_no_flows_when_empty(mock_db, mock_kestra):
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_ingestion_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert result["created"] == []
    assert result["deleted"] == []
    mock_kestra.create_or_update_flow.assert_not_called()


@pytest.mark.asyncio
async def test_sync_returns_dict_keys(mock_db, mock_kestra):
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_ingestion_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert "created" in result
    assert "deleted" in result
    assert "unchanged" in result
