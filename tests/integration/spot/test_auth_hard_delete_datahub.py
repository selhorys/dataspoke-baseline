"""Spot integration test: DELETE /admin/users/{id} retracts the DataHub projection.

Local-row deletion + token rejection + email reuse are covered by
spot/test_admin_users.py::test_delete_user_removes_row using DB-seeded users.
This test covers step 2 of the retraction sequence — the corpuser hard-delete —
which requires a corpuser to exist in the first place.

Why spot: the corpuser is created by DataHub's OIDC JIT on a real DataHub login,
which a test cannot perform, so it is materialised here with a direct aspect emit.

spec: spec/feature/AUTH.md §Projection retraction sequence
"""

import uuid

import httpx
import pytest
from sqlalchemy import text


def _unique_email(prefix: str = "hard-del") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


@pytest.mark.asyncio
async def test_hard_delete_propagates_corpuser_delete_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """DELETE /admin/users/{id}: the DataHub corpuser is hard-deleted.

    Retraction is not gated on the identity binding — it is the only path that
    clears a projection, and it must clear one regardless of how the corpuser came
    to exist.

    spec: spec/feature/AUTH.md §Projection retraction sequence — "1. Hard-delete
    the DataSpoke users row. 2. Hard-delete the DataHub corpuser via
    hard_delete_entity." Group memberships, role assignments, and ownership
    references are removed automatically by DataHub.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass, StatusClass

    from src.backend.datahub.users import corpuser_exists, hard_delete_corpuser

    email = _unique_email()

    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Delete Target", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {reg.json()['access_token']}"},
    )
    assert me.status_code == 200, f"GET /auth/me failed: {me.text}"
    user_id = me.json()["id"]

    # Stand in for DataHub's OIDC JIT provisioning on first DataHub login — the
    # only mechanism that creates a corpuser for a DataSpoke user.
    corpuser_urn = f"urn:li:corpuser:{email.lower()}"

    # The happy path removes both the row and the corpuser — that is what is under
    # test. The teardown only matters when an assertion fails partway, and must not
    # leave either behind on the shared cluster.
    try:
        await datahub_client.emit_aspect(corpuser_urn, StatusClass(removed=False))
        await datahub_client.emit_aspect(
            corpuser_urn,
            CorpUserInfoClass(active=True, email=email.lower(), displayName="Delete Target"),
        )
        assert await corpuser_exists(datahub_client, corpuser_urn) is True, (
            "Precondition: the corpuser must exist before the retraction is exercised"
        )

        del_resp = await api_client.delete(
            f"/api/v1/admin/users/{user_id}",
            headers=admin_headers,
        )
        assert del_resp.status_code == 204, (
            f"DELETE /admin/users/{user_id} must return 204: {del_resp.text}"
        )

        aspect_after = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
        assert aspect_after is None, (
            f"DataHub corpuser {corpuser_urn} must be gone after hard delete "
            "per spec/feature/AUTH.md §Projection retraction sequence step 2"
        )
    finally:
        if await corpuser_exists(datahub_client, corpuser_urn):
            await hard_delete_corpuser(datahub_client, corpuser_urn)
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": user_id}
        )
        await async_session.commit()
