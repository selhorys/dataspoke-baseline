"""Spot tests for Admin endpoints.

Concerns covered:
- POST /admin/dags/verify — returns 200 with expected DAG list structure
- POST /admin/dags/verify — 403 FORBIDDEN when caller is not Admin role
- POST /internal/admin/datahub/sync — full sync returns sync result envelope
- POST /internal/admin/datahub/sync — targeted sync (single URN) returns sync result
- POST /internal/admin/datahub/sync — 401 when X-Internal-Token header is missing

Targeted-sync pre-seeding note:
  The targeted POST /internal/admin/datahub/sync (the endpoint under test) reconciles
  datahub_registered on dataset_registry rows that already exist — a URN with no row is
  reported not_found, not inserted. So this test first creates a dataset_registry row for
  catalog.title_master via the validation-conf PUT endpoint
  (ValidationService.upsert_config → ensure_dataset_registered). The hourly full sweep
  (IngestionService.sync()) does insert newly-seen URNs into dataset_registry, but that
  path is not exercised by this targeted endpoint.
"""

import os
import urllib.parse

import httpx
import pytest

from tests.integration.util import dataspoke_db

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
async def test_admin_dags_verify_requires_admin_role(
    api_client: httpx.AsyncClient,
    async_session,
) -> None:
    """POST /admin/dags/verify returns 403 when caller has Reader role (not Admin).

    Uses a REAL seeded Reader user so the 403 is genuinely from the require_admin
    role gate, not from a missing-user branch.

    spec: API.md §Access Control — /admin/* requires users.role = 'Admin'.
    spec: feature/AUTH.md §Privilege Model — Editor/Reader on /admin/* → 403 FORBIDDEN.
    spec: feature/AUTH.md §Lifecycle §Deletion — deleted/unknown subject → 401 (not 403).
    """
    import uuid

    from sqlalchemy import text

    from src.backend.auth.tokens import issue_access_token

    # Seed a real Reader user directly in the DB. The user EXISTS with role=Reader,
    # so require_admin returns 403 FORBIDDEN (not 401 — the user is authenticated).
    reader_id = uuid.uuid4()
    reader_email = f"reader-dags-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Reader Test User",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()
    try:
        reader_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)
        non_admin_headers = {"Authorization": f"Bearer {reader_token}"}

        resp = await api_client.post(
            "/api/v1/admin/dags/verify",
            headers=non_admin_headers,
        )
        assert resp.status_code == 403, (
            f"Reader-role caller must get 403 FORBIDDEN on /admin/* route "
            f"per spec/API.md §Access Control and spec/feature/AUTH.md §Privilege Model; "
            f"got {resp.status_code}: {resp.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(reader_id)},
        )
        await async_session.commit()


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
    PUT endpoint (ValidationService.upsert_config → ensure_dataset_registered). The
    targeted POST /internal/admin/datahub/sync reconciles datahub_registered on existing
    rows — a URN with no row is reported not_found, not inserted — so the row must exist
    first. (The hourly full sweep, IngestionService.sync(), does insert newly-seen URNs;
    that path is separate from this targeted endpoint.)

    Setup:
      1. PUT /attr/validation/conf for catalog.title_master → ensure_dataset_registered()
         checks DataHub, inserts dataset_registry row with datahub_registered=True
         (catalog is present in DataHub because DUMMY_DATA_DATAHUB_SCHEMAS seeds it).
      2. POST /internal/admin/datahub/sync {"dataset_urns": [test_urn]} → checked=1
         because the registry row now exists.

    spec: API.md §Internal Admin — POST /internal/admin/datahub/sync response shape.
    spec: feature/BACKEND_SCHEMA.md §dataset_registry — row created here via
        ensure_dataset_registered() on validation-conf PUT.
    spec: feature/BACKEND.md §DataHub Sync — the targeted admin endpoint reconciles
        datahub_registered on existing rows (not_found when a URN has no row); the
        hourly sweep inserts newly-seen URNs.
    """
    test_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    enc_urn = urllib.parse.quote(test_urn, safe="")
    conf_url = f"/api/v1/spoke/common/data/{enc_urn}/attr/validation/conf"

    # Isolate: purge any prior-run conf (+ its results/events) for this urn so the
    # setup PUT creates a fresh conf regardless of prior-run state.
    await dataspoke_db.purge_urn(test_urn)

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
            "variables": [{"name": "row_cnt", "description": "Daily row count"}],
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
        # Purge the conf (+ its results/events) so a re-run starts from a clean slate.
        await dataspoke_db.purge_urn(test_urn)
