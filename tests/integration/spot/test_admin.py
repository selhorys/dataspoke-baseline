"""Spot tests for Admin endpoints.

Concerns covered:
- POST /admin/dags/verify — returns 200 with expected DAG list structure
- POST /admin/dags/verify — 403 FORBIDDEN when caller is not Admin role
- POST /internal/admin/datahub/sync — full sync returns sync result envelope
- POST /internal/admin/datahub/sync — targeted sync (single URN) returns sync result
- POST /internal/admin/datahub/sync — 401 when X-Internal-Token header is missing
"""

import os
import urllib.parse

import httpx
import pytest

_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")
_VAULT_NAME = "dataspoke-source-cred-spot-pg"
_VAULT_KEY = "password"


@pytest.mark.asyncio
async def test_admin_dags_verify(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /admin/dags/verify returns 200 with found/missing/total_expected.

    spec: API.md §Access Control — Admin role required for /admin/*.
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
async def test_admin_dags_verify_requires_admin_role(api_client: httpx.AsyncClient) -> None:
    """POST /admin/dags/verify returns 403 when caller is not Admin role.

    spec: API.md §Access Control — /admin/* requires users.role = 'Admin'.
    spec: feature/AUTH.md §Privilege Model — Admin row in the role × method matrix.
    """
    import uuid

    from src.backend.auth.tokens import issue_access_token

    # Unknown UUID → user not found in DB → 403 FORBIDDEN on /admin/* routes.
    # Wave F will replace this with a properly seeded non-Admin user.
    fake_id = uuid.UUID("ffffffff-0000-0000-0000-000000000010")
    non_admin_token, _ = issue_access_token(fake_id, "non-admin@example.com")
    non_admin_headers = {"Authorization": f"Bearer {non_admin_token}"}

    resp = await api_client.post(
        "/api/v1/admin/dags/verify",
        headers=non_admin_headers,
    )
    assert resp.status_code == 403, (
        f"Non-Admin caller must get 403 on /admin route per spec/API.md "
        f"§Access Control, got {resp.status_code}"
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

    Pre-seeds dataset_registry by creating an ACTIVE_CUSTOM_MANAGED source whose
    dataset mapping covers catalog.title_master. The sync sweep registers the URN in
    dataset_registry; sync_with_datahub then checks that URN against DataHub.

    spec: API.md §Internal Admin — POST /internal/admin/datahub/sync response shape.
    spec: BACKEND.md §Ingestion Service — dataset_registry populated by source creation.
    """
    test_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    pg_host_port = os.environ.get(
        "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT",
        "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
    )

    # Create a source that covers the catalog schema; the sync sweep populates dataset_registry.
    create_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": "admin-sync-targeted-test-source",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": pg_host_port,
                        "database": "example_db",
                        "username": "postgres",
                        "password": "${admin_test_pg__password}",
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert create_resp.status_code == 201, (
        f"Create source failed: {create_resp.status_code} {create_resp.text}"
    )
    source_id = create_resp.json()["id"]

    try:
        # Trigger ingestion sync to populate dataset_registry via the source mapping.
        await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
        )

        # Now run the targeted datahub sync.
        # Internal routes are mounted WITHOUT /api/v1 prefix (see main.py).
        resp = await api_client.post(
            "/internal/admin/datahub/sync",
            headers=internal_headers,
            json={"dataset_urns": [test_urn]},
        )
        assert resp.status_code == 200, (
            f"/internal/admin/datahub/sync expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "checked" in body
        # Targeted: checked should be >= 1 (the URN we submitted, if it is in dataset_registry)
        assert body["checked"] >= 1, (
            f"Expected checked >= 1 for the submitted URN; got checked={body['checked']}. "
            "spec: API.md §Internal Admin — POST /internal/admin/datahub/sync"
        )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{source_id}", headers=admin_headers
        )
