"""Spot integration test: role change propagates to DataHub.

Concerns covered:
- PATCH /admin/users/{id}/role to Editor propagates to DataHub RoleMembership aspect
- The aspect's atomic single-role reflects the new role after the PATCH

spec: spec/feature/AUTH.md §Admin Surface — PATCH /admin/users/{id}/role writes DataSpoke first
then propagates to DataHub via batchAssignRole.
spec: spec/feature/AUTH.md §Identity Model — DataSpoke is the SSOT; DataHub holds propagated copies.
"""

import uuid

import httpx
import pytest
from datahub.metadata.schema_classes import RoleMembershipClass


def _unique_email(prefix: str = "role-change") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _read_role_aspect(datahub_client, corpuser_urn: str) -> str | None:
    """Return the corpuser's atomic single role from the RoleMembership aspect."""
    aspect = await datahub_client.get_aspect(corpuser_urn, RoleMembershipClass)
    if aspect is None or not aspect.roles:
        return None
    return aspect.roles[0].removeprefix("urn:li:dataHubRole:")


@pytest.mark.asyncio
async def test_role_change_propagates_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """PATCH /admin/users/{id}/role to Editor: RoleMembership aspect now holds Editor.

    spec: spec/feature/AUTH.md §Admin Surface — PATCH /admin/users/{id}/role writes DataSpoke first
    then propagates to DataHub via batchAssignRole. DataSpoke is SSOT.
    spec: spec/feature/AUTH.md §DataHub Mirror Semantics — one-way mirror DataSpoke→DataHub.
    """
    email = _unique_email()

    # Register a user (starts as Reader)
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Role Change User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"
    access_token = reg.json()["access_token"]

    # Get user id
    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    user_id = me.json()["id"]

    # Promote to Editor
    role_resp = await api_client.patch(
        f"/api/v1/admin/users/{user_id}/role",
        json={"role": "Editor"},
        headers=admin_headers,
    )
    assert role_resp.status_code == 200, f"Role patch failed: {role_resp.text}"

    # Verify the RoleMembership aspect (atomic single-role).
    corpuser_urn = f"urn:li:corpuser:{email}"
    role = await _read_role_aspect(datahub_client, corpuser_urn)
    assert role == "Editor", (
        f"RoleMembership aspect must be Editor after PATCH /admin/users/{user_id}/role "
        f"per spec/feature/AUTH.md §Admin Surface. Got: {role}"
    )
