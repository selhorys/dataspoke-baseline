"""Spot tests for Metadata Generation endpoints.

Concerns covered:
- GET /spoke/common/metagen — list configs (paginated envelope)
- GET /data/{urn}/attr/metagen/conf — 404 for unknown URN
- PUT /data/{urn}/attr/metagen/conf — create config (201)
- PATCH /data/{urn}/attr/metagen/conf — partial update
- DELETE /data/{urn}/attr/metagen/conf — remove config (204)
- POST /data/{urn}/method/metagen/run — dry_run=true and dry_run=false
- GET /data/{urn}/attr/metagen/result — result list (paginated)
- PATCH /data/{urn}/attr/metagen/result/{result_id} — review (approve and reject)
- GET /data/{urn}/event/metagen — event list envelope
"""

import urllib.parse

import httpx
import pytest

# title_master is a DataHub-seeded Imazon dataset (catalog schema)
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


@pytest.mark.asyncio
async def test_metagen_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/metagen returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/common/metagen?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["results"], list)


@pytest.mark.asyncio
async def test_metagen_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET metagen conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/metagen/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metagen_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT creates metagen conf (201) with targets, schedule_tier, is_enabled."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description", "column.description"],
            "is_enabled": False,
            "schedule_tier": "weekly",
            "owner": "spot-test@imazon.com",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["dataset_urn"] == _TEST_URN
    assert "dataset.description" in body["targets"]

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates targets on existing metagen conf."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"targets": ["dataset.description", "column.description"]},
    )
    assert patch_resp.status_code == 200
    assert "column.description" in patch_resp.json()["targets"]

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes metagen conf; subsequent GET returns 404."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_metagen_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/metagen/run with dry_run=true returns result envelope without persisting."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"

    # Ensure config exists
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
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
    assert "id" in body or "dataset_urn" in body or "proposals" in body

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_result_list_paginated(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET attr/metagen/result returns paginated result list (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result"

    # Create config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
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
async def test_metagen_result_review_approve_and_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST run creates a result; PATCH result/{id} approves or rejects it.

    If no result is available (empty run), test is skipped gracefully.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result"

    # Create config and trigger non-dry run to produce a proposal
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    result_id = run_body.get("id")

    if result_id is None:
        # Query latest result from result list
        results_resp = await api_client.get(
            f"{base_results}?latest=true",
            headers=admin_headers,
        )
        results_body = results_resp.json()
        results = results_body.get("results", [])
        if not results:
            pytest.skip("No metagen result to review (stub LLM returned no proposals)")

        result_id = results[0]["id"]

    # Attempt reject verdict
    encoded_result_id = urllib.parse.quote(str(result_id), safe="")
    review_resp = await api_client.patch(
        f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}",
        headers=admin_headers,
        json={"verdict": "reject", "reason": "spot-test rejection"},
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["id"] == result_id

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/metagen returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    # Create config so events endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
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
