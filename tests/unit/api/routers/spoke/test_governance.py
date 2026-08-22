"""Unit tests for the governance metrics router — /spoke/governance/metric/...

Spec traceability:
- spec/API.md §Metric (/spoke/governance/metric)
- spec/USE_CASE_en.md §UC5 — create vs replace, is_enabled=false gate, passive mode 501
- spec/feature/BACKEND.md §Metrics Service §Create vs replace
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_airflow_client, get_metrics_service, get_redis
from src.api.main import app
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.api.conftest import auth_headers

_METRIC_RUN_URL = "/api/v1/spoke/governance/metric/{metric_id}/method/run"
_METRIC_CONF_URL = "/api/v1/spoke/governance/metric/{metric_id}/attr/conf"
_METRIC_CREATE_URL = "/api/v1/spoke/governance/metric"
_METRIC_LIST_URL = "/api/v1/spoke/governance/metric"
_METRIC_GET_URL = "/api/v1/spoke/governance/metric/{metric_id}"


def _make_definition_record(
    metric_id: str = "ingestion-freshness",
    is_enabled: bool = True,
    metric_conf: dict | None = None,
    last_run_at: datetime | None = None,
) -> MagicMock:
    rec = MagicMock()
    rec.id = metric_id
    rec.mode = "active"
    rec.metric_type = "ingestion-freshness"
    rec.title = "Ingestion Freshness"
    rec.description = "Measures freshness of ingested data"
    rec.metrics = [
        {"name": "total", "color": "#64748B", "idx": 1},
        {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
    ]
    rec.metric_conf = metric_conf if metric_conf is not None else {"time_window_sec": 172800}
    rec.dataset_filter = ""
    rec.schedule_tier = "daily"
    rec.is_enabled = is_enabled
    rec.created_at = datetime.now(tz=UTC)
    rec.updated_at = datetime.now(tz=UTC)
    rec.last_run_at = last_run_at
    return rec


@pytest.fixture
def mock_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_airflow() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_cache() -> AsyncMock:
    svc = AsyncMock()
    svc.set_nx = AsyncMock(return_value=True)
    svc.delete_if_value = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def override_dependencies(mock_service: AsyncMock, mock_airflow: AsyncMock, mock_cache: AsyncMock):
    app.dependency_overrides[get_metrics_service] = lambda: mock_service
    app.dependency_overrides[get_airflow_client] = lambda: mock_airflow
    app.dependency_overrides[get_redis] = lambda: mock_cache
    yield
    app.dependency_overrides.pop(get_metrics_service, None)
    app.dependency_overrides.pop(get_airflow_client, None)
    app.dependency_overrides.pop(get_redis, None)


# ── GET /spoke/governance/metric — list (last_run_at exposure) ───────────────────────


@pytest.mark.asyncio
async def test_get_metric_list_row_carries_last_run_at(
    client,
    mock_service: AsyncMock,
) -> None:
    """GET /spoke/governance/metric list rows surface last_run_at from the record.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric — each row carries
          last_run_at (occurred_at of the latest METRIC.RUN_COMPLETE, null when
          never run).
    """
    last_run = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    ran = _make_definition_record(metric_id="has-run", last_run_at=last_run)
    never = _make_definition_record(metric_id="never-run", last_run_at=None)
    mock_service.list_metrics = AsyncMock(return_value=([ran, never], 2))

    resp = await client.get(_METRIC_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200, resp.text
    rows = {r["id"]: r for r in resp.json()["metrics"]}
    assert "last_run_at" in rows["has-run"], (
        "list rows must carry last_run_at. Spec: spec/API.md §Metric — list row."
    )
    served = datetime.fromisoformat(rows["has-run"]["last_run_at"].replace("Z", "+00:00"))
    assert served == last_run, (
        f"last_run_at must echo the record's value; got {rows['has-run']['last_run_at']!r}."
    )
    assert rows["never-run"]["last_run_at"] is None, (
        "a metric with no completed run must serialize last_run_at=null. "
        "Spec: spec/API.md §Metric — null when never run."
    )


@pytest.mark.asyncio
async def test_get_metric_single_omits_last_run_at(
    client,
    mock_service: AsyncMock,
) -> None:
    """GET /spoke/governance/metric/{id} uses the bare definition response and does
    NOT expose last_run_at (a list-row-only field).

    Spec: spec/feature/BACKEND.md §Metrics Service — last_run_at is list-row-only.
    Spec: spec/API.md §Metric — single-GET returns the bare definition.
    """
    rec = _make_definition_record(metric_id="ingestion-freshness")
    mock_service.get_metric = AsyncMock(return_value=rec)

    resp = await client.get(
        _METRIC_GET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    assert "last_run_at" not in resp.json(), (
        "single-GET must not expose last_run_at (list-row-only field). "
        "Spec: spec/feature/BACKEND.md §Metrics Service."
    )


# ── POST /spoke/governance/metric — create ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_metric_create_returns_201(
    client,
    mock_service: AsyncMock,
) -> None:
    """POST /spoke/governance/metric returns 201 on successful create.

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — POST /spoke/governance/metric creates a metric;
          metric_id is supplied in the request body. Success → 201.
    Spec: spec/API.md §Metric — POST /spoke/governance/metric.
    """
    definition = _make_definition_record(metric_id="my-new-metric")
    mock_service.create_metric_config = AsyncMock(return_value=definition)

    resp = await client.post(
        _METRIC_CREATE_URL,
        json={
            "metric_id": "my-new-metric",
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "My New Metric",
            "description": "A freshness metric",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 201, (
        f"Expected 201 on successful create, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    body = resp.json()
    assert body["id"] == "my-new-metric", (
        "Response id must match the created metric_id. "
        "Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_post_metric_create_duplicate_returns_409(
    client,
    mock_service: AsyncMock,
) -> None:
    """POST /spoke/governance/metric returns 409 METRIC_EXISTS on duplicate metric_id.

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — a colliding id returns 409 METRIC_EXISTS.
    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          ConflictError("METRIC_EXISTS") propagated as 409.
    """
    mock_service.create_metric_config = AsyncMock(
        side_effect=ConflictError("METRIC_EXISTS", "Metric my-new-metric already exists")
    )

    resp = await client.post(
        _METRIC_CREATE_URL,
        json={
            "metric_id": "my-new-metric",
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "My New Metric",
            "description": "A freshness metric",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 409, (
        f"Expected 409 METRIC_EXISTS on duplicate, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    body = resp.json()
    assert body.get("error_code") == "METRIC_EXISTS", (
        f"Expected error_code='METRIC_EXISTS'; got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_post_metric_create_passive_mode_returns_501(
    client,
    mock_service: AsyncMock,
) -> None:
    """POST /spoke/governance/metric with mode='passive' returns 501 NOT_IMPLEMENTED.

    Spec: API.md §Governance — Metric (Definition body) — passive mode is reserved; POST with
          mode:'passive' returns 501 NOT_IMPLEMENTED.
    Spec: spec/API.md §Metric — 'passive' mode reserved.
    """
    resp = await client.post(
        _METRIC_CREATE_URL,
        json={
            "metric_id": "passive-metric",
            "mode": "passive",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Passive Metric",
            "description": "Should fail",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive' on POST create, got {resp.status_code}: {resp.text}. "
        "Spec: API.md §Governance — Metric (Definition body) — passive mode → 501."
    )
    body = resp.json()
    assert body.get("error_code") == "NOT_IMPLEMENTED", (
        f"Expected error_code='NOT_IMPLEMENTED'; got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )
    # Service must NOT be called — the guard fires at the route layer
    mock_service.create_metric_config.assert_not_called()


@pytest.mark.asyncio
async def test_post_metric_create_bad_metric_id_format_returns_422(
    client,
    mock_service: AsyncMock,
) -> None:
    """POST /spoke/governance/metric with bad-format metric_id returns 422.

    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          bad-format metric_id → 422 at the schema validation layer.
    """
    resp = await client.post(
        _METRIC_CREATE_URL,
        json={
            "metric_id": "UPPER_INVALID",
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Bad ID",
            "description": "Should fail on metric_id format",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 422, (
        f"Expected 422 for bad-format metric_id, got {resp.status_code}: {resp.text}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace."
    )


# ── PUT /spoke/governance/metric/{id}/attr/conf — replace only ───────────────────────


@pytest.mark.asyncio
async def test_put_metric_conf_absent_id_returns_404(
    client,
    mock_service: AsyncMock,
) -> None:
    """PUT .../attr/conf returns 404 METRIC_NOT_FOUND when the metric id does not exist.

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — PUT replaces an existing definition
          and returns 404 METRIC_NOT_FOUND when the id is absent.
    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          replace_metric_config raises EntityNotFoundError for absent id.
    """
    mock_service.replace_metric_config = AsyncMock(
        side_effect=EntityNotFoundError("metric", "nonexistent-metric")
    )

    resp = await client.put(
        _METRIC_CONF_URL.format(metric_id="nonexistent-metric"),
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Nonexistent",
            "description": "Should return 404",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 404, (
        f"Expected 404 METRIC_NOT_FOUND for absent metric id on PUT, "
        f"got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    body = resp.json()
    assert body.get("error_code") == "METRIC_NOT_FOUND", (
        f"Expected error_code='METRIC_NOT_FOUND'; got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_put_metric_conf_existing_id_returns_200(
    client,
    mock_service: AsyncMock,
) -> None:
    """PUT .../attr/conf returns 200 when the metric id exists (replace succeeds).

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — PUT replaces existing → 200.
    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace —
          replace_metric_config returns the updated definition.
    """
    definition = _make_definition_record(metric_id="ingestion-freshness")
    mock_service.replace_metric_config = AsyncMock(return_value=definition)

    resp = await client.put(
        _METRIC_CONF_URL.format(metric_id="ingestion-freshness"),
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Ingestion Freshness",
            "description": "Updated description",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 200, (
        f"Expected 200 on replace of existing metric, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    body = resp.json()
    assert body["id"] == "ingestion-freshness"


@pytest.mark.asyncio
async def test_put_metric_conf_passive_mode_returns_501(
    client,
    mock_service: AsyncMock,
) -> None:
    """PUT .../attr/conf with mode='passive' returns 501 NOT_IMPLEMENTED.

    Spec: API.md §Governance — Metric (Definition body) — passive mode is reserved; PUT with
          mode:'passive' returns 501 NOT_IMPLEMENTED.
    """
    resp = await client.put(
        _METRIC_CONF_URL.format(metric_id="ingestion-freshness"),
        json={
            "mode": "passive",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Passive Replace",
            "description": "Should fail",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": "",
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive' on PUT replace, got {resp.status_code}: {resp.text}. "
        "Spec: API.md §Governance — Metric (Definition body) — passive mode → 501."
    )
    body = resp.json()
    assert body.get("error_code") == "NOT_IMPLEMENTED"
    # replace_metric_config must NOT be called
    mock_service.replace_metric_config.assert_not_called()


# ── POST /spoke/governance/metric/{id}/method/run — disabled guard ───────────────────


@pytest.mark.asyncio
async def test_post_metric_run_disabled_returns_409(
    client,
    mock_service: AsyncMock,
    mock_airflow: AsyncMock,
    mock_cache: AsyncMock,
) -> None:
    """Route layer rejects non-dry-run when metric is_enabled=False with 409 METRIC_DISABLED.

    Proves the guard fires upstream of both airflow.trigger_and_wait and cache.set_nx,
    so neither is called when the metric is disabled.

    spec: API.md §Governance — Metric (/spoke/governance/metric) — POST /{metric_id}/method/run
    is "Rejected with 409 METRIC_DISABLED when the metric is disabled and dry_run is not true".
    """
    metric_id = "ingestion-freshness"
    definition = _make_definition_record(metric_id=metric_id, is_enabled=False)
    mock_service.get_metric = AsyncMock(return_value=definition)

    resp = await client.post(
        _METRIC_RUN_URL.format(metric_id=metric_id),
        headers=auth_headers(),
    )

    assert resp.status_code == 409, (
        f"Expected 409 METRIC_DISABLED when is_enabled=False, "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "METRIC_DISABLED", (
        f"Expected error_code='METRIC_DISABLED'; got {body.get('error_code')!r}"
    )

    # The gate must fire before the Redis lock attempt and before Airflow is triggered.
    mock_cache.set_nx.assert_not_called()
    mock_airflow.trigger_and_wait.assert_not_called()


def test_the_public_run_route_exposes_no_scheduled_at_input() -> None:
    """`scheduled_at` is an internal-activity concept — the public route has no such input.

    A manual run has no schedule to anchor to, so admitting the field here would let a
    caller back-date a metric's timeseries against an interval nobody scheduled. The
    OpenAPI document is what is inspected because the route's *only* inputs are the path
    and query parameters it declares: neither a body nor a query `scheduled_at` may
    exist.

    spec: feature/BACKEND.md §Metrics Service — Measurement instant: "`POST
    /spoke/governance/metric/{id}/method/run` (on-demand, incl. `dry_run`)" uses
    "Wall-clock `now()` — a manual run has no schedule to anchor to", and "The public run
    route is unchanged by this — `scheduled_at` is an internal-activity concept only".
    spec: API.md §Metric — the route's documented input is "`?dry_run=true`".
    """
    operation = app.openapi()["paths"][
        "/api/v1/spoke/governance/metric/{metric_id}/method/run"
    ]["post"]

    parameter_names = {parameter["name"] for parameter in operation.get("parameters", [])}
    assert "dry_run" in parameter_names, (
        "backstop: the route's documented query parameter must be present, or the "
        f"absence below proves nothing. Got {sorted(parameter_names)}"
    )
    assert "scheduled_at" not in parameter_names, (
        f"the public route must expose no scheduled_at input; got {sorted(parameter_names)}"
    )
    assert "requestBody" not in operation, (
        "the public run route takes no body at all, so there is nowhere for a "
        f"scheduled_at to be smuggled in; got {operation.get('requestBody')!r}"
    )


# ── GET /spoke/governance/metric/{id}/dataset ────────────────────────────────

_METRIC_DATASET_URL = "/api/v1/spoke/governance/metric/{metric_id}/dataset"


def _dataset_record(
    dataset_urn: str,
    met: str,
    last_check_at: datetime | None = None,
    detail: dict | None = None,
) -> MagicMock:
    rec = MagicMock()
    rec.dataset_urn = dataset_urn
    rec.met = met
    rec.last_check_at = last_check_at
    rec.detail = detail
    return rec


@pytest.mark.asyncio
async def test_get_metric_datasets_returns_the_row_inventory(
    client, mock_service: AsyncMock
) -> None:
    """Each row carries dataset_urn, met, last_check_at and detail.

    Spec: spec/API.md §Metric — "Each row carries `dataset_urn`, `met` (`"true"` |
          `"false"` | `"unknown"` […]), `last_check_at`, and `detail`."
    """
    checked = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    mock_service.list_metric_datasets = AsyncMock(
        return_value=(
            [
                _dataset_record(
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)",
                    "true",
                    checked,
                    {"evidence_tier": "observation"},
                ),
                _dataset_record(
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)", "unknown"
                ),
            ],
            2,
            None,
        )
    )

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["dataset_urn"] for row in body["datasets"]] == [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)",
    ]
    assert [row["met"] for row in body["datasets"]] == ["true", "unknown"]
    assert body["datasets"][0]["detail"] == {"evidence_tier": "observation"}
    assert body["datasets"][1]["last_check_at"] is None
    assert body["total_count"] == 2


@pytest.mark.asyncio
async def test_get_metric_datasets_carries_the_scope_sync_watermark(
    client, mock_service: AsyncMock
) -> None:
    """The envelope carries `attrs_synced_at` beside the content key.

    Without it, a filter matching nothing and a filter whose attributes have not yet
    synced are indistinguishable.

    Spec: spec/API.md §Metric — "The response envelope also carries `attrs_synced_at` —
          the **maximum** `dataset_registry.attrs_synced_at` over the datasets in scope".
    """
    synced = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, synced))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["attrs_synced_at"] is not None
    assert datetime.fromisoformat(body["attrs_synced_at"].replace("Z", "+00:00")) == synced


@pytest.mark.asyncio
async def test_get_metric_datasets_attrs_synced_at_is_null_when_never_synced(
    client, mock_service: AsyncMock
) -> None:
    """Spec: spec/API.md §Metric — "`null` when the scope is empty or no covered dataset
    has ever synced"."""
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["attrs_synced_at"] is None


@pytest.mark.asyncio
async def test_get_metric_datasets_met_param_is_repeatable(
    client, mock_service: AsyncMock
) -> None:
    """`met` repeats to select several states; the router forwards the list.

    Spec: spec/API.md §Metric — "Repeatable `met` query param (default: all three)."
    """
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness")
        + "?met=false&met=unknown",
        headers=auth_headers(),
    )

    assert resp.status_code == 200, resp.text
    assert mock_service.list_metric_datasets.await_args.kwargs["met"] == ["false", "unknown"]


@pytest.mark.asyncio
async def test_get_metric_datasets_without_met_defaults_to_all_three(
    client, mock_service: AsyncMock
) -> None:
    """Omitting `met` selects every state, expressed as no narrowing at the service.

    Spec: spec/API.md §Metric — "(default: all three)".
    """
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    assert mock_service.list_metric_datasets.await_args.kwargs["met"] is None


@pytest.mark.asyncio
async def test_get_metric_datasets_rejects_an_unknown_met_value(
    client, mock_service: AsyncMock
) -> None:
    """`met` is a closed vocabulary, so an unknown value is a 422 at the boundary.

    Spec: spec/API.md §Metric — `met` is `"true"` | `"false"` | `"unknown"`.
    """
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness") + "?met=maybe",
        headers=auth_headers(),
    )

    assert resp.status_code == 422, resp.text
    mock_service.list_metric_datasets.assert_not_called()


@pytest.mark.asyncio
async def test_get_metric_datasets_paginates_and_sorts_by_dataset_urn(
    client, mock_service: AsyncMock
) -> None:
    """`offset`/`limit` reach the service and each `sort` maps to its own direction.

    A `sort=dataset_urn_desc` that silently resolved to ASC would be invisible to a
    reader of the panel, so the direction of the clause is asserted, not just its
    presence. Leaving `sort` off must hand the service `None` — the service owns the
    `dataset_urn_asc` default.

    Spec: spec/API.md §Metric — "Paginated (`offset`/`limit`/`total_count`), sortable by
          `dataset_urn` (default `dataset_urn_asc`)."
    """
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness")
        + "?offset=40&limit=10&sort=dataset_urn_desc",
        headers=auth_headers(),
    )

    assert resp.status_code == 200, resp.text
    kwargs = mock_service.list_metric_datasets.await_args.kwargs
    assert kwargs["offset"] == 40
    assert kwargs["limit"] == 10
    desc_clause = str(kwargs["order_by"]).lower()
    assert desc_clause == "dataspoke.dataset_registry.dataset_urn desc", (
        f"'dataset_urn_desc' must order by dataset_urn descending; got {desc_clause!r}"
    )
    body = resp.json()
    assert body["offset"] == 40
    assert body["limit"] == 10

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness")
        + "?sort=dataset_urn_asc",
        headers=auth_headers(),
    )

    assert resp.status_code == 200, resp.text
    asc_clause = str(mock_service.list_metric_datasets.await_args.kwargs["order_by"]).lower()
    assert asc_clause == "dataspoke.dataset_registry.dataset_urn asc", (
        f"'dataset_urn_asc' must order by dataset_urn ascending; got {asc_clause!r}"
    )

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="ingestion-freshness"), headers=auth_headers()
    )

    assert resp.status_code == 200, resp.text
    assert mock_service.list_metric_datasets.await_args.kwargs["order_by"] is None, (
        "no sort param leaves the default to the service (dataset_urn_asc)"
    )


@pytest.mark.asyncio
async def test_get_metric_datasets_absent_metric_returns_404(
    client, mock_service: AsyncMock
) -> None:
    """Spec: spec/API.md §Error Catalogue — 404 METRIC_NOT_FOUND for an absent id."""
    mock_service.list_metric_datasets = AsyncMock(
        side_effect=EntityNotFoundError("metric", "nope")
    )

    resp = await client.get(
        _METRIC_DATASET_URL.format(metric_id="nope"), headers=auth_headers()
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_metric_datasets_requires_authentication(
    client, mock_service: AsyncMock
) -> None:
    """The route is behind the router's authenticated guard, like every sibling read.

    Spec: spec/API.md §Authentication — spoke routes require a JWT.
    """
    mock_service.list_metric_datasets = AsyncMock(return_value=([], 0, None))

    resp = await client.get(_METRIC_DATASET_URL.format(metric_id="ingestion-freshness"))

    assert resp.status_code == 401, resp.text
    mock_service.list_metric_datasets.assert_not_called()
