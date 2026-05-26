"""DataHub mirror primitives for DataSpoke-managed users.

Wrappers around ``src.shared.datahub.client.DataHubClient``.
All functions are idempotent (aspect writes overwrite in place; GraphQL
mutations accept duplicates gracefully).

All functions raise ``DataHubUnavailableError`` on transport failure —
the retry / circuit-breaker logic in ``DataHubClient`` handles this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient


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
    client: "DataHubClient",
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
    client: "DataHubClient",
    group_name: str,
) -> None:
    """Ensure the marker corpGroup exists without overwriting existing state.

    First checks whether the group already exists via a GraphQL query.
    If the group is present, this is a no-op (existing members/admins are
    left untouched).  Only emits a new ``corpGroupInfo`` aspect when the
    group does not exist yet.
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import CorpGroupInfoClass, StatusClass

    urn = corpgroup_urn(group_name)
    result = await client.execute_graphql(
        "query($u: String!) { corpGroup(urn: $u) { urn } }",
        {"u": urn},
    )
    if (result or {}).get("corpGroup"):
        return  # already exists — do not overwrite

    # Emit CorpGroupInfo + Status in the same write so DataHub's entity store
    # marks the group as active immediately.  Without Status(removed=False),
    # the addGroupMembers GraphQL mutation returns 404 ("Group does not exist")
    # because DataHub's mutation layer checks the Status aspect, not just the
    # aspect store presence.
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
    client: "DataHubClient",
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
    client: "DataHubClient",
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
    client: "DataHubClient",
    corpuser_urn_str: str,
) -> str | None:
    """Read the DataHub-side role for *corpuser_urn_str*.

    Returns the role short name (``'Admin'`` / ``'Editor'`` / ``'Reader'``)
    or ``None`` when no role is assigned.

    Only used by the nightly ``auth-role-sync-daily`` DAG — not on the hot path.
    """
    query = """
    query($u: String!) {
      corpUser(urn: $u) {
        relationships(input: {types: ["IsMemberOfRole"], direction: OUTGOING, start: 0, count: 10}) {
          relationships { entity { ... on DataHubRole { urn name } } }
        }
      }
    }
    """
    result = await client.execute_graphql(query, {"u": corpuser_urn_str})

    corp_user = (result or {}).get("corpUser") or {}
    relationships = (corp_user.get("relationships") or {}).get("relationships") or []
    for rel in relationships:
        entity = (rel or {}).get("entity") or {}
        name = entity.get("name")
        if name in ("Admin", "Editor", "Reader"):
            return name
    return None


async def hard_delete_corpuser(
    client: "DataHubClient",
    corpuser_urn_str: str,
) -> None:
    """Hard-delete the DataHub corpuser entity and all its references."""
    await client.hard_delete_entity(corpuser_urn_str)
