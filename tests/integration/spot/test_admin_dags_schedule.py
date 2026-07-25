"""Spot tests for Admin DAG schedule control — pause/unpause DAG groups.

Operational schedule control over Airflow's per-DAG ``is_paused`` flag, exposed as
``GET /admin/dags`` and ``PATCH /admin/dags/{group}``. Airflow is the SSOT for paused
state; DataSpoke keeps no copy. These concerns together cover the integration scope of
the feature (the api-wired UC stories never touch admin schedule control, so the spot
set alone owns it):

Concerns covered:
- GET /admin/dags returns the controllable groups (incl. auth_role_sync) with the
  spec group→DAG map
- PATCH /admin/dags/{group} {paused} flips Airflow's is_paused for every member DAG
  (single-member datahub_sync AND multi-member ontogen, to catch a partial-loop bug)
- GET /admin/dags folds per-DAG state into group paused/mixed aggregation
- PATCH /admin/dags/{unknown} → 404 DAG_GROUP_NOT_FOUND
- PATCH /admin/dags/{group} requires Admin role (Reader → 403)
- The ontogen tier activity skips (not fails) a disabled-but-tier-matching conf

The group→DAG expectations below are inlined from the spec group→DAG table (not imported
from src) so the assertions derive from the contract, not the implementation.

spec: spec/API.md §Admin — GET /admin/dags, PATCH /admin/dags/{group}
spec: spec/feature/BACKEND.md §Schedule Control, §DAG Catalogue (tier-DAG selection)
spec: spec/ARCHITECTURE.md — scheduled DAGs ship paused; Airflow is SSOT for paused state
spec: spec/TESTING.md §Spot vs Api-Wired Integration Tests
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from src.backend.auth.tokens import issue_access_token
from src.workflows.airflow.client import AirflowClient

# The controllable groups and their member DAGs — inlined from the spec
# group→DAG table (spec/API.md §Admin, spec/feature/BACKEND.md §Schedule Control).
EXPECTED_GROUP_DAGS: dict[str, set[str]] = {
    "datahub_sync": {"datahub-sync-hourly"},
    "auth_role_sync": {"auth-role-sync-daily"},
    "ingestion_active": {
        "ingestion-active-hourly",
        "ingestion-active-daily",
        "ingestion-active-weekly",
    },
    "ontogen": {"ontogen-hourly", "ontogen-daily", "ontogen-weekly"},
    "metagen": {"metagen-hourly", "metagen-daily", "metagen-weekly"},
    "metrics": {"metrics-hourly", "metrics-daily", "metrics-weekly"},
}


def _group(body: dict, name: str) -> dict:
    """Return the group object with ``group == name`` from a /admin/dags body."""
    matches = [g for g in body["groups"] if g["group"] == name]
    assert matches, f"group {name!r} absent from /admin/dags response: {body}"
    return matches[0]


@pytest.mark.asyncio
async def test_get_dag_groups_structure(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /admin/dags returns all controllable groups, each with the spec member-DAG set.

    spec: API.md §Admin — GET /admin/dags response
        {groups: [{group, paused, mixed, dags: [{dag_id, paused}]}]}.
    spec: feature/BACKEND.md §Schedule Control — the controllable groups (incl.
        auth_role_sync → auth-role-sync-daily) and their member DAGs (group→DAG map).
    """
    resp = await api_client.get("/api/v1/admin/dags", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "groups" in body, body

    returned_groups = {g["group"] for g in body["groups"]}
    assert returned_groups == set(EXPECTED_GROUP_DAGS), (
        f"GET /admin/dags must return exactly the controllable groups "
        f"{set(EXPECTED_GROUP_DAGS)}; got {returned_groups}"
    )

    for name, expected_dags in EXPECTED_GROUP_DAGS.items():
        g = _group(body, name)
        # Each group carries the fixed status fields and per-DAG detail.
        assert isinstance(g["paused"], bool)
        assert isinstance(g["mixed"], bool)
        member_ids = {d["dag_id"] for d in g["dags"]}
        assert member_ids == expected_dags, (
            f"group {name!r} member DAGs must be {expected_dags}; got {member_ids}"
        )
        for d in g["dags"]:
            assert isinstance(d["paused"], bool)
        # paused is true only when ALL members are paused; mixed only when they disagree.
        all_paused = all(d["paused"] for d in g["dags"])
        any_paused = any(d["paused"] for d in g["dags"])
        assert g["paused"] is all_paused, (
            f"group {name!r}.paused must be all-members-paused per spec; "
            f"members={g['dags']}"
        )
        assert g["mixed"] is (any_paused and not all_paused), (
            f"group {name!r}.mixed must be (some-paused & not-all-paused) per spec; "
            f"members={g['dags']}"
        )


@pytest.mark.asyncio
async def test_patch_group_pause_unpause_flips_airflow(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    airflow_client: AirflowClient,
) -> None:
    """PATCH /admin/dags/{group} sets is_paused on every member DAG in Airflow.

    Uses the single-DAG ``datahub_sync`` group. The flip is confirmed independently by
    reading Airflow's is_paused directly (Airflow is the SSOT), and the PATCH response's
    folded group status must agree.

    spec: API.md §Admin — PATCH /admin/dags/{group} {paused: bool} sets is_paused on
        every member DAG and returns the recomputed group status.
    spec: feature/BACKEND.md §Schedule Control — Airflow is SSOT for paused state.
    """
    dag_id = "datahub-sync-hourly"
    initial = (await airflow_client.get_dag_paused_states()).get(dag_id, False)
    try:
        # Pause the group → Airflow shows the member paused; response.paused is true.
        resp = await api_client.patch(
            "/api/v1/admin/dags/datahub_sync",
            headers=admin_headers,
            json={"paused": True},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["group"] == "datahub_sync"
        assert status["paused"] is True
        assert status["mixed"] is False  # single-member group is never mixed
        assert {d["dag_id"] for d in status["dags"]} == {dag_id}
        assert (await airflow_client.get_dag_paused_states())[dag_id] is True

        # Unpause the group → Airflow shows it not paused; response.paused is false.
        resp = await api_client.patch(
            "/api/v1/admin/dags/datahub_sync",
            headers=admin_headers,
            json={"paused": False},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["paused"] is False
        assert status["mixed"] is False
        assert (await airflow_client.get_dag_paused_states())[dag_id] is False
    finally:
        await airflow_client.set_dag_paused(dag_id, initial)


@pytest.mark.asyncio
async def test_patch_auth_role_sync_pause_unpause_flips_airflow(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    airflow_client: AirflowClient,
) -> None:
    """PATCH /admin/dags/auth_role_sync flips is_paused on its member DAG in Airflow.

    auth_role_sync is the single-member group mapping to auth-role-sync-daily. The
    flip is confirmed independently by reading Airflow's is_paused directly (Airflow
    is the SSOT), and the PATCH response's folded group status must agree.

    spec: API.md §Admin — DagGroup includes auth_role_sync; PATCH /admin/dags/{group}
        {paused} sets is_paused on every member DAG and returns the group status.
    spec: feature/BACKEND.md §Schedule Control — auth_role_sync → auth-role-sync-daily.
    """
    dag_id = "auth-role-sync-daily"
    initial = (await airflow_client.get_dag_paused_states()).get(dag_id, False)
    try:
        resp = await api_client.patch(
            "/api/v1/admin/dags/auth_role_sync",
            headers=admin_headers,
            json={"paused": True},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["group"] == "auth_role_sync"
        assert status["paused"] is True
        assert status["mixed"] is False  # single-member group is never mixed
        assert {d["dag_id"] for d in status["dags"]} == {dag_id}
        assert (await airflow_client.get_dag_paused_states())[dag_id] is True

        resp = await api_client.patch(
            "/api/v1/admin/dags/auth_role_sync",
            headers=admin_headers,
            json={"paused": False},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["paused"] is False
        assert status["mixed"] is False
        assert (await airflow_client.get_dag_paused_states())[dag_id] is False
    finally:
        await airflow_client.set_dag_paused(dag_id, initial)


@pytest.mark.asyncio
async def test_patch_multimember_group_flips_every_member(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    airflow_client: AirflowClient,
) -> None:
    """PATCH /admin/dags/{group} flips is_paused on EVERY member of a multi-DAG group.

    Exercises the three-member ``ontogen`` group on the primary write path so a
    partial/early-breaking loop (pausing only one member) is caught — the single-member
    ``datahub_sync`` case cannot detect an off-by-one. The flip is confirmed
    independently by reading Airflow's is_paused for all three members directly (Airflow
    is the SSOT), and the PATCH response's folded status must agree.

    spec: API.md §Admin — PATCH /admin/dags/{group} {paused: bool} sets is_paused on
        every member DAG of the group (API.md L582-584).
    spec: feature/BACKEND.md §Schedule Control — set on every member; Airflow is SSOT.
    """
    members = sorted(EXPECTED_GROUP_DAGS["ontogen"])
    assert len(members) == 3, "ontogen must have three members for this test to bite"
    initial = await airflow_client.get_dag_paused_states()
    try:
        # Pause the whole group → every member must show paused in Airflow.
        resp = await api_client.patch(
            "/api/v1/admin/dags/ontogen",
            headers=admin_headers,
            json={"paused": True},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["group"] == "ontogen"
        assert status["paused"] is True, f"all-members-paused must fold to true: {status}"
        assert status["mixed"] is False, f"all paused must not be mixed: {status}"
        assert {d["dag_id"] for d in status["dags"]} == set(members)
        assert all(d["paused"] is True for d in status["dags"]), status
        # Independent read-back: ALL three member DAGs flipped, not just one.
        live = await airflow_client.get_dag_paused_states()
        for m in members:
            assert live[m] is True, (
                f"PATCH must pause every member; {m} still {live[m]} (partial loop?)"
            )

        # Unpause the whole group → every member must flip back.
        resp = await api_client.patch(
            "/api/v1/admin/dags/ontogen",
            headers=admin_headers,
            json={"paused": False},
        )
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["paused"] is False, status
        assert status["mixed"] is False, status
        assert all(d["paused"] is False for d in status["dags"]), status
        live = await airflow_client.get_dag_paused_states()
        for m in members:
            assert live[m] is False, (
                f"PATCH must unpause every member; {m} still {live[m]} (partial loop?)"
            )
    finally:
        for m in members:
            await airflow_client.set_dag_paused(m, initial.get(m, False))


@pytest.mark.asyncio
async def test_get_group_mixed_aggregation(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    airflow_client: AirflowClient,
) -> None:
    """GET /admin/dags folds per-DAG paused state into group paused/mixed.

    Drives the three ``ontogen`` member DAGs directly through Airflow (spot may call
    dataspoke Python directly) into each fold case, then asserts the GET aggregation:
      - some paused, some not  → paused=false, mixed=true
      - all paused             → paused=true,  mixed=false
      - none paused            → paused=false, mixed=false

    spec: API.md §Admin / feature/BACKEND.md §Schedule Control — group.paused is true
        only when ALL members are paused; group.mixed is true when members disagree.
    """
    members = sorted(EXPECTED_GROUP_DAGS["ontogen"])
    initial = await airflow_client.get_dag_paused_states()
    try:
        # Mixed: pause exactly one member, leave the others unpaused.
        await airflow_client.set_dag_paused(members[0], True)
        for m in members[1:]:
            await airflow_client.set_dag_paused(m, False)
        g = _group(
            (await api_client.get("/api/v1/admin/dags", headers=admin_headers)).json(),
            "ontogen",
        )
        assert g["paused"] is False, f"not-all-paused must fold to paused=false: {g}"
        assert g["mixed"] is True, f"disagreeing members must fold to mixed=true: {g}"

        # All paused → paused=true, mixed=false.
        for m in members:
            await airflow_client.set_dag_paused(m, True)
        g = _group(
            (await api_client.get("/api/v1/admin/dags", headers=admin_headers)).json(),
            "ontogen",
        )
        assert g["paused"] is True, f"all-paused must fold to paused=true: {g}"
        assert g["mixed"] is False, f"all-paused must fold to mixed=false: {g}"

        # None paused → paused=false, mixed=false.
        for m in members:
            await airflow_client.set_dag_paused(m, False)
        g = _group(
            (await api_client.get("/api/v1/admin/dags", headers=admin_headers)).json(),
            "ontogen",
        )
        assert g["paused"] is False, f"none-paused must fold to paused=false: {g}"
        assert g["mixed"] is False, f"none-paused must fold to mixed=false: {g}"
    finally:
        for m in members:
            await airflow_client.set_dag_paused(m, initial.get(m, False))


@pytest.mark.asyncio
async def test_patch_unknown_group_returns_404(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /admin/dags/{unknown} returns 404 DAG_GROUP_NOT_FOUND.

    spec: API.md §Admin — an unknown ``group`` returns 404 DAG_GROUP_NOT_FOUND.
    spec: feature/BACKEND.md §Schedule Control — unknown group raises 404 DAG_GROUP_NOT_FOUND.
    """
    resp = await api_client.patch(
        "/api/v1/admin/dags/not_a_real_group",
        headers=admin_headers,
        json={"paused": True},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "DAG_GROUP_NOT_FOUND", resp.text


@pytest.mark.asyncio
async def test_patch_dags_requires_admin_role(
    api_client: httpx.AsyncClient,
    async_session,
) -> None:
    """PATCH /admin/dags/{group} returns 403 for a Reader-role caller.

    Uses a REAL seeded Reader user so the 403 comes from the require_admin role gate,
    not from a missing-user branch (which would be 401). Pins error_code FORBIDDEN to
    confirm the /admin/* gate specifically (READ_ONLY_ROLE is the /spoke/* write gate).

    spec: API.md §Access Control — /admin/* requires users.role = 'Admin'.
    spec: feature/AUTH.md §Privilege Model — Editor/Reader on /admin/* → 403 FORBIDDEN;
        /spoke/* write by Reader → 403 READ_ONLY_ROLE (a distinct gate).
    """
    reader_id = uuid.uuid4()
    reader_email = f"reader-dagschd-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"
    await async_session.execute(
        text(
            "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
            " VALUES (:id, :email, :name, :google_sub, 'Reader')"
        ),
        {
            "id": str(reader_id),
            "email": reader_email,
            "name": "Reader Test User",
            "google_sub": f"test-sub-{uuid.uuid4()}",
        },
    )
    await async_session.commit()
    try:
        reader_token, _ = issue_access_token(reader_id, reader_email, session_epoch=0)
        resp = await api_client.patch(
            "/api/v1/admin/dags/datahub_sync",
            headers={"Authorization": f"Bearer {reader_token}"},
            json={"paused": True},
        )
        assert resp.status_code == 403, (
            f"Reader-role caller must get 403 on PATCH /admin/dags/* per "
            f"spec/API.md §Access Control; got {resp.status_code}: {resp.text}"
        )
        # The /admin/* admin gate returns FORBIDDEN, distinct from the /spoke/*
        # write gate's READ_ONLY_ROLE (feature/AUTH.md §Privilege Model L322).
        assert resp.json()["error_code"] == "FORBIDDEN", (
            f"the /admin/* role gate must return error_code FORBIDDEN (not "
            f"READ_ONLY_ROLE, which is scoped to /spoke/* writes): {resp.text}"
        )
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(reader_id)},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_ontogen_activity_skips_disabled_conf(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """The ontogen tier activity skips (not fails) a disabled, tier-matching conf.

    Sets the singleton ontogen conf to ``is_enabled=false`` with a known
    ``schedule_tier``, then invokes the tier activity at the matching tier. A disabled
    conf is a no-op, not an error: the activity returns
    ``{status: "skipped", reason: "disabled"}`` (distinct from the ``tier_mismatch``
    short-circuit). Pause state and conf enablement are independent axes.

    spec: feature/BACKEND.md §DAG Catalogue (tier-DAG selection) — when the singleton
        conf's tier matches but is_enabled is false, the ontogen activity returns
        {status: "skipped", reason: "disabled"} rather than failing.
    spec: feature/BACKEND.md §Schedule Control — an unpaused DAG still skips disabled
        confs at run time.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    # Snapshot the current conf so we can restore it afterwards.
    original = (await api_client.get(conf_url, headers=admin_headers)).json()

    try:
        # Disable the conf at a known tier.
        put = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": original.get("dataset_filter") or {},
                "default_run_prompt": original.get("default_run_prompt"),
            },
        )
        assert put.status_code in (200, 201), put.text

        # Invoke the tier activity at the matching tier. Internal routes are mounted
        # WITHOUT the /api/v1 prefix and gated by X-Internal-Token.
        resp = await api_client.post(
            "/internal/activities/ontogen/run",
            headers=internal_headers,
            json={"tier": "daily"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("status") == "skipped", (
            f"a disabled but tier-matching conf must be skipped, not run/failed: {body}"
        )
        assert body.get("reason") == "disabled", (
            f"the skip reason for a disabled conf must be 'disabled' "
            f"(not 'tier_mismatch'): {body}"
        )
    finally:
        # Restore the original conf.
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": original.get("is_enabled", True),
                "schedule_tier": original.get("schedule_tier"),
                "dataset_filter": original.get("dataset_filter") or {},
                "default_run_prompt": original.get("default_run_prompt"),
            },
        )
