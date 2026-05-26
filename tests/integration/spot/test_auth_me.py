"""Spot integration test: GET /auth/me and PATCH /auth/me.

Concerns covered:
- Login as bootstrap admin (dataspoke/dataspoke) succeeds
- GET /auth/me returns the right shape (no password_hash); role=Admin for bootstrap admin
- PATCH /auth/me with new name works (name updated, DataHub propagated best-effort)
- PATCH /auth/me with new password works (new password accepted on next login)

spec: spec/feature/AUTH.md §Lifecycle §Profile read & update
spec: spec/API.md §Auth GET /auth/me, PATCH /auth/me
"""

import uuid

import httpx
import pytest
import pytest_asyncio


def _unique_email(prefix: str = "me-test") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


# Module-scoped shared user: seed directly in DB to avoid the /auth/register
# rate limit (5/min per IP) when multiple spot modules run together.
@pytest_asyncio.fixture(scope="module")
async def me_user_token(integration_db_url: str) -> str:
    """Seed a user directly in DB and return a JWT token for GET/PATCH /auth/me tests.

    Uses DB seeding instead of /auth/register to avoid rate-limit exhaustion.
    Seeds via google_sub (password_hash=NULL) — these tests never call POST /auth/token.
    """
    from sqlalchemy import pool as sa_pool, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("me-mod")
    user_id = uuid.uuid4()
    google_sub = f"test-sub-{uuid.uuid4()}"

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
                    " VALUES (:id, :email, :name, :google_sub, 'Reader')"
                ),
                {"id": str(user_id), "email": email, "name": "Original Name", "google_sub": google_sub},
            )
    finally:
        await engine.dispose()

    token, _ = issue_access_token(user_id, email)

    yield token

    # Cleanup
    engine2 = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine2.begin() as conn:
            await conn.execute(
                text("DELETE FROM dataspoke.users WHERE id = :id"),
                {"id": str(user_id)},
            )
    finally:
        await engine2.dispose()


@pytest.mark.asyncio
async def test_get_me_bootstrap_admin_shape(
    api_client: httpx.AsyncClient,
    admin_token: str,
) -> None:
    """GET /auth/me returns correct shape for bootstrap admin.

    spec: spec/API.md §Auth GET /auth/me — returns {id, email, name, has_google, role, created_at, updated_at};
    password_hash is never returned.
    spec: spec/feature/AUTH.md §Identity Model — DataSpoke is the SSOT for user identity.
    """
    resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, f"GET /auth/me must return 200, got {resp.status_code}: {resp.text}"

    me = resp.json()

    # spec: spec/API.md §Auth GET /auth/me — required fields
    required_fields = {"id", "email", "name", "has_google", "role", "created_at", "updated_at"}
    for field in required_fields:
        assert field in me, f"GET /auth/me response must include '{field}' per spec/API.md §Auth"

    # spec: spec/API.md §Auth GET /auth/me — password_hash is never returned
    assert "password_hash" not in me, (
        "password_hash must NEVER appear in GET /auth/me response per spec/API.md §Auth"
    )

    # Bootstrap admin has role=Admin
    assert me["role"] == "Admin", (
        "Bootstrap admin (dataspoke) must have Admin role per plan §Bootstrap"
    )


@pytest.mark.asyncio
async def test_get_me_no_token_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/me without token returns 401 UNAUTHORIZED.

    spec: spec/feature/AUTH.md §Privilege Model — authenticated routes require a valid token.
    """
    resp = await api_client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_me_name_update(
    api_client: httpx.AsyncClient,
    me_user_token: str,
) -> None:
    """PATCH /auth/me with name updates the display name.

    spec: spec/feature/AUTH.md §Lifecycle §Profile read & update — PATCH /auth/me accepts
    {name?, password?}; name updates write to DataSpoke and propagate to DataHub.
    spec: spec/API.md §Auth PATCH /auth/me — returns updated profile.
    """
    # Patch name using the module-scoped user
    patch_resp = await api_client.patch(
        "/api/v1/auth/me",
        json={"name": "Updated Name"},
        headers={"Authorization": f"Bearer {me_user_token}"},
    )
    assert patch_resp.status_code == 200, (
        f"PATCH /auth/me with new name must return 200 per spec/API.md §Auth, "
        f"got {patch_resp.status_code}: {patch_resp.text}"
    )

    updated = patch_resp.json()
    assert updated["name"] == "Updated Name", (
        "PATCH /auth/me must return the updated name per spec/API.md §Auth PATCH /auth/me"
    )
    assert "password_hash" not in updated


@pytest.mark.asyncio
async def test_patch_me_password_update_allows_new_login(
    api_client: httpx.AsyncClient,
    async_session,
) -> None:
    """PATCH /auth/me password update: new password accepted on next login.

    spec: spec/feature/AUTH.md §Lifecycle §Profile read & update — password updates
    rewrite users.password_hash only; do not touch DataHub.

    Uses a DB-seeded user (via async_session) to bypass the /auth/register
    rate limit (5/min per IP) without bypassing the login endpoint.
    Uses create_user to produce the password hash — the test never sees the hash.
    """
    from sqlalchemy import text
    from src.backend.auth import users as user_service

    email = _unique_email("patch-pw")
    # Use create_user so the password hash protocol stays inside the impl.
    user = await user_service.create_user(
        async_session, email, "PW Update User", password="oldpassword123"
    )
    await async_session.commit()
    user_id = str(user.id)

    try:
        # Login with initial password via the API
        login_resp = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "oldpassword123"},
        )
        assert login_resp.status_code == 200, f"Initial login failed: {login_resp.text}"
        access_token = login_resp.json()["access_token"]

        # Update password via PATCH /auth/me
        patch_resp = await api_client.patch(
            "/api/v1/auth/me",
            json={"password": "newpassword567"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert patch_resp.status_code == 200

        # Old password should no longer work
        old_login = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "oldpassword123"},
        )
        assert old_login.status_code == 401, "Old password must be rejected after update"

        # New password must work
        new_login = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "newpassword567"},
        )
        assert new_login.status_code == 200, (
            f"New password must be accepted after PATCH /auth/me per spec/feature/AUTH.md §Lifecycle, "
            f"got {new_login.status_code}"
        )
        assert "access_token" in new_login.json()
    finally:
        # Cleanup: remove the test user
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": user_id},
        )
        await async_session.commit()
