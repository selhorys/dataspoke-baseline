"""Spot integration test: the write-through projection on PATCH /admin/users/{id}/role.

Concerns covered (one per test):
- A bound user (google_sub set) with a provisioned corpuser: the new role lands on
  the RoleMembership aspect
- An unbound user (password registration only): the role changes in DataSpoke and
  nothing at all is written to DataHub

Why spot: both cases require a ``users`` row whose ``google_sub`` state is chosen
by the test, and the bound case additionally requires a corpuser that DataHub's
OIDC JIT provisioned. Neither is reachable through the REST pipeline — the Google
OAuth round-trip and a real DataHub login cannot be driven from a test — so the
row is seeded with raw SQL and the corpuser with a direct aspect emit.

spec: spec/feature/AUTH.md §Projection contract — write-through path
spec: spec/feature/AUTH.md §Identity-binding requirement
spec: spec/feature/AUTH.md §Admin Surface
"""

import uuid

import httpx
import pytest
from datahub.metadata.schema_classes import (
    CorpUserInfoClass,
    RoleMembershipClass,
    StatusClass,
)
from sqlalchemy import text


def _unique_email(prefix: str = "role-change") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _read_role_aspect(datahub_client, corpuser_urn: str) -> str | None:
    """Return the corpuser's atomic single role from the RoleMembership aspect."""
    aspect = await datahub_client.get_aspect(corpuser_urn, RoleMembershipClass)
    if aspect is None or not aspect.roles:
        return None
    return aspect.roles[0].removeprefix("urn:li:dataHubRole:")


async def _provision_corpuser(datahub_client, email: str, name: str) -> str:
    """Stand in for DataHub's OIDC JIT provisioning of a corpuser on first DataHub login."""
    urn = f"urn:li:corpuser:{email.lower()}"
    await datahub_client.emit_aspect(urn, StatusClass(removed=False))
    await datahub_client.emit_aspect(
        urn, CorpUserInfoClass(active=True, email=email.lower(), displayName=name)
    )
    return urn


@pytest.mark.asyncio
async def test_role_change_projects_onto_bound_user_corpuser(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """PATCH /admin/users/{id}/role on a bound user: the RoleMembership aspect holds Editor.

    spec: spec/feature/AUTH.md §Projection contract — "Write-through |
    PATCH /admin/users/{id}/role | Role, via batchAssignRole, after the users.role
    write commits."
    spec: spec/feature/AUTH.md §Admin Surface — "Update users.role (Admin / Editor /
    Reader) and, when the row carries a google_sub, propagate to DataHub via
    batchAssignRole."
    """
    from src.backend.datahub.users import hard_delete_corpuser

    email = _unique_email("bound")
    user_id = str(uuid.uuid4())

    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, password_hash, google_sub, role) "
            "VALUES (:id, :email, 'Bound Role Change User', NULL, :google_sub, 'Reader')"
        ),
        {"id": user_id, "email": email, "google_sub": f"google-sub-{user_id}"},
    )
    await async_session.commit()

    urn = f"urn:li:corpuser:{email.lower()}"
    try:
        await _provision_corpuser(datahub_client, email, "Bound Role Change User")

        role_resp = await api_client.patch(
            f"/api/v1/admin/users/{user_id}/role",
            json={"role": "Editor"},
            headers=admin_headers,
        )
        assert role_resp.status_code == 200, f"Role patch failed: {role_resp.text}"
        assert role_resp.json() == {"role": "Editor"}

        role = await _read_role_aspect(datahub_client, urn)
        assert role == "Editor", (
            f"RoleMembership aspect must be Editor after PATCH /admin/users/{user_id}/role "
            "on a google_sub-bound row per spec/feature/AUTH.md §Projection contract. "
            f"Got: {role}"
        )
    finally:
        await hard_delete_corpuser(datahub_client, urn)
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": user_id}
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_role_change_on_unbound_user_writes_nothing_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """PATCH /admin/users/{id}/role on a password-registered user: no DataHub write at all.

    Containment is load-bearing for the typosquatting mitigation: a squatter's row
    must never steer the real address-owner's DataHub identity.

    spec: spec/feature/AUTH.md §Identity-binding requirement — "Both paths project
    only onto users whose row has google_sub IS NOT NULL. A row created by password
    registration alone is never projected, on either path."
    spec: spec/feature/AUTH.md §Security Considerations §Email verification omitted
    by design — "a password-registered row has no google_sub, so neither projection
    path ever writes against urn:li:corpuser:ceo@example.com."
    """
    from src.backend.datahub.users import hard_delete_corpuser, propagate_role

    email = _unique_email("unbound")

    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Unbound Role Change User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {reg.json()['access_token']}"},
    )
    assert me.status_code == 200, f"GET /auth/me failed: {me.text}"
    user_id = me.json()["id"]

    urn = f"urn:li:corpuser:{email.lower()}"
    try:
        # The corpuser must EXIST and hold a role for this test to have teeth. Against
        # a nonexistent corpuser a batchAssignRole is silently dropped by DataHub's
        # RoleService while reporting success, so an absent aspect would prove nothing
        # about the gate. This is spec/feature/AUTH.md §Security Considerations: the
        # genuine owner of the address signs into DataHub and the corpuser is theirs.
        await _provision_corpuser(datahub_client, email, "Genuine Owner")
        await propagate_role(datahub_client, urn, "Admin")
        assert await _read_role_aspect(datahub_client, urn) == "Admin", (
            "Precondition: the genuine owner's corpuser holds Admin before the PATCH"
        )

        role_resp = await api_client.patch(
            f"/api/v1/admin/users/{user_id}/role",
            json={"role": "Editor"},
            headers=admin_headers,
        )
        assert role_resp.status_code == 200, f"Role patch failed: {role_resp.text}"
        assert role_resp.json() == {"role": "Editor"}, (
            "The DataSpoke-side role write is unconditional — only the projection is "
            "gated per spec/feature/AUTH.md §Identity-binding requirement"
        )

        # Backstop: the role really did change in DataSpoke, so the assertions below
        # are about a projection that was declined, not about a no-op request.
        stored = await async_session.execute(
            text("SELECT role, google_sub FROM dataspoke.users WHERE id = :id"),
            {"id": user_id},
        )
        row = stored.fetchone()
        assert row is not None and row.role == "Editor", (
            f"users.role must be Editor after the PATCH; got {row}"
        )
        assert row.google_sub is None, (
            "Precondition: the row must be unbound for the binding gate to apply"
        )

        assert await _read_role_aspect(datahub_client, urn) == "Admin", (
            f"The unbound row's Editor role must NOT overwrite the role held at {urn} "
            "— that corpuser belongs to the identity DataHub verified, not to the "
            "unbound DataSpoke row, per spec/feature/AUTH.md §Identity-binding "
            "requirement"
        )
    finally:
        await hard_delete_corpuser(datahub_client, urn)
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": user_id}
        )
        await async_session.commit()
