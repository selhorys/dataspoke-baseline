"""Spot integration test: API token lifecycle.

Concerns covered:
- POST /auth/api-tokens mints a token with dsk_ prefix; raw token in response, not thereafter
- GET /auth/api-tokens lists active tokens (raw token absent from list)
- DELETE /auth/api-tokens/{id} revokes a token; subsequent use returns 401 TOKEN_REVOKED
- 10-token cap: 11th mint returns 409 TOKEN_LIMIT_EXCEEDED
- PAT authentication stamps api_tokens.last_used_at, and the throttle holds within the window

spec: spec/feature/AUTH.md §API Tokens
spec: spec/API.md §Auth GET/POST/DELETE /auth/api-tokens
"""

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import URL


def _unique_email(prefix: str = "api-tokens") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


# Module-scoped user: seed directly in DB to avoid the /auth/register rate limit
# (5/min per IP). All api-token tests (mint/list/revoke) share this one user.

@pytest_asyncio.fixture(scope="module")
async def api_token_user_access_token(integration_db_url: URL) -> str:
    """Seed a Reader user directly in DB and return a JWT token for API token tests.

    Uses DB seeding instead of /auth/register to avoid rate-limit exhaustion
    when multiple spot modules run together in the same minute window.

    Seeds via google_sub (password_hash=NULL) — API token tests never call
    POST /auth/token, so no password hash is needed.
    """
    from sqlalchemy import pool as sa_pool
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("token-mod")
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
                {
                    "id": str(user_id),
                    "email": email,
                    "name": "API Token Test User",
                    "google_sub": google_sub,
                },
            )
    finally:
        await engine.dispose()

    token, _ = issue_access_token(user_id, email, session_epoch=0)

    yield token

    # Cleanup: remove api_tokens (CASCADE) and the user
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
async def test_mint_token_returns_dsk_prefix(
    api_client: httpx.AsyncClient,
    api_token_user_access_token: str,
) -> None:
    """POST /auth/api-tokens returns raw token with dsk_ prefix.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage — opaque random tokens
    of the form dsk_<32 url-safe random bytes>. Raw token returned once only.
    """
    resp = await api_client.post(
        "/api/v1/auth/api-tokens",
        json={"name": "ci-token"},
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert resp.status_code == 201, (
        f"POST /auth/api-tokens must return 201, got {resp.status_code}: {resp.text}"
    )

    body = resp.json()
    assert "token" in body, "Mint response must include 'token' field"
    assert body["token"].startswith("dsk_"), (
        "Minted token must start with 'dsk_' per spec/feature/AUTH.md §API Tokens §Token format"
    )
    assert "id" in body
    assert "name" in body
    assert "role_snapshot" in body
    assert body["role_snapshot"] == "Reader", "New user registration → Reader role snapshot"
    assert "created_at" in body


@pytest.mark.asyncio
async def test_list_tokens_does_not_expose_raw_token(
    api_client: httpx.AsyncClient,
    api_token_user_access_token: str,
) -> None:
    """GET /auth/api-tokens never returns the raw token in the list.

    spec: spec/feature/AUTH.md §API Tokens — only SHA-256 hash stored; raw token
    returned once in POST response body and never retrievable again.
    spec: spec/API.md §Auth GET /auth/api-tokens — returns id, name, role_snapshot,
    created_at, last_used_at, expires_at (no raw token).
    """
    # Mint a token for this test
    mint_resp = await api_client.post(
        "/api/v1/auth/api-tokens",
        json={"name": "list-test"},
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert mint_resp.status_code == 201
    raw_token = mint_resp.json()["token"]

    # List tokens — raw token must not appear
    list_resp = await api_client.get(
        "/api/v1/auth/api-tokens",
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert "tokens" in body
    assert "total_count" in body

    # Confirm raw token not exposed anywhere in the list response
    response_text = list_resp.text
    assert raw_token not in response_text, (
        "Raw token must NEVER appear in GET /auth/api-tokens response "
        "per spec/feature/AUTH.md §API Tokens — raw token returned once only"
    )

    # Each token item has the right fields
    for item in body["tokens"]:
        assert "id" in item
        assert "name" in item
        assert "role_snapshot" in item
        assert "created_at" in item
        assert "token" not in item, "Individual token items must not expose the raw token"


@pytest.mark.asyncio
async def test_revoke_token_returns_401_on_reuse(
    api_client: httpx.AsyncClient,
    api_token_user_access_token: str,
) -> None:
    """Revoking an API token causes subsequent use to return 401 TOKEN_REVOKED.

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege — a revoked token
    (revoked_at IS NOT NULL) fails authentication with 401 TOKEN_REVOKED.
    spec: spec/feature/AUTH.md §Failure Modes — API token revoked while in use → 401 TOKEN_REVOKED.
    """
    # Mint a token specifically for this revocation test
    mint_resp = await api_client.post(
        "/api/v1/auth/api-tokens",
        json={"name": "revoke-test"},
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert mint_resp.status_code == 201, f"Mint failed: {mint_resp.text}"
    raw_token = mint_resp.json()["token"]
    token_id = mint_resp.json()["id"]

    # Verify the token works before revocation
    me_resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert me_resp.status_code == 200, "API token must work before revocation"

    # Revoke the token
    revoke_resp = await api_client.delete(
        f"/api/v1/auth/api-tokens/{token_id}",
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert revoke_resp.status_code == 204, f"DELETE /auth/api-tokens/{token_id} must return 204"

    # Token must now be rejected
    me_after = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert me_after.status_code == 401, (
        "Revoked token must return 401 per spec/feature/AUTH.md §API Tokens §Effective privilege"
    )
    assert me_after.json()["error_code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_10_token_cap(
    api_client: httpx.AsyncClient,
    integration_db_url: URL,
) -> None:
    """11th mint returns 409 TOKEN_LIMIT_EXCEEDED.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage — cap: at most 10 active
    tokens per user; mint beyond cap returns 409 TOKEN_LIMIT_EXCEEDED.

    Uses a dedicated fresh DB-seeded user to ensure a clean slate (0 existing tokens).
    DB seeding avoids the /auth/register rate limit (5/min per IP).
    Seeds via google_sub (password_hash=NULL) — this test never calls POST /auth/token.
    """
    from sqlalchemy import pool as sa_pool
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("cap-test")
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
                {
                    "id": str(user_id),
                    "email": email,
                    "name": "Token Cap User",
                    "google_sub": google_sub,
                },
            )
    finally:
        await engine.dispose()

    access_token, _ = issue_access_token(user_id, email, session_epoch=0)

    try:
        # Mint 10 tokens
        for i in range(10):
            resp = await api_client.post(
                "/api/v1/auth/api-tokens",
                json={"name": f"cap-token-{i}"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert resp.status_code == 201, (
                f"Mint #{i+1} must succeed, got {resp.status_code}: {resp.text}"
            )

        # 11th mint must fail
        eleventh = await api_client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "cap-token-11"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert eleventh.status_code == 409, (
            f"11th mint must return 409 TOKEN_LIMIT_EXCEEDED per spec/feature/AUTH.md §API Tokens, "
            f"got {eleventh.status_code}: {eleventh.text}"
        )
        assert eleventh.json()["error_code"] == "TOKEN_LIMIT_EXCEEDED"
    finally:
        # Cleanup: CASCADE removes api_tokens
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
async def test_pat_authentication_stamps_last_used_at_and_then_throttles(
    api_client: httpx.AsyncClient,
    api_token_user_access_token: str,
    async_session,
) -> None:
    """Authenticating with a PAT advances last_used_at; an immediate re-use does not.

    Both halves are here because both are invisible from the request. The stamp runs on a
    session of its own and its failure is swallowed and logged at ERROR, so a broken write
    still answers 200 with a permanently NULL audit column — nothing in band reports it.
    The predicate that carries the throttle
    (``now() - make_interval(0,0,0,0,0,0,60)``) is SQL, and only a real PostgreSQL behind
    asyncpg can say whether the function resolves, whether the argument list is the one
    PostgreSQL expects, and whether the interval subtraction types check. No unit test runs
    this statement against an engine, so this is the only place a resolution failure is
    caught before it freezes the column in production.

    Seeding both sides of the predicate: the first authentication lands on the NULL leg
    (``last_used_at IS NULL``), the second on the timestamp comparison inside the window.
    A predicate that never matches fails the first assertion; one that always matches fails
    the second.

    What this test cannot see is a window that is too *long*: a 60-year interval also
    stamps once and then declines, so both assertions hold. The window's length is pinned
    at the unit tier instead, where the statement's clause tree can be read directly —
    ``tests/unit/api/auth/test_api_tokens.py::
    test_the_throttle_holds_the_stamp_off_for_sixty_seconds``. This test's job is the half
    that needs a real engine: that the SQL resolves, executes, and moves the column.

    **The column is read out of band, straight from Postgres, on purpose.** Reading it back
    through ``GET /auth/api-tokens`` authenticated by this same token cannot see it: the
    router body and the authentication dependency share one FastAPI-cached ``get_db``
    session with ``expire_on_commit=False``, and ``list_active`` re-selects without
    ``populate_existing=True``, so the identity map answers with the row as it was loaded
    *before* this request's stamp. Every observation would be off by one request and the
    throttle assertion would pass no matter what the predicate did. Do not "simplify" this
    into an API read-back.

    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — "Every successful API-token
        authentication updates ``api_tokens.last_used_at``. The update is throttled to
        per-minute granularity — the authentication path issues the ``UPDATE`` with a
        ``WHERE`` clause that makes it a no-op below 60s — so a high-frequency client
        doesn't flood the DB."
    spec: spec/feature/BACKEND_SCHEMA.md §api_tokens — ``last_used_at`` is "Updated per use
        (throttled to per-minute granularity to avoid DB pressure). Null until first use."
    spec: spec/feature/BACKEND.md §Privilege Enforcement — "``last_used_at`` is stamped by a
        separate ``UPDATE``, issued and committed on its own session after the token-state
        checks have passed."
    """
    from sqlalchemy import text as _text

    mint_resp = await api_client.post(
        "/api/v1/auth/api-tokens",
        json={"name": "last-used-stamp"},
        headers={"Authorization": f"Bearer {api_token_user_access_token}"},
    )
    assert mint_resp.status_code == 201, f"Mint failed: {mint_resp.text}"
    raw_token = mint_resp.json()["token"]
    token_id = mint_resp.json()["id"]

    async def _read_last_used_at():
        """The column as PostgreSQL holds it, on a connection the API does not share."""
        result = await async_session.execute(
            _text("SELECT last_used_at FROM dataspoke.api_tokens WHERE id = :id"),
            {"id": str(token_id)},
        )
        row = result.fetchone()
        # End this read's transaction so the next read starts a fresh snapshot and can
        # see a stamp committed in between.
        await async_session.rollback()
        assert row is not None, f"the minted api_tokens row {token_id} must exist"
        return row.last_used_at

    async def _db_now():
        result = await async_session.execute(_text("SELECT now() AS ts"))
        ts = result.fetchone().ts
        await async_session.rollback()
        return ts

    assert await _read_last_used_at() is None, (
        "a freshly minted token is unused, so last_used_at starts NULL — the baseline the "
        "first stamp is measured against. spec: spec/feature/BACKEND_SCHEMA.md §api_tokens "
        "— 'Null until first use.'"
    )

    before_first_use = await _db_now()

    first_use = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert first_use.status_code == 200, (
        f"the minted PAT must authenticate; got {first_use.status_code}: {first_use.text}"
    )

    first_stamp = await _read_last_used_at()
    assert first_stamp is not None, (
        "a successful PAT authentication must stamp last_used_at. A NULL here is what a "
        "silently failing stamp looks like: the stamp is best-effort, so a predicate that "
        "PostgreSQL cannot resolve leaves the column frozen while the request still "
        "answers 200. spec: spec/feature/AUTH.md §Audit and last_used_at — 'Every "
        "successful API-token authentication updates api_tokens.last_used_at.'"
    )
    assert first_stamp >= before_first_use, (
        f"the stamp must be this authentication's, not an earlier value: {first_stamp!r} "
        f"predates the request, which began at {before_first_use!r}."
    )

    second_use = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert second_use.status_code == 200, (
        f"the second authentication must also succeed — the throttle is about the write, "
        f"not the request; got {second_use.status_code}: {second_use.text}"
    )

    assert await _read_last_used_at() == first_stamp, (
        "a second authentication inside the 60s window must leave the column untouched, "
        "or a high-frequency client rewrites this row on every request. spec: "
        "spec/feature/AUTH.md §Audit and last_used_at — the UPDATE's WHERE clause 'makes "
        "it a no-op below 60s'."
    )


@pytest.mark.asyncio
async def test_api_token_hash_stored_as_sha256(
    api_client: httpx.AsyncClient,
    async_session,
    integration_db_url: URL,
) -> None:
    """api_tokens.token_hash is exactly 64 chars and equals sha256(raw_token).

    Verifies the schema constraint (CHAR(64)) and the storage contract: only the
    SHA-256 hash of the raw dsk_ token is stored; the raw token cannot be recovered
    from the DB.

    spec: spec/feature/AUTH.md §API Tokens §Token format and storage —
          only the SHA-256 hash of the token is stored in the api_tokens table
          (column token_hash). The raw token is returned once and never retrievable again.
    spec: spec/feature/BACKEND_SCHEMA.md — token_hash is CHAR(64).
    """
    import hashlib

    from sqlalchemy import pool as sa_pool
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.backend.auth.tokens import issue_access_token

    email = _unique_email("hash-verify")
    user_id = uuid.uuid4()
    google_sub = f"test-sub-{uuid.uuid4()}"

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _text(
                    "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
                    " VALUES (:id, :email, :name, :google_sub, 'Reader')"
                ),
                {
                    "id": str(user_id),
                    "email": email,
                    "name": "Hash Verify User",
                    "google_sub": google_sub,
                },
            )
    finally:
        await engine.dispose()

    access_token, _ = issue_access_token(user_id, email, session_epoch=0)

    try:
        # Mint a token
        mint_resp = await api_client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "hash-verify-token"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert mint_resp.status_code == 201, (
            f"Mint must succeed, got {mint_resp.status_code}: {mint_resp.text}"
        )
        raw_token = mint_resp.json()["token"]
        assert raw_token.startswith("dsk_"), "Minted token must start with dsk_"

        # Read the stored token_hash from the DB
        result = await async_session.execute(
            _text(
                "SELECT token_hash FROM dataspoke.api_tokens"
                " WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"
            ),
            {"uid": str(user_id)},
        )
        row = result.fetchone()
        assert row is not None, "api_tokens row must exist after mint"

        token_hash = row.token_hash

        # CHAR(64) constraint — exactly 64 hex characters
        assert len(token_hash) == 64, (
            f"token_hash must be exactly 64 characters (SHA-256 hex) "
            f"per spec/feature/AUTH.md §API Tokens §Token format and storage, "
            f"got len={len(token_hash)}"
        )

        # The stored hash must equal sha256(raw_token) — verified hash, not partial or plaintext
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        assert token_hash == expected_hash, (
            f"token_hash in DB must equal sha256(raw_token) "
            f"per spec/feature/AUTH.md §API Tokens §Token format and storage. "
            f"Expected: {expected_hash}, got: {token_hash}"
        )

        # Raw token must NOT be stored anywhere in the row
        assert raw_token not in token_hash, (
            "Raw dsk_ token must NEVER appear in token_hash column"
        )

    finally:
        engine3 = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
        try:
            async with engine3.begin() as conn:
                await conn.execute(
                    _text("DELETE FROM dataspoke.users WHERE id = :id"),
                    {"id": str(user_id)},
                )
        finally:
            await engine3.dispose()
