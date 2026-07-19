"""Spot integration test: DataSpoke→DataHub projection of role and marker-group membership.

Concerns covered (one per test):
- corpuser_exists resolves True for a provisioned corpuser and False for a nonexistent URN
- Reconciliation projects users.role onto a bound, provisioned corpuser
- Reconciliation adds a bound, provisioned corpuser to the marker corpGroup
- A password-registered (unbound) user is never projected and creates no corpuser
- A bound user with no corpuser is reported skipped_unprovisioned, and nothing is written

Why spot rather than api-wired: the projection preconditions are a ``users`` row
carrying a ``google_sub`` and a corpuser that DataHub's OIDC JIT has already
provisioned. Neither is reachable from the REST pipeline — the Google OAuth
round-trip cannot be driven from a test, and DataHub JIT provisioning requires a
real DataHub login. Both are seeded directly here (raw SQL for the row, an aspect
emit for the corpuser), which is exactly the case §Spot reserves.

The reconciliation pass is invoked through its internal activity endpoint
(``POST /internal/activities/auth/role-sync``), which owns the loop; DAG wiring is
covered separately by test_auth_role_sync_dag.py.

spec: spec/feature/AUTH.md §Projection contract
spec: spec/feature/AUTH.md §Identity-binding requirement
spec: spec/feature/AUTH.md §Role Drift Reconciliation
spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation
"""

import uuid

import httpx
import pytest
from sqlalchemy import text


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _seed_bound_user(session, *, email: str, name: str, role: str) -> str:
    """Insert a ``users`` row carrying a google_sub (the provider-verified binding).

    Mirrors the row the Google-OAuth new-user branch writes: ``password_hash`` NULL,
    ``google_sub`` set. Returns the row id as a string.
    """
    user_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, password_hash, google_sub, role) "
            "VALUES (:id, :email, :name, NULL, :google_sub, :role)"
        ),
        {
            "id": user_id,
            "email": email,
            "name": name,
            "google_sub": f"google-sub-{user_id}",
            "role": role,
        },
    )
    await session.commit()
    return user_id


async def _delete_user(session, user_id: str) -> None:
    await session.execute(
        text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": user_id}
    )
    await session.execute(
        text("DELETE FROM dataspoke.events WHERE entity_type = 'user' AND entity_id = :id"),
        {"id": user_id},
    )
    await session.commit()


async def _provision_corpuser(datahub_client, email: str, name: str) -> str:
    """Stand in for DataHub's OIDC JIT provisioning of a corpuser.

    JIT provisioning happens on a real DataHub login, which a test cannot perform,
    so the same entity is materialised directly. The projection under test reads
    only the key/Status/RoleMembership/nativeGroupMembership aspects, all of which
    behave identically however the entity came to exist.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass, StatusClass

    urn = f"urn:li:corpuser:{email.lower()}"
    await datahub_client.emit_aspect(urn, StatusClass(removed=False))
    await datahub_client.emit_aspect(
        urn, CorpUserInfoClass(active=True, email=email.lower(), displayName=name)
    )
    return urn


async def _run_reconciliation(api_client: httpx.AsyncClient, internal_headers: dict) -> dict:
    resp = await api_client.post(
        "/internal/activities/auth/role-sync",
        headers=internal_headers,
        content="{}",
        timeout=180.0,
    )
    assert resp.status_code == 200, f"role-sync activity failed: {resp.text}"
    return resp.json()


# ── Existence probe ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corpuser_exists_probe_against_live_datahub(datahub_client) -> None:
    """corpuser_exists is True for a provisioned corpuser and False for a nonexistent URN.

    The probe is load-bearing for the whole pass: DataHub's RoleService returns
    early when the actor does not exist while the GraphQL mutation still reports
    success, so a wrong verdict here silently mis-reports every repair.

    spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2 —
    "Existence probe — resolve whether the corpuser exists ... Probing first is
    required: a batchAssignRole against a non-existent actor returns success while
    RoleService skips the write."
    """
    from src.backend.datahub.users import corpuser_exists, hard_delete_corpuser

    email = _unique_email("probe")
    urn = await _provision_corpuser(datahub_client, email, "Probe User")

    try:
        assert await corpuser_exists(datahub_client, urn) is True, (
            f"corpuser_exists must resolve True for the provisioned corpuser {urn} "
            "per spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
        )

        absent_urn = f"urn:li:corpuser:{_unique_email('never-provisioned')}"
        assert await corpuser_exists(datahub_client, absent_urn) is False, (
            f"corpuser_exists must resolve False for the nonexistent URN {absent_urn} "
            "so the pass counts the user skipped_unprovisioned per "
            "spec/feature/AUTH.md §Failure Modes"
        )
    finally:
        await hard_delete_corpuser(datahub_client, urn)


# ── Role facet ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_projects_role_onto_bound_provisioned_corpuser(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """The pass projects users.role onto a bound user's provisioned corpuser.

    spec: spec/feature/AUTH.md §Projection contract — "Reconciliation | Nightly
    auth-role-sync-daily | Role **and** marker-group membership, for every eligible
    row in users."
    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 3 — "On divergence
    from users.role, re-assert via batchAssignRole — DataSpoke wins."
    """
    from datahub.metadata.schema_classes import RoleMembershipClass

    from src.backend.datahub.users import hard_delete_corpuser

    email = _unique_email("project-role")
    user_id = await _seed_bound_user(
        async_session, email=email, name="Projected Role User", role="Editor"
    )
    urn = f"urn:li:corpuser:{email.lower()}"
    try:
        await _provision_corpuser(datahub_client, email, "Projected Role User")
        await _run_reconciliation(api_client, internal_headers)

        aspect = await datahub_client.get_aspect(urn, RoleMembershipClass)
        roles = list(aspect.roles) if aspect is not None else []
        assert roles == ["urn:li:dataHubRole:Editor"], (
            "The DataSpoke users.role must be projected onto the corpuser via "
            "batchAssignRole, and RoleMembership is atomic single-role per "
            "spec/DATAHUB_INTEGRATION.md §Role read — an appended role rather than "
            "a replaced one is a defect. spec/feature/AUTH.md §Role Drift "
            f"Reconciliation step 3. RoleMembership roles: {roles}"
        )
    finally:
        await hard_delete_corpuser(datahub_client, urn)
        await _delete_user(async_session, user_id)


# ── Group facet ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_adds_bound_user_to_marker_corpgroup(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """The pass adds a bound user's corpuser to the marker corpGroup.

    Verified via the nativeGroupMembership aspect — the aspect addGroupMembers
    writes (GroupService.addUserToNativeGroup), so this is a direct aspect read with
    no ES relationship-index lag.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation step 4 — "Group facet —
    read the corpuser's nativeGroupMembership aspect. If the marker group URN is
    absent, add it via addGroupMembers."
    spec: spec/feature/AUTH.md §Marker corpGroup — group URN is
    urn:li:corpGroup:<admin/conf.auth_datahub_corp_group>.
    """
    from datahub.metadata.schema_classes import NativeGroupMembershipClass

    from src.backend.datahub.users import hard_delete_corpuser

    conf_resp = await api_client.get("/api/v1/admin/conf", headers=admin_headers)
    assert conf_resp.status_code == 200, f"GET /admin/conf failed: {conf_resp.text}"
    group_name = conf_resp.json()["auth_datahub_corp_group"]
    expected_group_urn = f"urn:li:corpGroup:{group_name}"

    email = _unique_email("project-group")
    user_id = await _seed_bound_user(
        async_session, email=email, name="Projected Group User", role="Reader"
    )
    urn = f"urn:li:corpuser:{email.lower()}"
    try:
        await _provision_corpuser(datahub_client, email, "Projected Group User")
        await _run_reconciliation(api_client, internal_headers)

        membership = await datahub_client.get_aspect(urn, NativeGroupMembershipClass)
        group_urns = list(membership.nativeGroups) if membership is not None else []
        assert expected_group_urn in group_urns, (
            f"The corpuser must be a member of the marker corpGroup {expected_group_urn} "
            "per spec/feature/AUTH.md §Role Drift Reconciliation step 4. "
            f"Found nativeGroup URNs: {group_urns}"
        )
    finally:
        await hard_delete_corpuser(datahub_client, urn)
        await _delete_user(async_session, user_id)


# ── Identity-binding gate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_registered_user_is_never_projected(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """A password-registered user gets no corpuser and no projection, before or after the pass.

    Registration itself makes no DataHub call, and the reconciliation pass refuses
    to write against an unverified email.

    spec: spec/feature/AUTH.md §Projection contract — "User creation is local-only.
    Neither POST /auth/register ... makes a DataHub call ... DataSpoke never creates
    a corpuser."
    spec: spec/feature/AUTH.md §Identity-binding requirement — "A row created by
    password registration alone is never projected, on either path."
    """
    from datahub.metadata.schema_classes import NativeGroupMembershipClass

    from src.backend.datahub.users import (
        corpuser_exists,
        hard_delete_corpuser,
        propagate_role,
        read_role,
    )

    email = _unique_email("unbound")

    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Unbound User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration must succeed locally: {reg.text}"

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {reg.json()['access_token']}"},
    )
    assert me.status_code == 200, f"GET /auth/me failed: {me.text}"
    user_id = me.json()["id"]

    urn = f"urn:li:corpuser:{email.lower()}"

    try:
        assert await corpuser_exists(datahub_client, urn) is False, (
            f"Registration must not create the corpuser {urn} — creation is "
            "local-only per spec/feature/AUTH.md §Projection contract"
        )

        # The corpuser must EXIST and belong to someone else for this test to have
        # teeth. Against a nonexistent corpuser a batchAssignRole is silently
        # dropped by DataHub's RoleService while reporting success, so an absent
        # aspect would prove nothing about the binding gate. This reproduces
        # spec/feature/AUTH.md §Security Considerations: the genuine owner of the
        # address signs into DataHub, JIT provisions the corpuser, and it is
        # theirs — the squatter's unbound row must still never address it.
        await _provision_corpuser(datahub_client, email, "Genuine Owner")
        await propagate_role(datahub_client, urn, "Admin")
        assert await read_role(datahub_client, urn) == "Admin", (
            "Precondition: the genuine owner's corpuser holds Admin before the pass"
        )
        before = await datahub_client.get_aspect(urn, NativeGroupMembershipClass)
        groups_before = list(before.nativeGroups) if before is not None else []

        result = await _run_reconciliation(api_client, internal_headers)
        assert result["skipped_unbound"] >= 1, (
            "A row with google_sub IS NULL must be counted skipped_unbound per "
            f"spec/feature/AUTH.md §Role Drift Reconciliation step 1. Got: {result}"
        )
        # Attributable to THIS row: the counter alone is satisfied by the bootstrap
        # admin, which is unbound on every cluster.
        fixed_events = await async_session.execute(
            text(
                "SELECT count(*) AS n FROM dataspoke.events "
                "WHERE entity_type = 'user' AND entity_id = :id "
                "AND event_type = 'AUTH.ROLE_SYNC_FIXED'"
            ),
            {"id": user_id},
        )
        assert fixed_events.fetchone().n == 0, (
            "No repair event may be emitted for an unbound row per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )

        assert await read_role(datahub_client, urn) == "Admin", (
            "The unbound row's Reader role must NOT overwrite the role held by the "
            "corpuser at that URN — it belongs to the identity DataHub verified, "
            "not to the unbound DataSpoke row, per spec/feature/AUTH.md "
            "§Identity-binding requirement"
        )
        after = await datahub_client.get_aspect(urn, NativeGroupMembershipClass)
        groups_after = list(after.nativeGroups) if after is not None else []
        assert groups_after == groups_before, (
            "An unbound row must not alter group membership on a corpuser it does "
            "not own per spec/feature/AUTH.md §Identity-binding requirement"
        )
    finally:
        await _delete_user(async_session, user_id)
        await hard_delete_corpuser(datahub_client, urn)


@pytest.mark.asyncio
async def test_bound_user_without_corpuser_is_skipped_unprovisioned(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    datahub_client,
    async_session,
) -> None:
    """A bound row whose corpuser has never been provisioned is skipped, not created.

    spec: spec/feature/AUTH.md §Failure Modes — "A DataSpoke user has never logged
    into DataHub, so no corpuser exists | The reconciliation pass's existence probe
    skips them without mutating; counted skipped_unprovisioned."
    """
    from datahub.metadata.schema_classes import RoleMembershipClass

    from src.backend.datahub.users import corpuser_exists

    email = _unique_email("unprovisioned")
    user_id = await _seed_bound_user(
        async_session, email=email, name="Unprovisioned User", role="Admin"
    )
    urn = f"urn:li:corpuser:{email.lower()}"

    try:
        # Precondition: DataHub has never provisioned this corpuser.
        assert await corpuser_exists(datahub_client, urn) is False

        result = await _run_reconciliation(api_client, internal_headers)
        assert result["skipped_unprovisioned"] >= 1, (
            "A bound row with no corpuser must be counted skipped_unprovisioned per "
            f"spec/feature/AUTH.md §Role Drift Reconciliation. Got: {result}"
        )

        assert await corpuser_exists(datahub_client, urn) is False, (
            "The pass creates no corpuser — DataSpoke never creates one per "
            "spec/feature/AUTH.md §Projection contract"
        )
        assert await datahub_client.get_aspect(urn, RoleMembershipClass) is None, (
            "Nothing may be mutated for an unprovisioned corpuser per "
            "spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation step 2"
        )
    finally:
        await _delete_user(async_session, user_id)
