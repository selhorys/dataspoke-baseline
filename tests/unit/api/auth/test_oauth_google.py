"""Unit tests for src/backend/auth/oauth_google.py.

Concerns covered:
- is_configured returns True only when all three of client_id / client_secret / oauth_state_secret
  are non-empty
- resolve_or_create_user resolver table:
  - known google_sub + same name → returns user, no name update (verify update_name not called)
  - known google_sub + different name → updates the DataSpoke row only (no DataHub call)
  - no google_sub + known email, row unbound → binds the sub, resets the row's
    credentials, and records exactly one AUTH.GOOGLE_LINK_CREDENTIAL_RESET event
  - no google_sub + known email, row already carrying THIS sub → plain login, no event
  - no google_sub + known email, row carrying a DIFFERENT sub → refusal propagates
  - no google_sub + unknown email → creates the local row; succeeds with DataHub unreachable

spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
spec: spec/feature/AUTH.md §Credential reset on link
spec: spec/feature/AUTH.md §Projection contract
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


def _make_user(google_sub: str | None = None, name: str = "Test User", session_epoch: int = 0):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "test@example.com"
    user.name = name
    user.google_sub = google_sub
    user.role = "Reader"
    user.session_epoch = session_epoch
    return user


def _capturing_db():
    """Return an AsyncMock session whose ``add()`` records what the resolver stages.

    ``Session.add`` is synchronous, so it is replaced with a plain MagicMock —
    an AsyncMock attribute would return an un-awaited coroutine and record
    nothing useful.
    """
    db = AsyncMock()
    db.add = MagicMock()
    return db


def _auth_events(db):
    """Return the AUTH.* Event rows the resolver staged on *db*."""
    from src.shared.db.models import Event

    return [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], Event)
    ]


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

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
    ):
        mock_get_sub.return_value = existing_user

        result = await resolve_or_create_user(
            mock_db,
            google_sub="google-sub-123",
            email="alice@example.com",
            name="Alice Smith",  # same name — no update needed
        )

    assert result is existing_user
    # update_name must NOT be called when the name is unchanged
    # per spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login
    mock_update_name.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_known_google_sub_different_name_updates_locally_only() -> None:
    """Known google_sub + different name → updates the DataSpoke row and calls no DataHub op.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 1: google_sub known → "Log in. Refresh display name from the Google profile
    onto the DataSpoke row."
    spec: spec/feature/AUTH.md §Identity Model — "DataHub corpuser entity + profile |
    DataHub | ... DataSpoke writes no corpuser aspect." Display name is not projected.
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.datahub import users as dh_users

    existing_user = _make_user(google_sub="google-sub-456", name="Old Name")
    updated_user = _make_user(google_sub="google-sub-456", name="New Name")

    mock_db = AsyncMock()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
        patch.object(dh_users, "propagate_role", new_callable=AsyncMock) as mock_role,
        patch.object(dh_users, "add_user_to_marker_group", new_callable=AsyncMock) as mock_group,
        patch.object(
            dh_users, "ensure_marker_group_exists", new_callable=AsyncMock
        ) as mock_ensure_group,
    ):
        mock_get_sub.return_value = existing_user
        mock_update_name.return_value = updated_user

        result = await resolve_or_create_user(
            mock_db,
            google_sub="google-sub-456",
            email="existing@example.com",
            name="New Name",  # name changed
        )

    assert result is updated_user
    # Backstop: the name-change branch really ran, so the no-DataHub-call asserts
    # below are not vacuously true.
    mock_update_name.assert_called_once()
    for mock_op, op_name in (
        (mock_role, "propagate_role"),
        (mock_group, "add_user_to_marker_group"),
        (mock_ensure_group, "ensure_marker_group_exists"),
    ):
        assert not mock_op.called, (
            f"A display-name refresh must not call {op_name} — display name is not "
            "projected and login makes no DataHub call per spec/feature/AUTH.md "
            "§Identity Model"
        )


@pytest.mark.asyncio
async def test_resolve_known_sub_branch_records_no_credential_reset_event() -> None:
    """A plain login by known ``sub`` writes no binding, so it records no reset event.

    Seeded against the same event predicate the bind branch is asserted on, so the
    absence here is a real exclusion rather than an untested path.

    spec: spec/feature/AUTH.md §Credential reset on link — "Exactly one
    AUTH.GOOGLE_LINK_CREDENTIAL_RESET event ... per bind that actually writes
    `google_sub`".
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user

    existing_user = _make_user(google_sub="google-sub-123", name="Alice Smith")
    mock_db = _capturing_db()

    with patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub:
        mock_get_sub.return_value = existing_user
        result = await resolve_or_create_user(
            mock_db,
            google_sub="google-sub-123",
            email="alice@example.com",
            name="Alice Smith",
        )

    assert result is existing_user
    assert _auth_events(mock_db) == [], (
        "the sub-known branch binds nothing, so it emits no "
        "AUTH.GOOGLE_LINK_CREDENTIAL_RESET event per spec/feature/AUTH.md "
        "§Credential reset on link"
    )


@pytest.mark.asyncio
async def test_resolve_unbound_row_binds_and_records_exactly_one_reset_event() -> None:
    """No google_sub + known unbound row → bind, reset, and exactly one event.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row has `google_sub IS NULL` | Bind `google_sub` onto the row, refresh
    display name from the Google profile, run the credential reset, and log in."
    spec: spec/feature/AUTH.md §Credential reset on link — "Exactly one
    AUTH.GOOGLE_LINK_CREDENTIAL_RESET event (`entity_type = user`, `entity_id` =
    the user id) per bind that actually writes `google_sub`, recording what was
    cleared."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.auth.users import GoogleBindResult
    from src.shared.models.enums import EventStatus

    existing_user = _make_user(google_sub=None, name="Bob Smith", session_epoch=0)
    bound_user = _make_user(google_sub="new-google-sub-000", name="Bob Smith", session_epoch=1)
    bound_user.id = existing_user.id

    mock_db = _capturing_db()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "bind_google_identity", new_callable=AsyncMock) as mock_bind,
    ):
        mock_get_sub.return_value = None  # no match by google_sub
        mock_get_email.return_value = existing_user  # match by email
        mock_bind.return_value = GoogleBindResult(
            user=bound_user, bound=True, api_tokens_revoked=2, reset_tokens_deleted=1
        )

        result = await resolve_or_create_user(
            mock_db,
            google_sub="new-google-sub-000",
            email="bob@example.com",
            name="Bob Smith",
        )

    assert result is bound_user
    mock_bind.assert_awaited_once_with(mock_db, existing_user.id, "new-google-sub-000")

    events = _auth_events(mock_db)
    assert len(events) == 1, (
        "exactly one AUTH.GOOGLE_LINK_CREDENTIAL_RESET event per bind that writes "
        "google_sub per spec/feature/AUTH.md §Credential reset on link; "
        f"got {len(events)}"
    )
    event = events[0]
    assert event.event_type == "AUTH.GOOGLE_LINK_CREDENTIAL_RESET"
    assert event.entity_type == "user", (
        "the event's entity_type is `user` per spec/feature/AUTH.md "
        "§Credential reset on link"
    )
    assert event.entity_id == str(bound_user.id), (
        "the event's entity_id is the user id per spec/feature/AUTH.md "
        "§Credential reset on link"
    )
    # BACKEND_SCHEMA.md §events permits any of success/ok/failure/error/running/
    # warning/info; `success` is the project's chosen value for a completed auth
    # write, pinned here so the two AUTH.* writers stay consistent. The spec does
    # not single it out.
    assert event.status == EventStatus.SUCCESS
    # "recording what was cleared" — the counts the reset returned.
    assert event.detail["api_tokens_revoked"] == 2
    assert event.detail["reset_tokens_deleted"] == 1
    assert event.detail["session_epoch"] == 1
    # The Google `sub` is the one credential value in scope at this layer; the
    # cleared password hash and reset-token hashes never reach this function, so
    # their absence is asserted against real values by the spot sibling
    # (tests/integration/spot/test_auth_google_credential_reset.py).
    assert "new-google-sub-000" not in str(event.detail), (
        "the event detail records what was cleared, never the identity value that "
        f"replaced it, per spec/feature/AUTH.md §Credential reset on link; got {event.detail!r}"
    )


@pytest.mark.asyncio
async def test_resolve_bind_branch_refreshes_the_display_name() -> None:
    """The bind branch takes the display name from the Google claim.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "The bind
    branch refreshes `name` from the Google claim for the same reason the
    `sub`-known branch does: the row now belongs to the verified identity, and the
    display name it presents ... must be that identity's rather than the previous
    holder's."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.auth.users import GoogleBindResult

    squatted_row = _make_user(google_sub=None, name="Squatter Name", session_epoch=0)
    bound_row = _make_user(google_sub="verified-owner-sub", name="Squatter Name")
    bound_row.id = squatted_row.id
    renamed_row = _make_user(google_sub="verified-owner-sub", name="Verified Owner")
    renamed_row.id = squatted_row.id

    mock_db = _capturing_db()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "bind_google_identity", new_callable=AsyncMock) as mock_bind,
        patch.object(_users, "update_name", new_callable=AsyncMock) as mock_update_name,
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = squatted_row
        mock_bind.return_value = GoogleBindResult(
            user=bound_row, bound=True, api_tokens_revoked=0, reset_tokens_deleted=0
        )
        mock_update_name.return_value = renamed_row

        result = await resolve_or_create_user(
            mock_db,
            google_sub="verified-owner-sub",
            email="cto@imazon.example.com",
            name="Verified Owner",
        )

    mock_update_name.assert_awaited_once_with(mock_db, bound_row.id, "Verified Owner")
    assert result is renamed_row, (
        "the resolver must return the row carrying the verified identity's display "
        "name per spec/feature/AUTH.md §Google OAuth registration & login"
    )


@pytest.mark.asyncio
async def test_resolve_row_already_carrying_this_sub_is_a_plain_login() -> None:
    """A row the bind reports as already carrying THIS sub logs in and emits nothing.

    The raced/retried-callback resolution: the second callback finds the first's
    bind committed. It is the same request the sub-known branch serves.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row already carries **this** `sub` | Log in, exactly as the `sub`-known
    branch. No bind, no reset, no epoch bump, no event."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.auth.users import GoogleBindResult

    raced_row = _make_user(google_sub="sub-committed-by-the-first-callback", session_epoch=1)

    mock_db = _capturing_db()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "bind_google_identity", new_callable=AsyncMock) as mock_bind,
    ):
        # The sub lookup missed — the first callback's commit is not visible to it.
        mock_get_sub.return_value = None
        mock_get_email.return_value = raced_row
        mock_bind.return_value = GoogleBindResult(
            user=raced_row, bound=False, api_tokens_revoked=0, reset_tokens_deleted=0
        )

        result = await resolve_or_create_user(
            mock_db,
            google_sub="sub-committed-by-the-first-callback",
            email="raced@imazon.example.com",
            name="Test User",
        )

    assert result is raced_row, "the raced callback resolves to the same row — a plain login"
    # Backstop: the bind branch really ran, so the no-event assert is not vacuous.
    mock_bind.assert_awaited_once()
    assert _auth_events(mock_db) == [], (
        "a callback that finds the row already carrying its own sub writes nothing "
        "and emits nothing per spec/feature/AUTH.md §Credential reset on link"
    )


@pytest.mark.asyncio
async def test_resolve_row_bound_to_another_account_propagates_and_emits_nothing() -> None:
    """A row bound to a different Google account refuses; no event is recorded.

    spec: spec/feature/AUTH.md §Google OAuth registration & login — "No | Yes, and
    that row carries a **different** `google_sub` | Refuse — 409
    EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT. No row is modified and no session is
    issued."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.shared.exceptions import ConflictError

    bound_elsewhere = _make_user(google_sub="the-previous-workspace-sub", session_epoch=4)

    mock_db = _capturing_db()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "bind_google_identity", new_callable=AsyncMock) as mock_bind,
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = bound_elsewhere
        mock_bind.side_effect = ConflictError(
            "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
            "This email is bound to a different Google account.",
        )

        with pytest.raises(ConflictError) as exc_info:
            await resolve_or_create_user(
                mock_db,
                google_sub="the-reissued-address-sub",
                email="reissued@imazon.example.com",
                name="New Holder",
            )

    assert exc_info.value.error_code == "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT", (
        "the resolver must surface the refusal rather than fall through to another "
        "branch per spec/feature/AUTH.md §Google OAuth registration & login"
    )
    assert _auth_events(mock_db) == [], (
        "no row is modified on the refusal, so nothing is recorded per "
        "spec/feature/AUTH.md §Google OAuth registration & login"
    )


@pytest.mark.asyncio
async def test_resolve_new_user_creates_local_row_only() -> None:
    """No google_sub + unknown email → creates the local row and makes no DataHub call.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
    Row 3: "Create a fresh users row with password_hash=null and role = 'Reader'."
    spec: spec/feature/AUTH.md §Projection contract — "User creation is local-only.
    Neither POST /auth/register, nor the Google-OAuth new-user branch, nor
    POST /internal/admin/bootstrap makes a DataHub call".
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.datahub import users as dh_users

    new_user = _make_user(google_sub="brand-new-sub", name="Carol Jones")

    mock_db = _capturing_db()

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "create_user", new_callable=AsyncMock) as mock_create,
        patch.object(
            dh_users, "ensure_marker_group_exists", new_callable=AsyncMock
        ) as mock_ensure_group,
        patch.object(dh_users, "add_user_to_marker_group", new_callable=AsyncMock) as mock_group,
        patch.object(dh_users, "propagate_role", new_callable=AsyncMock) as mock_role,
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = None
        mock_create.return_value = new_user

        result = await resolve_or_create_user(
            mock_db,
            google_sub="brand-new-sub",
            email="carol@example.com",
            name="Carol Jones",
        )

    assert result is new_user
    # Backstop: the create branch really ran.
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["role"] == "Reader", (
        "A Google-OAuth new user is created with role='Reader' per "
        "spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login (Row 3)"
    )
    assert mock_create.call_args.kwargs["password"] is None, (
        "A Google-OAuth new user is created with password_hash=null per "
        "spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login (Row 3)"
    )
    for mock_op, op_name in (
        (mock_ensure_group, "ensure_marker_group_exists"),
        (mock_group, "add_user_to_marker_group"),
        (mock_role, "propagate_role"),
    ):
        assert not mock_op.called, (
            f"New-user creation must not call {op_name} — creation is local-only "
            "per spec/feature/AUTH.md §Projection contract"
        )
    assert _auth_events(mock_db) == [], (
        "a fresh row has no pre-bind credential to clear, so the new-user branch "
        "records no AUTH.GOOGLE_LINK_CREDENTIAL_RESET event per spec/feature/AUTH.md "
        "§Credential reset on link"
    )


@pytest.mark.asyncio
async def test_resolve_new_user_succeeds_when_datahub_unreachable() -> None:
    """OAuth new-user creation succeeds even when DataHub is unreachable.

    Every DataHub projection primitive is made to raise; the resolver must still
    return the created user and must not roll back the session.

    spec: spec/feature/AUTH.md §Failure Modes — "DataHub unreachable or unconfigured
    during any user-creation path (POST /auth/register, Google-OAuth new user,
    POST /internal/admin/bootstrap) | No DataHub call is attempted; the transaction
    is purely local. | Creation succeeds and the user is logged in."
    spec: spec/feature/AUTH.md §Projection contract — "There is no compensating
    delete anywhere on the creation paths, and no user-facing error code for a
    failed projection."
    """
    from src.backend.auth import users as _users
    from src.backend.auth.oauth_google import resolve_or_create_user
    from src.backend.datahub import users as dh_users
    from src.shared.exceptions import DataHubUnavailableError

    new_user = _make_user(google_sub="offline-datahub-sub", name="Dave Offline")

    mock_db = AsyncMock()

    unreachable = DataHubUnavailableError("DataHub unreachable")

    with (
        patch.object(_users, "get_by_google_sub", new_callable=AsyncMock) as mock_get_sub,
        patch.object(_users, "get_by_email", new_callable=AsyncMock) as mock_get_email,
        patch.object(_users, "create_user", new_callable=AsyncMock) as mock_create,
        patch.object(
            dh_users, "corpuser_exists", new_callable=AsyncMock, side_effect=unreachable
        ),
        patch.object(
            dh_users,
            "ensure_marker_group_exists",
            new_callable=AsyncMock,
            side_effect=unreachable,
        ),
        patch.object(
            dh_users,
            "add_user_to_marker_group",
            new_callable=AsyncMock,
            side_effect=unreachable,
        ),
        patch.object(
            dh_users, "propagate_role", new_callable=AsyncMock, side_effect=unreachable
        ),
    ):
        mock_get_sub.return_value = None
        mock_get_email.return_value = None
        mock_create.return_value = new_user

        result = await resolve_or_create_user(
            mock_db,
            google_sub="offline-datahub-sub",
            email="dave@example.com",
            name="Dave Offline",
        )

    assert result is new_user, (
        "Google-OAuth new-user creation must succeed with DataHub unreachable "
        "per spec/feature/AUTH.md §Failure Modes"
    )
    # Backstop: the create branch really ran, so the no-rollback assert is meaningful.
    mock_create.assert_called_once()
    assert not mock_db.rollback.called, (
        "There is no compensating delete on the creation paths — a DataHub failure "
        "must not roll back the local row per spec/feature/AUTH.md §Projection contract"
    )


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

    # Build a fake OAuth client whose authorize_access_token always raises
    mock_google_client = AsyncMock()
    mock_google_client.authorize_access_token = AsyncMock(
        side_effect=Exception("mismatched_state")
    )
    mock_oauth_client = MagicMock()
    mock_oauth_client.google = mock_google_client

    with (
        patch("src.api.routers.auth.oauth_google.is_configured", return_value=True),
        patch(
            "src.api.routers.auth.oauth_google.build_oauth_client",
            return_value=mock_oauth_client,
        ),
    ):
        with pytest.raises(BadRequestError) as exc_info:
            await auth_router.get_google_callback(
                request=mock_request,
                db=mock_db,
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
        patch(
            "src.api.routers.auth.oauth_google.build_oauth_client",
            return_value=mock_oauth_client,
        ),
    ):
        with pytest.raises(BadRequestError) as exc_info:
            await auth_router.get_google_callback(
                request=mock_request,
                db=mock_db,
            )

    assert exc_info.value.error_code == "OAUTH_EMAIL_NOT_VERIFIED", (
        "ID token with email_verified=False must raise BadRequestError('OAUTH_EMAIL_NOT_VERIFIED') "
        "per spec/feature/AUTH.md §Security Considerations §OAuth flow hardening"
    )
