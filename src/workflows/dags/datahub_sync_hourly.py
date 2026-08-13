"""Airflow DAG: datahub-sync-hourly

Full DataHub→DataSpoke reconciliation sweep, every two hours. In a single pass it:
- reconciles dataset_registry existence from DataHub's entity enumeration,
- refreshes the dataset attributes every dataset_filter resolves against,
- rebuilds ingestion source→dataset mappings,
- mirrors managed source definitions,
- mirrors run events into the events table.

The cadence is the upper bound on dataset_filter scope staleness across UC3,
UC4 and UC5. The `-hourly` suffix in the dag_id, filename and tags is a
retained identifier, not a cadence claim.

Single task — the service iterates all sources internally.

Spec: spec/feature/BACKEND.md §Sync + mapping sweep, §DAG Catalogue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from _internal_headers import internal_headers
from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

_DAG_ID = "datahub-sync-hourly"

with DAG(
    dag_id=_DAG_ID,
    description=(
        "Two-hourly DataHub→DataSpoke reconciliation: dataset_registry existence, "
        "dataset filter attributes, ingestion source→dataset mapping, managed "
        "source defs, run events"
    ),
    schedule="0 */2 * * *",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=10),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["datahub", "ingestion", "sync", "hourly"],
) as dag:
    datahub_sync = HttpOperator(
        task_id="datahub_sync",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ingestion/sync",
        method="POST",
        headers=internal_headers(),
        data=json.dumps({}),
        response_filter=lambda r: r.json(),
        log_response=True,
    )
