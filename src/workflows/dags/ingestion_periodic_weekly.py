"""Airflow DAG: ingestion-periodic-weekly

Periodic ingestion for the weekly schedule tier. Lists datasets due for weekly
ingestion, then fans out one `run_ingestion` task per dataset using dynamic
task mapping (Airflow 2.3+).

"""
from __future__ import annotations

import json
from datetime import timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.providers.http.operators.http import HttpOperator

from _internal_headers import internal_headers

_TIER = "weekly"
_SCHEDULE = "@weekly"

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id=f"ingestion-periodic-{_TIER}",
    description=f"Periodic ingestion for {_TIER} schedule",
    schedule=_SCHEDULE,
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=True,
    default_args=_DEFAULT_ARGS,
    tags=["ingestion", "periodic", _TIER],
    doc_md=f"""
## ingestion-periodic-{_TIER}

Runs on a `{_SCHEDULE}` schedule. Lists all datasets configured for `{_TIER}`
ingestion and triggers a parallel ingestion run for each one.

**Tasks**:
1. `list_datasets` — POST `/internal/activities/ingestion/list-periodic` — returns list of dataset URNs
2. `prepare_payloads` — converts URN list to JSON request bodies (PythonOperator via @task)
3. `run_ingestion` — POST `/internal/activities/ingestion/run` — dynamically mapped, one task per URN
""",
) as dag:
    list_datasets = HttpOperator(
        task_id="list_datasets",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ingestion/list-periodic",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({"schedule_tier": _TIER}),
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    @task
    def prepare_payloads(dataset_urns: list[str]) -> list[str]:
        """Convert a list of dataset URNs into JSON-encoded request bodies."""
        return [json.dumps({"dataset_urn": urn, "dry_run": False}) for urn in dataset_urns]

    payloads = prepare_payloads(list_datasets.output)

    run_ingestion = HttpOperator.partial(
        task_id="run_ingestion",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ingestion/run",
        method="POST",
        headers=internal_headers(),
        log_response=True,
    ).expand(data=payloads)
