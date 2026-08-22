"""Airflow DAG: metrics-weekly

Weekly metrics tier. Lists all enabled metric definitions for the weekly tier,
then fans out one run_metric task per metric using dynamic task mapping.

Spec: spec/feature/BACKEND.md §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.decorators import task
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "metrics-weekly"
_TIER = "weekly"

with DAG(
    dag_id=_DAG_ID,
    description=f"Metric measurement runs for {_TIER} schedule tier",
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
    tags=["metrics", _TIER],
) as dag:
    list_active = HttpOperator(
        task_id="list_active",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/list-active",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({"tier": _TIER}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )

    @task(task_id="extract_targets")  # type: ignore[untyped-decorator]
    def extract_targets(metric_ids: list[str], scheduled_at: str) -> list[str]:
        """Convert a list of metric IDs into JSON-encoded run request bodies."""
        return [
            json.dumps({"metric_id": metric_id, "scheduled_at": scheduled_at})
            for metric_id in metric_ids
        ]

    targets = extract_targets(
        list_active.output, "{{ (dag_run.data_interval_end or dag_run.run_after).isoformat() }}"
    )

    HttpOperator.partial(
        task_id="run_metric",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metrics/run",
        method="POST",
        headers=internal_headers(),
        log_response=True,
    ).expand(data=targets)
