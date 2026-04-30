"""Spot tests for Ingestion Control endpoints.

Concerns covered:
- GET /spoke/common/ingestion — list configs (paginated envelope)
- GET /data/{urn}/attr/ingestion/conf — 404 for unknown URN
- PUT /data/{urn}/attr/ingestion/conf — create config (201)
- PATCH /data/{urn}/attr/ingestion/conf — partial update
- DELETE /data/{urn}/attr/ingestion/conf — remove config (204)
- POST /data/{urn}/method/ingestion/run — dry_run=true triggers without writing
- GET /data/{urn}/event/ingestion — event list returns paginated envelope
"""

import urllib.parse

import httpx
import pytest

# Use a fixed test URN that we know won't conflict with Imazon seed data.
# This dataset is registered in DataHub during the module's DUMMY_DATA_DATAHUB_SCHEMAS reset.
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


@pytest.mark.asyncio
async def test_ingestion_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ingestion returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ingestion?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "configs" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["configs"], list)


@pytest.mark.asyncio
async def test_ingestion_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET ingestion conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/ingestion/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_conf_put_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT creates ingestion config (201), PATCH updates it, DELETE removes it (204)."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    # PUT — create
    put_resp = await api_client.put(
        base,
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
            "is_enabled": False,
            "schedule_tier": None,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    put_body = put_resp.json()
    assert put_body["dataset_urn"] == _TEST_URN
    assert put_body["platform"] == "postgres"
    assert put_body["mode"] == "active"

    # PATCH — disable (partial update)
    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"is_enabled": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["is_enabled"] is False

    # DELETE — remove
    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    # Verify gone — subsequent GET returns 404
    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/ingestion/run with dry_run=true returns run envelope without writing."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/ingestion/run"

    # Ensure config exists before run
    await api_client.put(
        base_conf,
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
            "is_enabled": False,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )

    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert "run_id" in run_body
    assert "status" in run_body

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/ingestion returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

    # Create config so events endpoint is accessible
    await api_client.put(
        base_conf,
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
            "is_enabled": False,
        },
    )

    events_resp = await api_client.get(base_events, headers=admin_headers)
    assert events_resp.status_code == 200
    body = events_resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)
