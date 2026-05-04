"""Spot tests for Admin endpoints.

Concerns covered:
- POST /admin/dags/verify — returns 200 with expected DAG list structure
- POST /admin/dags/verify — 403 FORBIDDEN when token lacks 'admin' group
- POST /internal/admin/datahub/sync — full sync returns sync result envelope
- POST /internal/admin/datahub/sync — targeted sync (single URN) returns sync result
- POST /internal/admin/datahub/sync — 401 when X-Internal-Token header is missing
"""

import os
import urllib.parse

import httpx
import pytest

_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")
_VAULT_NAME = "dataspoke-source-cred-spot-pg"
_VAULT_KEY = "password"


@pytest.mark.asyncio
async def test_admin_dags_verify(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /admin/dags/verify returns 200 with found/missing/total_expected.

    spec: API.md §Admin — /admin routes require 'admin' group claim.
    spec: API.md §Internal Admin — POST /admin/dags/verify returns {found, missing, total_expected}.
    """
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
    # spec: API.md §Internal Admin — {found, missing, total_expected} structural invariant:
    # total_expected == len(found) + len(missing) (every expected DAG is either found or missing)
    assert body["total_expected"] == len(body["found"]) + len(body["missing"]), (
        f"total_expected ({body['total_expected']}) must equal "
        f"len(found) ({len(body['found'])}) + len(missing) ({len(body['missing'])})"
    )


@pytest.mark.asyncio
async def test_admin_dags_verify_requires_admin_group(api_client: httpx.AsyncClient) -> None:
    """POST /admin/dags/verify returns 403 when token has no 'admin' group.

    spec: API.md §Group-to-Route Access Control — /admin/* requires 'admin' group exclusively.
    spec: API.md §Admin Role — admin routes require the 'admin' claim exclusively.
    """
    from src.api.auth.jwt import create_access_token

    # Mint a real signed token with only 'dg' group — no 'admin' claim
    dg_only_token, _ = create_access_token(
        subject="dg-only-user",
        groups=["dg"],
        email="dg-user@example.com",
    )
    dg_only_headers = {"Authorization": f"Bearer {dg_only_token}"}

    resp = await api_client.post(
        "/api/v1/admin/dags/verify",
        headers=dg_only_headers,
    )
    assert resp.status_code == 403, (
        f"A 'dg'-only token must get 403 on /admin route per spec/API.md "
        f"§Group-to-Route Access Control, got {resp.status_code}"
    )


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
async def test_internal_admin_datahub_sync_missing_token_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /internal/admin/datahub/sync without X-Internal-Token header returns 401.

    spec: API.md §Internal Admin — internal routes gated by X-Internal-Token shared-secret header.
    spec: API.md §Application Error Codes — UNAUTHORIZED (401) for missing/wrong auth.
    Omitting the header entirely (not a wrong value) must still produce 401, not 403.
    """
    resp = await api_client.post(
        "/internal/admin/datahub/sync",
        # No X-Internal-Token header — must be rejected
        json={},
    )
    assert resp.status_code == 401, (
        f"Missing X-Internal-Token must return 401 per spec/API.md §Internal Admin, "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_internal_admin_datahub_sync_full(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/admin/datahub/sync with no body performs a full sync.

    spec: API.md §Internal Admin — POST /internal/admin/datahub/sync returns
    {checked, flipped_true, flipped_false, unchanged, not_found}.
    """
    # Internal routes are mounted WITHOUT /api/v1 prefix (see main.py)
    resp = await api_client.post(
        "/internal/admin/datahub/sync",
        headers=internal_headers,
        json={},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: API.md §Internal Admin — response shape
    assert "checked" in body
    assert "flipped_true" in body
    assert "flipped_false" in body
    assert "unchanged" in body
    assert "not_found" in body
    assert body["checked"] >= 0


@pytest.mark.asyncio
async def test_internal_admin_datahub_sync_targeted(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/admin/datahub/sync with dataset_urns performs a targeted sync.

    Pre-seeds dataset_registry by upserting an ingestion conf for the URN — this is
    the natural flow that populates the registry (BACKEND.md §Ingestion Service
    "Config upsert registers the dataset URN in dataset_registry"). Without seeding,
    sync_with_datahub counts the URN as not_found rather than checked.
    """
    test_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    encoded_urn = urllib.parse.quote(test_urn, safe="")
    conf_path = f"/api/v1/spoke/common/data/{encoded_urn}/attr/ingestion/conf"

    pg_host = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
    pg_port = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
    pg_db = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")

    # Seed registry via ingestion conf upsert (calls ensure_dataset_registered).
    put_resp = await api_client.put(
        conf_path,
        headers=admin_headers,
        json={
            "mode": "active",
            "platform": "postgres",
            "locator": {"host": pg_host, "port": pg_port},
            "identifier": {"database": pg_db, "schema_name": "catalog", "table": "title_master"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": _VAULT_NAME,
                    "key": _VAULT_KEY,
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
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
    finally:
        await api_client.delete(conf_path, headers=admin_headers)
