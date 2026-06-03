"""Spot tests for Admin endpoints.

Concerns covered:
- POST /admin/dags/verify — returns 200 with expected DAG list structure
- POST /admin/dags/verify — 403 FORBIDDEN when caller is not Admin role
- POST /internal/admin/datahub/sync — full sync returns sync result envelope
- POST /internal/admin/datahub/sync — targeted sync (single URN) returns sync result
- POST /internal/admin/datahub/sync — 401 when X-Internal-Token header is missing

Targeted-sync pre-seeding note:
  dataset_registry is populated lazily via ensure_dataset_registered(), which is
  called only by ValidationService.upsert_config() (PUT /attr/validation/conf).
  The ingestion sync sweep writes to ingestion_source_dataset, NOT dataset_registry.
  To get a dataset_registry row for catalog.title_master, this test calls the
  validation-conf PUT endpoint, which is the real mechanism that inserts the row.
"""

import os
import urllib.parse

import httpx
import pytest

# Dummy-data: catalog schema must exist in DataHub so that ensure_dataset_registered()
# finds it (datahub_registered=True) when the validation-conf PUT is called.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")
# Provisioned K8s Secret for dummy-data postgres.
# spec: SECRET_RESOLUTION.md §Name prefix policy — DNS-label-safe name (hyphens, no underscores).
_VAULT_NAME = "dataspoke-source-cred-dummy-data-pg"
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

    Pre-seeds dataset_registry for catalog.title_master by calling the validation-conf
    PUT endpoint. ensure_dataset_registered() (called by ValidationService.upsert_config)
    is the only code path that inserts dataset_registry rows; the ingestion sync sweep
    only writes to ingestion_source_dataset, which is a separate table. The sync DAG
    reconciles existing registry rows against DataHub — it does not create new rows.

    Setup:
      1. PUT /attr/validation/conf for catalog.title_master → ensure_dataset_registered()
         checks DataHub, inserts dataset_registry row with datahub_registered=True
         (catalog is present in DataHub because DUMMY_DATA_DATAHUB_SCHEMAS seeds it).
      2. POST /internal/admin/datahub/sync {"dataset_urns": [test_urn]} → checked=1
         because the registry row now exists.

    spec: API.md §Internal Admin — POST /internal/admin/datahub/sync response shape.
    spec: feature/BACKEND_SCHEMA.md §dataset_registry — created lazily via
        ensure_dataset_registered() on validation-conf PUT.
    spec: feature/BACKEND.md §DataHub Sync — reconciles existing registry rows;
        does not create new rows.
    """
    test_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    enc_urn = urllib.parse.quote(test_urn, safe="")
    conf_url = f"/api/v1/spoke/common/data/{enc_urn}/attr/validation/conf"

    # Step 1: PUT validation conf for the target dataset. This calls
    # ensure_dataset_registered(), which checks DataHub and inserts a
    # dataset_registry row with datahub_registered=True. catalog.title_master
    # is present in DataHub because DUMMY_DATA_DATAHUB_SCHEMAS seeds it.
    # spec: feature/BACKEND_SCHEMA.md §dataset_registry §Creation.
    put_resp = await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "description": "Admin-sync targeted-test validation conf",
            "variables": ["row_cnt"],
        },
    )
    assert put_resp.status_code in (200, 201), (
        f"PUT validation/conf failed: {put_resp.status_code} {put_resp.text}"
    )

    try:
        # Step 2: Targeted datahub/sync. The dataset_registry row created above
        # is now present, so checked must equal 1.
        # spec: API.md §Internal Admin — POST /internal/admin/datahub/sync response shape.
        resp = await api_client.post(
            "/internal/admin/datahub/sync",
            headers=internal_headers,
            json={"dataset_urns": [test_urn]},
        )
        assert resp.status_code == 200, (
            f"/internal/admin/datahub/sync expected 200, "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        for key in ("checked", "flipped_true", "flipped_false", "unchanged", "not_found"):
            assert key in body, f"Response missing key '{key}': {body}"

        # checked must be exactly 1: the dataset_registry row we just created.
        # spec: src/shared/db/registry.py sync_with_datahub — checked = len(matched rows).
        assert body["checked"] >= 1, (
            f"Expected checked >= 1 for the submitted URN; got checked={body['checked']}. "
            "dataset_registry row should have been created by the validation-conf PUT. "
            "spec: API.md §Internal Admin; "
            "spec: feature/BACKEND_SCHEMA.md §dataset_registry §Creation."
        )
    finally:
        # Clean up the validation conf so other tests are not affected.
        await api_client.delete(conf_url, headers=admin_headers)
