"""Spot integration test: /admin/users/* CRUD.

Concerns covered:
- GET /admin/users returns paginated list with role column
- PATCH /admin/users/{id} updates the display name
- PATCH /admin/users/{id}/role promotes Reader to Editor, new role reflected on next GET
- DELETE /admin/users/{id} removes the row; subsequent GET /auth/me with their token returns 401

spec: spec/feature/AUTH.md §Admin Surface
spec: spec/API.md §Admin /admin/users
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _unique_email(prefix: str = "admin-users") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _seed_user(session: AsyncSession, email: str) -> dict:
    """Seed a user directly in the DB and return {access_token, id}.

    Uses DB seeding + issue_access_token to avoid the /auth/register rate limit
    (5/min per IP) when multiple spot test files run together in the same minute.

    Seeds via google_sub (password_hash=NULL) — these tests never call POST /auth/token,
    so no password hash is needed. Satisfies the DB CHECK (password_hash IS NOT NULL
    OR google_sub IS NOT NULL).
    """
    from src.backend.auth.tokens import issue_access_token

    user_id = uuid.uuid4()
    google_sub = f"test-sub-{uuid.uuid4()}"
    await session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {"id": str(user_id), "email": email, "name": "Test User", "google_sub": google_sub},
    )
    await session.commit()
    token, _ = issue_access_token(user_id, email, session_epoch=0)
    return {"access_token": token, "id": str(user_id)}


@pytest.mark.asyncio
async def test_list_users_returns_pagination_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/users returns {users, total} with role and the auth-method booleans per row.

    The bootstrap admin is the positive `has_password` case: it is seeded with a
    password and carries no Google identity, so an implementation that hardcoded
    either boolean would fail here.

    spec: spec/feature/AUTH.md §Admin Surface — GET /admin/users: users.role returned per row.
    spec: spec/API.md §Admin — "content key `users: [{id, email, name, has_password,
    has_google, role, created_at, updated_at}]` — `role` from the DB column".
    spec: spec/feature/AUTH.md §Built-in Bootstrap Admin — login identifier
    `dataspoke@dataspoke.local`, initial password `dataspoke`, "The row carries no
    `google_sub`".
    """
    # Ascending by created_at: the bootstrap admin is seeded at install time, so it
    # is the oldest row and stays on the first page however many users other spot
    # modules have added since.
    resp = await api_client.get(
        "/api/v1/admin/users",
        params={"sort": "created_at_asc"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, (
        f"GET /admin/users must return 200, got {resp.status_code}: {resp.text}"
    )

    body = resp.json()
    assert "users" in body, "Response must include 'users' list"
    assert "total_count" in body, (
        "Response must include 'total_count' (standard pagination envelope)"
    )

    for user in body["users"]:
        assert "role" in user, (
            "Each user row must include 'role' per spec/feature/AUTH.md §Admin Surface"
        )
        assert "id" in user
        assert "email" in user
        assert "name" in user
        assert "has_password" in user, (
            "Each user row must include 'has_password' per spec/API.md §Admin"
        )
        assert "has_google" in user, (
            "Each user row must include 'has_google' per spec/API.md §Admin"
        )
        assert "password_hash" not in user, "password_hash must not appear in admin list"
        assert "google_sub" not in user, (
            "the sub is reduced to the has_google boolean per spec/API.md §Admin"
        )

    bootstrap = [u for u in body["users"] if u["email"] == "dataspoke@dataspoke.local"]
    assert bootstrap, (
        "the bootstrap admin must appear in the list — it is the row this test reads "
        "the positive has_password case from per spec/feature/AUTH.md §Built-in Bootstrap Admin"
    )
    assert bootstrap[0]["has_password"] is True, (
        "the bootstrap admin is seeded with a password per spec/feature/AUTH.md "
        f"§Built-in Bootstrap Admin; got {bootstrap[0]!r}"
    )
    assert bootstrap[0]["has_google"] is False, (
        "the bootstrap admin row carries no google_sub per spec/feature/AUTH.md "
        f"§Built-in Bootstrap Admin; got {bootstrap[0]!r}"
    )


@pytest.mark.asyncio
async def test_patch_user_name(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """PATCH /admin/users/{id} updates the display name.

    spec: spec/feature/AUTH.md §Admin Surface — PATCH /admin/users/{id}: update display name
    (email is immutable post-creation because the DataHub corpuser URN is immutable).
    """
    email = _unique_email("patch-name")
    user = await _seed_user(async_session, email)

    patch_resp = await api_client.patch(
        f"/api/v1/admin/users/{user['id']}",
        json={"name": "Admin Updated Name"},
        headers=admin_headers,
    )
    assert patch_resp.status_code == 200, (
        f"PATCH /admin/users/{user['id']} must return 200: {patch_resp.text}"
    )

    updated = patch_resp.json()
    assert updated["name"] == "Admin Updated Name", (
        "Name must be updated per spec/feature/AUTH.md §Admin Surface"
    )


@pytest.mark.asyncio
async def test_patch_user_role_reader_to_editor(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """PATCH /admin/users/{id}/role promotes Reader to Editor.

    spec: spec/feature/AUTH.md §Admin Surface — PATCH /admin/users/{id}/role writes DataSpoke first
    then propagates to DataHub via batchAssignRole.
    spec: spec/feature/AUTH.md §Privilege Model — role changes take effect on the next request.
    """
    email = _unique_email("role-promote")
    user = await _seed_user(async_session, email)

    # Initially Reader
    me_before = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    assert me_before.json()["role"] == "Reader"

    # Promote to Editor
    role_resp = await api_client.patch(
        f"/api/v1/admin/users/{user['id']}/role",
        json={"role": "Editor"},
        headers=admin_headers,
    )
    assert role_resp.status_code == 200, (
        f"PATCH /admin/users/{user['id']}/role must return 200: {role_resp.text}"
    )
    assert role_resp.json()["role"] == "Editor"

    # Verify new role takes effect on next request
    me_after = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    assert me_after.json()["role"] == "Editor", (
        "Role change must take effect on the next request per spec/feature/AUTH.md §Privilege Model"
    )


@pytest.mark.asyncio
async def test_delete_user_removes_row(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """DELETE /admin/users/{id} removes the row; their token returns 401 on next use.

    spec: spec/feature/AUTH.md §Lifecycle §Deletion — hard delete; deleted subject is an
    authentication failure (401 UNAUTHORIZED), not an authorization failure.
    spec: spec/feature/AUTH.md §Projection retraction sequence.
    """
    email = _unique_email("delete-user")
    user = await _seed_user(async_session, email)

    # Verify user exists
    me_before = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    assert me_before.status_code == 200

    # Delete the user
    del_resp = await api_client.delete(
        f"/api/v1/admin/users/{user['id']}",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204, (
        f"DELETE /admin/users/{user['id']} must return 204: {del_resp.text}"
    )

    # A still-valid access token whose subject was deleted must return 401 UNAUTHORIZED.
    # spec: spec/feature/AUTH.md §Lifecycle §Deletion — "A still-valid access token
    # whose subject was deleted fails with 401 UNAUTHORIZED ... A deleted subject is
    # an authentication failure — the client must re-authenticate, not an authorization
    # failure."
    me_after = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    assert me_after.status_code == 401, (
        f"Deleted user's token must return exactly 401 UNAUTHORIZED "
        f"per spec/feature/AUTH.md §Lifecycle §Deletion (deleted subject = "
        f"authentication failure, not authorization failure); got {me_after.status_code}"
    )

    # Email must be immediately reusable
    rereg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Re-registered", "password": "password1234"},
    )
    assert rereg.status_code == 201, (
        f"Email must be immediately reusable after deletion "
        f"per spec/feature/AUTH.md §Lifecycle §Deletion, got {rereg.status_code}: {rereg.text}"
    )
