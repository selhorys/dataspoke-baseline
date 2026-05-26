"""API-wired integration test: DataHub mirror on user registration.

Concerns covered:
- POST /auth/register creates urn:li:corpuser:<email> in DataHub with corpUserInfo aspect
- The marker corpGroup contains this corpuser (group name from /admin/conf.auth_datahub_corp_group)
- The corpuser has role Reader via IsMemberOfRole relationship

spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence
spec: spec/feature/AUTH.md §Marker corpGroup
"""

import asyncio
import uuid

import httpx
import pytest


def _unique_email(prefix: str = "mirror") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _poll_graphql(datahub_client, query: str, variables: dict, predicate, *, timeout: int = 60, interval: float = 3.0):
    """Poll a DataHub GraphQL query until predicate(result) is True or timeout.

    DataHub GraphQL relationship queries depend on ES indexing which can lag
    several seconds after a mutation. This helper retries until the expected
    state is visible or the timeout is reached.
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
async def test_register_creates_datahub_corpuser(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """POST /auth/register mirrors user into DataHub as corpuser with corpUserInfo aspect.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence —
    step 2: write the DataHub corpUserInfo aspect (email, displayName, active=True).
    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §URN conventions —
    corpuser URN: urn:li:corpuser:<email>.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass

    email = _unique_email()

    # Register user
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Mirror Test User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    # Verify DataHub corpuser exists via direct SDK call
    corpuser_urn = f"urn:li:corpuser:{email}"
    aspect = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)

    assert aspect is not None, (
        f"DataHub corpuser {corpuser_urn} must exist after registration "
        f"per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence"
    )
    assert aspect.email == email, (
        "corpUserInfo.email must match the registered email "
        "per spec/feature/AUTH.md §DataHub Mirror Semantics"
    )
    assert aspect.displayName == "Mirror Test User", (
        "corpUserInfo.displayName must match the registered name"
    )
    assert aspect.active is True, "corpUserInfo.active must be True"


@pytest.mark.asyncio
async def test_register_adds_user_to_marker_corpgroup(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """POST /auth/register adds the corpuser to the marker corpGroup.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence —
    step 3: ensure the marker corpGroup exists, then add the corpuser to it.
    spec: spec/feature/AUTH.md §Marker corpGroup — provenance signal; group name from
    /admin/conf.auth_datahub_corp_group (default 'dataspoke-users').

    Verified via NativeGroupMembershipClass aspect (direct aspect on corpuser, no ES indexing lag).
    DataHub's addGroupMembers GraphQL mutation calls GroupService.addUserToNativeGroup() which
    writes the nativeGroupMembership aspect — not groupMembership. This is a direct aspect read
    and does not require the ES relationship graph index.
    """
    from datahub.metadata.schema_classes import NativeGroupMembershipClass

    email = _unique_email("marker-grp")

    # Get the configured group name
    conf_resp = await api_client.get("/api/v1/admin/conf", headers=admin_headers)
    assert conf_resp.status_code == 200
    group_name = conf_resp.json().get("auth_datahub_corp_group", "dataspoke-users")

    # Register user
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Marker Group User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    # Verify group membership via NativeGroupMembershipClass aspect on the corpuser.
    # addGroupMembers → GroupService.addUserToNativeGroup() → nativeGroupMembership aspect.
    # Direct aspect read — no ES indexing lag.
    corpuser_urn = f"urn:li:corpuser:{email}"
    expected_group_urn = f"urn:li:corpGroup:{group_name}"

    native_membership = await datahub_client.get_aspect(corpuser_urn, NativeGroupMembershipClass)
    group_urns = list(native_membership.nativeGroups) if native_membership is not None else []

    assert expected_group_urn in group_urns, (
        f"corpuser must be a member of the marker corpGroup {expected_group_urn} "
        f"per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence step 3. "
        f"Found nativeGroup URNs: {group_urns}"
    )


@pytest.mark.asyncio
async def test_register_assigns_reader_role_in_datahub(
    api_client: httpx.AsyncClient,
    datahub_client,
) -> None:
    """POST /auth/register assigns Reader role to the corpuser via batchAssignRole.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence —
    step 4: propagate the user's users.role (Reader) to DataHub via batchAssignRole.
    """
    email = _unique_email("role-check")

    # Register user
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Role Check User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"

    # Verify DataHub role via IsMemberOfRole.
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

    def _has_reader(result):
        corp_user = (result or {}).get("corpUser") or {}
        rels = (corp_user.get("relationships") or {}).get("relationships") or []
        return "Reader" in [(rel.get("entity") or {}).get("name", "") for rel in rels]

    result = await _poll_graphql(datahub_client, query, {"u": corpuser_urn}, _has_reader, timeout=60)

    corp_user = (result or {}).get("corpUser") or {}
    relationships = (corp_user.get("relationships") or {}).get("relationships") or []
    role_names = [
        (rel.get("entity") or {}).get("name", "")
        for rel in relationships
    ]

    assert "Reader" in role_names, (
        f"corpuser must have Reader role after registration "
        f"per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence step 4. "
        f"Found roles: {role_names}"
    )
