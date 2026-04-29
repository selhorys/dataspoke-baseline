"""Airflow DAG: ingestion-passive-hourly

Hourly passive ingestion sync. Mirrors DataHub run history for all
passive-mode ingestion configs into the DataSpoke events table.
Single task, no fan-out — the service iterates all passive configs internally.

Spec: spec/feature/BACKEND.md §Ingestion Workflow, §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "ingestion-passive-hourly"

with DAG(
    dag_id=_DAG_ID,
    description="Hourly mirror of DataHub passive ingestion run history into events table",
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
    tags=["ingestion", "passive", "hourly"],
) as dag:
    passive_sync = HttpOperator(
        task_id="passive_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ingestion/passive-sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
