"""Airflow DAG: embedding-sync

Reindexes dataset vectors in Qdrant. Triggered via API (no schedule).
"""
from __future__ import annotations

import os
from datetime import timedelta

from airflow import DAG
from airflow.providers.http.operators.http import SimpleHttpOperator

_DEFAULT_CALLBACK_BASE_URL = os.environ.get(
    "DATASPOKE_AIRFLOW_CALLBACK_BASE_URL", "http://dataspoke-api:8002"
)

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id="embedding-sync",
    description="Reindex dataset vectors in Qdrant",
    schedule=None,
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    default_args=_DEFAULT_ARGS,
    tags=["embedding", "search", "on-demand"],
    doc_md="""
## embedding-sync

Triggered via the DataSpoke API to reindex dataset vectors in Qdrant.

**Inputs** (via `dag_run.conf`):
- `callback_base_url`: DataSpoke API base URL (default: env `DATASPOKE_AIRFLOW_CALLBACK_BASE_URL`)
- `mode`: `"full"` or `"single"` (default: `"full"`)
- `dataset_urn`: dataset URN string (default: `""`, used when mode is `"single"`)

**Tasks**:
1. `enumerate_datasets` — POST `/internal/activities/search/enumerate` — returns list of URNs
2. `reindex_batch` — POST `/internal/activities/search/reindex-batch` — consumes URN list
""",
) as dag:
    enumerate_datasets = SimpleHttpOperator(
        task_id="enumerate_datasets",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/search/enumerate",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=(
            '{"mode": "{{ dag_run.conf.get(\'mode\', \'full\') }}",'
            ' "dataset_urn": "{{ dag_run.conf.get(\'dataset_urn\', \'\') }}"}'
        ),
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    reindex_batch = SimpleHttpOperator(
        task_id="reindex_batch",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/search/reindex-batch",
        method="POST",
        headers={"Content-Type": "application/json"},
        data='{"dataset_urns": {{ ti.xcom_pull(task_ids="enumerate_datasets") | tojson }}}',
        log_response=True,
    )

    enumerate_datasets >> reindex_batch
