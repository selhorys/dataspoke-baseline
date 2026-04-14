"""Airflow DAG: generation

LLM-powered metadata generation for a dataset. Triggered via API (no schedule).
"""
from __future__ import annotations

from datetime import timedelta

from airflow import DAG
from airflow.providers.http.operators.http import SimpleHttpOperator

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id="generation",
    description="LLM-powered metadata generation",
    schedule=None,
    catchup=False,
    max_active_runs=2,
    is_paused_upon_creation=False,
    default_args=_DEFAULT_ARGS,
    tags=["generation", "llm", "on-demand"],
    doc_md="""
## generation

Triggers LLM-powered metadata generation for a single dataset.

**Inputs** (via `dag_run.conf`):
- `callback_base_url`: DataSpoke API base URL (default: `"http://dataspoke-api:8002"`)
- `dataset_urn`: dataset URN string (required)

**Tasks**:
1. `run_generation` — POST `/internal/activities/generation/run`
""",
) as dag:
    run_generation = SimpleHttpOperator(
        task_id="run_generation",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/generation/run",
        method="POST",
        headers={"Content-Type": "application/json"},
        data='{"dataset_urn": "{{ dag_run.conf.get(\'dataset_urn\', \'\') }}"}',
        log_response=True,
    )
