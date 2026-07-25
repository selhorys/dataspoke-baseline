"""Spot integration test: the credential reset a Google bind runs on a pre-registered row.

The Google side cannot be driven over HTTP — `GET /auth/google/callback` needs a
real authorisation code from Google — so the bind itself is performed by calling
`oauth_google.resolve_or_create_user` against the real database, which the spot
tier explicitly permits (spec/TESTING.md §Spot integration tests: "a spot test may
call dataspoke Python directly ... or call the API over HTTP"). Everything the
bind is supposed to have destroyed is then asserted **through the API**, which is
the only way to prove a credential is actually dead rather than merely
absent-looking in a table.

Concerns covered (one per test):
- the pre-bind password no longer logs in
- the pre-bind API token is revoked
- the pre-bind refresh cookie and access token are both rejected
- the unused password-reset row is gone
- the post-bind session works and `/auth/me` reports google-only
- exactly one `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event records what was cleared
- a second Google identity presenting the same email is refused

spec: spec/feature/AUTH.md §Credential reset on link
spec: spec/feature/AUTH.md §Google OAuth registration & login
spec: spec/feature/AUTH.md §Session epoch
spec: spec/feature/AUTH.md §Security Considerations §Account pre-hijacking on Google link
"""

import hashlib
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import pool as sa_pool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The squatter's password — held only here in plaintext; the bcrypt protocol stays
# inside src/backend/auth/users.create_user.
PRE_BIND_PASSWORD = "pre-bind-password-1"


def _ingress_url() -> str:
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    return f"http://api.{domain}"


@pytest_asyncio.fixture(scope="module")
async def bound_row(integration_db_url: str) -> AsyncGenerator[dict[str, object]]:
    """Pre-register a row, stock it with every credential, then bind a Google identity onto it.

    Mirrors the pre-hijacking scenario: a squatter registers the address with a
    password, mints an API token, holds a live session and a pending reset link;
    then the address's verified owner signs in with Google for the first time.

    Every credential handed to the tests is proved to work **before** the bind, so
    a later 401 is the reset doing its job rather than a credential that was never
    valid. Yields only plain values — no live engine or session crosses the
    fixture boundary.

    Two rows are seeded, not one. The bystander carries the same credential kinds
    as the target and is never named by the bind, so it holds the reset's blast
    radius: a `revoke_all_for_user` that lost its `user_id` predicate would revoke
    tokens across the whole deployment while every single-user assertion stayed
    green. The bound row additionally carries a *consumed* reset row, which the
    `used_at IS NULL` predicate must spare.

    spec: spec/feature/AUTH.md §Security Considerations §Account pre-hijacking on
    Google link — "each credential it clears is an independent re-entry path".
    """
    from src.backend.auth import oauth_google
    from src.backend.auth import users as user_service
    from src.backend.auth.tokens import issue_access_token

    email = f"prehijack-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    google_sub = f"test-sub-{uuid.uuid4()}"
    # The raw reset token is never stored; only its SHA-256 hash is (spec
    # §Password-reset token storage), so the seeded row carries the hash.
    raw_reset_token = secrets.token_urlsafe(32)
    reset_token_hash = hashlib.sha256(raw_reset_token.encode()).hexdigest()
    used_reset_token_hash = hashlib.sha256(
        secrets.token_urlsafe(32).encode()
    ).hexdigest()

    bystander_email = f"bystander-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    bystander_reset_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    client = httpx.AsyncClient(base_url=_ingress_url(), timeout=30.0)
    user_id: uuid.UUID | None = None
    bystander_id: uuid.UUID | None = None

    try:
        async with factory() as session:
            user = await user_service.create_user(
                session, email, "Pre-registered Squatter", password=PRE_BIND_PASSWORD
            )
            bystander = await user_service.create_user(
                session, bystander_email, "Unrelated Bystander", password="bystander-password-1"
            )
            await session.commit()
            user_id = user.id
            bystander_id = bystander.id
            epoch_before = user.session_epoch

            # A pending password-reset link — the fourth credential the reset clears.
            # No REST route reaches this state without a configured SMTP peripheral,
            # which is exactly why this test lives at spot.
            await session.execute(
                text(
                    "INSERT INTO dataspoke.password_reset_tokens"
                    " (token_hash, user_id, expires_at) VALUES (:h, :uid, :exp)"
                ),
                {
                    "h": reset_token_hash,
                    "uid": str(user_id),
                    "exp": datetime.now(tz=UTC) + timedelta(minutes=15),
                },
            )
            # A consumed reset row on the same user. The reset deletes only unused
            # rows, so this one must survive: it authenticates nothing and is left
            # for the periodic housekeeping pass.
            await session.execute(
                text(
                    "INSERT INTO dataspoke.password_reset_tokens"
                    " (token_hash, user_id, expires_at, used_at)"
                    " VALUES (:h, :uid, :exp, :used)"
                ),
                {
                    "h": used_reset_token_hash,
                    "uid": str(user_id),
                    "exp": datetime.now(tz=UTC) + timedelta(minutes=15),
                    "used": datetime.now(tz=UTC) - timedelta(minutes=1),
                },
            )
            # An unused reset row belonging to the bystander — the row a
            # predicate-less DELETE would sweep along with the target's.
            await session.execute(
                text(
                    "INSERT INTO dataspoke.password_reset_tokens"
                    " (token_hash, user_id, expires_at) VALUES (:h, :uid, :exp)"
                ),
                {
                    "h": bystander_reset_hash,
                    "uid": str(bystander_id),
                    "exp": datetime.now(tz=UTC) + timedelta(minutes=15),
                },
            )
            await session.commit()

        # ── The bystander's own API token, minted through the API. ──
        bystander_jwt, _ = issue_access_token(bystander_id, bystander_email, session_epoch=0)
        bystander_mint = await client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "bystander-ci-token"},
            headers={"Authorization": f"Bearer {bystander_jwt}"},
        )
        assert bystander_mint.status_code == 201, (
            f"the bystander's API-token mint must succeed: {bystander_mint.text}"
        )
        bystander_api_token = bystander_mint.json()["token"]

        # ── Pre-bind: password login works, and yields session A. ──
        login_a = await client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": PRE_BIND_PASSWORD},
        )
        assert login_a.status_code == 200, (
            f"the pre-bind password must log in before the bind: {login_a.text}"
        )
        access_token = login_a.json()["access_token"]
        refresh_cookie_a = login_a.cookies["refresh_token"]

        # ── Pre-bind: a minted API token authenticates. ──
        mint = await client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "squatter-ci-token"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert mint.status_code == 201, f"pre-bind API-token mint must succeed: {mint.text}"
        api_token = mint.json()["token"]

        probe = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {api_token}"}
        )
        assert probe.status_code == 200, (
            f"the pre-bind API token must authenticate before the bind: {probe.text}"
        )

        # ── Pre-bind: the access token authenticates. ──
        probe = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
        )
        assert probe.status_code == 200, (
            f"the pre-bind access token must authenticate before the bind: {probe.text}"
        )

        # ── Pre-bind: the refresh path works. Session A's cookie is spent proving
        # it (refresh rotates and revokes the presented token), so session B below
        # supplies the untouched cookie the post-bind assertion needs. ──
        rotated = await client.post(
            "/api/v1/auth/token/refresh", cookies={"refresh_token": refresh_cookie_a}
        )
        assert rotated.status_code == 200, (
            f"the refresh path must work for this user before the bind: {rotated.text}"
        )

        login_b = await client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": PRE_BIND_PASSWORD},
        )
        assert login_b.status_code == 200, f"second pre-bind login must succeed: {login_b.text}"
        refresh_cookie_b = login_b.cookies["refresh_token"]

        # ── The owner's first Google sign-in: bind + credential reset, one commit. ──
        async with factory() as session:
            bound = await oauth_google.resolve_or_create_user(
                session,
                google_sub=google_sub,
                email=email,
                name="Verified Owner",
            )
            await session.commit()
            epoch_after = bound.session_epoch

        yield {
            "user_id": user_id,
            "email": email,
            "google_sub": google_sub,
            "epoch_before": epoch_before,
            "epoch_after": epoch_after,
            "access_token": access_token,
            "api_token": api_token,
            "refresh_cookie": refresh_cookie_b,
            "reset_token_hash": reset_token_hash,
            "used_reset_token_hash": used_reset_token_hash,
            "bystander_id": bystander_id,
            "bystander_api_token": bystander_api_token,
            "bystander_reset_hash": bystander_reset_hash,
        }
    finally:
        await client.aclose()
        try:
            # CASCADE removes each row's api_tokens and password_reset_tokens.
            async with factory() as session:
                for row_id in (user_id, bystander_id):
                    if row_id is not None:
                        await session.execute(
                            text("DELETE FROM dataspoke.users WHERE id = :id"),
                            {"id": str(row_id)},
                        )
                await session.commit()
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_the_bind_increments_the_session_epoch_by_one(
    bound_row: dict[str, object],
) -> None:
    """The reset moves the row's generation counter exactly one step.

    spec: spec/feature/AUTH.md §Session epoch §Exactness — "A credential reset
    increments `session_epoch` by one in the same transaction as the other
    invalidations."
    """
    assert bound_row["epoch_after"] == bound_row["epoch_before"] + 1, (
        "the bind must increment session_epoch by exactly one per spec/feature/AUTH.md "
        f"§Session epoch; {bound_row['epoch_before']} → {bound_row['epoch_after']}"
    )


@pytest.mark.asyncio
async def test_the_pre_bind_password_no_longer_logs_in(
    api_client: httpx.AsyncClient,
    bound_row: dict[str, object],
) -> None:
    """POST /auth/token with the pre-bind password is refused.

    The fixture proved this exact password logged in before the bind, so the 401
    here is the cleared hash rather than a wrong password.

    spec: spec/feature/AUTH.md §Credential reset on link — "Password |
    `password_hash` set to `NULL`."
    """
    resp = await api_client.post(
        "/api/v1/auth/token",
        json={"email": bound_row["email"], "password": PRE_BIND_PASSWORD},
    )

    assert resp.status_code == 401, (
        "the pre-bind password must stop working once the row is bound per "
        f"spec/feature/AUTH.md §Credential reset on link; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_the_pre_bind_api_token_is_revoked(
    api_client: httpx.AsyncClient,
    bound_row: dict[str, object],
) -> None:
    """The API token minted before the bind fails 401 TOKEN_REVOKED.

    spec: spec/feature/AUTH.md §Credential reset on link — "API tokens | Every
    active token for the user is revoked (`revoked_at = now()`)."
    spec: spec/feature/AUTH.md §API Tokens — "A revoked token
    (`revoked_at IS NOT NULL`) fails authentication with `401 TOKEN_REVOKED`".
    """
    resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {bound_row['api_token']}"},
    )

    assert resp.status_code == 401, (
        "a pre-bind API token must be revoked by the bind per spec/feature/AUTH.md "
        f"§Credential reset on link; got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "TOKEN_REVOKED", (
        "the revoked token must report TOKEN_REVOKED per spec/feature/AUTH.md §API Tokens; "
        f"got {resp.json()!r}"
    )


@pytest.mark.asyncio
async def test_the_pre_bind_refresh_cookie_is_rejected(
    api_client: httpx.AsyncClient,
    bound_row: dict[str, object],
) -> None:
    """A refresh cookie issued under the old epoch mints nothing.

    spec: spec/feature/AUTH.md §Credential reset on link — "JWT sessions | All
    outstanding access and refresh tokens are killed by incrementing
    `session_epoch`".
    spec: spec/feature/AUTH.md §Session epoch — "Enforcement points. The
    bearer-JWT authentication path and `POST /auth/token/refresh`."
    """
    resp = await api_client.post(
        "/api/v1/auth/token/refresh",
        cookies={"refresh_token": bound_row["refresh_cookie"]},
    )

    assert resp.status_code == 401, (
        "a refresh cookie predating the bind must be rejected per spec/feature/AUTH.md "
        f"§Session epoch; got {resp.status_code}: {resp.text}"
    )
    assert "access_token" not in resp.json(), (
        "no access token may be minted for a session the reset evicted"
    )


@pytest.mark.asyncio
async def test_the_pre_bind_access_token_is_rejected(
    api_client: httpx.AsyncClient,
    bound_row: dict[str, object],
) -> None:
    """The still-unexpired access token from before the bind no longer authenticates.

    spec: spec/feature/AUTH.md §Session epoch — "A JWT whose `ses` claim is absent,
    or does not equal the owner's current `session_epoch`, is rejected
    `401 UNAUTHORIZED`."
    spec: spec/feature/AUTH.md §Failure Modes — "A JWT presented after its owner's
    `session_epoch` was incremented ... `401 UNAUTHORIZED`".
    """
    resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {bound_row['access_token']}"},
    )

    assert resp.status_code == 401, (
        "an access token predating the bind must be rejected per spec/feature/AUTH.md "
        f"§Session epoch; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_the_unused_reset_token_row_is_deleted(
    async_session: AsyncSession,
    bound_row: dict[str, object],
) -> None:
    """The pending reset link the fixture seeded no longer matches a row.

    spec: spec/feature/AUTH.md §Credential reset on link — "Password-reset tokens |
    Unused `password_reset_tokens` rows for the user are deleted."
    """
    result = await async_session.execute(
        text(
            "SELECT token_hash FROM dataspoke.password_reset_tokens WHERE token_hash = :h"
        ),
        {"h": bound_row["reset_token_hash"]},
    )

    assert result.fetchone() is None, (
        "the bind must delete the row's unused password-reset tokens per "
        "spec/feature/AUTH.md §Credential reset on link"
    )


@pytest.mark.asyncio
async def test_a_consumed_reset_row_survives_the_bind(
    async_session: AsyncSession,
    bound_row: dict[str, object],
) -> None:
    """The delete spares reset rows that were already used — it is scoped to unused ones.

    Seeded alongside the unused row the sibling test proves is gone, so the two
    together show the `used_at IS NULL` predicate is doing work rather than the
    delete sweeping the user's whole reset history
    (spec/TESTING.md §Assertion Discipline — filter tests seed both sides).

    spec: spec/feature/AUTH.md §Credential reset on link — "Unused
    `password_reset_tokens` rows for the user are deleted."
    """
    result = await async_session.execute(
        text("SELECT used_at FROM dataspoke.password_reset_tokens WHERE token_hash = :h"),
        {"h": bound_row["used_reset_token_hash"]},
    )
    row = result.fetchone()

    assert row is not None, (
        "a consumed reset row authenticates nothing and must survive the bind — only "
        "unused rows are deleted per spec/feature/AUTH.md §Credential reset on link"
    )
    assert row.used_at is not None


@pytest.mark.asyncio
async def test_the_reset_does_not_reach_another_users_credentials(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
    bound_row: dict[str, object],
) -> None:
    """An unrelated user's API token and pending reset link are untouched by the bind.

    The blast-radius guard. The reset's revoke and delete are both scoped by
    `user_id`; a statement that lost that predicate would revoke every token in
    the deployment while every assertion about the bound row still passed.

    spec: spec/feature/AUTH.md §Credential reset on link — the reset covers "the
    whole pre-bind credential surface" of **the row it binds onto**: "API tokens |
    Every active token **for the user** is revoked"; "Unused
    `password_reset_tokens` rows **for the user** are deleted."
    """
    survives = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {bound_row['bystander_api_token']}"},
    )

    assert survives.status_code == 200, (
        "the reset must revoke only the bound row's API tokens per spec/feature/AUTH.md "
        f"§Credential reset on link; the bystander's token got {survives.status_code}: "
        f"{survives.text}"
    )
    assert survives.json()["id"] == str(bound_row["bystander_id"]), (
        "the surviving token must still resolve to its own owner"
    )

    result = await async_session.execute(
        text(
            "SELECT token_hash FROM dataspoke.password_reset_tokens WHERE token_hash = :h"
        ),
        {"h": bound_row["bystander_reset_hash"]},
    )
    assert result.fetchone() is not None, (
        "the reset must delete only the bound row's unused reset tokens per "
        "spec/feature/AUTH.md §Credential reset on link"
    )


@pytest.mark.asyncio
async def test_the_post_bind_session_works_and_me_reports_google_only(
    api_client: httpx.AsyncClient,
    bound_row: dict[str, object],
) -> None:
    """The session the callback mints after the commit is valid, and /auth/me shows the swap.

    The refresh token is minted here exactly as `GET /auth/google/callback` mints
    it — after the bind commits, carrying the post-reset epoch — because the
    Google leg of the callback cannot be driven over HTTP.

    spec: spec/feature/AUTH.md §Session epoch §Exactness — "the session token the
    OAuth callback mints afterwards reads the new epoch and is valid".
    spec: spec/feature/AUTH.md §Profile read & update — `/auth/me` returns
    "the booleans `has_password` and `has_google` — the presence of each
    authentication method, never the hash or the `sub`".
    spec: spec/feature/AUTH.md §Failure Modes — after a bind, "`GET /auth/me`
    reports `has_password: false`".
    spec: spec/feature/AUTH.md §Google OAuth registration & login — "The bind
    branch refreshes `name` from the Google claim ... the display name it presents
    in `/auth/me` ... must be that identity's rather than the previous holder's."
    """
    from src.backend.auth.tokens import issue_refresh_token

    post_bind_cookie = issue_refresh_token(
        bound_row["user_id"], bound_row["epoch_after"]
    )

    refreshed = await api_client.post(
        "/api/v1/auth/token/refresh", cookies={"refresh_token": post_bind_cookie}
    )
    assert refreshed.status_code == 200, (
        "the post-bind session must be valid per spec/feature/AUTH.md §Session epoch "
        f"§Exactness; got {refreshed.status_code}: {refreshed.text}"
    )

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert me.status_code == 200, f"the post-bind access token must authenticate: {me.text}"
    body = me.json()
    assert body["has_google"] is True, (
        "the row now carries a Google identity per spec/feature/AUTH.md "
        f"§Profile read & update; got {body!r}"
    )
    assert body["has_password"] is False, (
        "the bind cleared the password, which is what tells the user to set a new one "
        f"per spec/feature/AUTH.md §Profile read & update; got {body!r}"
    )
    assert body["email"] == bound_row["email"]
    # The row was created as "Pre-registered Squatter"; the bind refreshed the
    # display name from the Google claim, which is the only end-to-end proof of
    # that rule in the suite.
    assert body["name"] == "Verified Owner", (
        "the bind branch refreshes `name` from the Google claim, so /auth/me must "
        "present the verified identity's name rather than the previous holder's, per "
        f"spec/feature/AUTH.md §Google OAuth registration & login; got {body!r}"
    )
    assert "google_sub" not in body, (
        "/auth/me reports the presence of each authentication method, never the sub, "
        "per spec/feature/AUTH.md §Profile read & update"
    )


@pytest.mark.asyncio
async def test_the_bind_records_exactly_one_credential_reset_event(
    async_session: AsyncSession,
    bound_row: dict[str, object],
) -> None:
    """One AUTH.GOOGLE_LINK_CREDENTIAL_RESET row records what was cleared, and no secret.

    Bound by ``entity_id`` rather than counted over a time window, so a concurrent
    run on the shared cluster cannot perturb it
    (spec/TESTING.md §Integration Lifecycle & Isolation).

    spec: spec/feature/AUTH.md §Credential reset on link — "Exactly one
    `AUTH.GOOGLE_LINK_CREDENTIAL_RESET` event (`entity_type = user`, `entity_id` =
    the user id) per bind that actually writes `google_sub`, recording what was
    cleared."
    """
    result = await async_session.execute(
        text(
            "SELECT entity_type, entity_id, status, detail FROM dataspoke.events"
            " WHERE event_type = 'AUTH.GOOGLE_LINK_CREDENTIAL_RESET'"
            "   AND entity_id = :user_id"
        ),
        {"user_id": str(bound_row["user_id"])},
    )
    rows = result.fetchall()

    assert len(rows) == 1, (
        "exactly one AUTH.GOOGLE_LINK_CREDENTIAL_RESET event per bind that writes "
        f"google_sub per spec/feature/AUTH.md §Credential reset on link; got {len(rows)}"
    )
    row = rows[0]
    assert row.entity_type == "user", (
        "the event's entity_type is `user` per spec/feature/AUTH.md §Credential reset on link"
    )
    # BACKEND_SCHEMA.md §events permits success/ok/failure/error/running/warning/
    # info. `success` is the project's chosen value for a completed AUTH.* write,
    # not one the spec singles out; pinned so the two AUTH.* writers agree.
    assert row.status == "success", (
        f"auth events record the reset as a success; got {row.status!r}"
    )

    # The detail keys are named in spec/feature/BACKEND.md §Event Catalogue:
    # "Detail keys: `api_tokens_revoked` (int), `reset_tokens_deleted` (int),
    # `session_epoch` (the new value)." AUTH.md says only "recording what was
    # cleared" and names no keys.
    detail = row.detail if isinstance(row.detail, dict) else {}
    # The fixture minted one API token and seeded one unused reset row, so the
    # counts are the concrete effects rather than incidental values.
    assert detail.get("api_tokens_revoked") == 1, (
        "the detail records how many API tokens the reset revoked per "
        f"spec/feature/BACKEND.md §Event Catalogue; got {detail!r}"
    )
    assert detail.get("reset_tokens_deleted") == 1, (
        "the detail records how many unused reset tokens the reset deleted per "
        f"spec/feature/BACKEND.md §Event Catalogue; got {detail!r}"
    )
    assert detail.get("session_epoch") == bound_row["epoch_after"], (
        "the detail records the epoch the reset established per spec/feature/BACKEND.md "
        f"§Event Catalogue; got {detail!r}"
    )

    # The `sub` is the one credential value this event's writer actually holds, so
    # its absence is the real assertion here.
    # spec: spec/feature/BACKEND.md §Event Catalogue — the unbind event has the
    # "Same no-secrets shape as the bind event — no `sub`, no hash."
    assert str(bound_row["google_sub"]) not in str(detail), (
        "the event records what was cleared, never the identity value that replaced "
        f"it, per spec/feature/BACKEND.md §Event Catalogue; got {detail!r}"
    )
    # Defence-in-depth only: the pre-bind password, the raw PAT, and the reset-token
    # hash are never in scope of the code that builds `detail`, so these cannot fail
    # under any local change — they guard a future widening of the payload.
    for value in (bound_row["reset_token_hash"], bound_row["api_token"], PRE_BIND_PASSWORD):
        assert str(value) not in str(detail)


@pytest.mark.asyncio
async def test_a_second_google_identity_on_the_same_email_is_refused(
    integration_db_url: str,
    bound_row: dict[str, object],
) -> None:
    """A different Google `sub` arriving at the bound row is refused, and nothing moves.

    ``ConflictError`` is the 409 the callback surfaces — spec/API.md §Error
    Catalogue maps `EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT` to 409.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row carries a **different** `google_sub` | Refuse — `409
    EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT`. No row is modified and no session is
    issued."
    """
    from src.backend.auth import oauth_google
    from src.shared.exceptions import ConflictError

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            with pytest.raises(ConflictError) as exc_info:
                await oauth_google.resolve_or_create_user(
                    session,
                    google_sub=f"a-different-sub-{uuid.uuid4()}",
                    email=bound_row["email"],
                    name="Impostor",
                )

        async with factory() as session:
            after = await session.execute(
                text(
                    "SELECT google_sub, session_epoch FROM dataspoke.users WHERE id = :id"
                ),
                {"id": str(bound_row["user_id"])},
            )
            row = after.fetchone()
    finally:
        await engine.dispose()

    assert exc_info.value.error_code == "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT", (
        "a bound row is never silently rebound per spec/feature/AUTH.md "
        f"§Google OAuth registration & login; got {exc_info.value.error_code!r}"
    )
    assert row is not None
    assert row.google_sub == bound_row["google_sub"], (
        "no row is modified on the refusal per spec/feature/AUTH.md "
        "§Google OAuth registration & login"
    )
    assert row.session_epoch == bound_row["epoch_after"], (
        "a refused bind changes no credential, so the epoch does not move per "
        "spec/feature/AUTH.md §Session epoch"
    )
