"""Spot tests for Validation endpoints.

Concerns covered:
- GET /spoke/common/validation — list configs (paginated envelope)
- GET /data/{urn}/attr/validation/conf — 404 for unknown URN
- PUT /data/{urn}/attr/validation/conf — create with one of each rule type
- PATCH /data/{urn}/attr/validation/conf — partial update
- DELETE /data/{urn}/attr/validation/conf — remove config (204)
- POST /data/{urn}/method/validation/run — dry_run=true and dry_run=false
- GET /data/{urn}/attr/validation/result — result list envelope
- GET /data/{urn}/event/validation — event list envelope
"""

import urllib.parse

import httpx
import pytest

# title_master is a DataHub-seeded Imazon dataset (catalog schema)
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


@pytest.mark.asyncio
async def test_validation_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/validation returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/common/validation?offset=0&limit=10",
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
async def test_validation_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET validation conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/validation/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_conf_put_with_all_rule_types(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT validation conf with one rule of each supported type — 201 created."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-fresh-001",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                    "last_modified_field": "updated_at",
                },
                {
                    "rule_id": "spot-vol-001",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                },
                {
                    "rule_id": "spot-field-001",
                    "type": "field",
                    "field": "title",
                    "metric": "null_count",
                    "condition": {"type": "less_than_or_equal_to", "value": 0},
                },
                {
                    "rule_id": "spot-schema-001",
                    "type": "schema",
                    "fields": [{"field": "isbn", "type": "VARCHAR"}],
                    "compatibility": "superset",
                },
                {
                    "rule_id": "spot-sql-001",
                    "type": "sql",
                    "statement": "SELECT count(*) FROM catalog.title_master WHERE isbn IS NULL",
                    "condition": {"type": "less_than_or_equal_to", "value": 5},
                },
                {
                    "rule_id": "spot-custom-001",
                    "type": "custom",
                    "subtype": "sql_timeseries",
                    "sql": "SELECT count(*) FROM catalog.title_master",
                },
            ],
            "schedule_tier": "daily",
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["dataset_urn"] == _TEST_URN
    assert len(body["rules"]) == 6

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates schedule_tier and is_enabled on existing validation conf."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    # Create first
    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-patch-fresh",
                    "type": "freshness",
                    "lookback_interval": "48 hours",
                    "last_modified_field": "updated_at",
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"is_enabled": False, "schedule_tier": "weekly"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["schedule_tier"] == "weekly"

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes validation conf; subsequent GET returns 404."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-del-fresh",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                    "last_modified_field": "updated_at",
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/validation/run with dry_run=true returns run envelope without persisting."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"

    # Ensure config exists
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-dryrun-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )

    assert run_resp.status_code == 200
    body = run_resp.json()
    assert "run_id" in body
    assert "status" in body
    assert "total" in body
    assert "passed" in body
    assert "failed" in body
    assert "errored" in body

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_result_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET attr/validation/result returns paginated result envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    # Create config so results endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-result-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
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
async def test_validation_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/validation returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/validation"

    # Create config so events endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-events-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
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
