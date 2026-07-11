"""Unit tests for MetricsService — create vs replace, CRUD, run pipeline.

Spec sources:
  spec/USE_CASE_en.md §UC5 — Governance (metric definition fields, factory defaults,
    passive mode 501 lives at the route, not the service layer)
  spec/feature/BACKEND.md §Metrics Service (service contracts, create vs replace,
    breakdown, disabled guard)
  spec/feature/BACKEND_SCHEMA.md §metric_definitions, §metric_results
  spec/API.md §Metric (/spoke/governance/metric) — NOT_IMPLEMENTED lives at the route layer
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.service import MetricsService
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from tests.unit.backend.conftest import (
    make_event_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)

# ── Row factories ─────────────────────────────────────────────────────────────


def _make_definition_row(
    metric_id: str = "ingestion-freshness",
    mode: str = "active",
    metric_type: str = "ingestion-freshness",
    title: str = "Ingestion Freshness",
    description: str = "Measures freshness of ingestion",
    metrics: list | None = None,
    metric_conf: dict | None = None,
    dataset_filter: dict | None = None,
    schedule_tier: str | None = "daily",
    is_enabled: bool = True,
) -> MagicMock:
    row = MagicMock()
    row.id = metric_id
    row.mode = mode
    row.metric_type = metric_type
    row.title = title
    row.description = description
    row.metrics = metrics if metrics is not None else ["total", "ingested_in_time"]
    row.metric_conf = metric_conf if metric_conf is not None else {"time_window_sec": 172800}
    row.dataset_filter = dataset_filter if dataset_filter is not None else {}
    row.schedule_tier = schedule_tier
    row.is_enabled = is_enabled
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    metric_id: str = "ingestion-freshness",
    values: dict | None = None,
    breakdown: dict | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.metric_id = metric_id
    row.values = values if values is not None else {"total": 10.0, "ingested_in_time": 8.0}
    row.breakdown = breakdown if breakdown is not None else {
        "dataset_count": 10,
        "datasets": [
            {"urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t1,PROD)", "detail": {}},
        ],
    }
    row.measured_at = datetime.now(tz=UTC)
    return row


def _mock_list_metrics_query(
    db: MagicMock,
    rows: list,
    total_count: int,
    last_run_by_id: dict | None = None,
) -> None:
    """Mock the THREE db.execute calls list_metrics issues.

    list_metrics runs: (1) count, (2) page rows, and (3) the page-bounded
    last-run batch — a grouped ``MAX(occurred_at)`` over ``METRIC.RUN_COMPLETE``
    events whose ``entity_id`` is in the page, returned as ``(entity_id,
    occurred_at)`` pairs via ``result.all()``.

    Spec: spec/feature/BACKEND.md §Metrics Service — List last_run_at (page-bounded
          batch over METRIC.RUN_COMPLETE events).
    """
    count_result = MagicMock()
    count_result.scalar.return_value = total_count
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    lr_result = MagicMock()
    lr_result.all.return_value = list((last_run_by_id or {}).items())
    db.execute = AsyncMock(side_effect=[count_result, rows_result, lr_result])


def _emitted_query(db: MagicMock, call_index: int) -> tuple[str, set]:
    """Compile the SQLAlchemy statement passed to the ``call_index``-th db.execute call.

    Returns ``(sql_text, bound_param_values)``. A mocked ``db.execute`` returns a
    canned row list regardless of the query, so seeding both matching and
    non-matching rows cannot exercise a WHERE at the unit layer. Instead, filter
    tests assert the filter predicate reached the query the service *built* (column
    name in the SQL text, filter value among the bound params) — a regression that
    drops the ``.where(...)`` is then caught.
    """
    stmt = db.execute.call_args_list[call_index].args[0]
    compiled = stmt.compile()
    return str(compiled), set(compiled.params.values())


@pytest.fixture
def service(datahub, db, cache):
    return MetricsService(datahub=datahub, db=db, cache=cache)


# ── create_metric_config ──────────────────────────────────────────────────────


async def test_create_metric_config_inserts_new_row(service, db):
    """create_metric_config inserts a new row; returns MetricDefinitionRecord.

    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          POST /spoke/governance/metric creates a metric; metric_id supplied in the body.
    """
    mock_scalar_query(db, None)  # no existing row
    mock_db_refresh(db)

    await service.create_metric_config(
        metric_id="ingestion-freshness",
        mode="active",
        metric_type="ingestion-freshness",
        title="Ingestion Freshness",
        description="Measures freshness",
        metrics=["total", "ingested_in_time"],
        metric_conf={"time_window_sec": 172800},
        dataset_filter={},
        schedule_tier="daily",
        is_enabled=False,
    )
    assert db.add.called
    assert db.commit.await_count >= 1


async def test_create_metric_config_raises_conflict_on_duplicate(service, db):
    """create_metric_config raises ConflictError('METRIC_EXISTS') when id already exists.

    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          409 METRIC_EXISTS when colliding id is supplied.
    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — colliding id returns 409 METRIC_EXISTS.
    """
    existing = _make_definition_row()
    mock_scalar_query(db, existing)

    with pytest.raises(ConflictError) as exc_info:
        await service.create_metric_config(
            metric_id="ingestion-freshness",
            mode="active",
            metric_type="ingestion-freshness",
            title="Dup",
            description="Dup",
            metrics=["total"],
            metric_conf={"time_window_sec": 172800},
            dataset_filter={},
        )

    assert exc_info.value.error_code == "METRIC_EXISTS", (
        "Duplicate metric_id must raise ConflictError with error_code='METRIC_EXISTS'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace."
    )


async def test_create_metric_config_doc_health_empty_metric_conf(service, db):
    """create_metric_config accepts doc-health with empty metric_conf.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — doc-health
          metric_conf is empty {}.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    await service.create_metric_config(
        metric_id="doc-health",
        mode="active",
        metric_type="doc-health",
        title="Doc Health",
        description="Documentation coverage",
        metrics=["total", "doc_health"],
        metric_conf={},
        dataset_filter={},
        schedule_tier="daily",
        is_enabled=False,
    )
    assert db.add.called


# ── replace_metric_config ─────────────────────────────────────────────────────


async def test_replace_metric_config_overwrites_all_fields(service, db):
    """replace_metric_config replaces every field on the existing row.

    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          PUT .../attr/conf replaces an existing definition only.
    Spec: spec/API.md §Metric — PUT is replace-only, not create-or-upsert.
    """
    existing = _make_definition_row(
        metric_id="ingestion-freshness",
        mode="active",
        metric_type="ingestion-freshness",
        metrics=["total"],
        metric_conf={"time_window_sec": 3600},
        dataset_filter={},
    )
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    await service.replace_metric_config(
        metric_id="ingestion-freshness",
        mode="active",
        metric_type="ingestion-freshness",
        title="Updated Title",
        description="Updated desc",
        metrics=["total", "ingested_in_time"],
        metric_conf={"time_window_sec": 172800},
        dataset_filter={"origin": "PROD"},
        schedule_tier="weekly",
        is_enabled=True,
    )
    assert existing.title == "Updated Title"
    assert existing.metrics == ["total", "ingested_in_time"]
    assert existing.metric_conf == {"time_window_sec": 172800}
    assert existing.dataset_filter == {"origin": "PROD"}
    assert existing.schedule_tier == "weekly"
    assert existing.is_enabled is True
    assert db.commit.await_count >= 1


async def test_replace_metric_config_raises_not_found_when_absent(service, db):
    """replace_metric_config raises EntityNotFoundError when the metric does not exist.

    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          replace_metric_config raises 404 METRIC_NOT_FOUND when absent.
    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — PUT returns 404 METRIC_NOT_FOUND
          when the id is absent.
    """
    mock_scalar_query(db, None)  # no row found

    # replace_metric_config must raise EntityNotFoundError for an absent metric_id.
    # Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace.
    with pytest.raises(EntityNotFoundError):
        await service.replace_metric_config(
            metric_id="nonexistent",
            mode="active",
            metric_type="doc-health",
            title="t",
            description="d",
            metrics=["total", "doc_health"],
            metric_conf={},
            dataset_filter={},
        )


# ── patch_metric_config ───────────────────────────────────────────────────────


async def test_patch_metric_config_is_enabled_only_does_not_touch_other_fields(service, db):
    """PATCH that sets only is_enabled does not touch other fields.

    Spec: spec/API.md §Metric — PATCH updates metric definition fields (partial update).
    """
    row = _make_definition_row(
        metric_conf={"time_window_sec": 172800},
        metrics=["total", "ingested_in_time"],
        dataset_filter={"origin": "PROD"},
    )
    original_metrics = row.metrics[:]
    original_conf = dict(row.metric_conf)
    original_filter = dict(row.dataset_filter)

    mock_scalar_query(db, row)
    mock_db_refresh(db)

    await service.patch_metric_config(row.id, {"is_enabled": False})

    assert row.is_enabled is False
    assert row.metrics == original_metrics
    assert row.metric_conf == original_conf
    assert row.dataset_filter == original_filter


async def test_patch_metric_type_to_doc_health_with_time_window_raises(service, db):
    """PATCH metric_type='doc-health' with time_window_sec in conf raises PreconditionFailedError.

    Spec: spec/feature/BACKEND.md §Metrics Service — PATCH enforces cross-field invariants
          on merged state; doc-health requires metric_conf={}.
    """
    row = _make_definition_row(
        metric_type="ingestion-freshness",
        metric_conf={"time_window_sec": 172800},
        metrics=["total", "ingested_in_time"],
    )
    mock_scalar_query(db, row)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.patch_metric_config(row.id, {"metric_type": "doc-health"})

    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_patch_metric_conf_invalid_for_windowed_type_raises(service, db):
    """PATCH metric_conf without time_window_sec for ingestion-freshness raises.

    Spec: spec/feature/BACKEND.md §Metrics Service — ingestion-freshness and
          validation-score require time_window_sec (positive int).
    """
    row = _make_definition_row(
        metric_type="ingestion-freshness",
        metric_conf={"time_window_sec": 172800},
        metrics=["total", "ingested_in_time"],
    )
    mock_scalar_query(db, row)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.patch_metric_config(row.id, {"metric_conf": {"time_window_sec": -1}})

    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_patch_metrics_with_unknown_key_raises(service, db):
    """PATCH metrics[] with a key not emitted by the type raises PreconditionFailedError.

    Spec: spec/feature/BACKEND.md §Metrics Service — unknown metrics[] keys return
          422 INVALID_PARAMETER.
    """
    row = _make_definition_row(
        metric_type="ingestion-freshness",
        metric_conf={"time_window_sec": 172800},
        metrics=["total", "ingested_in_time"],
    )
    mock_scalar_query(db, row)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.patch_metric_config(row.id, {"metrics": ["nonexistent_key"]})

    assert exc_info.value.error_code == "INVALID_PARAMETER"


# ── list_metrics ──────────────────────────────────────────────────────────────


async def test_list_metrics_filter_by_metric_type(service, db):
    """list_metrics(metric_type_filter=...) filters by metric_type.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by metric_type.
    """
    rows = [_make_definition_row(metric_type="doc-health")]
    _mock_list_metrics_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(metric_type_filter="doc-health")
    assert total == 1
    assert len(metrics) == 1
    assert metrics[0].metric_type == "doc-health"
    # Backstop: the mock returns the row regardless of the WHERE, so also assert the
    # service actually applied a metric_type predicate to the emitted page query.
    sql, bound = _emitted_query(db, 1)
    assert "metric_type" in sql and "doc-health" in bound, (
        "list_metrics(metric_type_filter=...) must add a WHERE metric_type = filter "
        f"predicate; emitted SQL={sql!r} bound={bound!r}. "
        "spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by metric_type"
    )


async def test_list_metrics_filter_by_mode(service, db):
    """list_metrics(mode_filter=...) filters by mode.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by mode.
    """
    rows = [_make_definition_row(mode="active")]
    _mock_list_metrics_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(mode_filter="active")
    assert total == 1
    assert metrics[0].mode == "active"
    # Backstop: the mock ignores the WHERE, so assert the service applied a mode
    # predicate to the emitted page query.
    sql, bound = _emitted_query(db, 1)
    assert "mode" in sql and "active" in bound, (
        "list_metrics(mode_filter=...) must add a WHERE mode = filter predicate; "
        f"emitted SQL={sql!r} bound={bound!r}. "
        "spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by mode"
    )


async def test_list_metrics_filter_by_is_enabled(service, db):
    """list_metrics(is_enabled_filter=True) filters by is_enabled.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by is_enabled.
    """
    rows = [_make_definition_row(is_enabled=True)]
    _mock_list_metrics_query(db, rows, total_count=1)

    metrics, total = await service.list_metrics(is_enabled_filter=True)
    assert total == 1
    assert metrics[0].is_enabled is True
    # Backstop: the mock ignores the WHERE, so assert the service applied an
    # is_enabled predicate to the emitted page query. A boolean comparison renders
    # inline (is_enabled = true), so match the SQL text rather than a bound param.
    sql, _bound = _emitted_query(db, 1)
    assert "is_enabled = true" in sql.lower(), (
        "list_metrics(is_enabled_filter=True) must add a WHERE is_enabled = true filter "
        f"predicate; emitted SQL={sql!r}. "
        "spec: spec/API.md §Metric — GET /spoke/governance/metric filterable by is_enabled"
    )


async def test_list_metrics_pagination_returns_tuple(service, db):
    """list_metrics returns (rows, total_count) tuple.

    Spec: spec/API.md §Standard Response Envelope — paginated resources return
          offset, limit, total_count.
    """
    rows = [_make_definition_row(metric_id=f"m{i}") for i in range(3)]
    _mock_list_metrics_query(db, rows, total_count=10)

    metrics, total = await service.list_metrics(offset=0, limit=3)
    assert total == 10
    assert len(metrics) == 3


async def test_list_metrics_rows_carry_last_run_at(service, db):
    """List rows carry last_run_at = the latest METRIC.RUN_COMPLETE occurred_at;
    a metric with no completed run carries last_run_at=None.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric — each row carries
          last_run_at (occurred_at of the latest METRIC.RUN_COMPLETE event, null
          when the metric has never completed a run).
    Spec: spec/feature/BACKEND.md §Metrics Service — List last_run_at derivation.
    """
    ran = _make_definition_row(metric_id="has-run")
    never = _make_definition_row(metric_id="never-run")
    last_run = datetime.now(tz=UTC) - timedelta(hours=2)
    # The page-bounded batch returns a row only for the metric that has completed
    # a run; the metric absent from the batch must surface last_run_at=None.
    _mock_list_metrics_query(
        db,
        [ran, never],
        total_count=2,
        last_run_by_id={"has-run": last_run},
    )

    metrics, total = await service.list_metrics(offset=0, limit=20)
    assert total == 2
    by_id = {m.id: m for m in metrics}
    assert by_id["has-run"].last_run_at == last_run, (
        "last_run_at must equal the latest METRIC.RUN_COMPLETE occurred_at. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — List last_run_at."
    )
    assert by_id["never-run"].last_run_at is None, (
        "a metric with no completed run must carry last_run_at=None. "
        "Spec: spec/API.md §Metric — last_run_at null when never run."
    )


# ── get_metric_attr ───────────────────────────────────────────────────────────


async def test_get_metric_attr_returns_latest_values_dict(service, db):
    """get_metric_attr returns latest_values as dict (not a float).

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_results — values is JSONB (dict[str, float]).
    Spec: spec/API.md §Metric — attr endpoint exposes latest_values dict.
    """
    def_row = _make_definition_row()
    result_row = _make_result_row(values={"total": 5.0, "ingested_in_time": 3.0})

    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row
    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = result_row

    db.execute = AsyncMock(side_effect=[def_result, latest_result])

    attr = await service.get_metric_attr(def_row.id)

    assert attr["latest_values"] == {"total": 5.0, "ingested_in_time": 3.0}
    assert isinstance(attr["latest_values"], dict)
    assert "latest_measured_at" in attr


async def test_get_metric_attr_no_results_returns_none_values(service, db):
    """get_metric_attr returns latest_values=None when no result rows exist.

    Spec: spec/API.md §Metric — latest_values is null when no measurements yet.
    """
    def_row = _make_definition_row()
    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row
    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[def_result, latest_result])

    attr = await service.get_metric_attr(def_row.id)
    assert attr["latest_values"] is None
    assert attr["latest_measured_at"] is None


# ── delete_metric_config ──────────────────────────────────────────────────────


async def test_delete_metric_config_cascades_to_metric_results(service, db):
    """delete_metric_config cascades DELETE to metric_results rows before removing the definition.

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_results — metric_id is FK to metric_definitions.
    Spec: spec/feature/BACKEND.md §Metrics Service — delete_metric_config removes
          associated results.
    """
    row = _make_definition_row()
    mock_scalar_query(db, row)

    await service.delete_metric_config(row.id)

    # db.execute must be called for the DELETE (cascade) AND the initial SELECT
    assert db.execute.await_count >= 2
    # db.delete should be called with the definition row
    db.delete.assert_called_once_with(row)
    assert db.commit.await_count >= 1


async def test_delete_metric_config_not_found(service, db):
    """delete_metric_config raises EntityNotFoundError for missing metric.

    Spec: spec/API.md §Error Catalogue — 404 NOT_FOUND when resource absent.
    """
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.delete_metric_config("nonexistent")


# ── _validate_dataset_filter ─────────────────────────────────────────────────


async def test_create_metric_config_dataset_filter_over_cap_raises(service, db):
    """create_metric_config with dataset_filter 1001-entry list raises PreconditionFailedError.

    Spec: spec/API.md §Governance — Payload caps (≤ 1,000 entries per dimension).
    Spec: spec/API.md §Metric — 422 INVALID_PARAMETER for over-cap dimensions.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    over_cap_urns = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
        for i in range(1001)
    ]

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="ingestion-freshness",
            title="t",
            description="d",
            metrics=["total"],
            metric_conf={"time_window_sec": 172800},
            dataset_filter={"dataset_urns": over_cap_urns},
        )
    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_create_metric_config_dataset_filter_bad_urn_raises(service, db):
    """create_metric_config with malformed URN in dataset_filter raises InvalidDatasetUrnError.

    Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(InvalidDatasetUrnError):
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="ingestion-freshness",
            title="t",
            description="d",
            metrics=["total"],
            metric_conf={"time_window_sec": 172800},
            dataset_filter={"dataset_urns": ["not-a-urn"]},
        )


# ── passive mode — service layer accepts it ───────────────────────────────────


async def test_create_metric_config_passive_mode_accepted_by_service(service, db):
    """Service layer accepts mode='passive'; the 501 NOT_IMPLEMENTED is raised at the route.

    Spec: spec/API.md §Metric — 'passive' is reserved; POST with mode:'passive' returns
          501 NOT_IMPLEMENTED. This is enforced at the route handler, NOT the service.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    # Should not raise — the route layer's NotImplementedAPIError is absent at the service
    definition = await service.create_metric_config(
        metric_id="passive-test",
        mode="passive",
        metric_type="ingestion-freshness",
        title="Passive metric",
        description="Reserved",
        metrics=["total"],
        metric_conf={"time_window_sec": 172800},
        dataset_filter={},
    )
    assert definition.mode == "passive"


# ── list_active_for_tier ──────────────────────────────────────────────────────


async def test_list_active_for_tier_filters_enabled_and_tier(service, db):
    """list_active_for_tier returns only is_enabled=True rows matching schedule_tier.

    Spec: spec/feature/BACKEND.md §Metrics Service — Airflow tier DAGs call
          list_active_for_tier(tier) to enumerate metrics to run.
    """
    rows = [
        _make_definition_row(metric_id="m1", is_enabled=True, schedule_tier="daily"),
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result_mock)

    active = await service.list_active_for_tier("daily")

    assert len(active) == 1
    assert active[0].id == "m1"
    assert active[0].is_enabled is True
    assert active[0].schedule_tier == "daily"
    # Backstop: the mock returns the row regardless of the WHERE, so assert the
    # service constrained the query to BOTH is_enabled=True AND the requested tier —
    # otherwise a dropped predicate would run disabled or wrong-tier metrics.
    sql, bound = _emitted_query(db, 0)
    assert "is_enabled" in sql and "schedule_tier" in sql, (
        "list_active_for_tier must filter on both is_enabled and schedule_tier; "
        f"emitted SQL={sql!r}. "
        "spec: spec/feature/BACKEND.md §Metrics Service — tier DAGs enumerate enabled "
        "metrics for a tier"
    )
    assert "daily" in bound, (
        "list_active_for_tier('daily') must bind the requested tier into the WHERE; "
        f"bound={bound!r}. spec: spec/feature/BACKEND.md §Metrics Service"
    )


# ── disabled-config rejection ─────────────────────────────────────────────────


async def test_run_rejects_non_dry_run_when_disabled(service, db, datahub):
    """Non-dry-run against a disabled metric raises ConflictError('METRIC_DISABLED').

    Spec: spec/feature/BACKEND.md §Metrics Service — is_enabled=false rejects
          non-dry-run with 409 METRIC_DISABLED. Dry-run is always permitted.
    """
    def_row = _make_definition_row(is_enabled=False)
    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row
    db.execute = AsyncMock(return_value=def_result)

    with pytest.raises(ConflictError) as exc_info:
        await service.run("ingestion-freshness", dry_run=False)

    assert exc_info.value.error_code == "METRIC_DISABLED"


async def test_run_allows_dry_run_when_disabled(service, db, datahub):
    """Dry-run bypasses the disabled guard.

    Spec: spec/feature/BACKEND.md §Metrics Service — dry_run=True is always
          permitted regardless of is_enabled.
    """
    def_row = _make_definition_row(
        is_enabled=False,
        metric_type="ingestion-freshness",
        metric_conf={"time_window_sec": 172800},
        metrics=["total", "ingested_in_time"],
    )
    def_result = MagicMock()
    def_result.scalar_one_or_none.return_value = def_row

    datahub.enumerate_datasets = AsyncMock(return_value=[])
    # Empty dataset list: the first db.execute call returns the metric definition.
    # Subsequent calls (measurer internals) return an empty result regardless of count.
    # Using a callable side_effect so the mock tolerates any number of internal queries
    # without coupling the test to the measurer's exact SQL call count.
    empty_result = MagicMock()
    empty_result.all.return_value = []
    empty_result.scalar_one_or_none.return_value = None

    call_count = 0

    async def _execute_side_effect(stmt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return def_result
        return empty_result

    db.execute = AsyncMock(side_effect=_execute_side_effect)

    result = await service.run("ingestion-freshness", dry_run=True)

    assert result.status == "success"
    assert result.detail["dry_run"] is True
    assert db.commit.await_count == 0


# ── MetricDefinitionRecord field set ─────────────────────────────────────────


async def test_definition_record_field_set_matches_spec(service, db):
    """MetricDefinitionRecord exposes the metric_definitions field set plus the
    derived ``last_run_at`` (the value object carries it for list-row composition;
    it defaults to None on single-GET, which does not resolve a run).

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_definitions (persisted columns).
    Spec: spec/feature/BACKEND.md §Metrics Service — List last_run_at (derived field
          on the record, surfaced only on list rows).
    """
    row = _make_definition_row()
    mock_scalar_query(db, row)

    definition = await service.get_metric(row.id)
    actual_fields = set(definition.model_dump().keys())
    expected = {
        "id",
        "mode",
        "metric_type",
        "title",
        "description",
        "metrics",
        "metric_conf",
        "schedule_tier",
        "dataset_filter",
        "is_enabled",
        "created_at",
        "updated_at",
        "last_run_at",
    }
    assert actual_fields == expected
    # single-GET does not resolve a run → last_run_at defaults to None.
    assert definition.last_run_at is None


# ── MetricResultRecord shape ──────────────────────────────────────────────────


async def test_result_record_values_is_dict(service, db):
    """MetricResultRecord.values is a dict[str, float].

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_results — values is JSONB
          (dict of named floats).
    """
    rows = [_make_result_row(values={"total": 5.0, "ingested_in_time": 3.0})]
    mock_paginated_query(db, rows, total_count=1)

    results, total = await service.get_results("ingestion-freshness")

    assert total == 1
    r = results[0]
    assert isinstance(r.values, dict)
    assert r.values == {"total": 5.0, "ingested_in_time": 3.0}


# ── events ────────────────────────────────────────────────────────────────────


async def test_get_events_returns_paginated_list(service, db):
    """get_events returns (rows, total_count) with correct entity_type.

    Spec: spec/feature/BACKEND.md §Event Emission — METRIC domain events.
    """
    metric_id = "ingestion-freshness"
    rows = [
        make_event_row(entity_type="metric", event_type="METRIC.RUN_COMPLETE", entity_id=metric_id)
        for _ in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(metric_id, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "metric"


async def test_get_events_with_time_range(service, db):
    """get_events accepts from_dt/to_dt time range parameters.

    Spec: spec/API.md §Metric — GET .../event supports from/to time range filters.
    """
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
