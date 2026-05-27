"""DataHub mirror primitives for DataSpoke-managed users.

Wrappers around ``src.shared.datahub.client.DataHubClient``.
All functions are idempotent (aspect writes overwrite in place; GraphQL
mutations accept duplicates gracefully).

All functions raise ``DataHubUnavailableError`` on transport failure —
the retry / circuit-breaker logic in ``DataHubClient`` handles this.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from datahub.metadata.schema_classes import RoleMembershipClass

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


# ── URN helpers ───────────────────────────────────────────────────────────────


def corpuser_urn(email: str) -> str:
    """Return the DataHub corpuser URN for *email*."""
    return f"urn:li:corpuser:{email}"


def corpgroup_urn(name: str) -> str:
    """Return the DataHub corpGroup URN for *name*."""
    return f"urn:li:corpGroup:{name}"


def role_urn(role: str) -> str:
    """Return the DataHub dataHubRole URN for *role*."""
    return f"urn:li:dataHubRole:{role}"


# ── Mirror operations ─────────────────────────────────────────────────────────


async def ensure_corpuser_exists(
    client: DataHubClient,
    email: str,
    name: str,
) -> None:
    """Emit a ``corpUserInfo`` aspect for *email*.

    Idempotent — re-emitting the same data overwrites in place.
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import CorpUserInfoClass

    mcp = MetadataChangeProposalWrapper(
        entityUrn=corpuser_urn(email),
        aspect=CorpUserInfoClass(
            active=True,
            email=email,
            displayName=name,
        ),
    )
    await client.emit_mcp(mcp)


async def ensure_marker_group_exists(
    client: DataHubClient,
    group_name: str,
) -> None:
    """Ensure the marker corpGroup is active before ``addGroupMembers`` is called.

    Both ``Status(removed=False)`` and ``CorpGroupInfo`` are re-asserted on every
    call.  DataHub's ``addGroupMembers`` mutation rejects with "Group does not
    exist" when the corpGroup is not yet fully resolvable, which can happen after
    a prior attempt emitted ``Status`` but failed before ``CorpGroupInfo`` was
    committed (or its indexing caught up).  Re-emitting both aspects is safe:
    DataHub overwrites them in place by URN (idempotent).

    Note: operators wanting a custom display name should configure
    ``auth_datahub_corp_group`` via ``/admin/conf`` rather than editing DataHub
    directly, as this function overwrites ``displayName`` on every call.  The
    ``members``/``admins``/``groups`` arrays are also reset to empty on every
    call, so the marker group must not be used as a privilege carrier.
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import CorpGroupInfoClass, StatusClass

    urn = corpgroup_urn(group_name)
    await client.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=StatusClass(removed=False),
        )
    )
    await client.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=CorpGroupInfoClass(
                displayName=group_name,
                members=[],
                admins=[],
                groups=[],
            ),
        )
    )


async def add_user_to_marker_group(
    client: DataHubClient,
    group_urn_str: str,
    corpuser_urn_str: str,
) -> None:
    """Add *corpuser_urn_str* to *group_urn_str* via GraphQL ``addGroupMembers``.

    Idempotent — DataHub silently accepts duplicate membership writes.
    """
    mutation = (
        "mutation($g: String!, $u: [String!]!) "
        "{ addGroupMembers(input: {groupUrn: $g, userUrns: $u}) }"
    )
    await client.execute_graphql(mutation, {"g": group_urn_str, "u": [corpuser_urn_str]})


async def propagate_role(
    client: DataHubClient,
    corpuser_urn_str: str,
    role: str,
) -> None:
    """Assign *role* to *corpuser_urn_str* via GraphQL ``batchAssignRole``.

    Idempotent — re-assigning the same role is a no-op on DataHub.
    """
    mutation = (
        "mutation($r: String!, $u: [String!]!) "
        "{ batchAssignRole(input: {roleUrn: $r, actors: $u}) }"
    )
    await client.execute_graphql(mutation, {"r": role_urn(role), "u": [corpuser_urn_str]})


async def read_role(
    client: DataHubClient,
    corpuser_urn_str: str,
) -> str | None:
    """Read the DataHub-side role for *corpuser_urn_str*.

    Reads the ``RoleMembership`` aspect directly — atomic single-role per
    DataHub ``RoleService``. The GraphQL ``IsMemberOfRole`` relationship index
    is not used here: it lags MCL→ES indexing and transiently shows roles that
    were already overwritten in the aspect.

    Returns the role short name (``'Admin'`` / ``'Editor'`` / ``'Reader'``)
    or ``None`` when no role is assigned.
    """
    aspect = await client.get_aspect(corpuser_urn_str, RoleMembershipClass, strict=True)
    if aspect is None or not aspect.roles:
        return None
    if len(aspect.roles) > 1:
        logger.warning(
            "rolemembership_unexpected_multi_role",
            extra={"corpuser": corpuser_urn_str, "role_count": len(aspect.roles)},
        )
    name = aspect.roles[0].removeprefix("urn:li:dataHubRole:")
    return name if name in ("Admin", "Editor", "Reader") else None


async def hard_delete_corpuser(
    client: DataHubClient,
    corpuser_urn_str: str,
) -> None:
    """Hard-delete the DataHub corpuser entity and all its references."""
    await client.hard_delete_entity(corpuser_urn_str)
