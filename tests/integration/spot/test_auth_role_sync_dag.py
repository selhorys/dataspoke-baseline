"""Spot integration test: the auth-role-sync-daily DAG reconciles both projected facets.

Concerns covered:
- A DAG run repairs role drift and missing marker-group membership on the same
  bound user, and emits one AUTH.ROLE_SYNC_FIXED event naming both facets
- An unbound row present in the same pass is left entirely alone

Why spot: the pass only projects onto a row carrying a ``google_sub`` whose
corpuser DataHub has already provisioned. The Google OAuth round-trip and a real
DataHub login cannot be driven from a test, so the row is seeded with raw SQL and
the corpuser with a direct aspect emit.

The counter semantics of the pass ({checked, fixed, skipped_unbound,
skipped_unprovisioned, errors}) are asserted against the activity endpoint in
test_auth_projection.py — a DAG run does not surface them. This module covers the
DAG wiring and the end-to-end effect.

spec: spec/feature/AUTH.md §Role Drift Reconciliation
spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation
"""

import asyncio
import uuid

import httpx
import pytest
from datahub.metadata.schema_classes import (
    CorpUserInfoClass,
    NativeGroupMembershipClass,
    RoleMembershipClass,
    StatusClass,
)
from sqlalchemy import text


def _unique_email(prefix: str = "role-sync") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


async def _read_role_aspect(datahub_client, corpuser_urn: str) -> str | None:
    """Return the corpuser's atomic single role from the RoleMembership aspect.

    Reads the aspect directly (not the IsMemberOfRole GraphQL relationship index)
    so assertions are deterministic — see src/backend/datahub/users.py:read_role.
    """
    aspect = await datahub_client.get_aspect(corpuser_urn, RoleMembershipClass)
    if aspect is None or not aspect.roles:
        return None
    return aspect.roles[0].removeprefix("urn:li:dataHubRole:")


async def _seed_bound_user(session, *, email: str, name: str, role: str) -> str:
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


async def _provision_corpuser(datahub_client, email: str, name: str) -> str:
    """Stand in for DataHub's OIDC JIT provisioning of a corpuser on first DataHub login."""
    urn = f"urn:li:corpuser:{email.lower()}"
    await datahub_client.emit_aspect(urn, StatusClass(removed=False))
    await datahub_client.emit_aspect(
        urn, CorpUserInfoClass(active=True, email=email.lower(), displayName=name)
    )
    return urn


@pytest.mark.asyncio
async def test_dag_run_repairs_both_facets_and_leaves_unbound_row_alone(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
    airflow_client,
    async_session,
) -> None:
    """One auth-role-sync-daily run repairs both facets for the bound user only.

    Flow:
    1. Seed a bound user with users.role = Admin and provision its corpuser.
    2. Drive DataHub-side drift: assign Editor directly via batchAssignRole, and
       leave marker-group membership absent.
    3. Register a password-only (unbound) user in the same pass.
    4. Trigger auth-role-sync-daily and wait.
    5. Bound user: RoleMembership is Admin again and the marker group URN is on the
       nativeGroupMembership aspect; one AUTH.ROLE_SYNC_FIXED event names both facets.
    6. Unbound user: still no corpuser, no role, no membership.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation steps 3-5 — DataSpoke wins
    on the role facet; the marker group URN is added when absent; one
    AUTH.ROLE_SYNC_FIXED event per repaired user whose detail names the facet(s).
    spec: spec/feature/AUTH.md §Identity-binding requirement — an unbound row is
    never projected.
    """
    from src.backend.datahub.users import corpuser_exists, hard_delete_corpuser

    conf_resp = await api_client.get("/api/v1/admin/conf", headers=admin_headers)
    assert conf_resp.status_code == 200, f"GET /admin/conf failed: {conf_resp.text}"
    group_urn = f"urn:li:corpGroup:{conf_resp.json()['auth_datahub_corp_group']}"

    # URNs are derived before any cluster mutation so teardown can always run.
    bound_email = _unique_email("bound")
    bound_urn = f"urn:li:corpuser:{bound_email.lower()}"
    unbound_email = _unique_email("unbound")
    unbound_urn = f"urn:li:corpuser:{unbound_email.lower()}"
    bound_id: str | None = None
    unbound_id: str | None = None

    try:
        # 1. Bound user, DataSpoke says Admin.
        bound_id = await _seed_bound_user(
            async_session, email=bound_email, name="Role Sync Bound User", role="Admin"
        )
        await _provision_corpuser(datahub_client, bound_email, "Role Sync Bound User")

        # 3. Unbound user via open self-service registration.
        reg = await api_client.post(
            "/api/v1/auth/register",
            json={
                "email": unbound_email,
                "name": "Role Sync Unbound User",
                "password": "password1234",
            },
        )
        assert reg.status_code == 201, f"Registration failed: {reg.text}"
        me = await api_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {reg.json()['access_token']}"},
        )
        assert me.status_code == 200, f"GET /auth/me failed: {me.text}"
        unbound_id = me.json()["id"]
        # 2. Drive role drift directly on DataHub, bypassing DataSpoke.
        drift_result = await datahub_client.execute_graphql(
            "mutation($r: String!, $u: [String!]!) "
            "{ batchAssignRole(input: {roleUrn: $r, actors: $u}) }",
            {"r": "urn:li:dataHubRole:Editor", "u": [bound_urn]},
        )
        assert drift_result is not None, "batchAssignRole must succeed to establish drift"

        role_before = await _read_role_aspect(datahub_client, bound_urn)
        assert role_before == "Editor", (
            "Drift not established: RoleMembership should be Editor after the direct "
            f"batchAssignRole mutation. Got: {role_before}"
        )
        membership_before = await datahub_client.get_aspect(
            bound_urn, NativeGroupMembershipClass
        )
        groups_before = (
            list(membership_before.nativeGroups) if membership_before is not None else []
        )
        assert group_urn not in groups_before, (
            "Precondition: the marker group must be absent so the group facet has "
            f"something to repair. Got: {groups_before}"
        )

        # 4. Unpause the DAG, clear blockers (max_active_runs=1), trigger, wait.
        await airflow_client._authed_call(
            lambda: airflow_client._get_client().patch(
                "/api/v2/dags/auth-role-sync-daily",
                json={"is_paused": False},
            )
        )
        for existing_run in await airflow_client.find_active_dag_runs("auth-role-sync-daily"):
            await airflow_client.kill_dag_run("auth-role-sync-daily", existing_run.dag_run_id)
        await asyncio.sleep(3)

        dag_run = await airflow_client.trigger_and_wait(
            "auth-role-sync-daily",
            conf={},
            timeout_seconds=180,
        )
        assert dag_run is not None, "auth-role-sync-daily DAG run must complete"

        # 5. Bound user — role facet.
        role_after = await _read_role_aspect(datahub_client, bound_urn)
        assert role_after == "Admin", (
            "RoleMembership must be re-asserted to the DataSpoke role — DataSpoke wins "
            f"per spec/feature/AUTH.md §Role Drift Reconciliation step 3. Got: {role_after}"
        )

        # 5. Bound user — group facet.
        membership_after = await datahub_client.get_aspect(
            bound_urn, NativeGroupMembershipClass
        )
        groups_after = (
            list(membership_after.nativeGroups) if membership_after is not None else []
        )
        assert group_urn in groups_after, (
            f"The marker group {group_urn} must be added when absent per "
            f"spec/feature/AUTH.md §Role Drift Reconciliation step 4. Got: {groups_after}"
        )

        # 5. Bound user — one event naming both facets.
        event_result = await async_session.execute(
            text(
                "SELECT event_type, entity_id, status, detail "
                "FROM dataspoke.events "
                "WHERE event_type = 'AUTH.ROLE_SYNC_FIXED' AND entity_id = :user_id "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"user_id": bound_id},
        )
        row = event_result.fetchone()
        assert row is not None, (
            f"An AUTH.ROLE_SYNC_FIXED event must exist for entity_id={bound_id} per "
            "spec/feature/AUTH.md §Role Drift Reconciliation step 5"
        )
        assert row.status == "OK", f"Event status must be OK, got: {row.status}"
        detail = row.detail if isinstance(row.detail, dict) else {}
        assert set(detail.get("repaired_facets", [])) == {"role", "group"}, (
            "The event detail must name every repaired facet per "
            f"spec/feature/AUTH.md §Role Drift Reconciliation step 5. Got: {detail}"
        )
        assert detail.get("dataspoke_role_authoritative") == "Admin", (
            "The event detail records the authoritative role per "
            "spec/feature/AUTH.md §Role Drift Reconciliation step 5"
        )
        assert detail.get("datahub_role_observed") == "Editor", (
            "The event detail records the observed (drifted) role per "
            "spec/feature/AUTH.md §Role Drift Reconciliation step 5"
        )

        # 6. Unbound user — untouched by the same pass.
        assert await corpuser_exists(datahub_client, unbound_urn) is False, (
            "The pass must not create a corpuser for an unbound row per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )
        assert await datahub_client.get_aspect(unbound_urn, RoleMembershipClass) is None, (
            "No role may be projected onto an unbound row per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )
        assert (
            await datahub_client.get_aspect(unbound_urn, NativeGroupMembershipClass) is None
        ), (
            "No marker-group membership may be projected onto an unbound row per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )
        unbound_event = await async_session.execute(
            text(
                "SELECT count(*) AS n FROM dataspoke.events "
                "WHERE event_type = 'AUTH.ROLE_SYNC_FIXED' AND entity_id = :user_id"
            ),
            {"user_id": unbound_id},
        )
        assert unbound_event.fetchone().n == 0, (
            "No repair event may be emitted for a row the pass never touched per "
            "spec/feature/AUTH.md §Identity-binding requirement"
        )
    finally:
        await hard_delete_corpuser(datahub_client, bound_urn)
        for user_id in (bound_id, unbound_id):
            if user_id is None:
                continue  # setup failed before this row was created
            await async_session.execute(
                text("DELETE FROM dataspoke.events WHERE entity_type = 'user' "
                     "AND entity_id = :id"),
                {"id": user_id},
            )
            await async_session.execute(
                text("DELETE FROM dataspoke.users WHERE id = :id"), {"id": user_id}
            )
        await async_session.commit()
