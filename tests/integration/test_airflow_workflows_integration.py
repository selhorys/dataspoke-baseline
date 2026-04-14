"""Integration tests for AirflowClient against dev-env Airflow instance.

Verifies that src/workflows/airflow/client.py talks to Airflow's Stable REST
API correctly: DAG listing, DAG run lifecycle, status querying, and cleanup.

Uses the Airflow REST API directly — no DataSpoke activity callbacks, no
production DAGs required.

Prerequisites:
- Airflow accessible via DATASPOKE_AIRFLOW_URL (from dev_env/.env)

Run: uv run pytest tests/integration/test_airflow_workflows_integration.py -v
"""

import pytest
import pytest_asyncio

from src.workflows.airflow.client import AirflowClient
from src.workflows.airflow.errors import AirflowTimeoutError
from src.workflows.airflow.models import DagRunState
from tests.integration.util.airflow import kill_running_dag_runs

pytestmark = pytest.mark.asyncio(loop_scope="module")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def client(airflow_client: AirflowClient):
    """Re-use the session-scoped airflow_client fixture from conftest."""
    return airflow_client


# ── DAG Listing ──────────────────────────────────────────────────────────────


async def test_list_dags_returns_list(client: AirflowClient):
    """list_dags should return a non-error list (may be empty in bare env)."""
    dags = await client.list_dags()
    assert isinstance(dags, list)


async def test_list_dags_items_have_dag_id(client: AirflowClient):
    """Each item in the DAG list should have a dag_id field."""
    dags = await client.list_dags()
    for d in dags:
        assert "dag_id" in d


async def test_list_dags_with_prefix(client: AirflowClient):
    """list_dags with prefix should only return DAGs matching that prefix."""
    all_dags = await client.list_dags()
    if not all_dags:
        pytest.skip("No DAGs loaded — cannot test prefix filtering")

    first_dag_id = all_dags[0]["dag_id"]
    prefix = first_dag_id[:3]  # Take first 3 chars as prefix

    filtered = await client.list_dags(prefix=prefix)
    assert all(d["dag_id"].startswith(prefix) for d in filtered)


# ── find_running_dag_runs ─────────────────────────────────────────────────────


async def test_find_running_dag_runs_nonexistent_dag(client: AirflowClient):
    """find_running_dag_runs for a DAG that doesn't exist should return empty list."""
    running = await client.find_running_dag_runs("nonexistent-dag-that-does-not-exist")
    assert running == []


# ── kill_dag_run ──────────────────────────────────────────────────────────────


async def test_kill_dag_run_nonexistent_is_noop(client: AirflowClient):
    """kill_dag_run for a nonexistent run should not raise."""
    await client.kill_dag_run("nonexistent-dag", "nonexistent-run-id")


# ── delete_dag_run ────────────────────────────────────────────────────────────


async def test_delete_dag_run_nonexistent_is_noop(client: AirflowClient):
    """delete_dag_run for a nonexistent run should not raise."""
    await client.delete_dag_run("nonexistent-dag", "nonexistent-run-id")


# ── check_no_duplicate ────────────────────────────────────────────────────────


async def test_check_no_duplicate_no_running_dag(client: AirflowClient):
    """check_no_duplicate should not raise when no matching running DAG run exists."""
    await client.check_no_duplicate(
        "nonexistent-dag",
        "dataset_urn",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.catalog.title_master,DEV)",
        "INGESTION_RUNNING",
    )


# ── Cleanup Utilities ─────────────────────────────────────────────────────────


async def test_kill_running_dag_runs_utility(client: AirflowClient):
    """kill_running_dag_runs utility should not raise and return a non-negative count."""
    killed = await kill_running_dag_runs(client, "nonexistent-dag")
    assert isinstance(killed, int)
    assert killed >= 0
