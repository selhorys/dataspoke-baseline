"""API-wired integration test: user hard deletion.

Concerns covered:
- DELETE /admin/users/{id} removes the local users row
- DataHub corpuser is hard-deleted (corpuser entity returns null or 404)

spec: spec/feature/AUTH.md §Lifecycle §Deletion
spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence
spec: spec/API.md §Admin DELETE /admin/users/{id}
"""

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession


def _unique_email(prefix: str = "hard-del") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


@pytest.mark.asyncio
async def test_hard_delete_removes_local_row_and_datahub_corpuser(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
    async_session: AsyncSession,
) -> None:
    """DELETE /admin/users/{id}: local row gone AND DataHub corpuser hard-deleted.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence —
    step 1: hard-delete the DataSpoke users row.
    step 2: hard-delete the DataHub corpuser via hard_delete_entity.
    Group memberships, role assignments, and ownership references are removed by DataHub automatically.

    Uses /auth/register to create the user (so DataHub gets the corpuser mirror).
    Verifies email reusability via DB INSERT after deletion instead of a second
    /auth/register call (avoids the 5/min per-IP rate limit when run with other tests).
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

    # Verify DataHub corpuser exists before deletion
    corpuser_urn = f"urn:li:corpuser:{email}"
    aspect_before = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
    assert aspect_before is not None, "corpuser must exist before deletion"

    # Delete the user
    del_resp = await api_client.delete(
        f"/api/v1/admin/users/{user_id}",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204, f"DELETE /admin/users/{user_id} must return 204: {del_resp.text}"

    # Local row must be gone: token is rejected
    me_after = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_after.status_code in (401, 403), (
        "Deleted user's token must be rejected per spec/feature/AUTH.md §Lifecycle §Deletion"
    )

    # Email immediately reusable — verified via create_user (avoids /auth/register rate limit).
    # If the old row still exists, create_user raises EMAIL_ALREADY_REGISTERED.
    from sqlalchemy import text
    from src.backend.auth import users as user_service

    new_user = await user_service.create_user(
        async_session, email, "Re-registered User", password="password1234"
    )
    await async_session.commit()
    # If we reach here, the email is reusable (no uniqueness violation)
    # Clean up the re-created row
    await async_session.execute(
        text("DELETE FROM dataspoke.users WHERE id = :id"),
        {"id": str(new_user.id)},
    )
    await async_session.commit()

    # DataHub corpuser hard-deleted — get_aspect returns None for deleted entity
    # Note: DataHub hard_delete_entity removes the entity and all its references.
    # The SDK returns None when the entity does not exist.
    aspect_after = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
    assert aspect_after is None, (
        f"DataHub corpuser {corpuser_urn} must be gone after hard delete "
        "per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence step 2"
    )
