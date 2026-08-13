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
from src.shared.dataset_filter import DatasetFilterSyntaxError
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
from tests.unit.conftest import route_db_execute

# ── Row factories ─────────────────────────────────────────────────────────────


def _make_definition_row(
    metric_id: str = "ingestion-freshness",
    mode: str = "active",
    metric_type: str = "ingestion-freshness",
    title: str = "Ingestion Freshness",
    description: str = "Measures freshness of ingestion",
    metrics: list | None = None,
    metric_conf: dict | None = None,
    dataset_filter: str | None = None,
    schedule_tier: str | None = "daily",
    is_enabled: bool = True,
) -> MagicMock:
    row = MagicMock()
    row.id = metric_id
    row.mode = mode
    row.metric_type = metric_type
    row.title = title
    row.description = description
    row.metrics = (
        metrics
        if metrics is not None
        else [
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ]
    )
    row.metric_conf = metric_conf if metric_conf is not None else {"time_window_sec": 172800}
    row.dataset_filter = dataset_filter if dataset_filter is not None else ""
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
    # Route by SQL: the count query carries count(); the page-bounded last-run batch
    # is the grouped max(occurred_at) over events; the page rows are the default.
    route_db_execute(
        db,
        [("count(", count_result), ("max(", lr_result)],
        default=rows_result,
    )


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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
        metric_conf={"time_window_sec": 172800},
        dataset_filter="",
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
            metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
            metric_conf={"time_window_sec": 172800},
            dataset_filter="",
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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "doc_health", "color": "#A855F7", "idx": 2},
        ],
        metric_conf={},
        dataset_filter="",
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
        metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
        metric_conf={"time_window_sec": 3600},
        dataset_filter="",
    )
    mock_scalar_query(db, existing)
    mock_db_refresh(db)

    await service.replace_metric_config(
        metric_id="ingestion-freshness",
        mode="active",
        metric_type="ingestion-freshness",
        title="Updated Title",
        description="Updated desc",
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
        metric_conf={"time_window_sec": 172800},
        dataset_filter="origin = 'PROD'",
        schedule_tier="weekly",
        is_enabled=True,
    )
    assert existing.title == "Updated Title"
    assert existing.metrics == [
        {"name": "total", "color": "#64748B", "idx": 1},
        {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
    ]
    assert existing.metric_conf == {"time_window_sec": 172800}
    assert existing.dataset_filter == "origin = 'PROD'"
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
            metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "doc_health", "color": "#A855F7", "idx": 2},
        ],
            metric_conf={},
            dataset_filter="",
        )


# ── patch_metric_config ───────────────────────────────────────────────────────


async def test_patch_metric_config_is_enabled_only_does_not_touch_other_fields(service, db):
    """PATCH that sets only is_enabled does not touch other fields.

    Spec: spec/API.md §Metric — PATCH updates metric definition fields (partial update).
    """
    row = _make_definition_row(
        metric_conf={"time_window_sec": 172800},
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
        dataset_filter="origin = 'PROD'",
    )
    original_metrics = row.metrics[:]
    original_conf = dict(row.metric_conf)
    original_filter = row.dataset_filter

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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
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

    route_db_execute(db, [("metric_results", latest_result)], default=def_result)

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
    route_db_execute(db, [("metric_results", latest_result)], default=def_result)

    attr = await service.get_metric_attr(def_row.id)
    assert attr["latest_values"] is None
    assert attr["latest_measured_at"] is None


# ── delete_metric_config ──────────────────────────────────────────────────────


async def test_delete_metric_config_clears_results_and_verdicts(service, db):
    """Deleting a definition clears both its timeseries and its per-dataset verdicts.

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_results — metric_id is FK to
          metric_definitions;
    Spec: spec/feature/BACKEND.md §Metrics Service — "Deleting a metric definition
          clears its verdicts."
    """
    row = _make_definition_row()
    statements: list = []

    async def _execute(stmt, *args, **kwargs):
        statements.append((stmt, args[0] if args else None))
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        return result

    db.execute = AsyncMock(side_effect=_execute)

    await service.delete_metric_config(row.id)

    deletes = [
        (text, params)
        for text, params in _compiled_each(statements)
        if text.startswith("delete from")
    ]
    assert len(deletes) == 2, (
        "backstop: exactly two DELETEs expected (timeseries + verdicts); "
        f"got {[text for text, _ in deletes]}"
    )
    # The spec scopes the clearing to *this* metric's rows. An unscoped DELETE — or
    # one binding another id — wipes every metric's history estate-wide, which no
    # assertion on table names alone can see.
    for table in ("metric_results", "metric_dataset_results"):
        scoped = [
            (text, params)
            for text, params in deletes
            if text.startswith(f"delete from dataspoke.{table} ")
        ]
        assert len(scoped) == 1, (
            f"exactly one DELETE against {table} expected. "
            f"spec: feature/BACKEND.md §Metrics Service. Deletes issued: {deletes}"
        )
        delete_sql, delete_params = scoped[0]
        assert f"dataspoke.{table}.metric_id =" in delete_sql, (
            f"the {table} DELETE must be restricted to this metric's rows, "
            f"not the whole table; got:\n{delete_sql}"
        )
        assert row.id in delete_params.values(), (
            f"the {table} DELETE must bind the metric under test ({row.id!r}); "
            f"bound {delete_params!r}"
        )
    db.delete.assert_called_once_with(row)
    assert db.commit.await_count >= 1


async def test_delete_metric_config_not_found(service, db):
    """delete_metric_config raises EntityNotFoundError for missing metric.

    Spec: spec/API.md §Error Catalogue — 404 NOT_FOUND when resource absent.
    """
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.delete_metric_config("nonexistent")


# ── dataset_filter validation at the service layer ────────────────────────────
#
# Services validate independently of the request schemas: internal callers (activity
# endpoints, bootstrap) reach them without a request body.


async def test_create_metric_config_dataset_filter_over_cap_raises(service, db):
    """A filter over the 1,000-literal cap is rejected before the row is written.

    Spec: spec/API.md §`dataset_filter` grammar — Caps: "filter text ≤ 8,000 characters
          and ≤ 1,000 string literals";
    Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "exceeds a payload
          cap".
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    over_cap = ", ".join(f"'v{i}'" for i in range(1001))

    with pytest.raises(DatasetFilterSyntaxError) as exc_info:
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="ingestion-freshness",
            title="t",
            description="d",
            metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
            metric_conf={"time_window_sec": 172800},
            dataset_filter=f"origin IN ({over_cap})",
        )
    assert exc_info.value.error_code == "INVALID_DATASET_FILTER"
    db.add.assert_not_called()


async def test_create_metric_config_dataset_filter_syntax_error_raises(service, db):
    """A filter that does not parse is rejected with the position of the error.

    Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "`detail` carries
          the character position of the error […] Validated wherever a `dataset_filter`
          is written: […] `POST /spoke/governance/metric`".
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(DatasetFilterSyntaxError) as exc_info:
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="ingestion-freshness",
            title="t",
            description="d",
            metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
            metric_conf={"time_window_sec": 172800},
            dataset_filter="owner = 'alice'",
        )
    assert exc_info.value.detail == {"position": exc_info.value.position}
    db.add.assert_not_called()


async def test_create_metric_config_dataset_filter_bad_urn_raises(service, db):
    """A malformed `dataset_urn` literal raises the URN error, not the filter one.

    Spec: spec/API.md §Error Catalogue — INVALID_DATASET_URN, 422, "A `dataset_urn`
          literal inside a `dataset_filter` is not a well-formed `urn:li:dataset:(…)` URN".
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
            metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
            metric_conf={"time_window_sec": 172800},
            dataset_filter="dataset_urn = 'not-a-urn'",
        )


async def test_create_metric_config_accepts_a_well_formed_filter(service, db):
    """Backstop for the three rejections above: a valid filter is stored verbatim.

    Without this, a `create_metric_config` that rejected every filter would pass all
    three tests above.

    Spec: spec/feature/BACKEND_SCHEMA.md §metric_definitions — `dataset_filter` is the
          stored SQL WHERE clause.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    definition = await service.create_metric_config(
        metric_id="test",
        mode="active",
        metric_type="ingestion-freshness",
        title="t",
        description="d",
        metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
        metric_conf={"time_window_sec": 172800},
        dataset_filter="origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns",
    )

    assert definition.dataset_filter == "origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns"


# ── metrics[] series descriptors at the service layer ─────────────────────────


async def test_create_metric_config_rejects_a_key_the_type_does_not_emit(service, db):
    """A series naming a key outside the type's emitted set is rejected.

    Spec: spec/API.md §Metric — Definition body — "`name` is one of the type's emitted
          keys (see USE_CASE §UC5); unknown keys return `422 INVALID_PARAMETER`".
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="doc-health",
            title="t",
            description="d",
            metrics=[{"name": "ingested_in_time", "color": "#22C55E", "idx": 1}],
            metric_conf={},
            dataset_filter="",
        )
    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_create_metric_config_rejects_a_duplicate_series_name(service, db):
    """Spec: spec/API.md §Metric — Definition body — "`name` and `idx` are each unique
    within the metric"."""
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(PreconditionFailedError):
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="doc-health",
            title="t",
            description="d",
            metrics=[
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "total", "color": "#A855F7", "idx": 2},
            ],
            metric_conf={},
            dataset_filter="",
        )


async def test_create_metric_config_rejects_a_duplicate_series_idx(service, db):
    """Spec: spec/API.md §Metric — Definition body — "`name` and `idx` are each unique
    within the metric". Two series at the same idx have no defined draw order."""
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(PreconditionFailedError):
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="doc-health",
            title="t",
            description="d",
            metrics=[
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 1},
            ],
            metric_conf={},
            dataset_filter="",
        )


async def test_create_metric_config_rejects_a_non_hex_color(service, db):
    """Spec: spec/API.md §Metric — Definition body — "`color` is a `#RRGGBB` hex string"."""
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with pytest.raises(PreconditionFailedError):
        await service.create_metric_config(
            metric_id="test",
            mode="active",
            metric_type="doc-health",
            title="t",
            description="d",
            metrics=[{"name": "total", "color": "slate", "idx": 1}],
            metric_conf={},
            dataset_filter="",
        )


async def test_patch_revalidates_series_against_the_merged_metric_type(service, db):
    """A PATCH that changes only `metric_type` re-checks the stored series against it.

    The merged pair is only knowable at the service: a request carrying `metric_type`
    alone leaves the schema layer with no series to check it against, so a metric could
    otherwise end up with series its new type never emits.

    Spec: spec/API.md §Metric — Definition body — "`name` is one of the type's emitted
          keys […]; unknown keys return `422 INVALID_PARAMETER`".
    """
    row = _make_definition_row(
        metric_type="ingestion-freshness",
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
        metric_conf={"time_window_sec": 172800},
    )
    mock_scalar_query(db, row)
    mock_db_refresh(db)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.patch_metric_config(
            row.id, {"metric_type": "doc-health", "metric_conf": {}}
        )
    assert exc_info.value.error_code == "INVALID_PARAMETER"


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
        metrics=[{"name": "total", "color": "#64748B", "idx": 1}],
        metric_conf={"time_window_sec": 172800},
        dataset_filter="",
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
        metrics=[
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
        ],
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


# ── Verdict persistence (metric_dataset_results) ──────────────────────────────


def _stub_measurer(monkeypatch: pytest.MonkeyPatch, values: dict, verdicts: list) -> None:
    """Stand the registry's measurer lookup in with a fixed (values, verdicts) return.

    The measurer contract is covered by tests/unit/backend/metrics/measurers/; what is
    under test here is what the *service* does with a return that satisfies it.
    """

    async def _measure(datasets, metric_conf, *, datahub, db):
        return values, verdicts

    monkeypatch.setattr(
        "src.backend.metrics.service.get_measurer", lambda name: _measure
    )


def _run_session(db: AsyncMock, definition_row: MagicMock) -> list:
    """Route the run's reads: the definition lookup, then an empty registry scope."""
    statements: list = []

    async def _execute(stmt, *args, **kwargs):
        statements.append((stmt, args[0] if args else None))
        result = MagicMock()
        result.scalar_one_or_none.return_value = definition_row
        result.scalars.return_value.all.return_value = []
        result.all.return_value = []
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return statements


def _compile_one(stmt) -> tuple[str, dict]:
    """Render one statement to (whitespace-normalised lowercase SQL, bound params).

    Bound params matter as much as the text: a predicate that names the right column
    but binds the wrong value is exactly the scoping bug these tests exist to catch.
    """
    from sqlalchemy.dialects import postgresql

    compiled = stmt.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).lower().split()), dict(compiled.params)


def _compiled_each(statements: list) -> list[tuple[str, dict]]:
    rendered = []
    for stmt, _payload in statements:
        try:
            rendered.append(_compile_one(stmt))
        except Exception:  # pragma: no cover — a statement that will not compile
            rendered.append((" ".join(str(stmt).lower().split()), {}))
    return rendered


def _compiled(statements: list) -> str:
    return " ".join(sql for sql, _params in _compiled_each(statements))


async def test_a_real_run_replaces_the_metrics_verdict_rows(
    service, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dry run deletes the metric's verdicts and re-inserts the run's.

    Spec: spec/feature/BACKEND.md §Metrics Service — "A non-dry run replaces the
          metric's rows wholesale inside the result transaction, so the store always
          reflects exactly one run."
    """
    from src.backend.metrics.measurers import DatasetVerdict

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    row = _make_definition_row(is_enabled=True)
    statements = _run_session(db, row)
    _stub_measurer(
        monkeypatch,
        {"total": 1.0, "ingested_in_time": 1.0},
        [DatasetVerdict(urn=urn, met=True, evidence_at=None, detail={"k": "v"})],
    )

    result = await service.run(row.id, dry_run=False)

    assert result.status == "success"
    sql = _compiled(statements)
    deletes = [
        (text, params)
        for text, params in _compiled_each(statements)
        if text.startswith("delete from dataspoke.metric_dataset_results")
    ]
    assert len(deletes) == 1, (
        f"backstop: exactly one verdict DELETE must have run; got:\n{sql}"
    )
    delete_sql, delete_params = deletes[0]
    # An unscoped DELETE would wipe every *other* metric's verdicts, which the
    # /dataset panel then reports as "unknown" — the statement must name this
    # metric and bind this metric's id.
    assert "dataspoke.metric_dataset_results.metric_id =" in delete_sql, (
        "the DELETE must be scoped to this metric's rows, not the whole table; "
        f"got:\n{delete_sql}"
    )
    assert row.id in delete_params.values(), (
        f"the DELETE must bind the metric under test ({row.id!r}); "
        f"bound {delete_params!r}"
    )
    assert "insert into dataspoke.metric_dataset_results" in sql, (
        f"the run's verdicts must be inserted; got:\n{sql}"
    )
    inserted = [
        payload
        for stmt, payload in statements
        if payload is not None and "metric_dataset_results" in str(stmt).lower()
    ]
    assert inserted and inserted[0][0]["dataset_urn"] == urn
    assert inserted[0][0]["met"] is True
    assert db.commit.await_count >= 1


async def test_a_dry_run_persists_neither_result_nor_verdicts(
    service, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run writes nothing, leaving the previous run's verdicts readable.

    Spec: spec/feature/BACKEND.md §Metrics Service — "A **dry run persists nothing** —
          the standing metrics invariant — leaving the previous run's verdicts readable";
    Spec: spec/API.md §Metric — "a dry run persists none, so `/dataset` after a dry run
          still reports the previous run's verdicts."
    """
    from src.backend.metrics.measurers import DatasetVerdict

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    row = _make_definition_row(is_enabled=True)
    statements = _run_session(db, row)
    _stub_measurer(
        monkeypatch,
        {"total": 1.0, "ingested_in_time": 0.0},
        [DatasetVerdict(urn=urn, met=False, evidence_at=None, detail={})],
    )

    result = await service.run(row.id, dry_run=True)

    assert result.detail["dry_run"] is True
    sql = _compiled(statements)
    assert "metric_dataset_results" not in sql, (
        f"a dry run must not touch the verdict store; got:\n{sql}"
    )
    db.add.assert_not_called()
    assert db.commit.await_count == 0


async def test_the_breakdown_is_derived_from_the_verdicts(
    service, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`breakdown` lists only the failed verdicts; `dataset_count` is the total scanned.

    Deriving it from the same verdicts that populate the store is what keeps the two
    from ever disagreeing.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — "{"dataset_count":
          <total scanned>, "datasets": [{"urn": "...", "detail": {...}}]}"; "`datasets[]`
          lists **only failed datasets** […] It is the `met = false` subset of the run's
          verdicts";
    Spec: §Verdict contract — "The failures-only `breakdown` below is **derived** from
          the verdicts […] the two can never disagree."
    """
    from src.backend.metrics.measurers import DatasetVerdict

    good = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.good,PROD)"
    bad = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bad,PROD)"
    row = _make_definition_row(is_enabled=True)
    _run_session(db, row)
    added: list = []
    db.add = MagicMock(side_effect=added.append)
    _stub_measurer(
        monkeypatch,
        {"total": 2.0, "ingested_in_time": 1.0},
        [
            DatasetVerdict(urn=good, met=True, evidence_at=None, detail={"why": "fresh"}),
            DatasetVerdict(urn=bad, met=False, evidence_at=None, detail={"why": "stale"}),
        ],
    )

    result = await service.run(row.id, dry_run=False)

    stored = [obj for obj in added if hasattr(obj, "breakdown")]
    assert stored, "backstop: the run must have written a metric_results row"
    breakdown = stored[0].breakdown
    assert breakdown["dataset_count"] == 2, "dataset_count is the total scanned"
    assert breakdown["datasets"] == [{"urn": bad, "detail": {"why": "stale"}}], (
        f"only the failed verdict is listed; got {breakdown['datasets']!r}"
    )
    assert result.detail["breakdown_summary"] == {"dataset_count": 2, "affected_count": 1}


async def test_measured_values_are_filtered_to_the_declared_series(
    service, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the keys named by `metrics[].name` are persisted.

    Spec: spec/feature/BACKEND.md §Metrics Service — "the service filters the dict to
          the names declared by `attr/conf.metrics[]` before persisting."
    """
    row = _make_definition_row(
        is_enabled=True,
        metrics=[{"name": "ingested_in_time", "color": "#22C55E", "idx": 1}],
    )
    _run_session(db, row)
    _stub_measurer(monkeypatch, {"total": 9.0, "ingested_in_time": 4.0}, [])

    result = await service.run(row.id, dry_run=False)

    assert result.detail["values"] == {"ingested_in_time": 4.0}, (
        "'total' is emitted by the measurer but not declared by this metric's series. "
        "spec: feature/BACKEND.md §Metrics Service."
    )


# ── list_metric_datasets — GET /spoke/governance/metric/{id}/dataset ──────────


def _dataset_view_session(
    db: AsyncMock,
    definition_row: MagicMock,
    rows: list,
    *,
    total_count: int | None = None,
    attrs_synced_at: datetime | None = None,
) -> list:
    """Route the three reads the dataset view issues, by the SQL each compiles to.

    Never by call order: the count, the page and the sync-watermark aggregate are
    distinguishable by their own SQL, so a reordered or added query cannot silently
    shift a result (spec/TESTING.md §Unit Testing → Mocking rules).
    """
    from sqlalchemy.dialects import postgresql

    statements: list = []

    async def _execute(stmt, *args, **kwargs):
        statements.append(stmt)
        sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
        result = MagicMock()
        result.scalar_one_or_none.return_value = definition_row
        if "max(dataspoke.dataset_registry.attrs_synced_at)" in sql:
            result.scalar.return_value = attrs_synced_at
        elif "count(" in sql:
            result.scalar.return_value = len(rows) if total_count is None else total_count
        else:
            result.all.return_value = rows
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return statements


def _page_query(statements: list) -> tuple[str, dict]:
    """The paged row query among the dataset view's reads, as (SQL, bound params).

    Identified by its own SQL — it is the only one carrying LIMIT — never by call
    order (spec/TESTING.md §Unit Testing → Mocking rules).
    """
    compiled = [_compile_one(stmt) for stmt in statements]
    pages = [(sql, params) for sql, params in compiled if " limit " in sql]
    assert len(pages) == 1, f"backstop: one paged query expected; got {len(pages)}"
    return pages[0]


def _verdict_row(
    urn: str,
    met: bool | None,
    *,
    evidence_at: datetime | None = None,
    measured_at: datetime | None = None,
    detail: dict | None = None,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        dataset_urn=urn,
        met=met,
        evidence_at=evidence_at,
        measured_at=measured_at,
        detail=detail,
    )


async def test_dataset_view_reads_scope_from_the_registry_and_left_joins_verdicts(
    service, db
) -> None:
    """Scope is the filter clause over the registry; verdicts are left-joined onto it.

    Resolving scope from the same registry the run resolved it from is what keeps the
    two from disagreeing, and the LEFT join is what makes `unknown` expressible.

    Spec: spec/feature/BACKEND.md §Metrics Service — "it pushes the compiled filter
          clause into a paginated query over `dataset_registry` and left-joins the
          verdict rows, so a dataset in scope with no verdict reads `met = "unknown"`."
    Spec: spec/API.md §Metric — rows are "joined to the latest per-dataset verdict"
          of *this* metric.
    """
    row = _make_definition_row(dataset_filter="origin = 'PROD'")
    statements = _dataset_view_session(db, row, [])

    await service.list_metric_datasets(row.id)

    page_sql, page_params = _page_query(statements)
    assert "dataspoke.dataset_registry" in page_sql
    assert "left outer join dataspoke.metric_dataset_results" in page_sql, (
        f"the verdict join must be a LEFT join; got:\n{page_sql}"
    )
    # Without a metric_id predicate in the ON clause another metric's verdicts leak
    # onto this panel, and a dataset judged by several metrics fans out into
    # duplicate rows.
    on_clause = page_sql.split("left outer join dataspoke.metric_dataset_results on", 1)[
        1
    ].split(" where ", 1)[0]
    assert "dataspoke.metric_dataset_results.metric_id =" in on_clause, (
        "the join must be restricted to this metric's verdicts; "
        f"ON clause was:\n{on_clause}"
    )
    assert row.id in page_params.values(), (
        f"the join must bind the metric under test ({row.id!r}); bound {page_params!r}"
    )
    assert "dataspoke.dataset_registry.origin" in page_sql, (
        "the metric's own filter must be pushed into the query, not applied afterwards"
    )
    # Polarity, not just the column: `is_(False)` would render the exact complement
    # of the metric's scope — every dataset DataHub does not know about.
    assert "dataspoke.dataset_registry.datahub_registered is true" in page_sql, (
        "the scope must be restricted to registered datasets. "
        f"spec: feature/BACKEND.md §Dataset resolution. Got:\n{page_sql}"
    )


async def test_dataset_view_maps_the_tri_state_verdict(service, db) -> None:
    """A missing verdict row reads `unknown`; a present one reads `true`/`false`.

    Spec: spec/API.md §Metric — "`met` (`"true"` | `"false"` | `"unknown"` — `unknown`
          = in scope but never evaluated)"; "`met` is `"unknown"` exactly when the
          dataset is in the filter's scope but carries no verdict".
    """
    measured = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    row = _make_definition_row()
    rows = [
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", True,
                     measured_at=measured),
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.f,PROD)", False,
                     measured_at=measured),
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.u,PROD)", None),
    ]
    _dataset_view_session(db, row, rows)

    records, total, _synced = await service.list_metric_datasets(row.id)

    assert [r.met for r in records] == ["true", "false", "unknown"]
    assert total == 3


async def test_dataset_view_last_check_at_falls_back_to_the_run_time(service, db) -> None:
    """`last_check_at` is the evidence timestamp, falling back to the run's `measured_at`.

    The fallback is what makes doc-health readable at all — it has no per-dataset
    timestamp, so it always reports the run time.

    Spec: spec/API.md §Metric — "`last_check_at` is the per-dataset evidence timestamp
          […] falling back to the run's `measured_at` — `doc-health` has no per-dataset
          timestamp, so it always reports the run time."
    """
    evidence = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
    measured = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    row = _make_definition_row()
    rows = [
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.evidence,PROD)", True,
                     evidence_at=evidence, measured_at=measured),
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.noevidence,PROD)", True,
                     evidence_at=None, measured_at=measured),
        _verdict_row("urn:li:dataset:(urn:li:dataPlatform:postgres,db.unknown,PROD)", None),
    ]
    _dataset_view_session(db, row, rows)

    records, _total, _synced = await service.list_metric_datasets(row.id)

    assert [r.last_check_at for r in records] == [evidence, measured, None], (
        "evidence_at wins where present, measured_at is the fallback, and a dataset "
        "with no verdict has neither."
    )


async def test_dataset_view_met_filter_narrows_the_query(service, db) -> None:
    """A `met` selection is applied in SQL; selecting all three adds no predicate.

    Each state maps to its own predicate: swapping `true` and `false` would return the
    passing datasets under `met=false` and vice versa, so both are pinned here.

    Spec: spec/API.md §Metric — `met` is `"true"` | `"false"` | `"unknown"`, a
          repeatable query param (default: all three); "`met` is `"unknown"` exactly
          when the dataset is in the filter's scope but carries no verdict".
    """
    row = _make_definition_row()

    statements = _dataset_view_session(db, row, [])
    await service.list_metric_datasets(row.id, met=["true"])
    true_sql, _params = _page_query(statements)
    assert "dataspoke.metric_dataset_results.met is true" in true_sql, (
        f"'true' selects the datasets whose verdict passed; got:\n{true_sql}"
    )
    assert "dataspoke.metric_dataset_results.met is false" not in true_sql, (
        f"'true' must not also admit failing verdicts; got:\n{true_sql}"
    )

    statements = _dataset_view_session(db, row, [])
    await service.list_metric_datasets(row.id, met=["false"])
    false_sql, _params = _page_query(statements)
    assert "dataspoke.metric_dataset_results.met is false" in false_sql, (
        f"'false' selects the datasets whose verdict failed; got:\n{false_sql}"
    )
    assert "dataspoke.metric_dataset_results.met is true" not in false_sql, (
        f"'false' must not also admit passing verdicts; got:\n{false_sql}"
    )

    statements = _dataset_view_session(db, row, [])
    await service.list_metric_datasets(row.id, met=["unknown"])
    narrowed, _params = _page_query(statements)
    assert "dataspoke.metric_dataset_results.met is null" in narrowed, (
        f"'unknown' is the left-join miss; got:\n{narrowed}"
    )

    statements = _dataset_view_session(db, row, [])
    await service.list_metric_datasets(row.id, met=["true", "false", "unknown"])
    unnarrowed, _params = _page_query(statements)
    assert "dataspoke.metric_dataset_results.met is" not in unnarrowed, (
        "selecting all three states must add no predicate — the default is every row. "
        f"Got:\n{unnarrowed}"
    )


async def test_dataset_view_defaults_to_ascending_dataset_urn_order(service, db) -> None:
    """With no explicit sort the page is ordered by `dataset_urn` ascending.

    The default ordering is what makes the Datasets panel's pagination stable across
    pages; an unordered or descending default silently reshuffles them.

    Spec: spec/API.md §Metric — the `/dataset` route is "sortable by `dataset_urn`
          (default `dataset_urn_asc`)".
    """
    row = _make_definition_row()
    statements = _dataset_view_session(db, row, [])

    await service.list_metric_datasets(row.id, order_by=None)

    page_sql, _params = _page_query(statements)
    assert "order by dataspoke.dataset_registry.dataset_urn asc" in page_sql, (
        f"the default order is dataset_urn ascending; got:\n{page_sql}"
    )


async def test_dataset_view_honours_an_explicit_order(service, db) -> None:
    """An explicit `order_by` overrides the default rather than being ignored.

    Spec: spec/API.md §Metric — the `/dataset` route is "sortable by `dataset_urn`",
          so the router's resolved sort clause must reach the query.
    """
    from src.shared.db.models import DatasetRegistry

    row = _make_definition_row()
    statements = _dataset_view_session(db, row, [])

    await service.list_metric_datasets(row.id, order_by=DatasetRegistry.dataset_urn.desc())

    page_sql, _params = _page_query(statements)
    assert "order by dataspoke.dataset_registry.dataset_urn desc" in page_sql, (
        f"the caller's sort must be used verbatim; got:\n{page_sql}"
    )


async def test_dataset_view_rejects_an_unknown_met_value(service, db) -> None:
    """`met` is a closed vocabulary of three states.

    Spec: spec/API.md §Metric — `met` is `"true"` | `"false"` | `"unknown"`.
    """
    row = _make_definition_row()
    _dataset_view_session(db, row, [])

    with pytest.raises(PreconditionFailedError) as exc_info:
        await service.list_metric_datasets(row.id, met=["maybe"])
    assert exc_info.value.error_code == "INVALID_PARAMETER"


async def test_dataset_view_reports_the_scope_relative_sync_watermark(service, db) -> None:
    """`attrs_synced_at` is the max over the datasets in scope, not over the page.

    Spec: spec/API.md §Metric — "the **maximum** `dataset_registry.attrs_synced_at` over
          the datasets in scope […] It is scope-relative, not registry-wide, and
          unaffected by `met` filtering or paging".
    """
    from sqlalchemy.dialects import postgresql

    synced = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    row = _make_definition_row(dataset_filter="origin = 'PROD'")
    statements = _dataset_view_session(db, row, [], attrs_synced_at=synced)

    _records, _total, attrs_synced_at = await service.list_metric_datasets(
        row.id, met=["true"], offset=40, limit=10
    )

    assert attrs_synced_at == synced
    watermark = [
        str(stmt.compile(dialect=postgresql.dialect())).lower()
        for stmt in statements
        if "max(dataspoke.dataset_registry.attrs_synced_at)" in str(
            stmt.compile(dialect=postgresql.dialect())
        ).lower()
    ]
    assert len(watermark) == 1, f"one watermark query expected; got {len(watermark)}"
    assert "metric_dataset_results" not in watermark[0], (
        "the watermark must not be narrowed by the met filter — it describes the scope, "
        f"not the page. Got:\n{watermark[0]}"
    )
    assert "limit" not in watermark[0] and "offset" not in watermark[0], (
        f"the watermark must not be paged; got:\n{watermark[0]}"
    )
    assert "dataspoke.dataset_registry.origin" in watermark[0], (
        "the watermark is scope-relative, so the metric's filter still applies"
    )


async def test_dataset_view_raises_for_an_absent_metric(service, db) -> None:
    """Spec: spec/API.md §Error Catalogue — 404 METRIC_NOT_FOUND when the id is absent."""
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError):
        await service.list_metric_datasets("nonexistent")
