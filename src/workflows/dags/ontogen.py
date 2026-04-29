"""Airflow DAG: ontogen

On-demand ontogen inference pipeline. Triggered via
POST /api/v1/spoke/common/ontogen/method/run.

Concurrency guard: the triggering API route calls
AirflowClient.check_no_duplicate("ontogen", "conf_key", "ontogen-singleton")
before triggering; a second run returns 409 while one is already running.

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""
from __future__ import annotations

from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "ontogen"

with DAG(
    dag_id=_DAG_ID,
    description="On-demand ontogen inference pipeline",
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
    tags=["ontogen", "on-demand"],
    doc_md="""
## ontogen

Triggered by the DataSpoke API for a full ontogen inference run.

**Inputs** (via `dag_run.conf`):
- `dry_run`: boolean (default: false) — compute without persisting
- `prompt_md`: optional Markdown prompt override (default: "")
- `conf_key`: always "ontogen-singleton" (for duplicate detection)

**Tasks**:
1. `run_ontogen` — POST `/internal/activities/ontogen/run`
""",
) as dag:
    run_ontogen = HttpOperator(
        task_id="run_ontogen",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontogen/run",
        method="POST",
        headers=internal_headers(),
        data=(
            '{"dry_run": {{ dag_run.conf.get(\'dry_run\', false) | lower }},'
            ' "prompt_md": "{{ dag_run.conf.get(\'prompt_md\', \'\') }}"}'
        ),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
