"""Unit tests for MetricsService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.service import MetricsService
from src.shared.exceptions import EntityNotFoundError, PreconditionFailedError
from tests.unit.backend.conftest import (
    make_event_row,
    make_metric_breakdown_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)


def _make_definition_row(
    metric_id: str = "ingestion-freshness",
    title: str = "Ingestion Freshness",
    description: str = "Measures freshness of ingestion",
    theme: str = "freshness",
    measurement_query: dict | None = None,
    schedule_tier: str | None = None,
    is_enabled: bool = True,
):
    row = MagicMock()
    row.id = metric_id
    row.title = title
    row.description = description
    row.theme = theme
    row.measurement_query = measurement_query or {"aggregation": "ingestion-freshness"}
    row.schedule_tier = schedule_tier
    row.is_enabled = is_enabled
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    metric_id: str = "ingestion-freshness",
    value: float = 0.8,
    breakdown: dict | None = None,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.metric_id = metric_id
    row.value = value
    row.breakdown = breakdown or {
        "dataset_count": 10,
        "datasets": [
            {"urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t1,PROD)", "category": "fresh", "detail": {}},
        ],
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
    rows = [_make_definition_row(theme="freshness")]
    mock_paginated_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(theme_filter="freshness")
    assert total == 1
    assert len(metrics) == 1
    assert metrics[0].theme == "freshness"


async def test_list_metrics_with_is_enabled_filter(service, db):
    """is_enabled (not is_active) is the field name."""
    rows = [_make_definition_row(is_enabled=True)]
    mock_paginated_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(is_enabled_filter=True)
    assert total == 1
    assert metrics[0].is_enabled is True


# ── get_metric ────────────────────────────────────────────────────────────────


async def test_get_metric_found(service, db):
    row = _make_definition_row(title="Ingestion Freshness")
    mock_scalar_query(db, row)

    metric = await service.get_metric(row.id)
    assert metric.title == "Ingestion Freshness"
    assert metric.id == row.id


async def test_get_metric_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_metric("nonexistent")
    assert "NOT_FOUND" in exc_info.value.error_code


# ── get_metric_attr ──────────────────────────────────────────────────────────


async def test_get_metric_attr_with_latest_result(service, db):
    def_row = _make_definition_row()
    result_row = _make_result_row(value=0.85)

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = result_row

    db.execute = AsyncMock(side_effect=[def_result, latest_result])

    attr = await service.get_metric_attr(def_row.id)
    assert attr["title"] == def_row.title
    assert attr["latest_value"] == 0.85


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
        metric_id="ingestion-freshness",
        title="Ingestion Freshness",
        description="desc",
        theme="freshness",
        measurement_query={"aggregation": "ingestion-freshness"},
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
        measurement_query={"aggregation": "ingestion-freshness"},
    )
    assert existing.title == "Updated"
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

    results, total = await service.get_results("ingestion-freshness", offset=0, limit=2)
    assert total == 10
    assert len(results) == 2


# ── Unified breakdown shape ───────────────────────────────────────────────────


def test_metric_breakdown_row_shape():
    """make_metric_breakdown_row returns unified {dataset_count, datasets:[...]} shape."""
    row = make_metric_breakdown_row()
    breakdown = row.breakdown
    assert "dataset_count" in breakdown
    assert "datasets" in breakdown
    assert isinstance(breakdown["datasets"], list)
    if breakdown["datasets"]:
        entry = breakdown["datasets"][0]
        assert "urn" in entry
        assert "category" in entry


def test_metric_breakdown_row_custom():
    """Custom breakdown values are reflected accurately."""
    custom = {
        "dataset_count": 3,
        "datasets": [
            {"urn": "urn:1", "category": "rules_passing", "detail": {"rule_id": "r1", "failed": 0, "total": 4}},
            {"urn": "urn:2", "category": "rules_failing", "detail": {"rule_id": "r2", "failed": 1, "total": 4}},
        ],
    }
    row = make_metric_breakdown_row(breakdown=custom)
    assert row.breakdown["dataset_count"] == 3
    assert len(row.breakdown["datasets"]) == 2
    assert row.breakdown["datasets"][0]["category"] == "rules_passing"
    assert row.breakdown["datasets"][1]["category"] == "rules_failing"


# ── run (baseline measurers only) ─────────────────────────────────────────────


async def test_run_ingestion_freshness_persists(service, db, datahub):
    """ingestion-freshness run: measure + persist MetricResult + record event."""
    def_row = _make_definition_row(
        metric_id="ingestion-freshness",
        measurement_query={"aggregation": "ingestion-freshness"},
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1", "urn:li:dataset:2"])
    # No events for either dataset → stale
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result, event_result])
    db.refresh = AsyncMock()

    result = await service.run(def_row.id)
    assert result.status == "success"


async def test_run_dry_run_does_not_persist(service, db, datahub):
    """dry_run=True: measure but do not call db.add for MetricResult."""
    def_row = _make_definition_row(
        measurement_query={"aggregation": "ingestion-freshness"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1"])
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result])

    result = await service.run(def_row.id, dry_run=True)

    assert result.status == "success"
    assert result.detail["dry_run"] is True
    # No commit for dry run
    assert db.commit.await_count == 0


async def test_breakdown_field_names_unified(service, db, datahub):
    """Breakdown must use 'dataset_count' and 'datasets' keys."""
    def_row = _make_definition_row(
        measurement_query={"aggregation": "ingestion-freshness"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # No recent events → stale for both
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result, event_result])
    db.refresh = AsyncMock()

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1", "urn:2"])

    result = await service.run(def_row.id)
    assert result.status == "success"

    # Check the persisted breakdown via db.add call
    add_call_args = db.add.call_args_list
    metric_result_arg = None
    for call in add_call_args:
        obj = call[0][0]
        if hasattr(obj, "breakdown") and hasattr(obj, "metric_id"):
            metric_result_arg = obj
            break

    if metric_result_arg is not None:
        breakdown = metric_result_arg.breakdown
        assert "dataset_count" in breakdown
        assert "datasets" in breakdown
        assert "metric_type" not in breakdown


async def test_unknown_metric_type_raises(service, db, datahub):
    """Unknown aggregation key in measurement_query raises PreconditionFailedError."""
    def_row = _make_definition_row(
        measurement_query={"aggregation": "dataset_count"}
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    db.execute = AsyncMock(return_value=def_result)
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:1"])

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.run(def_row.id, dry_run=True)
    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_dataset_filter_passthrough(service, db, datahub):
    """tags/glossary_terms from measurement_query.dataset_filter are forwarded to enumerate_datasets."""
    def_row = _make_definition_row(
        measurement_query={
            "aggregation": "ingestion-freshness",
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


# ── No activate / deactivate methods ─────────────────────────────────────────


def test_no_activate_method(service):
    """MetricsService should not have activate() — removed in spec rewrite."""
    assert not hasattr(service, "activate"), (
        "MetricsService.activate() was removed in the spec rewrite; "
        "use PUT/PATCH is_enabled instead."
    )


def test_no_deactivate_method(service):
    """MetricsService should not have deactivate() — removed in spec rewrite."""
    assert not hasattr(service, "deactivate"), (
        "MetricsService.deactivate() was removed in the spec rewrite; "
        "use PUT/PATCH is_enabled instead."
    )


# ── events ──────────────────────────────────────────────────────────────────


async def test_get_events(service, db):
    metric_id = "ingestion-freshness"
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
    metric_id = "ingestion-freshness"
    mock_paginated_query(db, [], total_count=0)

    now = datetime.now(tz=UTC)
    events, total = await service.get_events(
        metric_id,
        from_dt=now - timedelta(hours=1),
        to_dt=now,
    )
    assert total == 0
    assert events == []
