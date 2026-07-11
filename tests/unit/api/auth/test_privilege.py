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

    with patch.object(_api_tokens, "lookup_and_validate", new_callable=AsyncMock) as mock_lookup:
        mock_lookup.return_value = (mock_user, "Editor")
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


@pytest.mark.asyncio
async def test_require_authenticated_jwt_token_uses_jwt_path() -> None:
    """Bearer JWT token is decoded via tokens.decode_access_token.

    spec: spec/API.md §Authentication Mechanisms — JWT path for user tokens.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.privilege import require_authenticated
    from src.backend.auth.tokens import issue_access_token

    user_id = uuid.uuid4()
    access_token, _ = issue_access_token(user_id, "jwttest@example.com")

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.role = "Admin"
    mock_user.email = "jwttest@example.com"

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
