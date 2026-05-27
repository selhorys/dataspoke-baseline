"""Spot integration test: auth-role-sync-daily DAG reconciles DataHub role drift.

Concerns covered:
- When DataHub-side role is mutated directly (set to Editor while DataSpoke users.role is Admin),
  running the auth-role-sync-daily DAG re-asserts the DataSpoke role (Admin wins).
- An AUTH.ROLE_SYNC_FIXED event row is emitted per fixed user.

spec: spec/feature/AUTH.md §Role Drift Reconciliation — DataSpoke is the SSOT;
      auth-role-sync-daily re-asserts users.role to DataHub on drift; DataSpoke wins.
spec: spec/feature/AUTH.md §Role Drift Reconciliation — emits AUTH.ROLE_SYNC_FIXED
      event row per user corrected.
spec: spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation
"""

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from datahub.metadata.schema_classes import RoleMembershipClass
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


@pytest.mark.asyncio
async def test_role_drift_corrected_by_dag(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    datahub_client,
    airflow_client,
    async_session,
) -> None:
    """auth-role-sync-daily: DataHub role drift is corrected; AUTH.ROLE_SYNC_FIXED event emitted.

    Flow:
    1. Register a user (starts as Reader).
    2. Promote to Admin via PATCH /admin/users/{id}/role.
    3. Directly mutate the DataHub-side role to Editor via batchAssignRole GraphQL
       (simulating drift — DataHub out of sync with DataSpoke).
    4. Trigger auth-role-sync-daily DAG via Airflow and wait for completion.
    5. Verify RoleMembership aspect shows Admin (DataSpoke won).
    6. Verify AUTH.ROLE_SYNC_FIXED event row in DB for this user.

    spec: spec/feature/AUTH.md §Role Drift Reconciliation — DataSpoke wins on drift.
    spec: spec/feature/AUTH.md §Role Drift Reconciliation — emits AUTH.ROLE_SYNC_FIXED event.
    """
    email = _unique_email()

    # 1. Register user
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Role Sync Test User", "password": "password1234"},
    )
    assert reg.status_code == 201, f"Registration failed: {reg.text}"
    access_token = reg.json()["access_token"]

    me = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me.status_code == 200
    user_id = me.json()["id"]

    # 2. Promote to Admin via API (DataSpoke SSOT now says Admin)
    role_resp = await api_client.patch(
        f"/api/v1/admin/users/{user_id}/role",
        json={"role": "Admin"},
        headers=admin_headers,
    )
    assert role_resp.status_code == 200, f"Role patch to Admin failed: {role_resp.text}"

    corpuser_urn = f"urn:li:corpuser:{email}"

    # 3. Directly mutate DataHub-side role to Editor (create drift)
    #    This uses the DataHub GraphQL batchAssignRole mutation directly,
    #    bypassing DataSpoke — simulating the drift scenario.
    editor_role_urn = "urn:li:dataHubRole:Editor"
    drift_mutation = (
        "mutation($r: String!, $u: [String!]!) "
        "{ batchAssignRole(input: {roleUrn: $r, actors: $u}) }"
    )
    drift_result = await datahub_client.execute_graphql(
        drift_mutation,
        {"r": editor_role_urn, "u": [corpuser_urn]},
    )
    # Mutation may return True or a truthy value on success
    assert drift_result is not None, "batchAssignRole mutation to create drift must succeed"

    # Confirm drift: the RoleMembership aspect now holds Editor (DataSpoke says Admin).
    role_before = await _read_role_aspect(datahub_client, corpuser_urn)
    assert role_before == "Editor", (
        f"Drift not established: RoleMembership aspect should be Editor after direct "
        f"batchAssignRole mutation. Got: {role_before}"
    )

    # 4. Unpause DAG, kill any existing active runs, then trigger manually.
    #    is_paused_upon_creation=True — must unpause before triggering.
    #    When the DAG is unpaused, the Airflow scheduler may immediately start
    #    a scheduled run for the missed daily slot; kill any such runs first
    #    so the manual trigger gets to run (max_active_runs=1).
    await airflow_client._authed_call(
        lambda: airflow_client._get_client().patch(
            "/api/v2/dags/auth-role-sync-daily",
            json={"is_paused": False},
        )
    )
    # Kill any running or queued runs that would block the manual trigger.
    # Uses find_active_dag_runs (running + queued) since max_active_runs=1 blocks
    # any new trigger regardless of whether the blocker is running or queued.
    active_before = await airflow_client.find_active_dag_runs("auth-role-sync-daily")
    for existing_run in active_before:
        await airflow_client.kill_dag_run("auth-role-sync-daily", existing_run.dag_run_id)
    # Allow the scheduler to update run states before we trigger.
    await asyncio.sleep(3)

    dag_run = await airflow_client.trigger_and_wait(
        "auth-role-sync-daily",
        conf={},
        timeout_seconds=180,
    )
    assert dag_run is not None, "auth-role-sync-daily DAG run must complete"

    # 5. Verify DataHub role is back to Admin (DataSpoke won) — atomic aspect read.
    role_after = await _read_role_aspect(datahub_client, corpuser_urn)
    assert role_after == "Admin", (
        f"RoleMembership aspect must be Admin after auth-role-sync-daily run "
        f"per spec/feature/AUTH.md §Role Drift Reconciliation (DataSpoke wins). "
        f"Got: {role_after}"
    )

    # 6. Verify AUTH.ROLE_SYNC_FIXED event row in DB for this user
    result = await async_session.execute(
        text(
            "SELECT id, event_type, entity_id, status, detail "
            "FROM dataspoke.events "
            "WHERE event_type = 'AUTH.ROLE_SYNC_FIXED' AND entity_id = :user_id "
            "ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"user_id": user_id},
    )
    row = result.fetchone()
    assert row is not None, (
        f"AUTH.ROLE_SYNC_FIXED event row must exist for user_id={user_id} "
        "per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert row.event_type == "AUTH.ROLE_SYNC_FIXED"
    assert row.status == "OK", f"AUTH.ROLE_SYNC_FIXED event status must be OK, got: {row.status}"

    detail = row.detail if isinstance(row.detail, dict) else {}
    assert detail.get("dataspoke_role_authoritative") == "Admin", (
        "AUTH.ROLE_SYNC_FIXED event detail must record dataspoke_role_authoritative=Admin "
        "per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
    assert detail.get("datahub_role_observed") == "Editor", (
        "AUTH.ROLE_SYNC_FIXED event detail must record datahub_role_observed=Editor "
        "(the drift role that was corrected) per spec/feature/AUTH.md §Role Drift Reconciliation"
    )
