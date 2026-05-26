"""API-wired integration test: role change propagates to DataHub.

Concerns covered:
- PATCH /admin/users/{id}/role to Editor propagates to DataHub IsMemberOfRole
- DataHub-side role reflects the new role after the PATCH

spec: spec/feature/AUTH.md §Admin Surface — PATCH /admin/users/{id}/role writes DataSpoke first
then propagates to DataHub via batchAssignRole.
spec: spec/feature/AUTH.md §Identity Model — DataSpoke is the SSOT; DataHub holds propagated copies.
"""

import asyncio
import uuid

import httpx
import pytest


def _unique_email(prefix: str = "role-change") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _poll_graphql(datahub_client, query: str, variables: dict, predicate, *, timeout: int = 60, interval: float = 3.0):
    """Poll a DataHub GraphQL query until predicate(result) is True or timeout.

    DataHub's relationship queries depend on ES indexing which can lag
    several seconds after a mutation.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = await datahub_client.execute_graphql(query, variables)
        if predicate(result):
            return result
        if asyncio.get_event_loop().time() >= deadline:
            return result
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_role_change_propagates_to_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """PATCH /admin/users/{id}/role to Editor: DataHub IsMemberOfRole now shows Editor.

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

    # Verify DataHub-side role.
    # DataHub's relationship queries depend on ES indexing — poll with 60s timeout.
    corpuser_urn = f"urn:li:corpuser:{email}"
    query = """
    query($u: String!) {
      corpUser(urn: $u) {
        relationships(input: {types: ["IsMemberOfRole"], direction: OUTGOING, start: 0, count: 10}) {
          relationships { entity { ... on DataHubRole { urn name } } }
        }
      }
    }
    """

    def _has_editor(result):
        corp_user = (result or {}).get("corpUser") or {}
        rels = (corp_user.get("relationships") or {}).get("relationships") or []
        return "Editor" in [(rel.get("entity") or {}).get("name", "") for rel in rels]

    result = await _poll_graphql(datahub_client, query, {"u": corpuser_urn}, _has_editor, timeout=60)

    corp_user = (result or {}).get("corpUser") or {}
    relationships = (corp_user.get("relationships") or {}).get("relationships") or []
    role_names = [
        (rel.get("entity") or {}).get("name", "")
        for rel in relationships
    ]

    assert "Editor" in role_names, (
        f"DataHub IsMemberOfRole must show Editor after PATCH /admin/users/{user_id}/role "
        f"per spec/feature/AUTH.md §Admin Surface. Found roles: {role_names}"
    )
    assert "Reader" not in role_names, (
        f"DataHub must NOT show Reader after role is promoted to Editor "
        f"per spec/feature/AUTH.md §Admin Surface (one role at a time, DataSpoke SSOT). "
        f"Found roles: {role_names}"
    )
