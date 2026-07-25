"""Unit tests: every user-creation path is a purely local transaction.

spec/feature/AUTH.md §Projection contract — "User creation is local-only. Neither
POST /auth/register, nor the Google-OAuth new-user branch, nor
POST /internal/admin/bootstrap makes a DataHub call — each inserts the DataSpoke
users row (default role = 'Reader', Admin for bootstrap) and issues tokens.
DataSpoke never creates a corpuser."

The Google-OAuth branch is covered in tests/unit/api/auth/test_oauth_google.py;
this module covers the two handlers.

spec: spec/feature/AUTH.md §Projection contract
spec: spec/feature/AUTH.md §Lifecycle §Email + password registration
spec: spec/feature/AUTH.md §Built-in Bootstrap Admin
spec: spec/feature/AUTH.md §Failure Modes
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Every DataHub projection primitive; a creation path may call none of them.
_DATAHUB_PRIMITIVES = (
    "corpuser_exists",
    "read_native_group_membership",
    "ensure_marker_group_exists",
    "add_user_to_marker_group",
    "propagate_role",
    "read_role",
    "hard_delete_corpuser",
)


def _created_user(email: str, role: str) -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = email
    user.name = "Created User"
    user.role = role
    user.google_sub = None
    # The row's epoch rides on the issued JWT as its ``ses`` claim
    # (spec/feature/AUTH.md §Session epoch), so it must be a real int.
    user.session_epoch = 0
    return user


@pytest.mark.asyncio
async def test_register_touches_no_datahub_primitive() -> None:
    """No DataHub projection primitive is reachable from POST /auth/register.

    spec: spec/feature/AUTH.md §Projection contract — "DataSpoke never creates a
    corpuser"; the write-through and reconciliation paths are the only two that
    touch DataHub.
    """
    from src.api.routers import auth as auth_router
    from src.api.schemas.auth import RegisterRequest
    from src.backend.auth import users as auth_users
    from src.backend.datahub import users as dh_users

    mock_db = AsyncMock()
    captured: dict[str, AsyncMock] = {}

    with patch.object(auth_users, "create_user", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _created_user("nodh@example.com", "Reader")
        from contextlib import ExitStack

        with ExitStack() as stack:
            # The handler is wrapped by the slowapi rate limiter, which demands a
            # real starlette Request and a live storage backend; the limit itself
            # is not what this test is about.
            stack.enter_context(patch.object(auth_router.limiter, "enabled", False))
            for name in _DATAHUB_PRIMITIVES:
                captured[name] = stack.enter_context(
                    patch.object(dh_users, name, new_callable=AsyncMock)
                )

            result = await auth_router.post_register(
                request=MagicMock(),
                body=RegisterRequest(
                    email="nodh@example.com", name="No DataHub", password="password1234"
                ),
                response=MagicMock(),
                db=mock_db,
            )

            # Backstop: the handler ran to completion and created the row.
            mock_create.assert_awaited_once()
            assert mock_create.await_args.kwargs["role"] == "Reader", (
                "Registration defaults the role to Reader per spec/feature/AUTH.md "
                "§Lifecycle §Email + password registration"
            )
            assert result.access_token, (
                "Registration returns tokens so the user is logged in immediately per "
                "spec/feature/AUTH.md §Lifecycle §Email + password registration"
            )
            for name, mock_op in captured.items():
                assert not mock_op.called, (
                    f"POST /auth/register must not call {name} — registration is a "
                    "purely local transaction per spec/feature/AUTH.md "
                    "§Lifecycle §Email + password registration"
                )


@pytest.mark.asyncio
async def test_bootstrap_seeds_the_admin_without_any_datahub_call() -> None:
    """POST /internal/admin/bootstrap writes only the local row.

    spec: spec/feature/AUTH.md §Built-in Bootstrap Admin — "DataHub interaction |
    None. Bootstrap writes only the local users row, so it requires no peripheral
    configuration and succeeds on a fresh install before DataHub is wired."
    """
    from contextlib import ExitStack

    from src.api.routers import admin as admin_router
    from src.backend.auth import users as auth_users
    from src.backend.datahub import users as dh_users

    seeded = _created_user("dataspoke@dataspoke.local", "Admin")

    # No Admin exists yet → the create branch runs.
    no_admin = MagicMock()
    no_admin.scalar_one_or_none.return_value = None
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=no_admin)

    with ExitStack() as stack:
        mock_create = stack.enter_context(
            patch.object(auth_users, "create_user", new_callable=AsyncMock)
        )
        mock_create.return_value = seeded
        captured = {
            name: stack.enter_context(patch.object(dh_users, name, new_callable=AsyncMock))
            for name in _DATAHUB_PRIMITIVES
        }

        result = await admin_router.internal_bootstrap(db=mock_db)

        # Backstop: the seed branch really ran.
        mock_create.assert_awaited_once()
        assert mock_create.await_args.kwargs["email"] == "dataspoke@dataspoke.local", (
            "The bootstrap login identifier is dataspoke@dataspoke.local per "
            "spec/feature/AUTH.md §Built-in Bootstrap Admin"
        )
        assert mock_create.await_args.kwargs["role"] == "Admin"
        assert mock_create.await_args.kwargs["password"] == "dataspoke", (
            "The initial password is 'dataspoke' per spec/feature/AUTH.md "
            "§Built-in Bootstrap Admin"
        )
        assert mock_create.await_args.kwargs.get("google_sub") is None, (
            "The bootstrap row carries no google_sub per spec/feature/AUTH.md "
            "§Built-in Bootstrap Admin"
        )
        assert result.created is True

        for name, mock_op in captured.items():
            assert not mock_op.called, (
                f"POST /internal/admin/bootstrap must not call {name} — it writes only "
                "the local users row per spec/feature/AUTH.md §Built-in Bootstrap Admin"
            )


@pytest.mark.asyncio
async def test_bootstrap_is_a_noop_when_an_admin_already_exists() -> None:
    """An existing Admin makes bootstrap return created=False and touch nothing.

    spec: spec/feature/AUTH.md §Built-in Bootstrap Admin — "seeds the row
    idempotently: if any user with role = 'Admin' already exists it returns
    {created: false} and changes nothing."
    """
    from src.api.routers import admin as admin_router
    from src.backend.auth import users as auth_users

    existing = MagicMock()
    existing.scalar_one_or_none.return_value = _created_user("someone@example.com", "Admin")
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=existing)

    with patch.object(auth_users, "create_user", new_callable=AsyncMock) as mock_create:
        result = await admin_router.internal_bootstrap(db=mock_db)

    assert result.created is False, (
        "Bootstrap is idempotent per spec/feature/AUTH.md §Built-in Bootstrap Admin"
    )
    assert not mock_create.called, "Nothing is changed when an Admin already exists"
    assert not mock_db.commit.called


def test_creation_endpoints_declare_no_datahub_dependency() -> None:
    """No user-creation route takes a DataHub dependency.

    The behavioural tests above call the endpoint functions directly, bypassing
    FastAPI's dependency injection — so a reintroduced ``Depends(get_datahub)`` in a
    signature would not fail them, while it would reintroduce exactly the peripheral
    coupling this contract forbids. Guard the declaration itself.

    spec: spec/feature/AUTH.md §Projection contract — "Neither POST /auth/register,
    nor the Google-OAuth new-user branch, nor POST /internal/admin/bootstrap makes a
    DataHub call."
    spec: spec/feature/AUTH.md §Built-in Bootstrap Admin — bootstrap "requires no
    peripheral configuration and succeeds on a fresh install before DataHub is wired."
    """
    import inspect

    from src.api.dependencies import get_datahub
    from src.api.routers import admin as admin_router
    from src.api.routers import auth as auth_router

    endpoints = {
        "POST /auth/register": auth_router.post_register,
        "GET /auth/google/callback": auth_router.get_google_callback,
        "POST /internal/admin/bootstrap": admin_router.internal_bootstrap,
    }

    for label, fn in endpoints.items():
        for name, param in inspect.signature(fn).parameters.items():
            dependency = getattr(param.default, "dependency", None)
            assert dependency is not get_datahub, (
                f"{label} declares a DataHub dependency via parameter '{name}' — "
                "user creation is local-only and must not require the peripheral "
                "per spec/feature/AUTH.md §Projection contract"
            )
