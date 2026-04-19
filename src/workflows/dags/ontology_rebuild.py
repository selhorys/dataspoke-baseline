"""Airflow DAG: ontology-rebuild

Multi-step ontology rebuild: classify, build hierarchy, infer relationships, detect drift.
Triggered via API or events (no schedule).
"""
from __future__ import annotations

from datetime import timedelta

from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator

from _internal_headers import internal_headers

_DEFAULT_ARGS = {
    "retries": 3,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id="ontology-rebuild",
    description="Multi-step ontology rebuild: classify, hierarchy, relationships, drift",
    schedule=None,
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    default_args=_DEFAULT_ARGS,
    tags=["ontology", "on-demand"],
    doc_md="""
## ontology-rebuild

Runs a full ontology rebuild pipeline. Each step receives the output of the previous
step via XCom.

**Inputs** (via `dag_run.conf`):
- `callback_base_url`: DataSpoke API base URL (default: `"http://dataspoke-api:8002"`)
- `force`: boolean flag to force reclassification (default: `false`)

**Tasks** (sequential):
1. `classify_datasets` — POST `/internal/activities/ontology/classify`
2. `build_hierarchy` — POST `/internal/activities/ontology/build-hierarchy` (uses output of step 1)
3. `infer_relationships` — POST `/internal/activities/ontology/infer-relationships` (uses output of step 2)
4. `detect_drift` — POST `/internal/activities/ontology/detect-drift` (uses output of step 2)
""",
) as dag:
    classify_datasets = HttpOperator(
        task_id="classify_datasets",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontology/classify",
        method="POST",
        headers=internal_headers(),
        data='{"force": {{ dag_run.conf.get(\'force\', false) | lower }}}',
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    build_hierarchy = HttpOperator(
        task_id="build_hierarchy",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontology/build-hierarchy",
        method="POST",
        headers=internal_headers(),
        data='{"classifications": {{ ti.xcom_pull(task_ids="classify_datasets") | tojson }}}',
        response_filter=lambda response: response.json(),
        log_response=True,
    )

    infer_relationships = HttpOperator(
        task_id="infer_relationships",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontology/infer-relationships",
        method="POST",
        headers=internal_headers(),
        data='{"hierarchy": {{ ti.xcom_pull(task_ids="build_hierarchy") | tojson }}}',
        log_response=True,
    )

    detect_drift = HttpOperator(
        task_id="detect_drift",
        http_conn_id="dataspoke_api",
        endpoint="/internal/activities/ontology/detect-drift",
        method="POST",
        headers=internal_headers(),
        data='{"current_hierarchy": {{ ti.xcom_pull(task_ids="build_hierarchy") | tojson }}}',
        log_response=True,
    )

    # infer_relationships and detect_drift both depend on build_hierarchy but
    # are independent of each other — run them in parallel after build_hierarchy.
    classify_datasets >> build_hierarchy >> [infer_relationships, detect_drift]
