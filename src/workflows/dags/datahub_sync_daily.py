"""Airflow DAG: datahub-sync-daily

Daily reconciliation of dataset_registry.datahub_registered against DataHub.
Posts to /internal/activities/datahub/sync with an empty body so the endpoint
performs a full sweep of every row in the registry.

Spec: spec/feature/BACKEND.md §DataHub Sync, §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "datahub-sync-daily"

with DAG(
    dag_id=_DAG_ID,
    description="Daily reconciliation of dataset_registry.datahub_registered against DataHub",
    schedule="@daily",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["datahub", "sync", "daily"],
    doc_md="""
## datahub-sync-daily

Runs on a `@daily` schedule. Calls `POST /internal/activities/datahub/sync` with an
empty body, which triggers a full sweep of every row in `dataset_registry` and
reconciles the `datahub_registered` flag against the live DataHub instance.

**Task**: `datahub_sync` — returns `{checked, flipped_true, flipped_false, unchanged, not_found}`.
""",
) as dag:
    datahub_sync = HttpOperator(
        task_id="datahub_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/datahub/sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
