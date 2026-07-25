"""Unit tests for src/backend/auth/privilege.py.

Concerns covered:
- require_authenticated: no header → 401; dsk_<...> → API-token path; other Bearer → JWT path
- require_writer: Reader + POST → ForbiddenError("READ_ONLY_ROLE"); Reader + GET → allowed;
  Editor + POST → allowed
- require_admin: Reader/Editor → ForbiddenError("FORBIDDEN"); Admin → allowed

spec: spec/feature/AUTH.md §Privilege Model
spec: spec/API.md §Access Control
spec: spec/API.md §Authentication & Authorization §Authentication Mechanisms
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The `client` fixture is provided by tests/unit/api/conftest.py, which pytest
# auto-discovers for this subdirectory — no explicit import needed.

# ── Helpers ──────────────────────────────────────────────────────────────────


async def _make_auth_context(role: str = "Admin"):
    """Return (user, effective_role) to simulate a resolved AuthContext."""
    from src.backend.auth.privilege import AuthContext
    from src.shared.db.models import User

    mock_user = MagicMock(spec=User)
    mock_user.id = uuid.uuid4()
    mock_user.role = role
    mock_user.email = f"{role.lower()}@test.example.com"
    mock_user.google_sub = None
    return AuthContext(user=mock_user, effective_role=role)


# ── require_authenticated ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_authenticated_no_token_raises_401(client) -> None:
    """Missing Authorization header returns 401 UNAUTHORIZED.

    spec: spec/feature/AUTH.md §Privilege Model — unauthenticated requests are rejected.
    spec: spec/API.md §Access Control — /auth/me requires authenticated caller.
    """
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401, (
        "Request with no Authorization header must return 401 "
        "per spec/feature/AUTH.md §Privilege Model"
    )
    body = response.json()
    assert body["error_code"] in ("UNAUTHORIZED", "FORBIDDEN"), (
        "Error code must be UNAUTHORIZED or FORBIDDEN per spec/API.md §Error Catalogue"
    )


@pytest.mark.asyncio
async def test_require_authenticated_dsk_token_uses_api_token_path() -> None:
    """Bearer dsk_<...> token is routed to the opaque PAT lookup path.

    spec: spec/feature/AUTH.md §API Tokens §Token carriage —
    middleware fast-path: tokens starting with dsk_ skip JWT decode.
    spec: spec/API.md §Authentication Mechanisms — dsk_<...> → API token lookup.
    """
    from src.backend.auth import api_tokens as _api_tokens
    from src.backend.auth.privilege import require_authenticated

    mock_user = MagicMock()
    mock_user.role = "Editor"

    mock_request = MagicMock()
    mock_request.method = "GET"
    mock_request.state = MagicMock()

    mock_credentials = MagicMock()
    mock_credentials.credentials = "dsk_fake_token_value_for_unit_test_1234567890"

    token_id = uuid.uuid4()

    with patch.object(_api_tokens, "lookup_and_validate", new_callable=AsyncMock) as mock_lookup:
        mock_lookup.return_value = (mock_user, "Editor", token_id)
        ctx = await require_authenticated(
            request=mock_request,
            db=AsyncMock(),
            redis=AsyncMock(),
            credentials=mock_credentials,
        )

    # dsk_-prefixed token must route to api_tokens.lookup_and_validate
    # per spec/feature/AUTH.md §API Tokens §Token carriage
    mock_lookup.assert_called_once()
    assert ctx.effective_role == "Editor"
    assert ctx.user is mock_user
    # spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    # a PAT-authorised caller "re-reads its own api_tokens row under the same
    # users row lock", so the context must name the row that authorised it.
    assert ctx.api_token_id == token_id, (
        "The PAT path must carry the authenticating api_tokens row id on the "
        "AuthContext per spec/feature/AUTH.md §Serialization of credential-creating writes"
    )


@pytest.mark.asyncio
async def test_require_authenticated_jwt_token_uses_jwt_path() -> None:
    """Bearer JWT token is decoded via tokens.decode_access_token.

    spec: spec/API.md §Authentication Mechanisms — JWT path for user tokens.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import require_authenticated
    from src.backend.auth.tokens import issue_access_token

    user_id = uuid.uuid4()
    access_token, _ = issue_access_token(user_id, "jwttest@example.com", 2)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Admin"
    mock_user.email = "jwttest@example.com"
    mock_user.session_epoch = 2

    mock_request = MagicMock()
    mock_request.state = MagicMock()

    mock_credentials = MagicMock()
    mock_credentials.credentials = access_token

    mock_db = AsyncMock()
    get_by_id_result = MagicMock()
    get_by_id_result.scalar_one_or_none.return_value = mock_user
    mock_db.execute = AsyncMock(return_value=get_by_id_result)

    with patch.object(_users, "get_by_id", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_user
        ctx = await require_authenticated(
            request=mock_request,
            db=mock_db,
            redis=AsyncMock(),
            credentials=mock_credentials,
        )

    assert ctx.effective_role == "Admin"
    assert ctx.user is mock_user
    # spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    # a JWT-authorised caller is "re-checked on its `ses` claim", so the context
    # must carry the epoch the token was accepted under.
    assert ctx.session_epoch == 2
    assert ctx.api_token_id is None, (
        "A JWT-authorised context names no api_tokens row — exactly one credential "
        "identifies the caller per spec/feature/AUTH.md "
        "§Serialization of credential-creating writes"
    )


# ── require_authenticated — session-epoch gate ────────────────────────────────


@pytest.mark.asyncio
async def test_require_authenticated_rejects_jwt_from_a_superseded_epoch() -> None:
    """A JWT whose ``ses`` predates a credential reset is rejected 401 UNAUTHORIZED.

    The row's epoch has moved to 4 (a Google bind reset the credentials); the
    token was minted under 3 and names a session that no longer exists.

    spec: spec/feature/AUTH.md §Session epoch — "A JWT whose `ses` claim is
    absent, or does not equal the owner's current `session_epoch`, is rejected
    401 UNAUTHORIZED"; "Enforcement points. The bearer-JWT authentication path
    and POST /auth/token/refresh."
    spec: spec/feature/AUTH.md §Failure Modes — "A JWT presented after its
    owner's session_epoch was incremented ... 401 UNAUTHORIZED".
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import require_authenticated
    from src.backend.auth.tokens import issue_access_token
    from src.shared.exceptions import AuthenticationError

    user_id = uuid.uuid4()
    stale_token, _ = issue_access_token(user_id, "reset@example.com", 3)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Admin"
    mock_user.email = "reset@example.com"
    mock_user.session_epoch = 4  # the bind's increment

    mock_credentials = MagicMock()
    mock_credentials.credentials = stale_token

    with patch.object(_users, "get_by_id", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_user
        with pytest.raises(AuthenticationError) as exc_info:
            await require_authenticated(
                request=MagicMock(),
                db=AsyncMock(),
                redis=AsyncMock(),
                credentials=mock_credentials,
            )

    assert exc_info.value.error_code == "UNAUTHORIZED", (
        "A token issued under a superseded session epoch must be rejected "
        "401 UNAUTHORIZED per spec/feature/AUTH.md §Session epoch"
    )


@pytest.mark.asyncio
async def test_require_authenticated_rejects_jwt_with_no_ses_claim() -> None:
    """A JWT carrying no ``ses`` claim at all is rejected, even against epoch 0.

    The claim is injected out of the payload here rather than merely omitted from
    a helper call, so the rejection is proved against a signature-valid token that
    genuinely lacks the claim.

    spec: spec/feature/AUTH.md §Session epoch — "A JWT whose `ses` claim is
    absent ... is rejected 401 UNAUTHORIZED."
    """
    from datetime import UTC, datetime, timedelta

    import jwt as _jwt

    from src.backend.auth import users as _users
    from src.backend.auth.privilege import require_authenticated
    from src.shared.exceptions import AuthenticationError
    from src.shared.settings import settings

    user_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    epochless_token = _jwt.encode(
        {
            "sub": str(user_id),
            "email": "epochless@example.com",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Admin"
    mock_user.email = "epochless@example.com"
    mock_user.session_epoch = 0

    mock_credentials = MagicMock()
    mock_credentials.credentials = epochless_token

    with patch.object(_users, "get_by_id", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_user
        with pytest.raises(AuthenticationError) as exc_info:
            await require_authenticated(
                request=MagicMock(),
                db=AsyncMock(),
                redis=AsyncMock(),
                credentials=mock_credentials,
            )

    assert exc_info.value.error_code == "UNAUTHORIZED", (
        "A token with no 'ses' claim must be rejected 401 UNAUTHORIZED "
        "per spec/feature/AUTH.md §Session epoch"
    )


@pytest.mark.asyncio
async def test_require_authenticated_runs_no_epoch_check_on_the_api_token_path() -> None:
    """A PAT authenticates regardless of the owner's epoch — those rows are revoked outright.

    The owner's row here carries an epoch far past anything a token could name;
    the PAT still authenticates, because the API-token path has no epoch to
    compare and relies on ``revoked_at`` instead.

    spec: spec/feature/AUTH.md §Session epoch — "The API-token path needs no
    check: those rows are revoked outright."
    spec: spec/feature/AUTH.md §Admin unbind — "It does not revoke the row's API
    tokens, and the PAT authentication path runs no epoch check, so tokens minted
    before the unbind keep working."
    """
    from src.backend.auth import api_tokens as _api_tokens
    from src.backend.auth import tokens as _tokens
    from src.backend.auth.privilege import require_authenticated

    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.role = "Editor"
    mock_user.session_epoch = 99  # far past any epoch a token could name

    mock_credentials = MagicMock()
    mock_credentials.credentials = "dsk_pat_surviving_an_unbind_0123456789"

    token_id = uuid.uuid4()

    with (
        patch.object(_api_tokens, "lookup_and_validate", new_callable=AsyncMock) as mock_lookup,
        patch.object(
            _tokens, "session_epoch_matches", wraps=_tokens.session_epoch_matches
        ) as spy_epoch,
    ):
        mock_lookup.return_value = (mock_user, "Editor", token_id)
        ctx = await require_authenticated(
            request=MagicMock(),
            db=AsyncMock(),
            redis=AsyncMock(),
            credentials=mock_credentials,
        )

    assert ctx.effective_role == "Editor", (
        "A PAT must authenticate irrespective of the owner's session epoch "
        "per spec/feature/AUTH.md §Session epoch"
    )
    spy_epoch.assert_not_called()
    assert ctx.session_epoch is None, (
        "The PAT path carries no session epoch — there is no epoch check on it "
        "per spec/feature/AUTH.md §Session epoch"
    )


# ── revalidate_under_user_lock — the re-check under the row lock ──────────────


@pytest.mark.asyncio
async def test_revalidate_rejects_a_jwt_caller_whose_epoch_moved_under_the_lock() -> None:
    """A JWT caller whose epoch was superseded before the lock is refused 401.

    The context was authorised at epoch 3; the row read under the lock reads 4,
    because a bind committed in between. The credential-creating write must not
    proceed.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "PATCH /auth/me (password) | Re-compare the request's `ses` claim against the
    freshly read `session_epoch`; mismatch → 401 UNAUTHORIZED."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import AuthContext, revalidate_under_user_lock
    from src.shared.db.models import User
    from src.shared.exceptions import AuthenticationError

    caller = MagicMock(spec=User)
    caller.id = uuid.uuid4()
    caller.role = "Editor"

    locked_row = MagicMock(spec=User)
    locked_row.id = caller.id
    locked_row.session_epoch = 4  # the bind's increment landed first

    ctx = AuthContext(user=caller, effective_role="Editor", session_epoch=3)

    with patch.object(_users, "lock_user", new_callable=AsyncMock) as mock_lock:
        mock_lock.return_value = locked_row
        with pytest.raises(AuthenticationError) as exc_info:
            await revalidate_under_user_lock(AsyncMock(), ctx)

    assert exc_info.value.error_code == "UNAUTHORIZED"
    # The re-check must happen under the users row lock, not before it.
    assert mock_lock.await_count == 1


@pytest.mark.asyncio
async def test_revalidate_rejects_a_pat_caller_whose_token_was_revoked_by_the_reset() -> None:
    """A PAT caller whose token the bind revoked is refused 401 TOKEN_REVOKED.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    such a request "instead re-reads its own `api_tokens` row under the same
    `users` row lock and fails 401 TOKEN_REVOKED once the reset has revoked it."
    """
    from src.backend.auth import api_tokens as _api_tokens
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import AuthContext, revalidate_under_user_lock
    from src.shared.db.models import User
    from src.shared.exceptions import AuthenticationError

    caller = MagicMock(spec=User)
    caller.id = uuid.uuid4()
    caller.role = "Editor"

    locked_row = MagicMock(spec=User)
    locked_row.id = caller.id
    locked_row.session_epoch = 4

    ctx = AuthContext(user=caller, effective_role="Editor", api_token_id=uuid.uuid4())

    with (
        patch.object(_users, "lock_user", new_callable=AsyncMock) as mock_lock,
        patch.object(_api_tokens, "is_active", new_callable=AsyncMock) as mock_active,
    ):
        mock_lock.return_value = locked_row
        mock_active.return_value = False  # revoke_all_for_user got there first
        with pytest.raises(AuthenticationError) as exc_info:
            await revalidate_under_user_lock(AsyncMock(), ctx)

    assert exc_info.value.error_code == "TOKEN_REVOKED", (
        "A PAT revoked by the credential reset must fail 401 TOKEN_REVOKED under the "
        "lock per spec/feature/AUTH.md §Serialization of credential-creating writes"
    )
    mock_active.assert_awaited_once()


@pytest.mark.asyncio
async def test_revalidate_rejects_a_caller_whose_row_was_hard_deleted() -> None:
    """A credential-creating write whose subject is gone fails closed with 401.

    The lock read finds no row — the user was hard-deleted after the request
    authenticated — so the write must not proceed against a missing owner.

    spec: spec/feature/AUTH.md §Deletion — "A still-valid access token whose
    subject was deleted fails with `401 UNAUTHORIZED`"; "User deletion is hard
    delete".
    spec: spec/feature/AUTH.md §Serialization of credential-creating writes — the
    write re-checks "the state that authorised it" under the lock.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import AuthContext, revalidate_under_user_lock
    from src.shared.db.models import User
    from src.shared.exceptions import AuthenticationError

    caller = MagicMock(spec=User)
    caller.id = uuid.uuid4()
    caller.role = "Editor"

    ctx = AuthContext(user=caller, effective_role="Editor", session_epoch=3)

    with patch.object(_users, "lock_user", new_callable=AsyncMock) as mock_lock:
        mock_lock.return_value = None  # row hard-deleted
        with pytest.raises(AuthenticationError) as exc_info:
            await revalidate_under_user_lock(AsyncMock(), ctx)

    assert exc_info.value.error_code == "UNAUTHORIZED", (
        "a deleted subject is an authentication failure per spec/feature/AUTH.md §Deletion"
    )


@pytest.mark.asyncio
async def test_revalidate_returns_the_locked_row_when_the_credential_still_holds() -> None:
    """An unsuperseded caller gets the freshly locked row back, so the write proceeds.

    The backstop for the two rejection cases above: without it they could pass by
    rejecting everything.

    spec: spec/feature/AUTH.md §Serialization of credential-creating writes —
    "because each re-reads after acquiring it, none can commit a credential
    authorised by state the bind superseded."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import AuthContext, revalidate_under_user_lock
    from src.shared.db.models import User

    caller = MagicMock(spec=User)
    caller.id = uuid.uuid4()
    caller.role = "Editor"

    locked_row = MagicMock(spec=User)
    locked_row.id = caller.id
    locked_row.session_epoch = 3

    ctx = AuthContext(user=caller, effective_role="Editor", session_epoch=3)

    with patch.object(_users, "lock_user", new_callable=AsyncMock) as mock_lock:
        mock_lock.return_value = locked_row
        result = await revalidate_under_user_lock(AsyncMock(), ctx)

    assert result is locked_row, (
        "the caller must receive the row read under the lock, not its pre-lock copy"
    )


# ── require_writer ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_writer_reader_on_post_raises_forbidden() -> None:
    """Reader + POST raises ForbiddenError('READ_ONLY_ROLE').

    spec: spec/feature/AUTH.md §Privilege Model — Reader role on /spoke/*
    POST/PUT/PATCH/DELETE → 403 READ_ONLY_ROLE.
    spec: spec/API.md §Access Control §Method × role gate.
    """
    from src.backend.auth.privilege import require_writer
    from src.shared.exceptions import ForbiddenError

    ctx = await _make_auth_context("Reader")

    mock_request = MagicMock()
    mock_request.method = "POST"

    with pytest.raises(ForbiddenError) as exc_info:
        await require_writer(request=mock_request, ctx=ctx)

    assert exc_info.value.error_code == "READ_ONLY_ROLE", (
        "Reader on POST must raise ForbiddenError('READ_ONLY_ROLE') "
        "per spec/feature/AUTH.md §Privilege Model"
    )


@pytest.mark.asyncio
async def test_require_writer_reader_on_get_is_allowed() -> None:
    """Reader + GET is allowed (Reader can perform read-only operations).

    spec: spec/feature/AUTH.md §Privilege Model — Reader can GET on /spoke/*.
    spec: spec/API.md §Access Control — Reader: GET/HEAD/OPTIONS only.
    """
    from src.backend.auth.privilege import require_writer

    ctx = await _make_auth_context("Reader")

    mock_request = MagicMock()
    mock_request.method = "GET"

    # Must NOT raise
    result = await require_writer(request=mock_request, ctx=ctx)
    assert result is ctx, "require_writer must pass through the AuthContext on success"


@pytest.mark.asyncio
async def test_require_writer_editor_on_post_is_allowed() -> None:
    """Editor + POST is allowed.

    spec: spec/feature/AUTH.md §Privilege Model — Editor can use all methods on /spoke/*.
    """
    from src.backend.auth.privilege import require_writer

    ctx = await _make_auth_context("Editor")

    mock_request = MagicMock()
    mock_request.method = "POST"

    result = await require_writer(request=mock_request, ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_writer_admin_on_delete_is_allowed() -> None:
    """Admin + DELETE is allowed.

    spec: spec/feature/AUTH.md §Privilege Model — Admin can use all methods.
    """
    from src.backend.auth.privilege import require_writer

    ctx = await _make_auth_context("Admin")

    mock_request = MagicMock()
    mock_request.method = "DELETE"

    result = await require_writer(request=mock_request, ctx=ctx)
    assert result is ctx


# ── require_editor ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_editor_reader_on_get_raises_forbidden() -> None:
    """Reader + GET raises ForbiddenError('READ_ONLY_ROLE') — method is irrelevant.

    spec: spec/feature/AUTH.md §Privilege Model — secrets enumeration is
    restricted to Editor-or-Admin regardless of HTTP method.
    spec: spec/API.md §Ingestion — GET /spoke/ingestion/secrets requires Editor+.
    """
    from src.backend.auth.privilege import require_editor
    from src.shared.exceptions import ForbiddenError

    ctx = await _make_auth_context("Reader")

    with pytest.raises(ForbiddenError) as exc_info:
        await require_editor(ctx=ctx)

    assert exc_info.value.error_code == "READ_ONLY_ROLE", (
        "Reader on require_editor must raise ForbiddenError('READ_ONLY_ROLE') "
        "per spec/feature/AUTH.md §Privilege Model"
    )


@pytest.mark.asyncio
async def test_require_editor_editor_is_allowed() -> None:
    """Editor is allowed through require_editor.

    spec: spec/feature/AUTH.md §Privilege Model — Editor can access Editor+ resources.
    """
    from src.backend.auth.privilege import require_editor

    ctx = await _make_auth_context("Editor")

    result = await require_editor(ctx=ctx)
    assert result is ctx, "require_editor must pass through the AuthContext for Editor role"


@pytest.mark.asyncio
async def test_require_editor_admin_is_allowed() -> None:
    """Admin is allowed through require_editor.

    spec: spec/feature/AUTH.md §Privilege Model — Admin has all privileges.
    """
    from src.backend.auth.privilege import require_editor

    ctx = await _make_auth_context("Admin")

    result = await require_editor(ctx=ctx)
    assert result is ctx, "require_editor must pass through the AuthContext for Admin role"


# ── require_admin ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_admin_reader_raises_forbidden() -> None:
    """Reader accessing /admin/* → ForbiddenError('FORBIDDEN').

    spec: spec/feature/AUTH.md §Privilege Model — /admin/* requires Admin role.
    spec: spec/API.md §Access Control — /admin/* requires users.role='Admin'.
    """
    from src.backend.auth.privilege import require_admin
    from src.shared.exceptions import ForbiddenError

    ctx = await _make_auth_context("Reader")

    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(ctx=ctx)

    assert exc_info.value.error_code == "FORBIDDEN", (
        "Reader on /admin/* must raise ForbiddenError('FORBIDDEN') "
        "per spec/feature/AUTH.md §Privilege Model"
    )


@pytest.mark.asyncio
async def test_require_admin_editor_raises_forbidden() -> None:
    """Editor accessing /admin/* → ForbiddenError('FORBIDDEN').

    spec: spec/feature/AUTH.md §Privilege Model — only Admin role can access /admin/*.
    """
    from src.backend.auth.privilege import require_admin
    from src.shared.exceptions import ForbiddenError

    ctx = await _make_auth_context("Editor")

    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(ctx=ctx)

    assert exc_info.value.error_code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_require_admin_admin_is_allowed() -> None:
    """Admin accessing /admin/* is allowed.

    spec: spec/feature/AUTH.md §Privilege Model — /admin/* requires Admin role.
    """
    from src.backend.auth.privilege import require_admin

    ctx = await _make_auth_context("Admin")

    result = await require_admin(ctx=ctx)
    assert result is ctx, "require_admin must pass through the AuthContext for Admin role"
