"""Unit tests for src/backend/datahub/users.py.

Concerns covered:
- URN helpers produce the right strings
- ensure_corpuser_exists emits the correct MCP (corpUserInfo aspect)
- ensure_marker_group_exists always emits Status + CorpGroupInfo (unconditional, idempotent)
- add_user_to_marker_group issues the GraphQL addGroupMembers mutation with the right variables
- propagate_role issues batchAssignRole with the right roleUrn
- read_role parses the RoleMembership aspect; returns None when missing, empty, or for non-baseline
role URNs
- hard_delete_corpuser calls client.hard_delete_entity with the URN

spec: spec/feature/AUTH.md §DataHub Mirror Semantics
spec: spec/feature/AUTH.md §Marker corpGroup
spec: spec/feature/AUTH.md §Role Drift Reconciliation
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# ── URN helpers ────────────────────────────────────────────────────────────────


def test_corpuser_urn_format() -> None:
    """corpuser_urn returns urn:li:corpuser:<email>.

    spec: spec/DATAHUB_INTEGRATION.md §URN Conventions —
    corpuser URN: urn:li:corpuser:<email>. The email-as-id form aligns with
    DataHub's AUTH_OIDC_USER_ID_CLAIM=email.
    """
    from src.backend.datahub.users import corpuser_urn

    assert corpuser_urn("alice@example.com") == "urn:li:corpuser:alice@example.com", (
        "corpuser URN must be urn:li:corpuser:<email> per DATAHUB_INTEGRATION.md §URN Conventions"
    )


def test_corpgroup_urn_format() -> None:
    """corpgroup_urn returns urn:li:corpGroup:<name>.

    spec: spec/DATAHUB_INTEGRATION.md §URN Conventions — group URN: urn:li:corpGroup:<name>.
    """
    from src.backend.datahub.users import corpgroup_urn

    assert corpgroup_urn("dataspoke-users") == "urn:li:corpGroup:dataspoke-users", (
        "corpGroup URN must be urn:li:corpGroup:<name> per DATAHUB_INTEGRATION.md §URN Conventions"
    )


def test_role_urn_format() -> None:
    """role_urn returns urn:li:dataHubRole:<role>.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Aspects DataSpoke writes —
    role propagation uses batchAssignRole with urn:li:dataHubRole:<role>.
    """
    from src.backend.datahub.users import role_urn

    assert role_urn("Reader") == "urn:li:dataHubRole:Reader"
    assert role_urn("Editor") == "urn:li:dataHubRole:Editor"
    assert role_urn("Admin") == "urn:li:dataHubRole:Admin"


# ── ensure_corpuser_exists ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_corpuser_exists_emits_correct_mcp() -> None:
    """ensure_corpuser_exists emits a CorpUserInfo MCP with active=True, email, displayName.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Aspects DataSpoke writes —
    corpuser | corpUserInfo | On create; on name change.
    """
    from src.backend.datahub.users import ensure_corpuser_exists

    mock_client = AsyncMock()
    mock_client.emit_mcp = AsyncMock()

    await ensure_corpuser_exists(mock_client, "bob@example.com", "Bob Smith")

    assert mock_client.emit_mcp.called, (
        "ensure_corpuser_exists must call client.emit_mcp "
        "per spec/feature/AUTH.md §DataHub Mirror Semantics"
    )

    # Inspect the MCP that was passed
    mcp = mock_client.emit_mcp.call_args[0][0]
    assert mcp.entityUrn == "urn:li:corpuser:bob@example.com", (
        "MCP entityUrn must be urn:li:corpuser:<email> per DATAHUB_INTEGRATION.md §URN Conventions"
    )
    # The aspect should have the right fields
    aspect = mcp.aspect
    assert aspect.active is True, "CorpUserInfo active must be True"
    assert aspect.email == "bob@example.com", "CorpUserInfo email must match"
    assert aspect.displayName == "Bob Smith", "CorpUserInfo displayName must match name"


# ── ensure_marker_group_exists ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_marker_group_exists_always_emits_both_aspects() -> None:
    """ensure_marker_group_exists always emits Status + CorpGroupInfo, even when the group exists.

    spec: spec/feature/AUTH.md §Marker corpGroup — idempotent; existing group is reused.
    Both aspects are re-asserted unconditionally so that a previous partial write
    (Status emitted, CorpGroupInfo not yet ES-indexed) cannot leave addGroupMembers
    seeing a 404 on retry.  No GraphQL pre-check is performed.
    """
    from datahub.metadata.schema_classes import CorpGroupInfoClass, StatusClass

    from src.backend.datahub.users import ensure_marker_group_exists

    mock_client = AsyncMock()
    mock_client.emit_mcp = AsyncMock()

    await ensure_marker_group_exists(mock_client, "dataspoke-users")

    assert mock_client.emit_mcp.call_count == 2, (
        "ensure_marker_group_exists must always emit exactly 2 MCPs (Status + CorpGroupInfo) "
        "regardless of whether the group already exists"
    )
    assert not mock_client.execute_graphql.called, (
        "ensure_marker_group_exists must not perform a GraphQL pre-check"
    )

    status_mcp = mock_client.emit_mcp.call_args_list[0][0][0]
    assert isinstance(status_mcp.aspect, StatusClass)
    assert status_mcp.aspect.removed is False

    info_mcp = mock_client.emit_mcp.call_args_list[1][0][0]
    assert isinstance(info_mcp.aspect, CorpGroupInfoClass)
    assert info_mcp.aspect.displayName == "dataspoke-users"


@pytest.mark.asyncio
async def test_ensure_marker_group_exists_emits_correct_aspect_content() -> None:
    """ensure_marker_group_exists emits Status + CorpGroupInfo with the right URN and field values.

    spec: spec/feature/AUTH.md §Marker corpGroup — auto-created by DataSpoke on first user
    registration if missing.

    Two MCPs are emitted in order:
    1. StatusClass(removed=False) — activates the entity so addGroupMembers can find it
    2. CorpGroupInfoClass — sets the group display name and empty membership lists
    """
    from datahub.metadata.schema_classes import CorpGroupInfoClass, StatusClass

    from src.backend.datahub.users import ensure_marker_group_exists

    mock_client = AsyncMock()
    mock_client.emit_mcp = AsyncMock()

    await ensure_marker_group_exists(mock_client, "dataspoke-users")

    assert mock_client.emit_mcp.call_count == 2, (
        "ensure_marker_group_exists must emit exactly 2 MCPs (Status + CorpGroupInfo) "
        "per spec/feature/AUTH.md §Marker corpGroup"
    )

    # First call: Status aspect (activates entity for addGroupMembers)
    status_mcp = mock_client.emit_mcp.call_args_list[0][0][0]
    assert status_mcp.entityUrn == "urn:li:corpGroup:dataspoke-users"
    assert isinstance(status_mcp.aspect, StatusClass)
    assert status_mcp.aspect.removed is False

    # Second call: CorpGroupInfo aspect
    info_mcp = mock_client.emit_mcp.call_args_list[1][0][0]
    assert info_mcp.entityUrn == "urn:li:corpGroup:dataspoke-users"
    assert isinstance(info_mcp.aspect, CorpGroupInfoClass)


# ── add_user_to_marker_group ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_user_to_marker_group_issues_correct_mutation() -> None:
    """add_user_to_marker_group issues GraphQL addGroupMembers with the right variables.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Aspects DataSpoke writes —
    group membership writes use addGroupMembers.
    """
    from src.backend.datahub.users import add_user_to_marker_group

    mock_client = AsyncMock()
    mock_client.execute_graphql = AsyncMock(return_value={"addGroupMembers": True})

    group_urn = "urn:li:corpGroup:dataspoke-users"
    user_urn = "urn:li:corpuser:alice@example.com"

    await add_user_to_marker_group(mock_client, group_urn, user_urn)

    assert mock_client.execute_graphql.called, (
        "add_user_to_marker_group must call execute_graphql "
        "per spec/feature/AUTH.md §DataHub Mirror Semantics"
    )

    _, kwargs = mock_client.execute_graphql.call_args
    call_args = mock_client.execute_graphql.call_args[0]  # positional

    # Variables must include groupUrn and userUrns
    variables = (
        call_args[1]
        if len(call_args) > 1
        else mock_client.execute_graphql.call_args[1].get("variables")
    )
    if variables is None:
        variables = call_args[1]

    assert variables["g"] == group_urn, (
        "addGroupMembers must pass groupUrn per spec/feature/AUTH.md §DataHub Mirror Semantics"
    )
    assert user_urn in variables["u"], (
        "addGroupMembers must include the corpuser URN per spec/feature/AUTH.md §DataHub Mirror "
        "Semantics"
    )


# ── propagate_role ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propagate_role_issues_batch_assign_role_mutation() -> None:
    """propagate_role issues batchAssignRole with the correct roleUrn.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics — role propagation uses
    batchAssignRole with urn:li:dataHubRole:<role>.
    """
    from src.backend.datahub.users import propagate_role

    mock_client = AsyncMock()
    mock_client.execute_graphql = AsyncMock(return_value={"batchAssignRole": True})

    user_urn = "urn:li:corpuser:charlie@example.com"
    await propagate_role(mock_client, user_urn, "Reader")

    assert mock_client.execute_graphql.called

    call_args = mock_client.execute_graphql.call_args[0]
    variables = call_args[1]

    assert variables["r"] == "urn:li:dataHubRole:Reader", (
        "role URN must be urn:li:dataHubRole:Reader per DATAHUB_INTEGRATION.md §URN Conventions"
    )
    assert user_urn in variables["u"], (
        "batchAssignRole must include the corpuser URN"
    )


# ── read_role ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Admin", "Editor", "Reader"])
async def test_read_role_parses_aspect_correctly(role: str) -> None:
    """read_role reads the RoleMembership aspect and returns the role short name.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — the DAG reads the
    RoleMembership aspect directly (atomic single-role per DataHub RoleService).
    """
    from datahub.metadata.schema_classes import RoleMembershipClass

    from src.backend.datahub.users import read_role

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(
        return_value=RoleMembershipClass(roles=[f"urn:li:dataHubRole:{role}"])
    )

    result = await read_role(mock_client, "urn:li:corpuser:dave@example.com")

    assert result == role, (
        f"read_role must return '{role}' when the aspect holds the {role} role URN "
        "per spec/feature/AUTH.md §Role Drift Reconciliation"
    )


@pytest.mark.asyncio
async def test_read_role_returns_none_when_aspect_missing() -> None:
    """read_role returns None when the corpuser has no RoleMembership aspect."""
    from src.backend.datahub.users import read_role

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(return_value=None)

    result = await read_role(mock_client, "urn:li:corpuser:norole@example.com")

    assert result is None


@pytest.mark.asyncio
async def test_read_role_returns_none_when_aspect_roles_empty() -> None:
    """read_role returns None when the aspect's roles array is empty."""
    from datahub.metadata.schema_classes import RoleMembershipClass

    from src.backend.datahub.users import read_role

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(return_value=RoleMembershipClass(roles=[]))

    result = await read_role(mock_client, "urn:li:corpuser:empty@example.com")

    assert result is None


@pytest.mark.asyncio
async def test_read_role_propagates_datahub_unavailable() -> None:
    """read_role uses strict=True so transient read failures surface as
    DataHubUnavailableError rather than being mis-recorded as 'no role'.

    spec: spec/DATAHUB_INTEGRATION.md §Failure Handling — GraphQL role read failure:
    skip-and-log the affected user; next nightly run retries.
    """
    from src.backend.datahub.users import read_role
    from src.shared.exceptions import DataHubUnavailableError

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(side_effect=DataHubUnavailableError("read failed"))

    with pytest.raises(DataHubUnavailableError):
        await read_role(mock_client, "urn:li:corpuser:transient@example.com")


@pytest.mark.asyncio
async def test_read_role_returns_none_for_unrecognised_role_urn() -> None:
    """read_role returns None for role URNs not in Admin/Editor/Reader.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — only baseline roles are valid.
    """
    from datahub.metadata.schema_classes import RoleMembershipClass

    from src.backend.datahub.users import read_role

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(
        return_value=RoleMembershipClass(roles=["urn:li:dataHubRole:CustomRole"])
    )

    result = await read_role(mock_client, "urn:li:corpuser:custom@example.com")

    assert result is None


# ── hard_delete_corpuser ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hard_delete_corpuser_calls_hard_delete_entity() -> None:
    """hard_delete_corpuser calls client.hard_delete_entity with the corpuser URN.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence —
    step 2: hard-delete the DataHub corpuser via hard_delete_entity.
    spec: spec/feature/AUTH.md §Lifecycle §Deletion — hard_delete_entity removes
    the entity and all its incoming/outgoing references.
    """
    from src.backend.datahub.users import hard_delete_corpuser

    mock_client = AsyncMock()
    mock_client.hard_delete_entity = AsyncMock()

    corpuser_urn_str = "urn:li:corpuser:eve@example.com"
    await hard_delete_corpuser(mock_client, corpuser_urn_str)

    # hard_delete_corpuser must call client.hard_delete_entity with the exact corpuser URN
    # per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror delete sequence
    mock_client.hard_delete_entity.assert_called_once_with(corpuser_urn_str)
