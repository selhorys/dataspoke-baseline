"""Airflow DAG: metrics

On-demand metric measurement run for a single metric definition.
Triggered via POST /api/v1/spoke/dg/metrics/{metric_id}/method/metrics/run.

Concurrency guard: the triggering API route calls
AirflowClient.check_no_duplicate("metrics", "conf_key", "metrics-{metric_id}")
before triggering; duplicate runs for the same metric return 409.

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""
from __future__ import annotations

from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "metrics"

with DAG(
    dag_id=_DAG_ID,
    description="On-demand metric measurement run for a single metric definition",
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
    tags=["metrics", "on-demand"],
    doc_md="""
## metrics

Triggered by the DataSpoke API for a specific metric ID.

**Inputs** (via `dag_run.conf`):
- `metric_id`: metric identifier string (required)
- `conf_key`: dedup key of the form `metrics-{metric_id}` (for duplicate detection)

**Tasks**:
1. `run_metric` — POST `/internal/activities/metrics/run`
""",
) as dag:
    run_metric = HttpOperator(
        task_id="run_metric",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/run",
        method="POST",
        headers=internal_headers(),
        data='{"metric_id": "{{ dag_run.conf.get(\'metric_id\', \'\') }}"}',
        response_filter=lambda r: r.json(),
        log_response=True,
    )
