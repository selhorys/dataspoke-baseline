"""Spot integration test: DELETE /admin/users/{id} propagates corpuser hard-delete to DataHub.

Local-row deletion + token rejection + email reuse are covered by
spot/test_admin_users.py::test_delete_user_removes_row using DB-seeded users
(which skip the DataHub mirror to avoid /auth/register rate-limit churn).
This test exists to cover the DataHub-side mirror semantics on hard delete,
which require a real registered user with a real corpuser entity.

spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence
"""

import uuid

import httpx
import pytest


def _unique_email(prefix: str = "hard-del") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


@pytest.mark.asyncio
async def test_hard_delete_propagates_corpuser_delete_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """DELETE /admin/users/{id}: DataHub corpuser is hard-deleted.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence —
    step 2: hard-delete the DataHub corpuser via hard_delete_entity. Group
    memberships, role assignments, and ownership references are removed by
    DataHub automatically.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass

    email = _unique_email()

    # Register user via API so DataHub corpuser mirror is created
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Delete Target", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"
    access_token = reg.json()["access_token"]

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    user_id = me.json()["id"]

    # Pre-condition: DataHub corpuser exists
    corpuser_urn = f"urn:li:corpuser:{email}"
    aspect_before = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
    assert aspect_before is not None, "corpuser must exist before deletion"

    # Delete
    del_resp = await api_client.delete(
        f"/api/v1/admin/users/{user_id}",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204, (
        f"DELETE /admin/users/{user_id} must return 204: {del_resp.text}"
    )

    # DataHub corpuser hard-deleted — get_aspect returns None for missing entity
    aspect_after = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
    assert aspect_after is None, (
        f"DataHub corpuser {corpuser_urn} must be gone after hard delete "
        "per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence step 2"
    )
