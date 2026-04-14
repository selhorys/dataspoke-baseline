"""Airflow DAG: validation-periodic-weekly

Periodic validation for the weekly schedule tier. Lists datasets due for weekly
validation, then fans out one `run_validation` task per dataset using dynamic
task mapping (Airflow 2.3+).

"""
from __future__ import annotations

import json
from datetime import timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.providers.http.operators.http import SimpleHttpOperator

_TIER = "weekly"
_SCHEDULE = "@weekly"

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id=f"validation-periodic-{_TIER}",
    description=f"Periodic validation for {_TIER} schedule",
    schedule=_SCHEDULE,
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=True,
    default_args=_DEFAULT_ARGS,
    tags=["validation", "periodic", _TIER],
    doc_md=f"""
## validation-periodic-{_TIER}

Runs on a `{_SCHEDULE}` schedule. Lists all datasets configured for `{_TIER}`
validation and triggers a parallel validation run for each one.

**Tasks**:
1. `list_datasets` — POST `/internal/activities/validation/list-periodic` — returns list of dataset URNs
2. `prepare_payloads` — converts URN list to JSON request bodies (PythonOperator via @task)
3. `run_validation` — POST `/internal/activities/validation/run` — dynamically mapped, one task per URN
""",
) as dag:
    list_datasets = SimpleHttpOperator(
        task_id="list_datasets",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/validation/list-periodic",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"schedule_tier": _TIER}),
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    @task
    def prepare_payloads(dataset_urns: list[str]) -> list[str]:
        """Convert a list of dataset URNs into JSON-encoded request bodies."""
        return [json.dumps({"dataset_urn": urn, "dry_run": False}) for urn in dataset_urns]

    payloads = prepare_payloads(list_datasets.output)

    run_validation = SimpleHttpOperator.partial(
        task_id="run_validation",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/validation/run",
        method="POST",
        headers={"Content-Type": "application/json"},
        log_response=True,
    ).expand(data=payloads)
