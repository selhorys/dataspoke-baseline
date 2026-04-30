"""Spot tests for internal activity endpoints.

Concerns covered — for each domain (ingestion, validation, metagen, metrics, ontogen):
- POST /internal/activities/{domain}/list-active — returns list of URNs/IDs for given tier
- POST /internal/activities/{domain}/run — executes for a dataset URN (or metric_id)

Auth: X-Internal-Token header (internal_headers fixture).
Internal routes are mounted WITHOUT the /api/v1 prefix (see src/api/main.py line 271).
"""

import urllib.parse

import httpx
import pytest

# Imazon dataset that is guaranteed to exist in DataHub after reset
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


@pytest.mark.asyncio
async def test_ingestion_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/list-active returns list of active URNs for tier."""
    resp = await api_client.post(
        "/internal/activities/ingestion/list-active",
        headers=internal_headers,
        json={"tier": "hourly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


@pytest.mark.asyncio
async def test_ingestion_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/run executes ingestion for a dataset URN.

    Pre-condition: an active ingestion config must exist. Creates one, runs activity,
    then cleans up.
    """
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    # Create active ingestion config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "mode": "active",
            "platform": "postgres",
            "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
            "identifier": {
                "database": "imazon",
                "schema_name": "catalog",
                "table": "title_master",
            },
            "auth": {
                "username": "spoke_reader",
                "secret_ref": "k8s-secret/pg-spoke-reader",
            },
            "is_enabled": True,
            "schedule_tier": "daily",
        },
    )

    resp = await api_client.post(
        "/internal/activities/ingestion/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN, "dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body or "status" in body

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_list_active_daily(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/validation/list-active returns list of active URNs."""
    resp = await api_client.post(
        "/internal/activities/validation/list-active",
        headers=internal_headers,
        json={"tier": "daily"},
    )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_validation_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/validation/run executes validation for a dataset URN."""
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    # Create validation config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-activity-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/validation/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body or "status" in body

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_list_active_weekly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metagen/list-active returns list of active URNs."""
    resp = await api_client.post(
        "/internal/activities/metagen/list-active",
        headers=internal_headers,
        json={"tier": "weekly"},
    )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_metagen_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metagen/run executes metagen for a dataset URN."""
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    # Create metagen config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": True,
            "schedule_tier": "weekly",
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/metagen/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN, "dry_run": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body or "dataset_urn" in body or "status" in body

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metrics_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/list-active returns list of active metric IDs."""
    resp = await api_client.post(
        "/internal/activities/metrics/list-active",
        headers=internal_headers,
        json={"tier": "hourly"},
    )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_metrics_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/run executes measurement for a metric_id."""
    metric_id = "spot-activity-freshness"
    conf_url = f"/api/v1/spoke/dg/metric/{metric_id}/attr/conf"

    # Create metric config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "title": "Spot Activity Freshness",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "ingestion-freshness",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    resp = await api_client.post(
        "/internal/activities/metrics/run",
        headers=internal_headers,
        json={"metric_id": metric_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body or "status" in body

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_ontogen_run_activity_dry(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ontogen/run executes ontogen inference (dry_run)."""
    resp = await api_client.post(
        "/internal/activities/ontogen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body or "dry_run" in body
