"""Unit tests for src/backend/datahub/users.py.

Concerns covered:
- URN helpers produce the right strings (corpuser URN lowercases the email)
- corpuser_exists probes the key aspect; soft-deleted corpusers read as absent
- read_native_group_membership returns nativeGroups, [] when the aspect is absent
- ensure_marker_group_exists always emits Status + CorpGroupInfo (unconditional, idempotent)
- add_user_to_marker_group issues the GraphQL addGroupMembers mutation with the right variables
- propagate_role issues batchAssignRole with the right roleUrn
- read_role parses the RoleMembership aspect; returns None when missing, empty, or for non-baseline
role URNs
- hard_delete_corpuser calls client.hard_delete_entity with the URN

spec: spec/feature/AUTH.md §DataHub Projection Semantics
spec: spec/feature/AUTH.md §Marker corpGroup
spec: spec/feature/AUTH.md §Role Drift Reconciliation
spec: spec/DATAHUB_INTEGRATION.md §User & Role Management
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


def test_corpuser_urn_lowercases_email() -> None:
    """corpuser_urn lowercases the email before deriving the URN.

    spec: spec/feature/AUTH.md §DataHub Projection Semantics §URN conventions —
    "The email is lowercased before URN derivation. ... a row stored as
    `Bob@example.com` must still derive `urn:li:corpuser:bob@example.com` to meet
    the URN DataHub provisions."
    """
    from src.backend.datahub.users import corpuser_urn

    assert corpuser_urn("Bob@Example.COM") == "urn:li:corpuser:bob@example.com", (
        "corpuser_urn must lowercase the email — users.email is CITEXT "
        "(case-preserving) while the DataHub corpuser URN is case-sensitive, "
        "per spec/feature/AUTH.md §URN conventions"
    )


def test_role_urn_format() -> None:
    """role_urn returns urn:li:dataHubRole:<role>.

    spec: spec/DATAHUB_INTEGRATION.md §URN Conventions —
    Role | urn:li:dataHubRole:<Admin|Editor|Reader>.
    """
    from src.backend.datahub.users import role_urn

    assert role_urn("Reader") == "urn:li:dataHubRole:Reader"
    assert role_urn("Editor") == "urn:li:dataHubRole:Editor"
    assert role_urn("Admin") == "urn:li:dataHubRole:Admin"


# ── DataSpoke writes no corpuser aspect ───────────────────────────────────────


def test_module_exposes_no_corpuser_create_operation() -> None:
    """The module offers no way to create a corpuser.

    spec: spec/DATAHUB_INTEGRATION.md §Corpuser provenance — "DataSpoke has no
    create-corpuser operation"; §Aspects DataSpoke Writes — "DataSpoke writes
    **no corpuser aspect at all**." The corpGroup row is the only aspect write.
    """
    import ast
    import inspect

    from src.backend.datahub import users as dh_users

    # The spec bans a corpuser *write*, not a mention — reading corpuser aspects is
    # expected (the existence probe does exactly that). So look for emit calls whose
    # target URN is a corpuser rather than grepping for a class name.
    tree = ast.parse(inspect.getsource(dh_users))
    emitters = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"emit_aspect", "emit_mcp"}
    }
    assert emitters == {"ensure_marker_group_exists"}, (
        "The marker corpGroup row is the only aspect write in this module — no "
        "corpuser aspect may be emitted, since corpuser creation belongs to "
        "DataHub's OIDC JIT provisioning per spec/DATAHUB_INTEGRATION.md "
        f"§Aspects DataSpoke Writes. Functions that emit: {sorted(emitters)}"
    )
    assert not hasattr(dh_users, "ensure_corpuser_exists"), (
        "No create-corpuser operation may exist per spec/DATAHUB_INTEGRATION.md "
        "§Corpuser provenance"
    )


# ── corpuser_exists ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corpuser_exists_true_when_key_aspect_present_and_not_removed() -> None:
    """corpuser_exists returns True when the key aspect resolves and Status is absent.

    spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2 —
    "Existence probe — resolve whether the corpuser exists: entity key plus
    `Status`, with a soft-deleted corpuser counting as absent."
    """
    from datahub.metadata.schema_classes import CorpUserKeyClass, StatusClass

    from src.backend.datahub.users import corpuser_exists

    async def _get_aspect(_urn, aspect_cls, **_kwargs):
        if aspect_cls is CorpUserKeyClass:
            return CorpUserKeyClass(username="alice@example.com")
        if aspect_cls is StatusClass:
            return None
        raise AssertionError(f"unexpected aspect read: {aspect_cls}")

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(side_effect=_get_aspect)

    result = await corpuser_exists(mock_client, "urn:li:corpuser:alice@example.com")

    assert result is True, (
        "A corpuser whose key aspect resolves and which is not soft-deleted must "
        "read as existing per spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
    )
    # Backstop: the probe must actually read the key aspect, not short-circuit.
    read_aspects = [call.args[1] for call in mock_client.get_aspect.call_args_list]
    assert CorpUserKeyClass in read_aspects, (
        "The probe must read the corpUserKey aspect — it is materialised for every "
        "existing entity, unlike corpUserInfo"
    )


@pytest.mark.asyncio
async def test_corpuser_exists_false_when_key_aspect_absent() -> None:
    """corpuser_exists returns False when no key aspect resolves (never JIT-provisioned).

    spec: spec/feature/AUTH.md §Failure Modes — "A DataSpoke user has never logged
    into DataHub, so no corpuser exists | The reconciliation pass's existence probe
    skips them without mutating".
    """
    from src.backend.datahub.users import corpuser_exists

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(return_value=None)

    result = await corpuser_exists(mock_client, "urn:li:corpuser:nobody@example.com")

    assert result is False, (
        "A corpuser DataHub has not provisioned must read as absent so the pass "
        "counts the user skipped_unprovisioned per spec/feature/AUTH.md §Failure Modes"
    )


@pytest.mark.asyncio
async def test_corpuser_exists_false_when_soft_deleted() -> None:
    """A soft-deleted corpuser (Status.removed=True) reads as absent.

    spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2 —
    "(entity key plus Status, soft-deleted counting as absent)".
    """
    from datahub.metadata.schema_classes import CorpUserKeyClass, StatusClass

    from src.backend.datahub.users import corpuser_exists

    async def _get_aspect(_urn, aspect_cls, **_kwargs):
        if aspect_cls is CorpUserKeyClass:
            return CorpUserKeyClass(username="ghost@example.com")
        if aspect_cls is StatusClass:
            return StatusClass(removed=True)
        raise AssertionError(f"unexpected aspect read: {aspect_cls}")

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(side_effect=_get_aspect)

    result = await corpuser_exists(mock_client, "urn:li:corpuser:ghost@example.com")

    assert result is False, (
        "A soft-deleted corpuser must read as absent per "
        "spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
    )
    # Backstop: the key aspect DID resolve, so the False came from the Status read,
    # not from a missing entity.
    read_aspects = [call.args[1] for call in mock_client.get_aspect.call_args_list]
    assert StatusClass in read_aspects, (
        "The soft-delete verdict must come from reading Status, not from the key "
        "aspect being absent"
    )


@pytest.mark.asyncio
async def test_corpuser_exists_propagates_datahub_unavailable() -> None:
    """A transport failure surfaces rather than being mis-read as 'no corpuser'.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — "errors | Users for whom
    at least one facet could not be reconciled. The next nightly run retries."
    Silently returning False would instead count the user skipped_unprovisioned and
    never retry the repair.
    """
    from src.backend.datahub.users import corpuser_exists
    from src.shared.exceptions import DataHubUnavailableError

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(side_effect=DataHubUnavailableError("probe failed"))

    with pytest.raises(DataHubUnavailableError):
        await corpuser_exists(mock_client, "urn:li:corpuser:transient@example.com")


# ── read_native_group_membership ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_native_group_membership_returns_native_groups() -> None:
    """read_native_group_membership returns the nativeGroups list from the aspect.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 4 — "Group facet —
    read the corpuser's nativeGroupMembership aspect. If the marker group URN is
    absent, add it via addGroupMembers."
    """
    from datahub.metadata.schema_classes import NativeGroupMembershipClass

    from src.backend.datahub.users import read_native_group_membership

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(
        return_value=NativeGroupMembershipClass(
            nativeGroups=[
                "urn:li:corpGroup:dataspoke-users",
                "urn:li:corpGroup:other",
            ]
        )
    )

    result = await read_native_group_membership(
        mock_client, "urn:li:corpuser:grouped@example.com"
    )

    assert result == [
        "urn:li:corpGroup:dataspoke-users",
        "urn:li:corpGroup:other",
    ], (
        "read_native_group_membership must return the aspect's nativeGroups so the "
        "pass can test for the marker group URN per spec/feature/AUTH.md "
        "§Role Drift Reconciliation step 4"
    )


@pytest.mark.asyncio
async def test_read_native_group_membership_returns_empty_when_aspect_absent() -> None:
    """An absent nativeGroupMembership aspect reads as membership of no group.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 4 — the marker group
    URN being absent is what triggers the addGroupMembers repair; a corpuser with no
    aspect at all is the base case of that.
    """
    from src.backend.datahub.users import read_native_group_membership

    mock_client = AsyncMock()
    mock_client.get_aspect = AsyncMock(return_value=None)

    result = await read_native_group_membership(
        mock_client, "urn:li:corpuser:nogroups@example.com"
    )

    assert result == [], (
        "A corpuser with no nativeGroupMembership aspect belongs to no native group "
        "per spec/feature/AUTH.md §Role Drift Reconciliation step 4"
    )


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

    spec: spec/feature/AUTH.md §Marker corpGroup — "every reconciliation pass asserts
    the group once, unconditionally, before its per-user loop".

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

    spec: spec/feature/AUTH.md §DataHub Projection Semantics — "Group membership writes
    use addGroupMembers / removeGroupMembers."
    """
    from src.backend.datahub.users import add_user_to_marker_group

    mock_client = AsyncMock()
    mock_client.execute_graphql = AsyncMock(return_value={"addGroupMembers": True})

    group_urn = "urn:li:corpGroup:dataspoke-users"
    user_urn = "urn:li:corpuser:alice@example.com"

    await add_user_to_marker_group(mock_client, group_urn, user_urn)

    assert mock_client.execute_graphql.called, (
        "add_user_to_marker_group must call execute_graphql "
        "per spec/feature/AUTH.md §DataHub Projection Semantics"
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
        "addGroupMembers must pass groupUrn per spec/feature/AUTH.md "
        "§DataHub Projection Semantics"
    )
    assert user_urn in variables["u"], (
        "addGroupMembers must include the corpuser URN per spec/feature/AUTH.md "
        "§DataHub Projection Semantics"
    )


# ── propagate_role ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propagate_role_issues_batch_assign_role_mutation() -> None:
    """propagate_role issues batchAssignRole with the correct roleUrn.

    spec: spec/feature/AUTH.md §DataHub Projection Semantics — "Role propagation uses the
    GraphQL batchAssignRole mutation"; role URN per §URN Conventions.
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

    spec: spec/feature/AUTH.md §Projection retraction sequence —
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
    # per spec/feature/AUTH.md §Projection retraction sequence
    mock_client.hard_delete_entity.assert_called_once_with(corpuser_urn_str)
