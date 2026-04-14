"""Airflow DAG: metrics

Metric collection and update publishing. Triggered via API (no schedule).
"""
from __future__ import annotations

from datetime import timedelta

from airflow import DAG
from airflow.providers.http.operators.http import SimpleHttpOperator

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id="metrics",
    description="Metric collection and update publishing",
    schedule=None,
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=False,
    default_args=_DEFAULT_ARGS,
    tags=["metrics", "on-demand"],
    doc_md="""
## metrics

Runs a metric computation and publishes the result update.

**Inputs** (via `dag_run.conf`):
- `callback_base_url`: DataSpoke API base URL (default: `"http://dataspoke-api:8002"`)
- `metric_id`: metric identifier string (required)
- `dry_run`: boolean flag (default: `false`)

**Tasks**:
1. `run_metric` — POST `/internal/activities/metrics/run` — computes the metric
2. `publish_metric_update` — POST `/internal/activities/metrics/publish-update` — publishes result (uses XCom from step 1)
""",
) as dag:
    run_metric = SimpleHttpOperator(
        task_id="run_metric",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/run",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=(
            '{"metric_id": "{{ dag_run.conf.get(\'metric_id\', \'\') }}",'
            ' "dry_run": {{ dag_run.conf.get(\'dry_run\', false) | lower }}}'
        ),
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    publish_metric_update = SimpleHttpOperator(
        task_id="publish_metric_update",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/publish-update",
        method="POST",
        headers={"Content-Type": "application/json"},
        data="{{ ti.xcom_pull(task_ids='run_metric') | tojson }}",
        log_response=True,
    )

    run_metric >> publish_metric_update
