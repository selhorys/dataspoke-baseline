"""Spot integration test: API token lifecycle, and the admin credential inventory.

Concerns covered:
- POST /auth/api-tokens mints a token with dsk_ prefix; raw token in response, not thereafter
- GET /auth/api-tokens lists active tokens (raw token absent from list)
- DELETE /auth/api-tokens/{id} revokes a token; subsequent use returns 401 TOKEN_REVOKED
- 10-token cap: 11th mint returns 409 TOKEN_LIMIT_EXCEEDED
- PAT authentication stamps api_tokens.last_used_at, and the throttle holds within the window
- GET /admin/api-tokens: cross-user visibility with the owner named; 403 for a non-Admin;
  include_revoked both ways; the user_id filter (unknown id ⇒ empty page, not 404);
  total_count and disjoint paging across a created_at tie; last_used_at NULLS LAST;
  token_hash on no response
- DELETE /admin/users/{id}/api-tokens/{token_id} writes exactly one AUTH.API_TOKEN_REVOKED
  on the owner's timeline, and nothing on a repeat; DELETE /auth/api-tokens/{id} writes none

**Why the admin-inventory cases sit at spot, not api-wired.** They need a *multi-user* token
population with pinned `created_at` ties, a NULL and a non-NULL `last_used_at`, an expired
row and a revoked row. No `USE_CASE_en.md` pipeline produces that: minting is owner-only, so
reaching it through REST alone would mean logging in as several users and would still leave
the timestamps at whatever wall clock the mint saw. The rows are seeded through raw SQL,
which is exactly the placement rule in spec/TESTING.md §Spot vs Api-Wired Integration Tests
("a spot test may call dataspoke Python directly ... or call the API over HTTP — pick
whichever proves the concern most directly"). The assertions themselves all go through REST.

Seeding note: every bound value below is a scalar or a tz-aware ``datetime``. Were a
``TEXT[]`` column ever added to this seed, bind a Python list through
``bindparam(..., ARRAY(Text()))`` — a ``"{a,b}"`` literal is accepted by psycopg and
rejected by asyncpg, which is the driver behind ``integration_db_url``.

spec: spec/feature/AUTH.md §API Tokens
spec: spec/feature/AUTH.md §Revoked-token visibility
spec: spec/feature/AUTH.md §Admin revoke audit
spec: spec/API.md §Auth GET/POST/DELETE /auth/api-tokens
spec: spec/API.md §Admin GET /admin/api-tokens, GET/DELETE /admin/users/{id}/api-tokens
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import URL


def _unique_email(prefix: str = "api-tokens") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


#: Every key spec/API.md §Auth lists on a **self**-list token item — and no other. The
#: three fields the admin shape adds (``revoked_at``, ``user_id``, ``user_email``) are
#: deliberately absent: spec/API.md §Admin — "Owner identity and ``revoked_at`` stay off
#: ``GET /auth/api-tokens``, which is scoped to the caller's own tokens and has no other
#: owner to name."
SELF_ITEM_KEYS = frozenset(
    {"id", "name", "role_snapshot", "created_at", "last_used_at", "expires_at"}
)

#: Every key spec/API.md §Admin lists on an admin token item.
ADMIN_ITEM_KEYS = frozenset(
    {
        "id",
        "name",
        "role_snapshot",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "user_id",
        "user_email",
    }
)


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
    """GET /auth/api-tokens returns the lean self item shape — no raw token, no owner.

    The shape is asserted as an **exact** key set rather than as a presence check, because
    what distinguishes the self read from the two admin reads is precisely the three fields
    it does *not* carry. A presence check passes just as happily on a self list that has
    started serving ``revoked_at`` / ``user_id`` / ``user_email``, which would collapse the
    two shapes into one — and that distinction is the whole reason the self list and the
    admin list are separate response models.

    spec: spec/feature/AUTH.md §API Tokens — only SHA-256 hash stored; raw token
    returned once in POST response body and never retrievable again.
    spec: spec/API.md §Auth — ``GET /auth/api-tokens`` returns "content key ``tokens``:
    ``[{id, name, role_snapshot, created_at, last_used_at, expires_at}]`` — never the raw
    token".
    spec: spec/API.md §Admin — "Owner identity and ``revoked_at`` stay off
    ``GET /auth/api-tokens``, which is scoped to the caller's own tokens and has no other
    owner to name."
    spec: spec/feature/AUTH.md §Revoked-token visibility — "``revoked_at`` is not on the
    self item shape, so there is no withdrawal timeline to read there either."
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

    # Backstop: the exact-shape assertion below iterates a non-empty list. The token minted
    # above is this caller's and unrevoked, so the self list must carry it.
    assert body["tokens"], (
        "the freshly minted token must appear in the caller's own list, or the per-item "
        "shape assertion below runs over nothing"
    )
    for item in body["tokens"]:
        assert set(item) == SELF_ITEM_KEYS, (
            f"the self item shape must be exactly {sorted(SELF_ITEM_KEYS)} — no raw token, "
            f"and none of the admin shape's revoked_at / user_id / user_email; got "
            f"{sorted(item)}. spec: spec/API.md §Auth and §Admin ('Owner identity and "
            f"revoked_at stay off GET /auth/api-tokens')."
        )


@pytest.mark.asyncio
async def test_the_self_list_has_no_revoked_opt_in(
    api_client: httpx.AsyncClient,
    api_token_user_access_token: str,
) -> None:
    """GET /auth/api-tokens hides revoked rows and ``?include_revoked=true`` changes nothing.

    The two admin reads take that opt-in; the self read does not, and the difference is
    specified rather than incidental — a revoked token is nothing its owner can act on, and
    ``revoked_at`` is not on the self item shape, so there is no withdrawal timeline to
    read there. An unrecognised query param is ignored by FastAPI, so the *risk* is not a
    500 — it is that someone wires the opt-in through and the self list quietly starts
    serving withdrawn credentials.

    The absence is injected: the token this test revokes is the caller's own, so a self
    list that honoured the opt-in would return it. Its presence in the pre-revoke list is
    asserted first, which is what makes the post-revoke absence a filter rather than a row
    that was never there.

    spec: spec/feature/AUTH.md §Revoked-token visibility — "``GET /auth/api-tokens``
        excludes revoked rows and offers no opt-in. A revoked token is nothing its owner can
        act on — it cannot be used, un-revoked, or revoked again — and ``revoked_at`` is not
        on the self item shape, so there is no withdrawal timeline to read there either.
        Audit of withdrawn credentials is an admin concern, served by the two routes above."
    spec: spec/API.md §Auth — ``DELETE /auth/api-tokens/{id}`` "Revoke own API token (sets
        ``revoked_at = now()``)".
    """
    owner_headers = {"Authorization": f"Bearer {api_token_user_access_token}"}

    mint = await api_client.post(
        "/api/v1/auth/api-tokens",
        json={"name": "self-opt-in-probe"},
        headers=owner_headers,
    )
    assert mint.status_code == 201, f"mint failed: {mint.text}"
    token_id = mint.json()["id"]

    before = await api_client.get("/api/v1/auth/api-tokens?limit=1000", headers=owner_headers)
    assert before.status_code == 200, before.text
    assert token_id in {item["id"] for item in before.json()["tokens"]}, (
        "the freshly minted token must be in its owner's list before revocation, or the "
        "absence asserted below is not the revocation filter at work"
    )

    revoke = await api_client.delete(
        f"/api/v1/auth/api-tokens/{token_id}", headers=owner_headers
    )
    assert revoke.status_code == 204, f"self revoke must answer 204; got {revoke.text}"

    plain = await api_client.get("/api/v1/auth/api-tokens?limit=1000", headers=owner_headers)
    assert plain.status_code == 200, plain.text
    plain_ids = {item["id"] for item in plain.json()["tokens"]}
    assert token_id not in plain_ids, (
        "the self list must exclude the caller's revoked token. spec: spec/feature/AUTH.md "
        "§Revoked-token visibility — 'GET /auth/api-tokens excludes revoked rows'."
    )

    opted_in = await api_client.get(
        "/api/v1/auth/api-tokens?include_revoked=true&limit=1000", headers=owner_headers
    )
    assert opted_in.status_code == 200, opted_in.text
    assert {item["id"] for item in opted_in.json()["tokens"]} == plain_ids, (
        "the self list offers no include_revoked opt-in, so the param must change nothing. "
        "spec: spec/feature/AUTH.md §Revoked-token visibility — 'GET /auth/api-tokens "
        "excludes revoked rows and offers no opt-in ... Audit of withdrawn credentials is "
        "an admin concern, served by the two routes above'."
    )
    assert token_id not in {item["id"] for item in opted_in.json()["tokens"]}, (
        "the revoked token must stay out under the opt-in too — that is the whole content "
        "of 'offers no opt-in'."
    )


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


# ══ Admin credential inventory ════════════════════════════════════════════════
#
# GET /admin/api-tokens and GET /admin/users/{id}/api-tokens, plus the audit event the
# admin revoke writes. Everything below reads through REST; only the fixture writes SQL.


@dataclass(frozen=True)
class _SeededToken:
    """One seeded ``api_tokens`` row, as the test knows it before any request is made."""

    id: uuid.UUID
    name: str
    token_hash: str
    owner_id: uuid.UUID
    owner_email: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class _Inventory:
    """The seeded multi-user token population two owners deep."""

    alpha_id: uuid.UUID
    alpha_email: str
    beta_id: uuid.UUID
    beta_email: str
    tokens: tuple[_SeededToken, ...]

    def owned_by(self, owner_id: uuid.UUID) -> tuple[_SeededToken, ...]:
        return tuple(t for t in self.tokens if t.owner_id == owner_id)

    def named(self, name: str) -> _SeededToken:
        return next(t for t in self.tokens if t.name == name)


@pytest_asyncio.fixture(scope="module")
async def token_inventory(integration_db_url: URL) -> _Inventory:
    """Seed two users and a token population with pinned timestamps; drop it after.

    Every timestamp is fixed here rather than left to the mint clock, because three of the
    assertions below are *about* timestamps: a block of rows sharing one ``created_at``
    (the tie a page boundary must survive), a mix of NULL and non-NULL ``last_used_at``
    (the NULLS LAST ordering), and a row already past its ``expires_at`` (which the default
    view must still show). A REST-minted population has none of those properties.

    Users are seeded via ``google_sub`` with a NULL ``password_hash`` — these tests never
    call ``POST /auth/token`` for them, and DB seeding avoids the ``/auth/register``
    rate limit (5/min per IP) that several spot modules share.
    """
    import hashlib

    from sqlalchemy import pool as sa_pool
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    alpha_id, beta_id = uuid.uuid4(), uuid.uuid4()
    alpha_email, beta_email = _unique_email("inv-alpha"), _unique_email("inv-beta")

    # An hour back, so no seeded row is dated in the future relative to the API pod's clock.
    base = datetime.now(tz=UTC) - timedelta(hours=1)

    def _tok(
        name: str,
        owner_id: uuid.UUID,
        owner_email: str,
        *,
        created_at: datetime,
        last_used_at: datetime | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
    ) -> _SeededToken:
        return _SeededToken(
            id=uuid.uuid4(),
            name=name,
            # A real 64-char SHA-256 hex digest, unique per row (uq_api_tokens_token_hash),
            # and — this is the point — a value the test knows verbatim, so "the hash is on
            # no response" is an absence assertion about something actually injected
            # (spec/TESTING.md §Assertion Discipline).
            token_hash=hashlib.sha256(f"{name}-{uuid.uuid4()}".encode()).hexdigest(),
            owner_id=owner_id,
            owner_email=owner_email,
            created_at=created_at,
            last_used_at=last_used_at,
            expires_at=expires_at,
            revoked_at=revoked_at,
        )

    tokens: tuple[_SeededToken, ...] = (
        # Four rows minted in one transaction share a created_at — the tie the id
        # tiebreak exists for. They are the newest, so under the default created_at_desc
        # they occupy the first four positions and a limit=2 page boundary lands inside.
        *(
            _tok(f"alpha-tied-{n}", alpha_id, alpha_email, created_at=base)
            for n in range(1, 5)
        ),
        _tok(
            "alpha-used-recent",
            alpha_id,
            alpha_email,
            created_at=base - timedelta(minutes=10),
            last_used_at=base - timedelta(minutes=1),
        ),
        _tok(
            "alpha-used-old",
            alpha_id,
            alpha_email,
            created_at=base - timedelta(minutes=20),
            last_used_at=base - timedelta(hours=2),
        ),
        _tok(
            "alpha-never-used",
            alpha_id,
            alpha_email,
            created_at=base - timedelta(minutes=30),
        ),
        _tok(
            "alpha-expired",
            alpha_id,
            alpha_email,
            created_at=base - timedelta(minutes=40),
            expires_at=base - timedelta(minutes=35),
        ),
        _tok(
            "alpha-revoked",
            alpha_id,
            alpha_email,
            created_at=base - timedelta(minutes=50),
            revoked_at=base - timedelta(minutes=5),
        ),
        _tok("beta-active", beta_id, beta_email, created_at=base - timedelta(minutes=25)),
        _tok(
            "beta-revoked",
            beta_id,
            beta_email,
            created_at=base - timedelta(minutes=35),
            revoked_at=base - timedelta(minutes=5),
        ),
    )

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine.begin() as conn:
            for user_id, email, name, role in (
                (alpha_id, alpha_email, "Inventory Alpha", "Reader"),
                (beta_id, beta_email, "Inventory Beta", "Editor"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
                        " VALUES (:id, :email, :name, :google_sub, :role)"
                    ),
                    {
                        "id": str(user_id),
                        "email": email,
                        "name": name,
                        "google_sub": f"test-sub-{uuid.uuid4()}",
                        "role": role,
                    },
                )
            for token in tokens:
                await conn.execute(
                    text(
                        "INSERT INTO dataspoke.api_tokens"
                        " (id, user_id, name, token_hash, role_snapshot,"
                        "  created_at, last_used_at, expires_at, revoked_at)"
                        " VALUES (:id, :user_id, :name, :token_hash, 'Reader',"
                        "  :created_at, :last_used_at, :expires_at, :revoked_at)"
                    ),
                    {
                        "id": str(token.id),
                        "user_id": str(token.owner_id),
                        "name": token.name,
                        "token_hash": token.token_hash,
                        "created_at": token.created_at,
                        "last_used_at": token.last_used_at,
                        "expires_at": token.expires_at,
                        "revoked_at": token.revoked_at,
                    },
                )
    finally:
        await engine.dispose()

    yield _Inventory(
        alpha_id=alpha_id,
        alpha_email=alpha_email,
        beta_id=beta_id,
        beta_email=beta_email,
        tokens=tokens,
    )

    teardown = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with teardown.begin() as conn:
            for user_id in (alpha_id, beta_id):
                # api_tokens go with the user (ON DELETE CASCADE).
                await conn.execute(
                    text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(user_id)}
                )
    finally:
        await teardown.dispose()


@pytest.mark.asyncio
async def test_the_inventory_shows_an_admin_a_token_they_do_not_own(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """GET /admin/api-tokens returns another user's token, naming its owner.

    This is the reason the route exists: before it, the only cross-user surface answered
    "what does *this* user hold", one user at a time, so an admin had to already know whose
    credentials to ask about. The seeded token here belongs to neither the caller nor any
    user the caller named — it is reached purely by being in the deployment.

    The owner fields are asserted, not just present: without a correct ``user_id`` the item
    cannot address ``DELETE /admin/users/{id}/api-tokens/{token_id}``, which §Lifecycle
    endpoints says is why the inventory needs no revoke route of its own; and without
    ``user_email`` the page names a credential nobody can attribute.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` returns "every user's API tokens —
        the deployment-wide inventory ... content key ``tokens: [{id, name, role_snapshot,
        created_at, last_used_at, expires_at, revoked_at, user_id, user_email}]``".
    spec: spec/feature/AUTH.md §API Tokens — "Admins read other users' tokens through two
        surfaces — a deployment-wide inventory and a per-user list".
    spec: spec/feature/AUTH.md §Lifecycle endpoints — "Revocation needs no route of its own
        on the inventory — each item carries the ``user_id`` that addresses
        ``DELETE /admin/users/{id}/api-tokens/{token_id}``."
    """
    me = await api_client.get("/api/v1/auth/me", headers=admin_headers)
    assert me.status_code == 200, f"admin identity lookup failed: {me.text}"
    caller_id = me.json()["id"]
    assert caller_id != str(token_inventory.beta_id), (
        "the seeded owner must not be the calling admin, or 'cross-user' proves nothing"
    )

    resp = await api_client.get("/api/v1/admin/api-tokens?limit=1000", headers=admin_headers)
    assert resp.status_code == 200, f"GET /admin/api-tokens failed: {resp.text}"
    body = resp.json()

    # Backstop for the membership claim below: with total_count inside the page size, this
    # one response is the whole inventory, so "present" is a fact and not luck about where
    # the seeded row happened to land. (Exact equality of len(tokens) and total_count is
    # deliberately not asserted — the two are separate queries on a shared dev cluster,
    # where a concurrent test's mint can land between them.)
    assert body["total_count"] <= 1000, (
        f"the deployment holds {body['total_count']} unrevoked tokens, more than this page "
        f"can carry — page through, or this test's membership assertions are unreliable"
    )

    expected = token_inventory.named("beta-active")
    matches = [item for item in body["tokens"] if item["id"] == str(expected.id)]
    assert len(matches) == 1, (
        f"the inventory must carry {expected.name!r} — a token owned by a user the caller "
        f"neither owns nor named; got {len(matches)} matching rows. spec: spec/API.md "
        f"§Admin — 'every user's API tokens'."
    )
    item = matches[0]

    assert item["user_id"] == str(token_inventory.beta_id), (
        f"the item must name its owner's id — that is what addresses the revoke route; got "
        f"{item['user_id']!r}, expected {token_inventory.beta_id}. spec: "
        f"spec/feature/AUTH.md §Lifecycle endpoints."
    )
    assert item["user_email"] == token_inventory.beta_email, (
        f"the item must name its owner's email; got {item['user_email']!r}, expected "
        f"{token_inventory.beta_email!r}. spec: spec/API.md §Admin."
    )
    assert item["name"] == expected.name
    assert set(item) == ADMIN_ITEM_KEYS, (
        f"the admin item shape must be exactly the documented keys; got {sorted(item)}, "
        f"expected {sorted(ADMIN_ITEM_KEYS)}. spec: spec/API.md §Admin."
    )
    for key in ("offset", "limit", "total_count"):
        assert key in body, f"the standard pagination envelope must carry {key!r}"


@pytest.mark.asyncio
async def test_a_non_admin_is_refused_both_admin_token_reads(
    api_client: httpx.AsyncClient,
    token_inventory: _Inventory,
    async_session,
) -> None:
    """A Reader gets 403 FORBIDDEN on the inventory and on the per-user read.

    Both routes are asserted because they are separate handlers behind one router-level
    gate: a gate applied to only one of them leaves the other serving every user's
    credential list to any authenticated caller.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` and
        ``GET /admin/users/{id}/api-tokens`` are both "JWT + Admin role".
    spec: spec/feature/AUTH.md §Privilege Model — "Editor or Reader attempting ``/admin/*``
        | ``403 FORBIDDEN``".
    """
    from sqlalchemy import text

    from src.backend.auth.tokens import issue_access_token

    reader_id = uuid.uuid4()
    reader_email = _unique_email("inv-reader")
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Inventory Reader",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()

    reader_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)
    reader_headers = {"Authorization": f"Bearer {reader_token}"}

    try:
        # Backstop: this JWT authenticates fine, so the 403s below are the role gate
        # rejecting the caller rather than the credential failing to resolve at all.
        me = await api_client.get("/api/v1/auth/me", headers=reader_headers)
        assert me.status_code == 200, (
            f"the Reader's own JWT must authenticate, or the 403s prove nothing about the "
            f"role gate; got {me.status_code}: {me.text}"
        )

        for path in (
            "/api/v1/admin/api-tokens",
            f"/api/v1/admin/users/{token_inventory.alpha_id}/api-tokens",
        ):
            resp = await api_client.get(path, headers=reader_headers)
            assert resp.status_code == 403, (
                f"{path} must refuse a non-Admin per spec/feature/AUTH.md §Privilege "
                f"Model; got {resp.status_code}: {resp.text}"
            )
            assert resp.json()["error_code"] == "FORBIDDEN", f"got {resp.json()!r}"
            assert "token_hash" not in resp.text, (
                "a refusal must not leak credential material either"
            )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(reader_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_revoked_rows_are_out_by_default_and_back_under_include_revoked(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """``include_revoked`` is the only filter between the two views — and expiry is not one.

    Both sides are seeded: alpha owns eight unrevoked rows and one revoked one, so an
    over-broad filter (revoked row leaking into the default) and an over-narrow one
    (unrevoked rows missing) each fail. The expired row is the third case and the one most
    easily got wrong: ``expires_at`` in the past is *not* revocation, so that row must sit
    in the default page like any other.

    spec: spec/feature/AUTH.md §Revoked-token visibility — "Both admin reads exclude revoked
        rows by default and take ``include_revoked=true`` to bring them back"; "``revoked_at
        IS NULL`` is the whole of the default filter. Expiry is not filtered: a token past
        its ``expires_at`` authenticates nothing (``401 TOKEN_EXPIRED``) yet sits in the
        default page like any other row."
    spec: spec/API.md §Admin — "``?include_revoked=true`` also returns rows with
        ``revoked_at`` set; default ``false`` (unrevoked rows only — expiry is not
        filtered)".
    """
    alpha = token_inventory.alpha_id
    seeded = token_inventory.owned_by(alpha)
    unrevoked = {str(t.id) for t in seeded if t.revoked_at is None}
    revoked = {str(t.id) for t in seeded if t.revoked_at is not None}
    assert unrevoked and revoked, "the fixture must seed both sides of the filter"

    default = await api_client.get(
        f"/api/v1/admin/api-tokens?user_id={alpha}&limit=1000", headers=admin_headers
    )
    assert default.status_code == 200, default.text
    default_ids = {item["id"] for item in default.json()["tokens"]}
    assert default_ids == unrevoked, (
        f"the default view must be exactly the unrevoked rows; missing "
        f"{sorted(unrevoked - default_ids)}, unexpected {sorted(default_ids - unrevoked)}. "
        f"spec: spec/feature/AUTH.md §Revoked-token visibility."
    )
    assert default.json()["total_count"] == len(unrevoked)

    expired = token_inventory.named("alpha-expired")
    assert str(expired.id) in default_ids, (
        "a token past its expires_at must still appear in the default page — expiry is not "
        "a filter, and the item's own expires_at is what identifies it. spec: "
        "spec/feature/AUTH.md §Revoked-token visibility."
    )
    expired_item = next(i for i in default.json()["tokens"] if i["id"] == str(expired.id))
    assert expired_item["expires_at"] is not None, (
        "the expired row must carry its expires_at, which is how a reader counting what is "
        "usable tells it apart. spec: spec/feature/AUTH.md §Revoked-token visibility."
    )
    assert expired_item["revoked_at"] is None, (
        "an expired token is not a revoked one; its revoked_at must stay null"
    )

    opted_in = await api_client.get(
        f"/api/v1/admin/api-tokens?user_id={alpha}&include_revoked=true&limit=1000",
        headers=admin_headers,
    )
    assert opted_in.status_code == 200, opted_in.text
    opted_in_items = opted_in.json()["tokens"]
    assert {item["id"] for item in opted_in_items} == unrevoked | revoked, (
        "include_revoked=true must return the withdrawn rows alongside the live ones. "
        "spec: spec/feature/AUTH.md §Revoked-token visibility."
    )
    assert opted_in.json()["total_count"] == len(seeded)

    revoked_item = next(i for i in opted_in_items if i["id"] in revoked)
    assert revoked_item["revoked_at"] is not None, (
        "a revoked row is returned precisely so incident review can read when the "
        "credential was withdrawn; revoked_at must be populated. spec: "
        "spec/feature/AUTH.md §Revoked-token visibility."
    )

    # The per-user route carries the same opt-in, and defaults the same way. Before this
    # change it always returned revoked rows.
    per_user_default = await api_client.get(
        f"/api/v1/admin/users/{alpha}/api-tokens?limit=1000", headers=admin_headers
    )
    assert per_user_default.status_code == 200, per_user_default.text
    assert {i["id"] for i in per_user_default.json()["tokens"]} == unrevoked, (
        "the per-user admin read must default to unrevoked rows too. spec: "
        "spec/feature/AUTH.md §Revoked-token visibility — 'Both admin reads exclude revoked "
        "rows by default'."
    )

    per_user_opted_in = await api_client.get(
        f"/api/v1/admin/users/{alpha}/api-tokens?include_revoked=true&limit=1000",
        headers=admin_headers,
    )
    assert per_user_opted_in.status_code == 200, per_user_opted_in.text
    assert {i["id"] for i in per_user_opted_in.json()["tokens"]} == unrevoked | revoked, (
        "the per-user admin read must take include_revoked=true as well. spec: "
        "spec/feature/AUTH.md §Revoked-token visibility."
    )


@pytest.mark.asyncio
async def test_user_id_narrows_to_one_owner_and_an_unknown_id_is_an_empty_page(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """``?user_id=`` selects one owner's rows; an id naming nobody yields 200 with zero rows.

    Both sides again: beta's tokens exist in the same deployment, so a filter that is
    ignored (or applied to the wrong column) shows them here and fails.

    The unknown-id leg is a contract, not a nicety. The id is a *filter*, so the
    "not found" answer is an empty collection — the same answer any filter matching nothing
    gives — rather than the ``404 USER_NOT_FOUND`` the sibling ``/admin/users/{id}`` routes
    return.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens``: "``?user_id=`` narrows to one
        owner — a ``user_id`` naming no user matches nothing and yields an empty page rather
        than ``404``".
    spec: spec/API.md §Admin — ``GET /admin/users/{id}/api-tokens``: "The ``id`` is an owner
        filter, so one naming no user yields an empty page rather than ``404``".
    spec: spec/API.md §Error Catalogue → Application Error Codes — ``USER_NOT_FOUND``: "The
        admin token reads are the exception: there the id is a filter, and one matching no
        user returns an empty page".
    """
    alpha_ids = {str(t.id) for t in token_inventory.owned_by(token_inventory.alpha_id)}
    beta_ids = {str(t.id) for t in token_inventory.owned_by(token_inventory.beta_id)}
    assert alpha_ids and beta_ids, "the fixture must seed tokens for both owners"

    scoped = await api_client.get(
        f"/api/v1/admin/api-tokens?user_id={token_inventory.alpha_id}"
        f"&include_revoked=true&limit=1000",
        headers=admin_headers,
    )
    assert scoped.status_code == 200, scoped.text
    scoped_ids = {item["id"] for item in scoped.json()["tokens"]}
    assert scoped_ids == alpha_ids, (
        f"?user_id must return exactly that owner's rows; missing "
        f"{sorted(alpha_ids - scoped_ids)}, unexpected {sorted(scoped_ids - alpha_ids)}"
    )
    assert not scoped_ids & beta_ids, (
        "another owner's tokens must not survive the filter — that is the over-broad "
        "predicate this seeding exists to catch"
    )
    assert all(
        item["user_id"] == str(token_inventory.alpha_id) for item in scoped.json()["tokens"]
    ), "every row on a scoped page must name the requested owner"

    unknown = uuid.uuid4()
    for path in (
        f"/api/v1/admin/api-tokens?user_id={unknown}",
        f"/api/v1/admin/users/{unknown}/api-tokens",
    ):
        resp = await api_client.get(path, headers=admin_headers)
        assert resp.status_code == 200, (
            f"{path} must answer 200 with an empty page — the id is a filter, not a lookup; "
            f"got {resp.status_code}: {resp.text}. spec: spec/API.md §Error Catalogue → "
            f"Application Error Codes — USER_NOT_FOUND, 'The admin token reads are the "
            f"exception'."
        )
        body = resp.json()
        assert body["tokens"] == [], f"{path} must return no rows; got {body['tokens']!r}"
        assert body["total_count"] == 0, (
            f"{path} must report total_count 0; got {body['total_count']!r}"
        )


@pytest.mark.asyncio
async def test_paging_covers_every_row_exactly_once_across_a_created_at_tie(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """Walking ``offset`` in ``limit``-sized steps yields each seeded row once — no more.

    The four ``alpha-tied-*`` rows share one ``created_at``, which is what tokens minted in
    a single transaction look like. Under a bare ``ORDER BY created_at DESC`` PostgreSQL is
    free to order that block differently between the page-1 and page-2 queries, and the
    page boundary here (``limit=2``) falls *inside* it — so a credential can be served
    twice or vanish from every page. Vanishing is the dangerous half: an inventory that
    silently omits a live token is worse than one that errors, because the omission looks
    exactly like the credential not existing.

    ``total_count`` is asserted on every page for the same reason it exists — a client
    computes its page count from it, so a value that drifts mid-walk (or that reports the
    page size) sends the client to pages that are not there.

    spec: spec/feature/AUTH.md §Revoked-token visibility — "Either ordering places nulls
        last and is tiebroken by token id, so paging an inventory returns each token exactly
        once regardless of the requested ``sort``." This test is the direct read-back of
        that sentence.
    spec: spec/feature/AUTH.md §Revoked-token visibility — ties are "reachable under
        ``created_at``, where tokens minted in one transaction share a timestamp — an
        unspecified order within a tie can shift between the page-1 and page-2 queries and
        drop a live credential from every page."
    spec: spec/API_DESIGN_PRINCIPLE_en.md §5 (Pagination) — "``offset`` (start index,
        default ``0``) and ``limit`` (page size...)"; "``total_count`` is the unpaged size
        of the filtered collection, letting clients render page counts without a second
        request."
    spec: spec/feature/AUTH.md §Revoked-token visibility — "Both routes express their
        filter, ordering, and page bounds in SQL, so a request transfers and materialises
        one page rather than the whole matching set."
    """
    alpha = token_inventory.alpha_id
    expected_ids = {str(t.id) for t in token_inventory.owned_by(alpha)}
    tied = {str(t.id) for t in token_inventory.owned_by(alpha) if t.name.startswith("alpha-tied-")}
    assert len(tied) == 4, "the fixture must seed a four-row created_at tie"

    seen: list[str] = []
    page_size = 2
    offset = 0
    # Bounded walk: one extra page beyond the seeded count, so a page that never empties
    # ends the loop and fails the coverage assertion rather than spinning.
    max_pages = len(expected_ids) // page_size + 2
    for _ in range(max_pages):
        resp = await api_client.get(
            f"/api/v1/admin/api-tokens?user_id={alpha}&include_revoked=true"
            f"&limit={page_size}&offset={offset}",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_count"] == len(expected_ids), (
            f"total_count must be the collection size on every page; at offset {offset} it "
            f"was {body['total_count']}, expected {len(expected_ids)}. spec: "
            f"spec/API_DESIGN_PRINCIPLE_en.md §5."
        )
        assert body["offset"] == offset and body["limit"] == page_size
        page_ids = [item["id"] for item in body["tokens"]]
        assert len(page_ids) <= page_size, (
            f"a page must not exceed its limit; got {len(page_ids)} rows at offset {offset}"
        )
        seen.extend(page_ids)
        if not page_ids:
            break
        offset += page_size

    assert len(seen) == len(set(seen)), (
        f"paging must not repeat a row — a page boundary inside the created_at tie "
        f"{sorted(tied)} reordered between requests. Served ids in order: {seen!r}. spec: "
        f"spec/API_DESIGN_PRINCIPLE_en.md §5."
    )
    assert set(seen) == expected_ids, (
        f"paging must reach every row: missing {sorted(expected_ids - set(seen))}, "
        f"unexpected {sorted(set(seen) - expected_ids)}. A credential omitted from every "
        f"page is indistinguishable from one that does not exist."
    )
    assert tied <= set(seen), (
        f"the tied block itself must be fully covered; missing {sorted(tied - set(seen))}"
    )


@pytest.mark.asyncio
async def test_last_used_at_sort_puts_never_used_tokens_last(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """``sort=last_used_at_desc`` ranks exercised credentials first and NULLs last.

    PostgreSQL sorts NULLs *first* under a bare ``DESC``, and ``last_used_at`` is NULL for
    every token that has never authenticated. Without NULLS LAST, the sort an auditor
    reaches for — "which credentials are actually being used" — returns the never-used ones
    at the top of page one, on a response that is otherwise perfectly well-formed.

    Both groups are asserted non-empty first, so neither leg of the ordering claim can pass
    on an empty half (spec/TESTING.md §Assertion Discipline).

    spec: spec/API.md §Admin — ``GET /admin/api-tokens`` is "Sortable by
        ``created_at``/``last_used_at``".
    spec: spec/feature/AUTH.md §Revoked-token visibility — "Either ordering places nulls
        last and is tiebroken by token id, so paging an inventory returns each token exactly
        once regardless of the requested ``sort``."
    spec: spec/feature/AUTH.md §Revoked-token visibility — "Nulls-last keeps
        ``last_used_at_desc``, the ordering that asks which credentials are in use, from
        opening with the ones that never have been."
    spec: spec/feature/AUTH.md §Audit and ``last_used_at`` — the column exists for human
        inspection of when a token was used.
    """
    alpha = token_inventory.alpha_id
    seeded = [t for t in token_inventory.owned_by(alpha) if t.revoked_at is None]
    used_ids = {str(t.id) for t in seeded if t.last_used_at is not None}
    unused_ids = {str(t.id) for t in seeded if t.last_used_at is None}
    assert used_ids and unused_ids, (
        "the fixture must seed both used and never-used tokens, or the ordering claim has "
        "an empty half"
    )

    resp = await api_client.get(
        f"/api/v1/admin/api-tokens?user_id={alpha}&sort=last_used_at_desc&limit=1000",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["tokens"]
    assert {i["id"] for i in items} == used_ids | unused_ids, (
        "the sort must not change which rows are returned"
    )

    positions = {item["id"]: index for index, item in enumerate(items)}
    last_used_position = max(positions[i] for i in used_ids)
    first_unused_position = min(positions[i] for i in unused_ids)
    assert last_used_position < first_unused_position, (
        f"every token with a last_used_at must precede every never-used one; the last used "
        f"token sits at {last_used_position} and the first never-used one at "
        f"{first_unused_position}. Order served: "
        f"{[(i['name'], i['last_used_at']) for i in items]!r}"
    )

    # Parsed rather than string-compared, so the ordering claim does not ride on the
    # serialiser rendering every timestamp to the same width.
    used_in_order = [
        datetime.fromisoformat(item["last_used_at"])
        for item in items
        if item["last_used_at"] is not None
    ]
    assert used_in_order == sorted(used_in_order, reverse=True), (
        f"the used rows must themselves be newest-used first under _desc; got "
        f"{[(i['name'], i['last_used_at']) for i in items if i['last_used_at']]!r}"
    )


@pytest.mark.asyncio
async def test_no_token_read_returns_the_stored_hash(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    token_inventory: _Inventory,
) -> None:
    """Neither admin read nor the self read carries ``token_hash`` — key or value.

    The hash is the only stored form of the credential, so a page that ships it hands out
    the material an attacker needs. The assertion is meaningful because the fixture knows
    each seeded hash verbatim and looks for those exact strings: this is an absence claim
    about something that was actually injected into the database
    (spec/TESTING.md §Assertion Discipline — "Absence assertions require injection").

    The ``include_revoked=true`` variant is checked too — a revoked row travels the same
    serialiser, and it is the row least likely to be exercised elsewhere.

    spec: spec/API.md §Admin — ``GET /admin/api-tokens``: "the token hash is never
        returned".
    spec: spec/feature/AUTH.md §API Tokens §Token format and storage — "Only the SHA-256
        hash of the token is stored in the ``api_tokens`` table (column ``token_hash``).
        The raw token is returned **once** ... and never retrievable again."
    """
    alpha = token_inventory.alpha_id
    seeded_hashes = [t.token_hash for t in token_inventory.tokens]
    assert seeded_hashes, "the fixture must have seeded hashes to look for"

    for path in (
        "/api/v1/admin/api-tokens?limit=1000",
        f"/api/v1/admin/api-tokens?user_id={alpha}&include_revoked=true&limit=1000",
        f"/api/v1/admin/users/{alpha}/api-tokens?include_revoked=true&limit=1000",
    ):
        resp = await api_client.get(path, headers=admin_headers)
        assert resp.status_code == 200, f"{path}: {resp.text}"
        # Backstop: the response really did carry the seeded rows, so the absence below is
        # the hash being withheld rather than the page being empty.
        assert resp.json()["tokens"], f"{path} returned no rows to check"
        assert "token_hash" not in resp.text, (
            f"{path} must not name token_hash. spec: spec/API.md §Admin — 'the token hash "
            f"is never returned'."
        )
        for stored in seeded_hashes:
            assert stored not in resp.text, (
                f"{path} returned a stored credential hash verbatim. spec: "
                f"spec/feature/AUTH.md §API Tokens §Token format and storage."
            )


@pytest.mark.asyncio
async def test_admin_revoke_writes_one_event_on_the_owners_timeline(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """DELETE /admin/users/{id}/api-tokens/{token_id} emits one AUTH.API_TOKEN_REVOKED.

    The event is booked on the **token's owner**, not on the acting admin — so every
    credential a user loses lands on one timeline, and the acting principal stays where
    request logs keep it. Both are asserted: an event on the owner, and no event on the
    caller.

    The repeat call is the second half. Setting ``revoked_at`` is the whole of what ends a
    token's life, so a call that writes nothing describes no security act; a second event
    would claim the credential was killed twice on the very timeline incident review reads.

    A fresh user is seeded for this test rather than reusing the shared inventory, because
    the assertion is a count over one user's events and must not see another test's writes.

    spec: spec/feature/AUTH.md §Admin revoke audit — "It emits one ``AUTH.API_TOKEN_REVOKED``
        event against the token's owner ... The event carries the token and its owner, not
        the acting admin — the request log is where the principal lives."
    spec: spec/feature/BACKEND.md §Event Catalogue — "``AUTH`` (``user``,
        ``entity_id=user_id`` of the token's owner) | ``API_TOKEN_REVOKED`` | An admin
        revokes a token they do not own via ``DELETE /admin/users/{id}/api-tokens/{token_id}``
        ... Detail keys: ``token_id``, ``owner_user_id``."
    spec: spec/API.md §Admin — ``DELETE /admin/users/{id}/api-tokens/{token_id}`` → "``204``
        — revokes a user's token (incident response)".
    """
    import hashlib
    import json

    from sqlalchemy import text

    from src.shared.events import AUTH_API_TOKEN_REVOKED

    owner_id = uuid.uuid4()
    owner_email = _unique_email("revoke-audit")
    token_id = uuid.uuid4()

    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(owner_id),
            "email": owner_email,
            "name": "Revoke Audit Owner",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.api_tokens"
            " (id, user_id, name, token_hash, role_snapshot)"
            " VALUES (:id, :user_id, 'revoke-audit-token', :token_hash, 'Reader')"
        ),
        {
            "id": str(token_id),
            "user_id": str(owner_id),
            "token_hash": hashlib.sha256(f"revoke-audit-{token_id}".encode()).hexdigest(),
        },
    )
    await async_session.commit()

    me = await api_client.get("/api/v1/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    caller_id = me.json()["id"]
    assert caller_id != str(owner_id), "the acting admin must not be the token's owner"

    async def _revoke_events(entity_id: str) -> list:
        # The event type is bound from the impl constant, never spelled as a literal here:
        # a renamed constant must break both this test and its sibling below at once, or
        # the sibling's "no event" assertion silently becomes a query for a type nothing
        # emits (spec/TESTING.md §Assertion Discipline — absence assertions).
        result = await async_session.execute(
            text(
                "SELECT entity_type, status, detail FROM dataspoke.events"
                " WHERE event_type = :event_type AND entity_id = :id"
            ),
            {"event_type": AUTH_API_TOKEN_REVOKED, "id": entity_id},
        )
        await async_session.rollback()  # fresh snapshot for the next read
        return list(result.fetchall())

    try:
        assert await _revoke_events(str(owner_id)) == [], (
            "baseline: the seeded owner starts with no revoke events"
        )

        first = await api_client.delete(
            f"/api/v1/admin/users/{owner_id}/api-tokens/{token_id}", headers=admin_headers
        )
        assert first.status_code == 204, (
            f"the admin revoke must answer 204 per spec/API.md §Admin; got "
            f"{first.status_code}: {first.text}"
        )

        revoked_at = (
            await async_session.execute(
                text("SELECT revoked_at FROM dataspoke.api_tokens WHERE id = :id"),
                {"id": str(token_id)},
            )
        ).scalar_one()
        await async_session.rollback()
        assert revoked_at is not None, (
            "the revoke must actually set revoked_at — that write is what ends the token's "
            "life, and the event describes it. spec: spec/feature/AUTH.md §Admin revoke "
            "audit."
        )

        events = await _revoke_events(str(owner_id))
        assert len(events) == 1, (
            f"exactly one AUTH.API_TOKEN_REVOKED must be booked on the owner; got "
            f"{len(events)}: {events!r}. spec: spec/feature/AUTH.md §Admin revoke audit."
        )
        assert events[0].entity_type == "user", (
            f"the event's entity_type is 'user' per spec/feature/BACKEND.md §Event "
            f"Catalogue; got {events[0].entity_type!r}"
        )
        assert events[0].status == "success", f"got {events[0].status!r}"
        # JSONB comes back as a dict through the asyncpg dialect's codec; the string form
        # is accepted (and parsed) rather than silently skipped, so a driver change cannot
        # turn this into a comparison against a type that never matches.
        detail = events[0].detail
        if isinstance(detail, str):
            detail = json.loads(detail)
        assert isinstance(detail, dict), (
            f"the event's detail must be a JSON object; got {type(detail).__name__}: "
            f"{events[0].detail!r}"
        )
        assert detail == {
            "token_id": str(token_id),
            "owner_user_id": str(owner_id),
        }, (
            f"the detail keys are token_id and owner_user_id, and nothing else — no name, "
            f"hash, or prefix; got {detail!r}. spec: spec/feature/BACKEND.md "
            f"§Event Catalogue — 'Detail keys: token_id, owner_user_id. No token name, "
            f"hash, or prefix'."
        )

        assert await _revoke_events(caller_id) == [], (
            "nothing may be booked on the acting admin's timeline — the event carries the "
            "token and its owner, and the request log is where the principal lives. spec: "
            "spec/feature/AUTH.md §Admin revoke audit."
        )

        repeat = await api_client.delete(
            f"/api/v1/admin/users/{owner_id}/api-tokens/{token_id}", headers=admin_headers
        )
        assert repeat.status_code == 204, (
            f"a repeat revoke is idempotent and still answers 204; got "
            f"{repeat.status_code}: {repeat.text}"
        )
        assert len(await _revoke_events(str(owner_id))) == 1, (
            "an already-revoked token wrote nothing, so it must add no second event — the "
            "write is the security act the event records. spec: spec/feature/AUTH.md "
            "§Admin revoke audit."
        )
        assert (
            await async_session.execute(
                text("SELECT revoked_at FROM dataspoke.api_tokens WHERE id = :id"),
                {"id": str(token_id)},
            )
        ).scalar_one() == revoked_at, (
            "the original revocation timestamp must survive the repeat — it is when the "
            "credential was withdrawn, which is what incident review reads"
        )
        await async_session.rollback()
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id"), {"id": str(owner_id)}
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(owner_id)}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_self_service_revoke_writes_no_audit_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """DELETE /auth/api-tokens/{id} revokes the caller's own token and emits nothing.

    The exclusion is deliberate and specified: a user retiring their own token has no
    privilege asymmetry to audit, and recording it would bury the admin event — the one
    that says someone acted on a credential that was not theirs — under ordinary
    self-service traffic.

    The absence is backstopped twice. The revocation itself is read back from the row, so
    "no event" cannot be the silence of a call that did nothing; and the admin leg in the
    same file proves an ``AUTH.API_TOKEN_REVOKED`` on this same entity type *is* written
    when the spec says it should be, so the query here is one that can find events.

    spec: spec/feature/AUTH.md §Admin revoke audit — "``DELETE /auth/api-tokens/{id}`` emits
        nothing. A user retiring their own token is routine hygiene with no privilege
        asymmetry to audit."
    spec: spec/feature/BACKEND.md §Event Catalogue — "The self-service
        ``DELETE /auth/api-tokens/{id}`` emits nothing."
    spec: spec/feature/AUTH.md §Lifecycle endpoints — "``DELETE /auth/api-tokens/{id}`` |
        Revoke an own token (sets ``revoked_at = now()``)".
    """
    from sqlalchemy import text

    from src.backend.auth.tokens import issue_access_token
    from src.shared.events import AUTH_API_TOKEN_REVOKED

    owner_id = uuid.uuid4()
    owner_email = _unique_email("self-revoke")
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(owner_id),
            "email": owner_email,
            "name": "Self Revoke Owner",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()

    owner_jwt, _ = issue_access_token(owner_id, owner_email, session_epoch=0)
    owner_headers = {"Authorization": f"Bearer {owner_jwt}"}

    try:
        mint = await api_client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "self-revoke-token"},
            headers=owner_headers,
        )
        assert mint.status_code == 201, f"mint failed: {mint.text}"
        token_id = mint.json()["id"]

        delete = await api_client.delete(
            f"/api/v1/auth/api-tokens/{token_id}", headers=owner_headers
        )
        assert delete.status_code == 204, (
            f"the owner's own revoke must answer 204; got {delete.status_code}: "
            f"{delete.text}"
        )

        # Backstop: the revoke really happened, so the absent event below is the spec'd
        # exclusion rather than a call that did nothing.
        revoked_at = (
            await async_session.execute(
                text("SELECT revoked_at FROM dataspoke.api_tokens WHERE id = :id"),
                {"id": token_id},
            )
        ).scalar_one()
        await async_session.rollback()
        assert revoked_at is not None, (
            "the self revoke must set revoked_at per spec/feature/AUTH.md §Lifecycle "
            "endpoints, or 'no event' describes a no-op"
        )

        # Same bound constant the admin leg queries with, so the two cannot drift: if the
        # event type is renamed, the admin leg fails rather than this one going quietly
        # vacuous on a type nothing emits.
        events = (
            await async_session.execute(
                text(
                    "SELECT event_type FROM dataspoke.events"
                    " WHERE event_type = :event_type AND entity_id = :id"
                ),
                {"event_type": AUTH_API_TOKEN_REVOKED, "id": str(owner_id)},
            )
        ).fetchall()
        await async_session.rollback()
        assert list(events) == [], (
            f"the self-service revoke must emit nothing — recording it would bury the admin "
            f"event under ordinary self-service traffic; got {events!r}. spec: "
            f"spec/feature/AUTH.md §Admin revoke audit."
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id"), {"id": str(owner_id)}
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": str(owner_id)}
        )
        await async_session.commit()
