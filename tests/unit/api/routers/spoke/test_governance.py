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


def _make_definition_record(
    metric_id: str = "ingestion-freshness",
    is_enabled: bool = True,
    metric_conf: dict | None = None,
) -> MagicMock:
    rec = MagicMock()
    rec.id = metric_id
    rec.mode = "active"
    rec.metric_type = "ingestion-freshness"
    rec.title = "Ingestion Freshness"
    rec.description = "Measures freshness of ingested data"
    rec.metrics = ["total", "ingested_in_time"]
    rec.metric_conf = metric_conf if metric_conf is not None else {"time_window_sec": 172800}
    rec.dataset_filter = {}
    rec.schedule_tier = "daily"
    rec.is_enabled = is_enabled
    rec.created_at = datetime.now(tz=UTC)
    rec.updated_at = datetime.now(tz=UTC)
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
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {},
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
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {},
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

    Spec: spec/USE_CASE_en.md §UC5 §Modes — passive is reserved; POST with
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
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive' on POST create, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §Modes."
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
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {},
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
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
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
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {},
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

    Spec: spec/USE_CASE_en.md §UC5 §Modes — passive is reserved; PUT with
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
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive' on PUT replace, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §Modes."
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

    spec: USE_CASE_en.md §UC5 — "When is_enabled=false, non-dry-run calls to
    method/run on a metric return 409 METRIC_DISABLED. Dry-run is always
    permitted regardless of is_enabled." (L739)
    spec: API.md §Metric (/spoke/governance/metric) — POST /{metric_id}/method/run
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
