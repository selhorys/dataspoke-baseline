"""Unit tests for validation_sync: cron scheduling and flow sync.

Tests cover:
- schedule_to_flow_id() stability and prefix
- generate_periodic_flow_yaml() valid YAML structure
- sync_periodic_validation_flows() create/delete/unchanged logic
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.workflows.validation_sync import (
    FLOW_PREFIX,
    generate_periodic_flow_yaml,
    schedule_to_flow_id,
    sync_periodic_validation_flows,
)


# ── schedule_to_flow_id ────────────────────────────────────────────────────────


def test_schedule_to_flow_id_has_prefix():
    flow_id = schedule_to_flow_id("0 3 * * *")
    assert flow_id.startswith(FLOW_PREFIX)


def test_schedule_to_flow_id_total_length():
    # "validation-periodic-" (20 chars) + 8 hex chars = 28
    flow_id = schedule_to_flow_id("0 3 * * *")
    assert len(flow_id) == len(FLOW_PREFIX) + 8


def test_schedule_to_flow_id_stable_for_same_cron():
    """Same cron expression always produces the same flow ID."""
    assert schedule_to_flow_id("0 3 * * *") == schedule_to_flow_id("0 3 * * *")


def test_schedule_to_flow_id_different_crons_produce_different_ids():
    """Distinct cron expressions must produce distinct flow IDs."""
    assert schedule_to_flow_id("0 3 * * *") != schedule_to_flow_id("0 6 * * *")
    assert schedule_to_flow_id("*/5 * * * *") != schedule_to_flow_id("0 1 * * 0")


def test_schedule_to_flow_id_deterministic_hash():
    """Regression: flow ID matches MD5 of cron bytes (first 8 hex chars)."""
    cron = "0 3 * * *"
    expected = FLOW_PREFIX + hashlib.md5(cron.encode()).hexdigest()[:8]
    assert schedule_to_flow_id(cron) == expected


# ── generate_periodic_flow_yaml ────────────────────────────────────────────────


@pytest.fixture()
def sample_flow() -> dict:
    raw = generate_periodic_flow_yaml("0 3 * * *", "http://localhost:8000")
    return yaml.safe_load(raw)


def test_generated_yaml_id_matches_schedule_to_flow_id(sample_flow):
    assert sample_flow["id"] == schedule_to_flow_id("0 3 * * *")


def test_generated_yaml_namespace_is_dataspoke(sample_flow):
    assert sample_flow["namespace"] == "dataspoke"


def test_generated_yaml_has_cron_trigger(sample_flow):
    triggers = sample_flow["triggers"]
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger["type"] == "io.kestra.plugin.core.trigger.Schedule"
    assert trigger["cron"] == "0 3 * * *"


def test_generated_yaml_has_list_datasets_task(sample_flow):
    tasks = sample_flow["tasks"]
    list_task = next(t for t in tasks if t["id"] == "list_datasets")
    assert list_task["type"] == "io.kestra.plugin.core.http.Request"
    assert list_task["method"] == "POST"
    assert "/internal/activities/validation/list-periodic" in list_task["uri"]


def test_generated_yaml_list_datasets_body_contains_schedule(sample_flow):
    tasks = sample_flow["tasks"]
    list_task = next(t for t in tasks if t["id"] == "list_datasets")
    assert "0 3 * * *" in list_task["body"]


def test_generated_yaml_has_each_parallel_task_with_default_concurrent(sample_flow):
    tasks = sample_flow["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    assert each_task["type"] == "io.kestra.plugin.core.flow.EachParallel"
    assert each_task["concurrent"] == 5


def test_generated_yaml_custom_concurrent_value():
    raw = generate_periodic_flow_yaml("*/15 * * * *", "http://host:8000", concurrent=10)
    parsed = yaml.safe_load(raw)
    each_task = next(t for t in parsed["tasks"] if t["id"] == "run_each")
    assert each_task["concurrent"] == 10


def test_generated_yaml_run_validation_task_exists(sample_flow):
    tasks = sample_flow["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    run_task = next(t for t in each_task["tasks"] if t["id"] == "run_validation")
    assert run_task["type"] == "io.kestra.plugin.core.http.Request"
    assert run_task["method"] == "POST"
    assert "/internal/activities/validation/run" in run_task["uri"]


def test_generated_yaml_run_validation_has_retry(sample_flow):
    tasks = sample_flow["tasks"]
    each_task = next(t for t in tasks if t["id"] == "run_each")
    run_task = next(t for t in each_task["tasks"] if t["id"] == "run_validation")
    assert run_task["retry"]["maxAttempt"] == 3
    assert run_task["retry"]["interval"] == "PT10S"


def test_generated_yaml_callback_base_url_in_input_default(sample_flow):
    inputs = sample_flow["inputs"]
    cb = next(i for i in inputs if i["id"] == "callback_base_url")
    assert cb["defaults"] == "http://localhost:8000"


def test_generated_yaml_kestra_expressions_survive_formatting():
    """Kestra {{ }} template expressions must pass through verbatim."""
    raw = generate_periodic_flow_yaml("*/5 * * * *", "http://localhost:8000")
    assert "{{ inputs.callback_base_url }}" in raw
    assert "{{ outputs.list_datasets.body }}" in raw
    assert "{{ taskrun.value }}" in raw


# ── sync_periodic_validation_flows ────────────────────────────────────────────


@pytest.fixture()
def mock_db():
    return AsyncMock()


@pytest.fixture()
def mock_kestra():
    client = AsyncMock()
    client.list_flows = AsyncMock(return_value=[])
    client.create_or_update_flow = AsyncMock(return_value={})
    client.delete_flow = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_sync_creates_flow_for_active_cron_schedule(mock_db, mock_kestra):
    cron = "0 6 * * 1"
    result_mock = MagicMock()
    result_mock.all.return_value = [(cron,)]
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert schedule_to_flow_id(cron) in result["created"]
    mock_kestra.create_or_update_flow.assert_called_once()


@pytest.mark.asyncio
async def test_sync_creates_separate_flow_per_distinct_schedule(mock_db, mock_kestra):
    crons = ["0 6 * * 1", "0 18 * * 5"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(c,) for c in crons]
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert len(result["created"]) == 2
    for cron in crons:
        assert schedule_to_flow_id(cron) in result["created"]
    assert mock_kestra.create_or_update_flow.call_count == 2


@pytest.mark.asyncio
async def test_sync_deletes_stale_validation_periodic_flows(mock_db, mock_kestra):
    stale_id = schedule_to_flow_id("0 0 * * 0")

    # No active schedules in DB
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    # Kestra has the stale flow
    mock_kestra.list_flows = AsyncMock(return_value=[{"id": stale_id}])

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert stale_id in result["deleted"]
    mock_kestra.delete_flow.assert_called_once_with(stale_id)


@pytest.mark.asyncio
async def test_sync_keeps_flow_matching_active_cron_as_unchanged(mock_db, mock_kestra):
    cron = "0 12 * * *"
    flow_id = schedule_to_flow_id(cron)

    result_mock = MagicMock()
    result_mock.all.return_value = [(cron,)]
    mock_db.execute = AsyncMock(return_value=result_mock)

    # create_or_update raises to simulate "already exists / no re-create needed"
    mock_kestra.create_or_update_flow = AsyncMock(
        side_effect=Exception("Already exists")
    )
    mock_kestra.list_flows = AsyncMock(return_value=[{"id": flow_id}])

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    # Flow was not created (raised), so it appears in unchanged
    assert flow_id in result["unchanged"]
    assert flow_id not in result["created"]


@pytest.mark.asyncio
async def test_sync_returns_empty_lists_when_no_active_schedules(mock_db, mock_kestra):
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert result["created"] == []
    assert result["deleted"] == []
    mock_kestra.create_or_update_flow.assert_not_called()


@pytest.mark.asyncio
async def test_sync_returns_dict_with_required_keys(mock_db, mock_kestra):
    result_mock = MagicMock()
    result_mock.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    assert "created" in result
    assert "deleted" in result
    assert "unchanged" in result


@pytest.mark.asyncio
async def test_sync_does_not_delete_active_flow_present_in_kestra(mock_db, mock_kestra):
    cron = "0 3 * * *"
    flow_id = schedule_to_flow_id(cron)

    result_mock = MagicMock()
    result_mock.all.return_value = [(cron,)]
    mock_db.execute = AsyncMock(return_value=result_mock)

    # Kestra already has the same flow
    mock_kestra.list_flows = AsyncMock(return_value=[{"id": flow_id}])

    result = await sync_periodic_validation_flows(
        mock_kestra, mock_db, "http://localhost:8000"
    )

    # Active flow should never appear in deleted
    assert flow_id not in result["deleted"]
    mock_kestra.delete_flow.assert_not_called()
