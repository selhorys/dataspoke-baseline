"""Spot tests for Admin endpoints.

Concerns covered:
- POST /admin/dags/verify — returns 200 with expected DAG list structure
- POST /internal/admin/datahub/sync — full sync returns sync result envelope
- POST /internal/admin/datahub/sync — targeted sync (single URN) returns sync result
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_admin_dags_verify(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /admin/dags/verify returns 200 with found/missing/total_expected."""
    resp = await api_client.post(
        "/api/v1/admin/dags/verify",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "found" in body
    assert "missing" in body
    assert "total_expected" in body
    assert isinstance(body["found"], list)
    assert isinstance(body["missing"], list)
    assert isinstance(body["total_expected"], int)
    assert body["total_expected"] > 0


@pytest.mark.asyncio
async def test_admin_dags_verify_all_found(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /admin/dags/verify — all expected DAGs must be registered (none missing)."""
    resp = await api_client.post(
        "/api/v1/admin/dags/verify",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    missing = body.get("missing", [])
    assert missing == [], f"Missing DAGs: {missing}"


@pytest.mark.asyncio
async def test_internal_admin_datahub_sync_full(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/admin/datahub/sync with no body performs a full sync."""
    # Internal routes are mounted WITHOUT /api/v1 prefix (see main.py)
    resp = await api_client.post(
        "/internal/admin/datahub/sync",
        headers=internal_headers,
        json={},
    )

    assert resp.status_code == 200
    body = resp.json()
    # Spec: returns {checked, flipped_true, flipped_false, unchanged, not_found}
    assert "checked" in body
    assert "flipped_true" in body
    assert "flipped_false" in body
    assert "unchanged" in body
    assert "not_found" in body
    assert body["checked"] >= 0


@pytest.mark.asyncio
async def test_internal_admin_datahub_sync_targeted(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/admin/datahub/sync with dataset_urns performs a targeted sync."""
    test_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

    # Internal routes are mounted WITHOUT /api/v1 prefix (see main.py)
    resp = await api_client.post(
        "/internal/admin/datahub/sync",
        headers=internal_headers,
        json={"dataset_urns": [test_urn]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "checked" in body
    # Targeted: checked should be 1 (the URN we submitted)
    assert body["checked"] >= 1
