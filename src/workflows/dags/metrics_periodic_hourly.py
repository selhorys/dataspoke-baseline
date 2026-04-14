"""Airflow DAG: metrics-periodic-hourly

Periodic metrics computation for the hourly schedule tier. Lists metrics due for
hourly execution, then fans out one `run_metric` task per metric using dynamic
task mapping (Airflow 2.3+).

"""
from __future__ import annotations

import json
from datetime import timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.providers.http.operators.http import SimpleHttpOperator

_TIER = "hourly"
_SCHEDULE = "@hourly"

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id=f"metrics-periodic-{_TIER}",
    description=f"Periodic metrics computation for {_TIER} schedule",
    schedule=_SCHEDULE,
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=True,
    default_args=_DEFAULT_ARGS,
    tags=["metrics", "periodic", _TIER],
    doc_md=f"""
## metrics-periodic-{_TIER}

Runs on a `{_SCHEDULE}` schedule. Lists all metrics configured for `{_TIER}`
execution and triggers a parallel metric run for each one.

**Tasks**:
1. `list_metrics` — POST `/internal/activities/metrics/list-periodic` — returns list of metric IDs
2. `prepare_payloads` — converts metric ID list to JSON request bodies (PythonOperator via @task)
3. `run_metric` — POST `/internal/activities/metrics/run` — dynamically mapped, one task per metric ID
""",
) as dag:
    list_metrics = SimpleHttpOperator(
        task_id="list_metrics",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/list-periodic",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"schedule_tier": _TIER}),
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    @task
    def prepare_payloads(metric_ids: list[str]) -> list[str]:
        """Convert a list of metric IDs into JSON-encoded request bodies."""
        return [json.dumps({"metric_id": mid, "dry_run": False}) for mid in metric_ids]

    payloads = prepare_payloads(list_metrics.output)

    run_metric = SimpleHttpOperator.partial(
        task_id="run_metric",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/run",
        method="POST",
        headers={"Content-Type": "application/json"},
        log_response=True,
    ).expand(data=payloads)
