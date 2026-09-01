"""DataHub projection primitives for DataSpoke-managed users.

Wrappers around ``src.shared.datahub.client.DataHubClient``.

DataHub owns the corpuser entity — it is provisioned by DataHub's OIDC JIT on
first DataHub login. These helpers project DataSpoke-owned state (role, marker-
group membership) onto corpusers that already exist, probe for that existence,
and retract the projection on user deletion. No function here writes a
``corpUserInfo`` aspect or otherwise creates a corpuser.

All write functions are idempotent (aspect writes overwrite in place; GraphQL
mutations accept duplicates gracefully).

All functions raise ``DataHubUnavailableError`` on transport failure —
the retry / circuit-breaker logic in ``DataHubClient`` handles this.

Spec: spec/feature/AUTH.md §DataHub Projection Semantics.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from datahub.metadata.schema_classes import (
    CorpUserKeyClass,
    NativeGroupMembershipClass,
    RoleMembershipClass,
    StatusClass,
)

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


# ── URN helpers ───────────────────────────────────────────────────────────────

#: Accepted shape for ``admin/conf.auth_datahub_corp_group`` — the marker
#: corpGroup name.  It is interpolated into ``urn:li:corpGroup:<name>`` and the
#: group's DataHub ``displayName``, so it must stay free of whitespace, URN
#: delimiters (``(`` ``)`` ``,``) and bidi/control characters.  The leading
#: ``^`` anchors and ``{0,127}$`` bounds the tail (128 chars total, matching the
#: column and field length cap).  ``src/api/schemas/admin.py`` imports this
#: string for the ``PATCH /admin/conf`` field constraint so the write boundary
#: and this emission-side guard cannot drift.
CORP_GROUP_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$"
_CORP_GROUP_NAME_RE: re.Pattern[str] = re.compile(CORP_GROUP_NAME_PATTERN)


def corpuser_urn(email: str) -> str:
    """Return the DataHub corpuser URN for *email*.

    *email* is lowercased: the URN is case-sensitive on the DataHub side and is
    minted by DataHub's OIDC JIT from the Google email claim, while DataSpoke's
    CITEXT ``users.email`` column preserves whatever case was registered.
    ``create_user`` already normalises on write; this is the second line of
    defence for rows written by other paths.
    """
    return f"urn:li:corpuser:{email.lower()}"


def corpgroup_urn(name: str) -> str:
    """Return the DataHub corpGroup URN for *name*.

    *name* is the operator-configured ``admin/conf.auth_datahub_corp_group``.
    It is re-checked here against :data:`CORP_GROUP_NAME_PATTERN` — the same
    shape the ``PATCH /admin/conf`` field enforces — because a row written
    before that constraint existed, or by direct SQL (including an empty
    string), can still reach this call and would otherwise be emitted as a
    malformed corpGroup URN.

    Raises:
        ValueError: *name* is not a valid marker corpGroup name.  The message
            does not quote *name*.
    """
    if not _CORP_GROUP_NAME_RE.fullmatch(name):
        raise ValueError("auth_datahub_corp_group is not a valid corpGroup name")
    return f"urn:li:corpGroup:{name}"


def role_urn(role: str) -> str:
    """Return the DataHub dataHubRole URN for *role*."""
    return f"urn:li:dataHubRole:{role}"


# ── Projection operations ─────────────────────────────────────────────────────


async def corpuser_exists(
    client: DataHubClient,
    corpuser_urn_str: str,
) -> bool:
    """Return True when DataHub holds a live corpuser entity for *corpuser_urn_str*.

    ``DataHubClient`` exposes no entity-existence call, so the probe is an
    aspect read. It reads the **key** aspect (``corpUserKey``), which is
    materialised for every existing entity, rather than ``corpUserInfo``:
    DataHub's ``RoleService`` gates on ``EntityClient.exists`` — "> 0 aspects in
    the DB" — so a corpuser materialised from an ingestion owner reference has
    a key but no ``corpUserInfo`` and would otherwise be skipped forever.

    A soft-deleted corpuser (``Status.removed = true``) is reported as absent.
    DataHub's own predicate would admit it, so this is deliberately stricter:
    DataSpoke does not project privileges onto a deactivated account. Being
    stricter than the mutation is the safe direction — the pass skips instead of
    recording a repair that DataHub silently dropped.

    Callers must run this before any projection mutation. ``RoleService``
    returns early when the actor does not exist while the GraphQL mutation still
    reports success, so an unguarded pass would report repairs it never made.
    """
    key = await client.get_aspect(corpuser_urn_str, CorpUserKeyClass, strict=True)
    if key is None:
        return False
    status = await client.get_aspect(corpuser_urn_str, StatusClass, strict=True)
    return status is None or not status.removed


async def read_native_group_membership(
    client: DataHubClient,
    corpuser_urn_str: str,
) -> list[str]:
    """Return the corpGroup URNs *corpuser_urn_str* is a native member of.

    Reads the ``nativeGroupMembership`` aspect — the aspect ``addGroupMembers``
    writes. Returns an empty list when the aspect is absent (the corpuser
    belongs to no native group).
    """
    aspect = await client.get_aspect(corpuser_urn_str, NativeGroupMembershipClass, strict=True)
    if aspect is None:
        return []
    return list(aspect.nativeGroups)


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
    from datahub.metadata.schema_classes import CorpGroupInfoClass

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
