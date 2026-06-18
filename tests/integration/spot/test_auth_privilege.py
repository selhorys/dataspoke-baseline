"""Spot integration test: role × method privilege matrix.

Concerns covered:
- Reader + POST on /spoke/* → 403 READ_ONLY_ROLE
- Reader + GET on /spoke/* → 200 (allowed)
- Editor + POST on /spoke/* → allowed (not 403)
- Reader accessing /admin/* → 403 FORBIDDEN
- No-auth → 401 UNAUTHORIZED

spec: spec/feature/AUTH.md §Privilege Model
spec: spec/API.md §Authentication — method × role gate
"""

import uuid

import httpx
import pytest
import pytest_asyncio

# Reader-accessible GET on /spoke/* — returns 200 for any authenticated user.
# The per-source overhaul replaced the bare /spoke/ingestion route; use the list endpoint.
# spec: API.md §Ingestion — GET /spoke/ingestion/sources lists all sources (Reader allowed)
_SPOKE_COMMON_GET_URL = "/api/v1/spoke/ingestion/sources"
# Writer-gated POST on /spoke/* for the role-gate check. The singleton metagen run
# route became a conf collection; POST /spoke/metagen/conf (create) requires writer
# (require_writer runs before body validation, so a Reader is rejected 403 before 422).
# spec: API.md §Metadata Generation — POST /spoke/metagen/conf (Editor/Admin)
_SPOKE_COMMON_POST_URL = "/api/v1/spoke/metagen/conf"
_ADMIN_URL = "/api/v1/admin/users"


def _unique_email(prefix: str = "priv") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


# Module-scoped fixtures: seed users directly into the DB to avoid hitting the
# /auth/register rate limit (5/min per IP) when multiple spot modules run together.


@pytest_asyncio.fixture(scope="module")
async def reader_token(integration_db_url: str) -> str:
    """Seed a Reader user directly in the DB and return a JWT token.

    Uses DB seeding instead of /auth/register to avoid rate-limit exhaustion
    when all spot test modules run together in the same minute window.

    Seeds via google_sub (password_hash=NULL) — these tests never call POST /auth/token,
    so no password hash is needed.
    """
    from sqlalchemy import pool as sa_pool, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("reader-mod")
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
                {"id": str(user_id), "email": email, "name": "Module Reader", "google_sub": google_sub},
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


@pytest_asyncio.fixture(scope="module")
async def editor_token(integration_db_url: str, admin_headers: dict[str, str]) -> str:
    """Seed an Editor user directly in the DB and return a JWT token.

    Uses DB seeding instead of /auth/register to avoid rate-limit exhaustion.
    Role is set to Editor directly in the INSERT (no admin PATCH needed).

    Seeds via google_sub (password_hash=NULL) — these tests never call POST /auth/token,
    so no password hash is needed.
    """
    from sqlalchemy import pool as sa_pool, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("editor-mod")
    user_id = uuid.uuid4()
    google_sub = f"test-sub-{uuid.uuid4()}"

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
                    " VALUES (:id, :email, :name, :google_sub, 'Editor')"
                ),
                {"id": str(user_id), "email": email, "name": "Module Editor", "google_sub": google_sub},
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
async def test_reader_get_spoke_common_allowed(
    api_client: httpx.AsyncClient,
    reader_token: str,
) -> None:
    """Reader + GET on /spoke/* → allowed (200).

    spec: spec/feature/AUTH.md §Privilege Model —
    Reader on /spoke/* and /hub/*: GET/HEAD/OPTIONS only → allowed.
    """
    resp = await api_client.get(
        _SPOKE_COMMON_GET_URL,
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert resp.status_code == 200, (
        f"Reader GET on /spoke/* must be allowed (200) per spec/feature/AUTH.md §Privilege Model, "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_reader_post_spoke_common_returns_403(
    api_client: httpx.AsyncClient,
    reader_token: str,
) -> None:
    """Reader + POST on /spoke/* → 403 READ_ONLY_ROLE.

    spec: spec/feature/AUTH.md §Privilege Model — Reader role on /spoke/* or /hub/*
    POST/PUT/PATCH/DELETE → 403 READ_ONLY_ROLE.
    spec: spec/API.md §Authentication — method × role gate.
    """
    # POST an endpoint that requires writer — even if the body is invalid, auth check comes first
    resp = await api_client.post(
        _SPOKE_COMMON_POST_URL,
        json={},
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert resp.status_code == 403, (
        f"Reader POST on /spoke/* must return 403 READ_ONLY_ROLE "
        f"per spec/feature/AUTH.md §Privilege Model, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "READ_ONLY_ROLE", (
        "Error code must be READ_ONLY_ROLE per spec/feature/AUTH.md §Privilege Model"
    )


@pytest.mark.asyncio
async def test_reader_admin_access_returns_403(
    api_client: httpx.AsyncClient,
    reader_token: str,
) -> None:
    """Reader accessing /admin/* → 403 FORBIDDEN.

    spec: spec/feature/AUTH.md §Privilege Model — Editor or Reader on /admin/* → 403 FORBIDDEN.
    spec: spec/API.md §Authentication — /admin/* requires users.role='Admin'.
    """
    resp = await api_client.get(
        _ADMIN_URL,
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert resp.status_code == 403, (
        f"Reader on /admin/* must return 403 per spec/feature/AUTH.md §Privilege Model, "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_editor_post_spoke_common_allowed(
    api_client: httpx.AsyncClient,
    editor_token: str,
) -> None:
    """Editor + POST on /spoke/* is allowed (not 403).

    spec: spec/feature/AUTH.md §Privilege Model — Editor can use all methods on /spoke/* and /hub/*.
    """
    resp = await api_client.post(
        _SPOKE_COMMON_POST_URL,
        json={},
        headers={"Authorization": f"Bearer {editor_token}"},
    )
    # Editor can post — may get 400/409/422 from business logic but NOT 403
    assert resp.status_code != 403, (
        f"Editor POST on /spoke/* must NOT be 403 (READ_ONLY_ROLE) "
        f"per spec/feature/AUTH.md §Privilege Model, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_no_auth_returns_401(
    api_client: httpx.AsyncClient,
) -> None:
    """Request with no Authorization header → 401 UNAUTHORIZED.

    spec: spec/feature/AUTH.md §Privilege Model — unauthenticated requests rejected.
    """
    resp = await api_client.get(_SPOKE_COMMON_GET_URL)
    assert resp.status_code == 401, (
        f"No-auth request must return 401 per spec/feature/AUTH.md §Privilege Model, "
        f"got {resp.status_code}"
    )
