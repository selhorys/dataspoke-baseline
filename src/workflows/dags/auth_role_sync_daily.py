"""Airflow DAG: auth-role-sync-daily

Daily reconciliation of the DataHub-side projection against DataSpoke
users.role (the SSOT).  Posts to /internal/activities/auth/role-sync;
the endpoint iterates every row in the users table and repairs two facets
per user — role and marker-group membership — by reading each corpuser's
RoleMembership and nativeGroupMembership aspects and re-asserting the
DataSpoke-side state on divergence.  DataSpoke wins.

Spec: spec/feature/AUTH.md §Role Drift Reconciliation,
      spec/DATAHUB_INTEGRATION.md §Nightly Role Reconciliation
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "auth-role-sync-daily"

with DAG(
    dag_id=_DAG_ID,
    description=(
        "Daily reconciliation of DataHub-side role assignments against "
        "DataSpoke users.role"
    ),
    schedule="@daily",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        # 15 min: the full users-table scan is O(n) DataHub round trips.  A
        # projected user costs up to five (existence probe, two aspect reads,
        # two mutations); rows without a google_sub binding and users whose
        # corpuser is unprovisioned short-circuit after the first check.
        # Larger deployments may approach this limit; the optimisation path is
        # scrollAcrossEntities on the marker corpGroup (see
        # spec/DATAHUB_INTEGRATION.md).
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["auth", "sync", "daily"],
    doc_md="""
## auth-role-sync-daily

Runs on a `@daily` schedule.  Calls `POST /internal/activities/auth/role-sync`
with an empty body.  The endpoint ensures the marker corpGroup exists, then
iterates every DataSpoke-managed user and repairs two independent facets on
the corpusers of users bound to a verified Google identity:

- **Role** — reads the `RoleMembership` aspect and re-asserts `users.role`
  via `batchAssignRole` on divergence (DataSpoke wins).
- **Marker group** — reads the `nativeGroupMembership` aspect and adds the
  corpuser to the marker corpGroup via `addGroupMembers` when absent.

An existence probe precedes every mutation: DataHub silently skips a role
assignment for an actor that does not exist while still reporting success,
so a pass that trusted the mutation result would report repairs it never
made.  A user with any facet repaired is recorded as one
`AUTH.ROLE_SYNC_FIXED` event whose `detail` names the repaired facets.

**Task**: `auth_role_sync` — returns
`{checked, fixed, skipped_unprovisioned, skipped_unbound, errors}`.
`fixed` counts users, not facets, and may overlap with `errors` when one
facet is repaired and the other fails.

Spec: [spec/feature/AUTH.md §Role Drift Reconciliation](../../../spec/feature/AUTH.md)
""",
) as dag:
    auth_role_sync = HttpOperator(
        task_id="auth_role_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/auth/role-sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
