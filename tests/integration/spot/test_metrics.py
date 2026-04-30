"""Spot tests for Governance Metrics endpoints.

Concerns covered:
- GET /spoke/dg/metric — list metrics (paginated envelope)
- PUT /spoke/dg/metric/{metric_id}/attr/conf — create definition (201)
- PATCH /spoke/dg/metric/{metric_id}/attr/conf — partial update
- DELETE /spoke/dg/metric/{metric_id}/attr/conf — remove definition (204)
- POST /spoke/dg/metric/{metric_id}/method/run — trigger metric run
- GET /spoke/dg/metric/{metric_id}/attr/result — result list (timeseries envelope)
- GET /spoke/dg/metric/{metric_id}/event — event list envelope
"""

import pytest
import httpx

_TEST_METRIC_ID = "spot-test-freshness"


@pytest.mark.asyncio
async def test_metric_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/dg/metric?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["metrics"], list)


@pytest.mark.asyncio
async def test_metric_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT /spoke/dg/metric/{id}/attr/conf creates or replaces a metric definition."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Test Ingestion Freshness",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "ingestion-freshness"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["id"] == _TEST_METRIC_ID
    assert body["theme"] == "freshness"
    assert body["is_enabled"] is False

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates a field on an existing metric definition."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    # Create first
    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Test Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "ingestion-freshness"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"schedule_tier": "daily", "title": "Spot Test Metric Updated"},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["schedule_tier"] == "daily"
    assert body["title"] == "Spot Test Metric Updated"

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes metric definition; subsequent GET returns 404."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Delete Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "ingestion-freshness"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_metric_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /spoke/dg/metric/{id}/method/run executes synchronously and persists.

    The run is bounded to a single dataset via measurement_query.dataset_filter
    so the test does ~2 DataHub calls (resolve + measure) rather than enumerating
    every dataset in DataHub. That keeps the test honest — it exercises the real
    measurement path including breakdown computation, result row insertion, and
    METRIC.RUN_COMPLETE event emission — without depending on dev-env OpenSearch
    being fully warmed.
    """
    base_conf = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/method/run"
    bounded_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        "example_db.catalog.title_master,DEV)"
    )

    # Create metric config bounded to one URN to keep DataHub I/O small
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Run Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "ingestion-freshness",
                "dataset_filter": {"dataset_urns": [bounded_urn]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )

    assert run_resp.status_code == 200, run_resp.text
    body = run_resp.json()
    assert body.get("status") == "success"
    assert "run_id" in body

    # Persistence is the observable contract for non-dry runs:
    # attr/result must show ≥1 row after a real run.
    results_resp = await api_client.get(
        f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/result?offset=0&limit=5",
        headers=admin_headers,
    )
    assert results_resp.status_code == 200
    assert len(results_resp.json()["results"]) >= 1

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_run_dry_run_does_not_persist(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST .../method/run with dry_run=true returns a result but does not write."""
    metric_id = "spot-test-dry-run"
    base_conf = f"/api/v1/spoke/dg/metric/{metric_id}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{metric_id}/method/run"
    base_results = f"/api/v1/spoke/dg/metric/{metric_id}/attr/result"

    # Clean any state from prior sessions: DELETE cascades to metric_results
    # (see MetricsService.delete_metric_config). Ignore 404.
    await api_client.delete(base_conf, headers=admin_headers)

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Dry-Run Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "ingestion-freshness",
                "dataset_filter": {
                    "dataset_urns": [
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
                    ]
                },
            },
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json().get("status") == "success"

    # Dry-run must not persist a result row — attr/result stays empty.
    results_resp = await api_client.get(
        f"{base_results}?offset=0&limit=1",
        headers=admin_headers,
    )
    assert results_resp.status_code == 200
    assert results_resp.json()["results"] == []

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_result_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric/{id}/attr/result returns paginated timeseries envelope."""
    base_conf = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"
    base_results = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/result"

    # Create metric config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Result Metric",
            "theme": "freshness",
            "measurement_query": {"aggregation": "ingestion-freshness"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    resp = await api_client.get(base_results, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["results"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_event_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric/{id}/event returns paginated event list (may be empty)."""
    base_conf = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"
    base_events = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/event"

    # Create metric config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Event Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "ingestion-freshness"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    resp = await api_client.get(base_events, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)
