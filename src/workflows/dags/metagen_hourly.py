"""Airflow DAG: metagen-hourly

Hourly metagen tier. Calls the metagen run activity with tier="hourly".
The activity fans out across all enabled metagen confs whose schedule_tier
equals "hourly", running each under its own per-conf lock. Confs on a
different tier are skipped server-side.

Single Airflow task — fan-out is handled entirely within the activity endpoint.

Spec: spec/feature/BACKEND.md §DAG Catalogue, tier-DAG selection note
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "metagen-hourly"
_TIER = "hourly"

with DAG(
    dag_id=_DAG_ID,
    description=(
        f"Metagen {_TIER} tier run — triggers all enabled confs "
        f"with schedule_tier='{_TIER}'"
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
    tags=["metagen", _TIER],
) as dag:
    run_metagen = HttpOperator(
        task_id="run_metagen",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/metagen/run",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({"tier": _TIER, "dry_run": False}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
