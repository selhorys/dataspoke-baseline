"""Unit tests for MetricsService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.service import MetricsService
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import (
    make_event_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)


def _make_definition_row(
    metric_id: str = "test.metric.doc_coverage",
    title: str = "Documentation Coverage",
    description: str = "Measures documentation quality",
    theme: str = "quality",
    measurement_query: dict | None = None,
    schedule: str | None = None,
    alarm_enabled: bool = False,
    alarm_threshold: dict | None = None,
    active: bool = True,
):
    row = MagicMock()
    row.id = metric_id
    row.title = title
    row.description = description
    row.theme = theme
    row.measurement_query = measurement_query or {"type": "dataset_count"}
    row.schedule = schedule
    row.alarm_enabled = alarm_enabled
    row.alarm_threshold = alarm_threshold
    row.active = active
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    metric_id: str = "test.metric.doc_coverage",
    value: float = 42.0,
    breakdown: dict | None = None,
    alarm_triggered: bool = False,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.metric_id = metric_id
    row.value = value
    row.breakdown = breakdown or {
        "metric_type": "dataset_count",
        "scanned_count": 10,
        "affected_datasets": [],
    }
    row.alarm_triggered = alarm_triggered
    row.run_id = uuid.uuid4()
    row.measured_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db, cache, notification):
    return MetricsService(datahub=datahub, db=db, cache=cache, notification=notification)


# ── list_metrics ──────────────────────────────────────────────────────────────


async def test_list_metrics_returns_paginated(service, db):
    rows = [_make_definition_row(metric_id=f"metric_{i}") for i in range(3)]
    mock_paginated_query(db, rows, total_count=5)

    metrics, total = await service.list_metrics(offset=0, limit=3)
    assert total == 5
    assert len(metrics) == 3


async def test_list_metrics_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    metrics, total = await service.list_metrics()
    assert total == 0
    assert metrics == []


async def test_list_metrics_with_theme_filter(service, db):
    rows = [_make_definition_row(theme="quality")]
    mock_paginated_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(theme_filter="quality")
    assert total == 1
    assert len(metrics) == 1
    assert metrics[0].theme == "quality"


async def test_list_metrics_with_active_filter(service, db):
    rows = [_make_definition_row(active=True)]
    mock_paginated_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(active_filter=True)
    assert total == 1
    assert metrics[0].active is True


# ── get_metric ────────────────────────────────────────────────────────────────


async def test_get_metric_found(service, db):
    row = _make_definition_row(title="Doc Coverage")
    mock_scalar_query(db, row)

    metric = await service.get_metric(row.id)
    assert metric.title == "Doc Coverage"
    assert metric.id == row.id


async def test_get_metric_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_metric("nonexistent")
    assert exc_info.value.error_code == "METRIC_DEFINITION_NOT_FOUND"


# ── get_metric_attr ──────────────────────────────────────────────────────────


async def test_get_metric_attr_with_latest_result(service, db):
    def_row = _make_definition_row()
    result_row = _make_result_row(value=85.5)

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = result_row

    db.execute = AsyncMock(side_effect=[def_result, latest_result])

    attr = await service.get_metric_attr(def_row.id)
    assert attr["title"] == def_row.title
    assert attr["latest_value"] == 85.5


async def test_get_metric_attr_no_results(service, db):
    def_row = _make_definition_row()

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, latest_result])

    attr = await service.get_metric_attr(def_row.id)
    assert attr["latest_value"] is None
    assert attr["latest_measured_at"] is None


# ── get_metric_config / upsert / patch / delete ─────────────────────────────


async def test_upsert_metric_config_create(service, db):
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    metric = await service.upsert_metric_config(
        metric_id="test.new",
        title="New Metric",
        description="desc",
        theme="quality",
        measurement_query={"type": "dataset_count"},
    )
    assert db.add.called
    assert db.commit.await_count == 1


async def test_upsert_metric_config_update(service, db):
    existing = _make_definition_row()
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    await service.upsert_metric_config(
        metric_id=existing.id,
        title="Updated",
        description="new desc",
        theme="freshness",
        measurement_query={"type": "stale_datasets"},
    )
    assert existing.title == "Updated"
    assert existing.theme == "freshness"
    assert db.commit.await_count == 1


async def test_patch_metric_config(service, db):
    row = _make_definition_row()
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    await service.patch_metric_config(row.id, {"title": "Patched Title"})
    assert row.title == "Patched Title"
    assert db.commit.await_count == 1


async def test_patch_metric_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.patch_metric_config("nonexistent", {"title": "x"})


async def test_delete_metric_config(service, db):
    row = _make_definition_row()
    mock_scalar_query(db, row)

    await service.delete_metric_config(row.id)
    db.delete.assert_called_once_with(row)
    assert db.commit.await_count == 1


async def test_delete_metric_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.delete_metric_config("nonexistent")


# ── get_results ──────────────────────────────────────────────────────────────


async def test_get_results_paginated(service, db):
    rows = [_make_result_row() for _ in range(2)]
    mock_paginated_query(db, rows, total_count=10)

    results, total = await service.get_results("test.metric", offset=0, limit=2)
    assert total == 10
    assert len(results) == 2


# ── run ──────────────────────────────────────────────────────────────────────


async def test_run_measures_and_persists(service, db, datahub):
    def_row = _make_definition_row()

    # get_metric lookup
    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # prev result lookup
    prev_result = MagicMock()
    prev_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, prev_result])
    db.refresh = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1", "urn:2"])

    result = await service.run(def_row.id)
    assert result.status == "success"
    assert result.detail["value"] == 2.0
    # commit: result persist + run.completed event
    assert db.commit.await_count == 2


async def test_run_dry_run_skips_persist(service, db, datahub):
    def_row = _make_definition_row()

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    prev_result = MagicMock()
    prev_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, prev_result])
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1"])

    result = await service.run(def_row.id, dry_run=True)
    assert result.status == "success"
    assert result.detail["dry_run"] is True
    # No commit for dry run
    assert db.commit.await_count == 0


async def test_run_with_alarm_triggered(service, db, datahub, notification):
    def_row = _make_definition_row(
        alarm_enabled=True,
        alarm_threshold={"operator": "gt", "value": 1},
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    prev_result = MagicMock()
    prev_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, prev_result])
    db.refresh = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1", "urn:2", "urn:3"])

    result = await service.run(def_row.id)
    assert result.detail["alarm_triggered"] is True
    # commit: result + run.completed event + alarm event
    assert db.commit.await_count == 3
    notification.send_alarm.assert_awaited_once()


async def test_run_alarm_not_triggered(service, db, datahub):
    def_row = _make_definition_row(
        alarm_threshold={"operator": "gt", "value": 100},
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    prev_result = MagicMock()
    prev_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, prev_result])
    db.refresh = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1"])

    result = await service.run(def_row.id)
    assert result.detail["alarm_triggered"] is False
    # commit: result + run.completed event only
    assert db.commit.await_count == 2


async def test_run_with_delta_findings(service, db, datahub):
    def_row = _make_definition_row(
        measurement_query={"type": "unowned_datasets"},
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # Previous run had urn:1 affected
    prev_row = MagicMock()
    prev_row.breakdown = {
        "metric_type": "unowned_datasets",
        "scanned_count": 2,
        "affected_datasets": [{"urn": "urn:1", "reason": "no owner"}],
    }
    prev_result = MagicMock()
    prev_result.scalar_one_or_none.return_value = prev_row

    db.execute = AsyncMock(side_effect=[def_result, prev_result])
    db.refresh = AsyncMock()

    # Current run: urn:2 unowned (urn:1 resolved, urn:2 new)
    ownership_mock = MagicMock()
    ownership_mock.owners = [MagicMock()]

    async def mock_get_aspect(urn, cls):
        if urn == "urn:1":
            return ownership_mock
        return None

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1", "urn:2"])
    datahub.get_aspect = AsyncMock(side_effect=mock_get_aspect)

    result = await service.run(def_row.id)
    assert result.status == "success"
    # commit: result + run.completed + findings.detected (urn:2 is new)
    assert db.commit.await_count >= 3


# ── activate / deactivate ───────────────────────────────────────────────────


async def test_activate_inactive_metric(service, db):
    row = _make_definition_row(active=False)
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    metric = await service.activate(row.id)
    assert metric.active is True
    # commit: activate + event
    assert db.commit.await_count == 2


async def test_activate_already_active_raises(service, db):
    row = _make_definition_row(active=True)
    mock_scalar_query(db, row)

    with pytest.raises(ConflictError) as exc_info:
        await service.activate(row.id)
    assert exc_info.value.error_code == "ALREADY_ACTIVE"


async def test_deactivate_active_metric(service, db):
    row = _make_definition_row(active=True)
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    metric = await service.deactivate(row.id)
    assert metric.active is False
    assert db.commit.await_count == 2


async def test_deactivate_already_inactive_raises(service, db):
    row = _make_definition_row(active=False)
    mock_scalar_query(db, row)

    with pytest.raises(ConflictError) as exc_info:
        await service.deactivate(row.id)
    assert exc_info.value.error_code == "ALREADY_INACTIVE"


# ── _check_threshold ────────────────────────────────────────────────────────


def test_check_threshold_gt():
    assert MetricsService._check_threshold(10, {"operator": "gt", "value": 5}) is True
    assert MetricsService._check_threshold(5, {"operator": "gt", "value": 5}) is False


def test_check_threshold_lt():
    assert MetricsService._check_threshold(3, {"operator": "lt", "value": 5}) is True
    assert MetricsService._check_threshold(5, {"operator": "lt", "value": 5}) is False


def test_check_threshold_gte():
    assert MetricsService._check_threshold(5, {"operator": "gte", "value": 5}) is True
    assert MetricsService._check_threshold(4, {"operator": "gte", "value": 5}) is False


def test_check_threshold_lte():
    assert MetricsService._check_threshold(5, {"operator": "lte", "value": 5}) is True
    assert MetricsService._check_threshold(6, {"operator": "lte", "value": 5}) is False


def test_check_threshold_none():
    assert MetricsService._check_threshold(10, None) is False


def test_check_threshold_invalid_operator():
    assert MetricsService._check_threshold(10, {"operator": "unknown", "value": 5}) is False


# ── events ──────────────────────────────────────────────────────────────────


async def test_get_events(service, db):
    metric_id = "test.metric.events"
    rows = [
        make_event_row(
            entity_type="metric",
            event_type="metric.run.completed",
            entity_id=metric_id,
            minutes_ago=i,
        )
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(metric_id, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "metric"


async def test_get_events_with_time_range(service, db):
    metric_id = "test.metric"
    mock_paginated_query(db, [], total_count=0)

    now = datetime.now(tz=UTC)
    events, total = await service.get_events(
        metric_id,
        from_dt=now - timedelta(hours=1),
        to_dt=now,
    )
    assert total == 0
    assert events == []


# ── _compute_delta ──────────────────────────────────────────────────────────


def test_compute_delta_no_prev():
    breakdown = {"affected_datasets": [{"urn": "urn:1"}]}
    assert MetricsService._compute_delta(breakdown, None) is None


def test_compute_delta_new_and_resolved():
    breakdown = {"affected_datasets": [{"urn": "urn:2"}, {"urn": "urn:3"}]}
    prev_row = MagicMock()
    prev_row.breakdown = {"affected_datasets": [{"urn": "urn:1"}, {"urn": "urn:2"}]}

    delta = MetricsService._compute_delta(breakdown, prev_row)
    assert delta is not None
    assert "urn:3" in delta["new_findings"]
    assert "urn:1" in delta["resolved_since_last"]
    assert "urn:2" not in delta["new_findings"]


def test_compute_delta_no_change():
    breakdown = {"affected_datasets": [{"urn": "urn:1"}]}
    prev_row = MagicMock()
    prev_row.breakdown = {"affected_datasets": [{"urn": "urn:1"}]}

    assert MetricsService._compute_delta(breakdown, prev_row) is None
