"""Airflow DAG: auth-role-sync-daily

Daily reconciliation of DataHub-side role assignments against DataSpoke
users.role (the SSOT).  Posts to /internal/activities/auth/role-sync;
the endpoint iterates every row in the users table, reads each corpuser's
IsMemberOfRole relationship on DataHub, and re-asserts the DataSpoke role
on any divergence.  DataSpoke wins.

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
        # 15 min: the full users-table scan is O(n) GraphQL round trips — one
        # IsMemberOfRole query per managed corpuser.  Larger deployments may
        # approach this limit; the optimisation path is scrollAcrossEntities
        # on the marker corpGroup (see spec/DATAHUB_INTEGRATION.md).
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["auth", "sync", "daily"],
    doc_md="""
## auth-role-sync-daily

Runs on a `@daily` schedule.  Calls `POST /internal/activities/auth/role-sync`
with an empty body.  The endpoint iterates every DataSpoke-managed user,
reads the corresponding DataHub corpuser's `IsMemberOfRole` relationship
via GraphQL, and re-asserts `users.role` to DataHub via `batchAssignRole`
whenever the two diverge (DataSpoke wins).  Each fix is recorded as an
`AUTH.ROLE_SYNC_FIXED` event row.

**Task**: `auth_role_sync` — returns `{checked, fixed, errors}`.

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
