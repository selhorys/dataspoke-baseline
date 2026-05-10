"""Unit tests for the DG metrics router — /spoke/dg/metric/...

Spec traceability:
- spec/API.md §Metric (/spoke/dg/metric)
- spec/USE_CASE_en.md §UC5 — is_enabled=false gate at the route layer
"""

from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime

import pytest

from src.api.dependencies import get_airflow_client, get_metrics_service, get_redis
from src.api.main import app
from src.shared.exceptions import ConflictError

from tests.unit.api.conftest import auth_headers

_METRIC_RUN_URL = "/api/v1/spoke/dg/metric/{metric_id}/method/run"


def _make_definition_record(metric_id: str = "ingestion-freshness", is_enabled: bool = True) -> MagicMock:
    rec = MagicMock()
    rec.id = metric_id
    rec.metric_id = metric_id
    rec.title = "Ingestion Freshness"
    rec.description = "Measures freshness of ingested data"
    rec.theme = "ingestion"
    rec.measurement_query = {"aggregation": "pct_fresh"}
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
    spec: API.md §Metric (/spoke/dg/metric) — POST /{metric_id}/method/run
    """
    metric_id = "ingestion-freshness"
    definition = _make_definition_record(metric_id=metric_id, is_enabled=False)
    mock_service.get_metric = AsyncMock(return_value=definition)

    resp = await client.post(
        _METRIC_RUN_URL.format(metric_id=metric_id),
        json={"dry_run": False},
        headers=auth_headers(["dg"]),
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
