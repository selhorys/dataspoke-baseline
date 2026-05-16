"""Airflow DAG: metagen

On-demand metagen inference pipeline. Triggered via
POST /api/v1/spoke/common/metagen/method/run.

Concurrency guard: the triggering API route calls
AirflowClient.check_no_duplicate("metagen", "conf_key", "metagen-singleton")
before triggering; a second run returns 409 while one is already running.

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
    description="On-demand metagen inference pipeline",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["metagen", "on-demand"],
    doc_md="""
## metagen

Triggered by the DataSpoke API for a full metagen inference run.

**Inputs** (via `dag_run.conf`):
- `dataset_urns`: optional list of dataset URNs to scope the run (default: null — all in-scope datasets)
- `dry_run`: boolean (default: false) — compute without persisting
- `conf_key`: always "metagen-singleton" (for duplicate detection)

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
            '{"dataset_urns": {{ dag_run.conf.get(\'dataset_urns\', None) | tojson }},'
            ' "dry_run": {{ dag_run.conf.get(\'dry_run\', false) | lower }}}'
        ),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
