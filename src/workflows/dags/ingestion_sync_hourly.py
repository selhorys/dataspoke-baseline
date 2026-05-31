"""Airflow DAG: ingestion-sync-hourly

Hourly sync sweep. Reconciles all ingestion sources (DATAHUB_MANAGED, PASSIVE,
ACTIVE_CUSTOM_MANAGED) against DataHub: pulls source definitions, rebuilds
dataset mappings, mirrors run events into the events table.

Single task — the service iterates all sources internally.

Spec: spec/feature/BACKEND.md §Sync + mapping sweep, §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "ingestion-sync-hourly"

with DAG(
    dag_id=_DAG_ID,
    description="Hourly sync of all ingestion sources against DataHub",
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
    tags=["ingestion", "sync", "hourly"],
) as dag:
    ingestion_sync = HttpOperator(
        task_id="ingestion_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ingestion/sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
