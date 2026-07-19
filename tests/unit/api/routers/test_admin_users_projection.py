"""Unit tests for the DataHub projection gate on PATCH /admin/users/{id}/role.

The write-through path is one of the two projection paths in
spec/feature/AUTH.md §Projection contract, and it is subject to the
§Identity-binding requirement: it writes to DataHub only when the DataSpoke row
carries a ``google_sub``.

Concerns covered:
- google_sub IS NULL → the local role write happens, no DataHub call at all
- google_sub present → batchAssignRole is issued against the lowercased corpuser URN
- DataHub failure → the DataSpoke-side write still commits and the call returns 200

spec: spec/feature/AUTH.md §Projection contract §Identity-binding requirement
spec: spec/feature/AUTH.md §Admin Surface
spec: spec/feature/AUTH.md §Failure Modes
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _user_row(*, email: str, google_sub: str | None, role: str = "Editor") -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = email
    user.google_sub = google_sub
    user.role = role
    return user


@pytest.mark.asyncio
async def test_patch_role_unbound_user_writes_locally_and_makes_no_datahub_call() -> None:
    """A row with google_sub IS NULL gets the local role write and no DataHub call.

    spec: spec/feature/AUTH.md §Identity-binding requirement — "Both paths project
    only onto users whose row has `google_sub IS NOT NULL`. A row created by
    password registration alone is never projected, on either path."
    spec: spec/feature/AUTH.md §Admin Surface — "PATCH /admin/users/{id}/role |
    Update users.role ... and, when the row carries a google_sub, propagate to
    DataHub via batchAssignRole."
    """
    from src.api.routers import admin as admin_router
    from src.api.schemas.admin import UserRolePatchRequest
    from src.backend.auth import users as auth_users
    from src.backend.datahub import users as dh_users

    unbound = _user_row(email="squatter@example.com", google_sub=None, role="Editor")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(auth_users, "update_role", new_callable=AsyncMock) as mock_update_role,
        patch.object(dh_users, "propagate_role", new_callable=AsyncMock) as mock_propagate,
    ):
        mock_update_role.return_value = unbound

        result = await admin_router.patch_user_role(
            unbound.id,
            UserRolePatchRequest(role="Editor"),
            db=mock_db,
            datahub=mock_datahub,
        )

    # Backstop: the local write DID happen, so "no DataHub call" is not vacuous.
    mock_update_role.assert_awaited_once()
    assert result == {"role": "Editor"}
    assert not mock_propagate.called, (
        "A password-registered row (google_sub IS NULL) must never be projected "
        "per spec/feature/AUTH.md §Identity-binding requirement"
    )
    assert not mock_datahub.execute_graphql.called, (
        "No DataHub call of any kind may be made for an unbound row per "
        "spec/feature/AUTH.md §Identity-binding requirement"
    )
    mock_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_patch_role_bound_user_projects_to_lowercased_corpuser_urn() -> None:
    """A row carrying a google_sub is projected via batchAssignRole on the lowercased URN.

    spec: spec/feature/AUTH.md §Projection contract — "Write-through |
    PATCH /admin/users/{id}/role | Role, via batchAssignRole, after the users.role
    write commits."
    spec: spec/feature/AUTH.md §URN conventions — "The email is lowercased before
    URN derivation."
    """
    from src.api.routers import admin as admin_router
    from src.api.schemas.admin import UserRolePatchRequest
    from src.backend.auth import users as auth_users
    from src.backend.datahub import users as dh_users

    bound = _user_row(email="Bound@Example.com", google_sub="google-sub-777", role="Admin")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(auth_users, "update_role", new_callable=AsyncMock) as mock_update_role,
        patch.object(dh_users, "propagate_role", new_callable=AsyncMock) as mock_propagate,
    ):
        mock_update_role.return_value = bound

        result = await admin_router.patch_user_role(
            bound.id,
            UserRolePatchRequest(role="Admin"),
            db=mock_db,
            datahub=mock_datahub,
        )

    assert result == {"role": "Admin"}
    mock_propagate.assert_awaited_once()
    projected_urn = mock_propagate.await_args.args[1]
    projected_role = mock_propagate.await_args.args[2]
    assert projected_urn == "urn:li:corpuser:bound@example.com", (
        "The projection must target the lowercased corpuser URN — the URN DataHub's "
        "OIDC JIT provisions — per spec/feature/AUTH.md §URN conventions; got "
        f"{projected_urn!r}"
    )
    assert projected_role == "Admin", (
        "DataSpoke is SSOT for role, so the newly-written role is what is projected "
        "per spec/feature/AUTH.md §Projection contract"
    )


@pytest.mark.asyncio
async def test_patch_role_datahub_failure_still_returns_the_new_role() -> None:
    """A failed projection does not roll back or block the DataSpoke-side role write.

    spec: spec/feature/AUTH.md §Failure Modes — "Role change via
    /admin/users/{id}/role: DataSpoke write succeeds, DataHub propagation fails |
    The new role takes effect immediately on the DataSpoke API ... The admin call
    returns 200".
    """
    from src.api.routers import admin as admin_router
    from src.api.schemas.admin import UserRolePatchRequest
    from src.backend.auth import users as auth_users
    from src.backend.datahub import users as dh_users
    from src.shared.exceptions import DataHubUnavailableError

    bound = _user_row(email="bound@example.com", google_sub="google-sub-888", role="Reader")

    mock_db = AsyncMock()
    mock_datahub = AsyncMock()

    with (
        patch.object(auth_users, "update_role", new_callable=AsyncMock) as mock_update_role,
        patch.object(
            dh_users,
            "propagate_role",
            new_callable=AsyncMock,
            side_effect=DataHubUnavailableError("DataHub down"),
        ) as mock_propagate,
    ):
        mock_update_role.return_value = bound

        result = await admin_router.patch_user_role(
            bound.id,
            UserRolePatchRequest(role="Reader"),
            db=mock_db,
            datahub=mock_datahub,
        )

    # Backstop: the failing projection was actually attempted.
    mock_propagate.assert_awaited_once()
    assert result == {"role": "Reader"}, (
        "A failed projection must not turn the admin call into an error per "
        "spec/feature/AUTH.md §Failure Modes"
    )
    mock_db.commit.assert_awaited()
    assert not mock_db.rollback.called, (
        "A DataHub failure never rolls back the DataSpoke-side write per "
        "spec/feature/AUTH.md §Projection contract"
    )
