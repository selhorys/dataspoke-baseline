"""Airflow DAG: metagen

On-demand LLM-powered metadata generation for a single dataset.
Triggered via POST /api/v1/spoke/common/attr/{dataset_urn}/method/metagen/run.

Concurrency guard: the triggering API route calls
AirflowClient.check_no_duplicate("metagen", "conf_key", metagen-{md5(urn)[:12]})
before triggering; duplicate runs for the same dataset return 409.

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""
from __future__ import annotations

from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "metagen"

with DAG(
    dag_id=_DAG_ID,
    description="On-demand LLM metadata generation for a single dataset",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=False,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["metagen", "on-demand"],
    doc_md="""
## metagen

Triggered by the DataSpoke API for a specific dataset URN.

**Inputs** (via `dag_run.conf`):
- `dataset_urn`: fully-qualified DataHub URN (required)
- `dry_run`: boolean (default: false)
- `conf_key`: dedup key of the form `metagen-{md5(urn)[:12]}` (for duplicate detection)

**Tasks**:
1. `run_metagen` — POST `/internal/activities/metagen/run`
""",
) as dag:
    run_metagen = HttpOperator(
        task_id="run_metagen",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metagen/run",
        method="POST",
        headers=internal_headers(),
        data=(
            '{"dataset_urn": "{{ dag_run.conf.get(\'dataset_urn\', \'\') }}",'
            ' "dry_run": {{ dag_run.conf.get(\'dry_run\', false) | lower }}}'
        ),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
