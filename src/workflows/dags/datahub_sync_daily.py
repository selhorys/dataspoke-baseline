"""Airflow DAG: datahub-sync-daily

Daily reconciliation of ``dataset_registry.datahub_registered`` against
DataHub. Posts to ``/internal/admin/datahub/sync`` with an empty body so the
endpoint performs a full sweep of every row in the registry.

"""
from __future__ import annotations

import json
from datetime import timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

with DAG(
    dag_id="datahub-sync-daily",
    description="Daily reconciliation of dataset_registry.datahub_registered against DataHub",
    schedule="@daily",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={"retries": 3, "retry_delay": timedelta(seconds=30)},
    tags=["datahub", "sync", "daily"],
    doc_md="""
## datahub-sync-daily

Runs on a `@daily` schedule. Calls `POST /internal/admin/datahub/sync` with an
empty body, which triggers a full sweep of every row in `dataset_registry` and
reconciles the `datahub_registered` flag against the live DataHub instance.

**Task**: `datahub_sync` — POST `/internal/admin/datahub/sync` — returns
`{checked, flipped_true, flipped_false, unchanged, not_found}`.
""",
) as dag:
    datahub_sync = HttpOperator(
        task_id="datahub_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/admin/datahub/sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),  # null dataset_urns → full sweep
        response_filter=lambda response: response.json(),
        log_response=True,
    )
