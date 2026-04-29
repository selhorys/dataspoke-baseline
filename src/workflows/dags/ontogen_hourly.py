"""Airflow DAG: ontogen-hourly

Hourly ontogen tier. Calls the ontogen run activity with tier="hourly".
OntogenService.run() checks ontogen_config.schedule_tier; if the singleton
conf is not set to "hourly", the run short-circuits and returns immediately
without performing inference.

Single task — ontogen is always a singleton run (no fan-out).

Spec: spec/feature/BACKEND.md §DAG Catalogue, tier-DAG selection note
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "ontogen-hourly"
_TIER = "hourly"

with DAG(
    dag_id=_DAG_ID,
    description=(
        f"Ontogen {_TIER} tier run "
        "(short-circuits if singleton conf does not match this tier)"
    ),
    schedule="@hourly",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["ontogen", _TIER],
) as dag:
    run_ontogen = HttpOperator(
        task_id="run_ontogen",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontogen/run",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({"tier": _TIER, "dry_run": False}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
