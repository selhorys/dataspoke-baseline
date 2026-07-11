"""Unit tests for src/backend/auth/oauth_google.py.

Concerns covered:
- is_configured returns True only when all three of client_id / client_secret / oauth_state_secret
  are non-empty
- resolve_or_create_user resolver table:
  - known google_sub + same name → returns user, no name update (verify update_name not called)
  - known google_sub + different name → updates name AND calls dh_users.ensure_corpuser_exists
    (best-effort) AND swallows DataHubUnavailableError
  - no google_sub + known email → links the sub onto the existing row
  - no google_sub + unknown email → creates user + runs DataHub mirror;
    on mirror failure rolls back session and raises DataHubSyncError

spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
spec: spec/feature/AUTH.md §Failure Modes
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── is_configured ─────────────────────────────────────────────────────────────


def test_is_configured_all_present_returns_true() -> None:
    """is_configured returns True when all three OAuth fields are non-empty.

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening — when
    client_id is empty, both routes return 503 OAUTH_NOT_CONFIGURED.
    """
    from src.backend.auth.oauth_google import is_configured

    mock_settings = MagicMock()
    mock_settings.google_oauth_client_id = "client-id-value"
    mock_settings.google_oauth_client_secret = "client-secret-value"
    mock_settings.oauth_state_secret = "state-secret-value"

    assert is_configured(mock_settings) is True, (
        "is_configured must return True when all three credentials are present "
        "per spec/feature/AUTH.md §Security Considerations §OAuth flow hardening"
    )


def test_is_configured_missing_client_id_returns_false() -> None:
    """is_configured returns False when google_oauth_client_id is empty.

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening —
    empty client_id → 503 OAUTH_NOT_CONFIGURED.
    """
    from src.backend.auth.oauth_google import is_configured

    mock_settings = MagicMock()
    mock_settings.google_oauth_client_id = ""
    mock_settings.google_oauth_client_secret = "client-secret-value"
    mock_settings.oauth_state_secret = "state-secret-value"

    assert is_configured(mock_settings) is False


def test_is_configured_missing_client_secret_returns_false() -> None:
    """is_configured returns False when google_oauth_client_secret is empty.

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening
    """
    from src.backend.auth.oauth_google import is_configured

    mock_settings = MagicMock()
    mock_settings.google_oauth_client_id = "client-id-value"
    mock_settings.google_oauth_client_secret = ""
    mock_settings.oauth_state_secret = "state-secret-value"

    assert is_configured(mock_settings) is False


def test_is_configured_missing_state_secret_returns_false() -> None:
    """is_configured returns False when oauth_state_secret is empty.

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening —
    state cookie is HMAC-signed with oauth_state_secret.
    """
    from src.backend.auth.oauth_google import is_configured

    mock_settings = MagicMock()
    mock_settings.google_oauth_client_id = "client-id-value"
    mock_settings.google_oauth_client_secret = "client-secret-value"
    mock_settings.oauth_state_secret = ""

    assert is_configured(mock_settings) is False


# ── resolve_or_create_user resolver table ────────────────────────────────────


def _make_runtime_config(group_name: str = "dataspoke-users"):
    rc = MagicMock()
    rc.auth_datahub_corp_group = group_name
    return rc


def _make_user(google_sub: str | None = None, name: str = "Test User"):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "test@example.com"
    user.name = name
    user.google_sub = google_sub
    user.role = "Reader"
    return user


@pytest.mark.asyncio
async def test_resolve_known_google_sub_same_name_no_update() -> None:
    """Known google_sub + same name → returns user without calling update_name.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 1: google_sub known → log in; refresh display name from Google profile.
    When name is unchanged, no update is required.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user

    existing_user = _make_user(google_sub="google-sub-123", name="Alice Smith")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
    ):
        mock_get_sub.return_value = existing_user

        result = await resolve_or_create_user(
            mock_db,
            mock_datahub,
            google_sub="google-sub-123",
            email="alice@example.com",
            name="Alice Smith",  # same name — no update needed
            runtime_config=_make_runtime_config(),
        )

    assert result is existing_user
    # update_name must NOT be called when the name is unchanged
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
    mock_update_name.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_known_google_sub_different_name_updates_and_propagates() -> None:
    """Known google_sub + different name → updates name and propagates to DataHub best-effort.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 1: google_sub known → log in; refresh display name from Google profile.
    Name change → update DataSpoke + propagate to DataHub (best-effort).
    """
    from src.backend.auth import users as _users
    from src.backend.datahub import users as dh_users
    from src.backend.auth.oauth_google import resolve_or_create_user

    existing_user = _make_user(google_sub="google-sub-456", name="Old Name")
    updated_user = _make_user(google_sub="google-sub-456", name="New Name")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
        patch.object(dh_users, "ensure_corpuser_exists", new_callable=AsyncMock) as mock_dh,
    ):
        mock_get_sub.return_value = existing_user
        mock_update_name.return_value = updated_user

        result = await resolve_or_create_user(
            mock_db,
            mock_datahub,
            google_sub="google-sub-456",
            email="existing@example.com",
            name="New Name",  # name changed
            runtime_config=_make_runtime_config(),
        )

    assert result is updated_user
    # update_name must be called when name changed
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
    mock_update_name.assert_called_once()
    # ensure_corpuser_exists must be called to propagate name to DataHub
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
    mock_dh.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_known_google_sub_different_name_swallows_datahub_error() -> None:
    """DataHubUnavailableError during name propagation is swallowed (best-effort).

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login — name propagation is
    best-effort; DataHub failure must not block login.
    """
    from src.backend.auth import users as _users
    from src.backend.datahub import users as dh_users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.shared.exceptions import DataHubUnavailableError

    existing_user = _make_user(google_sub="google-sub-789", name="Old Name")
    updated_user = _make_user(google_sub="google-sub-789", name="New Name")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
        patch.object(dh_users, "ensure_corpuser_exists", new_callable=AsyncMock) as mock_dh,
    ):
        mock_get_sub.return_value = existing_user
        mock_update_name.return_value = updated_user
        mock_dh.side_effect = DataHubUnavailableError("DataHub down")

        # Must NOT raise — DataHub error is swallowed
        result = await resolve_or_create_user(
            mock_db,
            mock_datahub,
            google_sub="google-sub-789",
            email="propagate@example.com",
            name="New Name",
            runtime_config=_make_runtime_config(),
        )

    assert result is updated_user, (
        "DataHubUnavailableError during name propagation must be swallowed (best-effort) "
        "per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login"
    )


@pytest.mark.asyncio
async def test_resolve_no_google_sub_known_email_links_sub() -> None:
    """No google_sub + known email → links the sub onto the existing row.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 2: google_sub unknown, email matches existing row → link google_sub, log in.
    Linking preserves password access.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user

    existing_user = _make_user(google_sub=None, name="Bob Smith")
    linked_user = _make_user(google_sub="new-google-sub-000", name="Bob Smith")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "link_google_sub", new_callable=AsyncMock) as mock_link,
    ):
        mock_get_sub.return_value = None  # no match by google_sub
        mock_get_email.return_value = existing_user  # match by email
        mock_link.return_value = linked_user

        result = await resolve_or_create_user(
            mock_db,
            mock_datahub,
            google_sub="new-google-sub-000",
            email="bob@example.com",
            name="Bob Smith",
            runtime_config=_make_runtime_config(),
        )

    assert result is linked_user
    # link_google_sub must be called when email matches but no google_sub
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login (Row 2)
    mock_link.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_new_user_mirror_success() -> None:
    """No google_sub + unknown email → creates new user + runs DataHub mirror.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 3: google_sub unknown, email unknown → create fresh user, run mirror sequence.
    """
    from src.backend.auth import users as _users
    from src.backend.datahub import users as dh_users
    from src.backend.auth.oauth_google import resolve_or_create_user

    new_user = _make_user(google_sub="brand-new-sub", name="Carol Jones")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "create_user", new_callable=AsyncMock) as mock_create,
        patch.object(dh_users, "ensure_corpuser_exists", new_callable=AsyncMock),
        patch.object(dh_users, "ensure_marker_group_exists", new_callable=AsyncMock),
        patch.object(dh_users, "add_user_to_marker_group", new_callable=AsyncMock),
        patch.object(dh_users, "propagate_role", new_callable=AsyncMock),
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = None
        mock_create.return_value = new_user

        result = await resolve_or_create_user(
            mock_db,
            mock_datahub,
            google_sub="brand-new-sub",
            email="carol@example.com",
            name="Carol Jones",
            runtime_config=_make_runtime_config(),
        )

    assert result is new_user
    # create_user must be called for a completely unknown email
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login (Row 3)
    mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_new_user_mirror_failure_raises_datahub_sync_error() -> None:
    """Mirror failure during new-user creation rolls back session and raises DataHubSyncError.

    spec: spec/feature/AUTH.md §Failure Modes — DataHub unreachable during register →
    compensating hard-delete of the DataSpoke users row; 503 DATAHUB_SYNC_FAILED.
    """
    from src.backend.auth import users as _users
    from src.backend.datahub import users as dh_users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.shared.exceptions import DataHubSyncError

    new_user = _make_user(google_sub="mirror-fail-sub", name="Dave Error")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "create_user", new_callable=AsyncMock) as mock_create,
        patch.object(dh_users, "ensure_corpuser_exists", new_callable=AsyncMock) as mock_dh,
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = None
        mock_create.return_value = new_user
        mock_dh.side_effect = Exception("DataHub transport error")

        # spec: spec/feature/AUTH.md §Failure Modes — mirror failure → DataHubSyncError
        with pytest.raises(DataHubSyncError):
            await resolve_or_create_user(
                mock_db,
                mock_datahub,
                google_sub="mirror-fail-sub",
                email="dave@example.com",
                name="Dave Error",
                runtime_config=_make_runtime_config(),
            )

    # Session must be rolled back on failure
    # Session must be rolled back after mirror failure
    # per spec/feature/AUTH.md §Failure Modes (compensating hard-delete)
    mock_db.rollback.assert_called_once()


# ── OAuth callback: state mismatch (F8) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_google_callback_state_mismatch_raises_bad_request() -> None:
    """get_google_callback raises BadRequestError(OAUTH_STATE_MISMATCH) when authlib raises.

    authlib's authorize_access_token raises when the state query param does not
    match the session-stored value. The callback handler catches any exception from
    that call and maps it to OAUTH_STATE_MISMATCH.

    Approach: monkeypatch is_configured to return True so the handler proceeds past
    the OAUTH_NOT_CONFIGURED guard, then mock build_oauth_client so that
    authorize_access_token raises (simulating a state/nonce mismatch).

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening —
          mismatches return 400 OAUTH_STATE_MISMATCH without attempting token exchange.
    spec: spec/feature/AUTH.md §Failure Modes — Google OAuth state mismatch →
          400 OAUTH_STATE_MISMATCH.
    """
    from unittest.mock import patch

    from src.api.routers import auth as auth_router
    from src.shared.exceptions import BadRequestError

    mock_request = MagicMock()
    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    # Build a fake OAuth client whose authorize_access_token always raises
    mock_google_client = AsyncMock()
    mock_google_client.authorize_access_token = AsyncMock(
        side_effect=Exception("mismatched_state")
    )
    mock_oauth_client = MagicMock()
    mock_oauth_client.google = mock_google_client

    with (
        patch("src.api.routers.auth.oauth_google.is_configured", return_value=True),
        patch("src.api.routers.auth.oauth_google.build_oauth_client", return_value=mock_oauth_client),
    ):
        with pytest.raises(BadRequestError) as exc_info:
            await auth_router.get_google_callback(
                request=mock_request,
                db=mock_db,
                datahub=mock_datahub,
            )

    assert exc_info.value.error_code == "OAUTH_STATE_MISMATCH", (
        "Any exception from authorize_access_token must map to "
        "BadRequestError('OAUTH_STATE_MISMATCH') per spec/feature/AUTH.md §Security Considerations "
        "§OAuth flow hardening"
    )


# ── OAuth callback: email_verified=False (F9) ─────────────────────────────────


@pytest.mark.asyncio
async def test_google_callback_email_not_verified_raises_bad_request() -> None:
    """get_google_callback raises BadRequestError(OAUTH_EMAIL_NOT_VERIFIED) for unverified email.

    The Google callback rejects ID tokens with email_verified=false. Unverified Google
    emails cannot resolve to a DataSpoke account.

    Approach: mock authorize_access_token to return a token_response with
    userinfo.email_verified=False. Assert the handler raises OAUTH_EMAIL_NOT_VERIFIED.

    spec: spec/feature/AUTH.md §Security Considerations §OAuth flow hardening —
          The Google callback rejects ID tokens with email_verified=false; unverified
          Google emails cannot resolve to a DataSpoke account.
    """
    from unittest.mock import patch

    from src.api.routers import auth as auth_router
    from src.shared.exceptions import BadRequestError

    mock_request = MagicMock()
    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    # Token response with email_verified=False
    mock_token_response = {
        "userinfo": {
            "sub": "google-sub-unverified",
            "email": "unverified@example.com",
            "name": "Unverified User",
            "email_verified": False,
        }
    }

    mock_google_client = AsyncMock()
    mock_google_client.authorize_access_token = AsyncMock(return_value=mock_token_response)
    mock_oauth_client = MagicMock()
    mock_oauth_client.google = mock_google_client

    with (
        patch("src.api.routers.auth.oauth_google.is_configured", return_value=True),
        patch("src.api.routers.auth.oauth_google.build_oauth_client", return_value=mock_oauth_client),
    ):
        with pytest.raises(BadRequestError) as exc_info:
            await auth_router.get_google_callback(
                request=mock_request,
                db=mock_db,
                datahub=mock_datahub,
            )

    assert exc_info.value.error_code == "OAUTH_EMAIL_NOT_VERIFIED", (
        "ID token with email_verified=False must raise BadRequestError('OAUTH_EMAIL_NOT_VERIFIED') "
        "per spec/feature/AUTH.md §Security Considerations §OAuth flow hardening"
    )
