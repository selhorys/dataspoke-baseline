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
    """ingestion-freshness run: measure + persist MetricResult + record event.

    Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline) — non-dry-run
    must call db.add at least twice (MetricResult + Event) and commit at least once.
    """
    def_row = _make_definition_row(
        metric_id="ingestion-freshness",
        measurement_query={"aggregation": "ingestion-freshness"},
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    datahub.enumerate_datasets = AsyncMock(return_value=["urn:li:dataset:1", "urn:li:dataset:2"])
    # No events for either dataset → stale
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result, event_result])
    db.refresh = AsyncMock()

    result = await service.run(def_row.id)
    assert result.status == "success"

    # db.add must be called at least twice: MetricResult + Event
    # Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline)
    assert db.add.call_count >= 2, (
        f"Expected db.add called >= 2 times (MetricResult + Event), got {db.add.call_count}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline)."
    )
    # db.commit must be awaited at least once to persist the result
    assert db.commit.await_count >= 1, (
        f"Expected db.commit awaited >= 1 time, got {db.commit.await_count}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline)."
    )


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
    """Breakdown must use 'dataset_count' and 'datasets' keys (not 'metric_type').

    Spec: spec/feature/BACKEND.md §Metrics Service L457-L459 — every measurement
    result includes a breakdown JSONB with unified per-dataset entry shape
    {"dataset_count": <int>, "datasets": [{"urn": "...", "category": "...", "detail": {...}}]}.
    """
    _BREAKDOWN_COUNT_KEY = "dataset_count"
    _BREAKDOWN_DATASETS_KEY = "datasets"
    _BREAKDOWN_FORBIDDEN_KEY = "metric_type"

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

    # Locate the persisted MetricResult object from db.add call_args_list.
    # The guard is an unconditional assertion (not `if`) so missing the object
    # is a test failure rather than a silent pass.
    metric_result_arg = None
    for call in db.add.call_args_list:
        obj = call[0][0]
        if hasattr(obj, "breakdown") and hasattr(obj, "metric_id"):
            metric_result_arg = obj
            break

    assert metric_result_arg is not None, (
        "db.add was not called with a MetricResult object — service may not be persisting "
        "the run result. Spec: BACKEND.md §Metrics Service (run pipeline)."
    )

    breakdown = metric_result_arg.breakdown
    assert _BREAKDOWN_COUNT_KEY in breakdown, (
        f"breakdown missing '{_BREAKDOWN_COUNT_KEY}' key. "
        "Spec: spec/feature/BACKEND.md §Metrics Service L457-L459."
    )
    assert _BREAKDOWN_DATASETS_KEY in breakdown, (
        f"breakdown missing '{_BREAKDOWN_DATASETS_KEY}' key. "
        "Spec: spec/feature/BACKEND.md §Metrics Service L457-L459."
    )
    assert _BREAKDOWN_FORBIDDEN_KEY not in breakdown, (
        f"breakdown must not contain legacy '{_BREAKDOWN_FORBIDDEN_KEY}' key. "
        "Spec: spec/feature/BACKEND.md §Metrics Service L457-L459."
    )


async def test_unknown_metric_type_raises(service, db, datahub):
    """Unknown aggregation key in measurement_query raises PreconditionFailedError.

    Spec: spec/feature/BACKEND.md §Metrics Service — unsupported aggregations
    return 422 INVALID_PARAMETER.

    NOTE (F8): Uses a clearly invalid string to decouple from F1 (aggregation enum
    mismatch). 'dataset_count' was previously used but that value conflicts with
    potential valid enum entries; 'definitely-not-a-real-aggregation' is always wrong.
    """
    def_row = _make_definition_row(
        measurement_query={"aggregation": "definitely-not-a-real-aggregation"}
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


async def test_dataset_filter_passthrough_dataset_urns(service, db, datahub):
    """dataset_filter.dataset_urns are resolved individually via get_aspect, not enumerate_datasets.

    Spec: spec/feature/BACKEND.md §Metrics Service — dataset_filter with dataset_urns
    (list of explicit urn:li:dataset:(…) URNs for pinning to a known set).
    Entries that resolve → included in measurement; entries that don't → unresolved_urns.
    """
    _PINNED_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

    def_row = _make_definition_row(
        measurement_query={
            "aggregation": "ingestion-freshness",
            "dataset_filter": {"dataset_urns": [_PINNED_URN]},
        }
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # Resolve the explicit URN: get_aspect returns a non-None props object
    props_mock = MagicMock()
    datahub.get_aspect = AsyncMock(return_value=props_mock)

    # The event query for the resolved URN → no event → stale
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result])
    db.refresh = AsyncMock()

    result = await service.run(def_row.id, dry_run=True)

    assert result.status == "success"
    # enumerate_datasets must NOT be called — explicit URNs bypass enumeration
    datahub.enumerate_datasets.assert_not_awaited()
    # get_aspect must be called for the pinned URN to resolve it
    datahub.get_aspect.assert_awaited_once()
    call_args = datahub.get_aspect.call_args[0]
    assert call_args[0] == _PINNED_URN, (
        "get_aspect should be called with the pinned URN. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — dataset_filter.dataset_urns."
    )
    # No unresolved URNs since the URN resolved
    assert result.detail["unresolved_urns"] == []


async def test_dataset_filter_empty_returns_all(service, db, datahub):
    """dataset_filter={} means all datasets — enumerate_datasets called with no filter args.

    Spec: spec/feature/BACKEND.md §Metrics Service — '{}' means all datasets;
    an empty array on any dimension contributes nothing.
    """
    def_row = _make_definition_row(
        measurement_query={
            "aggregation": "ingestion-freshness",
            "dataset_filter": {},
        }
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # enumerate_datasets returns all datasets (no filter)
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:all-1", "urn:all-2"])
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, event_result, event_result])
    db.refresh = AsyncMock()

    result = await service.run(def_row.id, dry_run=True)

    assert result.status == "success"
    # enumerate_datasets called once with no keyword filter arguments
    datahub.enumerate_datasets.assert_awaited_once_with()


async def test_dataset_filter_or_semantics(service, db, datahub):
    """tags + glossary_terms + dataset_urns are OR-ed: all three dimensions contribute datasets.

    Spec: spec/feature/BACKEND.md §Metrics Service L447-L451 — 'only datasets matching ANY
    listed tag, glossary term, or explicit URN are included — filters are OR-ed
    across all three dimensions'.

    # Note: Mock returns both URNs unconditionally; AND vs OR distinction at the
    # enumerate_datasets boundary not testable at unit level. Coverage gap surfaced
    # for api-wired integration.
    """
    _TAG_URN = "urn:li:tag:PII"
    _TERM_URN = "urn:li:glossaryTerm:CustomerData"
    _EXPLICIT_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.raw_events,DEV)"

    def_row = _make_definition_row(
        measurement_query={
            "aggregation": "ingestion-freshness",
            "dataset_filter": {
                "tags": [_TAG_URN],
                "glossary_terms": [_TERM_URN],
                "dataset_urns": [_EXPLICIT_URN],
            },
        }
    )

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    # Tags+glossary_terms path: enumerate_datasets returns two distinct URNs
    _TAG_MATCHED = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)"
    _TERM_MATCHED = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    datahub.enumerate_datasets = AsyncMock(return_value=[_TAG_MATCHED, _TERM_MATCHED])

    # Explicit URN path: get_aspect resolves successfully
    props_mock = MagicMock()
    datahub.get_aspect = AsyncMock(return_value=props_mock)

    # One event query per resolved URN (3 total: TAG_MATCHED, TERM_MATCHED, EXPLICIT_URN)
    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(
        side_effect=[def_result, event_result, event_result, event_result]
    )
    db.refresh = AsyncMock()

    # Use dry_run=False so MetricResult is passed to db.add and we can inspect breakdown.datasets
    result = await service.run(def_row.id, dry_run=False)

    assert result.status == "success"
    # enumerate_datasets called once for tags+glossary_terms combined
    datahub.enumerate_datasets.assert_awaited_once_with(
        tags=[_TAG_URN],
        glossary_terms=[_TERM_URN],
    )
    # get_aspect called once for the explicit URN
    datahub.get_aspect.assert_awaited_once()

    # The union of TAG_MATCHED + TERM_MATCHED + EXPLICIT_URN = 3 distinct URNs
    # Verify via breakdown_summary.dataset_count (OR semantics, no duplicates)
    # Spec: spec/feature/BACKEND.md §Metrics Service L447-L451
    assert result.detail["breakdown_summary"]["dataset_count"] == 3, (
        "Expected exactly 3 datasets (TAG_MATCHED + TERM_MATCHED + EXPLICIT_URN). "
        "Spec: BACKEND.md §Metrics Service L447-L451 — OR-ed across all three dimensions."
    )

    # Inspect the MetricResult object passed to db.add and verify all 3 URNs
    # appear in breakdown.datasets — confirms set union is written to the DB row.
    # Spec: spec/feature/BACKEND.md §Metrics Service L454-L458 — breakdown shape.
    metric_result_arg = None
    for call in db.add.call_args_list:
        obj = call[0][0]
        if hasattr(obj, "breakdown") and hasattr(obj, "metric_id"):
            metric_result_arg = obj
            break

    assert metric_result_arg is not None, (
        "db.add was not called with a MetricResult object. "
        "Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline)."
    )
    persisted_urns = {entry["urn"] for entry in metric_result_arg.breakdown.get("datasets", [])}
    assert _TAG_MATCHED in persisted_urns, (
        f"TAG_MATCHED URN '{_TAG_MATCHED}' missing from persisted breakdown.datasets. "
        "Spec: BACKEND.md §Metrics Service L447-L451."
    )
    assert _TERM_MATCHED in persisted_urns, (
        f"TERM_MATCHED URN '{_TERM_MATCHED}' missing from persisted breakdown.datasets. "
        "Spec: BACKEND.md §Metrics Service L447-L451."
    )
    assert _EXPLICIT_URN in persisted_urns, (
        f"EXPLICIT_URN '{_EXPLICIT_URN}' missing from persisted breakdown.datasets. "
        "Spec: BACKEND.md §Metrics Service L447-L451."
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
