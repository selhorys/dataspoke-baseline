"""Unit tests for MetricsService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.service import MetricsService
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionError
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
    schedule_tier: str | None = None,
    active: bool = True,
):
    row = MagicMock()
    row.id = metric_id
    row.title = title
    row.description = description
    row.theme = theme
    row.measurement_query = measurement_query or {"type": "poorly_documented"}
    row.schedule_tier = schedule_tier
    row.is_active = active
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    metric_id: str = "test.metric.doc_coverage",
    value: float = 42.0,
    breakdown: dict | None = None,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.metric_id = metric_id
    row.value = value
    row.breakdown = breakdown or {
        "dataset_count": 10,
        "datasets": [],
    }
    row.measured_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db, cache):
    return MetricsService(datahub=datahub, db=db, cache=cache)


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


async def test_list_metrics_with_is_active_filter(service, db):
    rows = [_make_definition_row(active=True)]
    mock_paginated_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(is_active_filter=True)
    assert total == 1
    assert metrics[0].is_active is True


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

    metric, created = await service.upsert_metric_config(
        metric_id="test.new",
        title="New Metric",
        description="desc",
        theme="quality",
        measurement_query={"type": "poorly_documented"},
    )
    assert db.add.called
    assert db.commit.await_count >= 1
    assert created is True


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
    assert db.commit.await_count >= 1


async def test_patch_metric_config(service, db):
    row = _make_definition_row()
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    await service.patch_metric_config(row.id, {"title": "Patched Title"})
    assert row.title == "Patched Title"
    assert db.commit.await_count >= 1


async def test_patch_metric_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.patch_metric_config("nonexistent", {"title": "x"})


async def test_delete_metric_config(service, db):
    row = _make_definition_row()
    mock_scalar_query(db, row)

    await service.delete_metric_config(row.id)
    db.delete.assert_called_once_with(row)
    assert db.commit.await_count >= 1


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


# ── run (simplified pipeline) ─────────────────────────────────────────────────


async def test_run_poorly_documented_persists(service, db, datahub):
    """poorly_documented run: measure + persist MetricResult + record event."""
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    def_row = _make_definition_row(
        measurement_query={"type": "poorly_documented"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1", "urn:li:dataset:2"])

    props_short = MagicMock(spec=DatasetPropertiesClass)
    props_short.description = "short"  # < 20 chars -> affected

    props_long = MagicMock(spec=DatasetPropertiesClass)
    props_long.description = "A very long description that is well documented"  # >= 20 chars

    datahub.get_aspect = AsyncMock(side_effect=[props_short, props_long])

    result = await service.run(def_row.id)

    assert result.status == "success"
    assert result.detail["value"] == 1.0
    # commit: result persist + run.completed event
    assert db.commit.await_count == 2
    db.add.assert_called()


async def test_run_dry_run_does_not_persist(service, db, datahub):
    """dry_run=True: measure but do not call db.add for MetricResult."""
    def_row = _make_definition_row(
        measurement_query={"type": "poorly_documented"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1"])
    datahub.get_aspect = AsyncMock(return_value=None)

    result = await service.run(def_row.id, dry_run=True)

    assert result.status == "success"
    assert result.detail["dry_run"] is True
    # No commit for dry run
    assert db.commit.await_count == 0


async def test_run_stale_datasets_no_freshness_rule(service, db, datahub):
    """stale_datasets: dataset with no ValidationConfig -> no_freshness_rule category."""
    def_row = _make_definition_row(
        measurement_query={"type": "stale_datasets"}
    )

    # get_metric lookup
    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # ValidationConfig query for urn:1 -> None (no config)
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[def_result, config_result])
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1"])

    result = await service.run(def_row.id)

    assert result.status == "success"
    assert result.detail["value"] == 1.0
    breakdown = result.detail["breakdown_summary"]
    assert breakdown["affected_count"] == 1


async def test_run_stale_datasets_freshness_failure(service, db, datahub):
    """stale_datasets: dataset with freshness rule + FAILURE result -> counted."""
    def_row = _make_definition_row(
        measurement_query={"type": "stale_datasets"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # ValidationConfig with a freshness rule
    config_row = MagicMock()
    config_row.rules = [{"type": "freshness", "rule_id": "rule-001"}]
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row

    # Latest ValidationResult: FAILURE
    val_row = MagicMock()
    val_row.assertion_result = "FAILURE"
    val_result = MagicMock()
    val_result.scalar_one_or_none.return_value = val_row

    db.execute = AsyncMock(side_effect=[def_result, config_result, val_result])
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1"])

    result = await service.run(def_row.id)

    assert result.status == "success"
    assert result.detail["value"] == 1.0


async def test_run_stale_datasets_freshness_pass(service, db, datahub):
    """stale_datasets: dataset with freshness rule + SUCCESS result -> NOT counted."""
    def_row = _make_definition_row(
        measurement_query={"type": "stale_datasets"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    config_row = MagicMock()
    config_row.rules = [{"type": "freshness", "rule_id": "rule-002"}]
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row

    # Latest ValidationResult: SUCCESS (not FAILURE)
    val_row = MagicMock()
    val_row.assertion_result = "SUCCESS"
    val_result = MagicMock()
    val_result.scalar_one_or_none.return_value = val_row

    db.execute = AsyncMock(side_effect=[def_result, config_result, val_result])
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1"])

    result = await service.run(def_row.id)

    assert result.status == "success"
    assert result.detail["value"] == 0.0


async def test_dataset_filter_passthrough(service, db, datahub):
    """tags/glossary_terms from measurement_query.dataset_filter are forwarded to enumerate_datasets."""
    def_row = _make_definition_row(
        measurement_query={
            "type": "poorly_documented",
            "dataset_filter": {
                "tags": ["urn:li:tag:PII"],
                "glossary_terms": ["urn:li:glossaryTerm:CustomerData"],
            },
        }
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    await service.run(def_row.id, dry_run=True)

    datahub.enumerate_datasets.assert_awaited_once_with(
        tags=["urn:li:tag:PII"],
        glossary_terms=["urn:li:glossaryTerm:CustomerData"],
    )


async def test_unknown_metric_type_raises(service, db, datahub):
    """Unknown metric type in measurement_query raises PreconditionError."""
    def_row = _make_definition_row(
        measurement_query={"type": "dataset_count"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1"])

    with pytest.raises(PreconditionError) as exc_info:
        await service.run(def_row.id, dry_run=True)
    assert exc_info.value.error_code == "UNSUPPORTED_METRIC_TYPE"


async def test_breakdown_field_names(service, db, datahub):
    """Breakdown must use 'dataset_count' and 'datasets' keys; 'metric_type' must be absent."""
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    def_row = _make_definition_row(
        measurement_query={"type": "poorly_documented"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1", "urn:2"])

    # Both with short descriptions -> both affected
    short_props = MagicMock(spec=DatasetPropertiesClass)
    short_props.description = "bad"
    datahub.get_aspect = AsyncMock(return_value=short_props)

    result = await service.run(def_row.id)
    assert result.status == "success"

    # Check the persisted breakdown via db.add call
    # The MetricResult object passed to db.add has the breakdown
    add_call_args = db.add.call_args_list
    # Find the MetricResult add (not the Event add)
    metric_result_arg = None
    for call in add_call_args:
        obj = call[0][0]
        if hasattr(obj, "breakdown") and hasattr(obj, "metric_id"):
            metric_result_arg = obj
            break

    assert metric_result_arg is not None
    breakdown = metric_result_arg.breakdown
    assert "dataset_count" in breakdown
    assert "datasets" in breakdown
    assert "metric_type" not in breakdown
    assert "scanned_count" not in breakdown
    assert "affected_datasets" not in breakdown


# ── activate / deactivate ───────────────────────────────────────────────────


async def test_activate_inactive_metric(service, db):
    row = _make_definition_row(active=False)
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    metric = await service.activate(row.id)
    assert metric.is_active is True
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
    assert metric.is_active is False
    assert db.commit.await_count == 2


async def test_deactivate_already_inactive_raises(service, db):
    row = _make_definition_row(active=False)
    mock_scalar_query(db, row)

    with pytest.raises(ConflictError) as exc_info:
        await service.deactivate(row.id)
    assert exc_info.value.error_code == "ALREADY_INACTIVE"


# ── events ──────────────────────────────────────────────────────────────────


async def test_get_events(service, db):
    metric_id = "test.metric.events"
    rows = [
        make_event_row(
            entity_type="metric",
            event_type="METRIC.RUN_COMPLETE",
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
