"""Airflow DAG: metagen-weekly

Weekly metagen tier. Lists all datasets with active metagen configs for the
weekly tier, then fans out one run_metagen task per dataset.

Spec: spec/feature/BACKEND.md §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.decorators import task
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "metagen-weekly"
_TIER = "weekly"

with DAG(
    dag_id=_DAG_ID,
    description=f"Metadata generation for {_TIER} schedule tier",
    schedule="@weekly",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=True,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["metagen", _TIER],
) as dag:
    list_active = HttpOperator(
        task_id="list_active",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metagen/list-active",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({"tier": _TIER}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )

    @task(task_id="extract_targets")  # type: ignore[untyped-decorator]
    def extract_targets(dataset_urns: list[str]) -> list[str]:
        """Convert a list of dataset URNs into JSON-encoded run request bodies."""
        return [json.dumps({"dataset_urn": urn, "dry_run": False}) for urn in dataset_urns]

    targets = extract_targets(list_active.output)

    HttpOperator.partial(
        task_id="run_metagen",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metagen/run",
        method="POST",
        headers=internal_headers(),
        log_response=True,
    ).expand(data=targets)
