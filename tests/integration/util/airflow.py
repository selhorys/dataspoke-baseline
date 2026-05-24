"""Airflow test utilities for integration tests.

Provides helpers to ensure a clean Airflow state before and after test runs:
- Kill stale running DAG runs
- Delete test DAG runs
- AirflowClient factory from env vars
"""

import asyncio
import logging
import os
from pathlib import Path

from src.workflows.airflow.client import AirflowClient

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load helm-charts/.env into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# DAG IDs that DataSpoke registers in Airflow
ALL_DAG_IDS = frozenset([
    "ingestion",
    "generation",
    "embedding-sync",
    "metrics",
    "ontology-rebuild",
])


async def kill_running_dag_runs(
    client: AirflowClient,
    dag_id: str | None = None,
    *,
    wait_seconds: float = 15.0,
    poll_interval: float = 1.0,
) -> int:
    """Kill all running DAG runs for a DAG (or all known DAGs) and wait for termination.

    Returns the number of DAG runs killed.
    """
    dag_ids = [dag_id] if dag_id else list(ALL_DAG_IDS)
    killed = 0

    for did in dag_ids:
        running = await client.find_running_dag_runs(did)
        for dag_run in running:
            try:
                await client.kill_dag_run(did, dag_run.dag_run_id)
                killed += 1
                logger.info("Killed DAG run %s (dag_id=%s)", dag_run.dag_run_id, did)
            except Exception:
                logger.warning(
                    "Failed to kill DAG run %s (dag_id=%s)",
                    dag_run.dag_run_id,
                    did,
                    exc_info=True,
                )

    if killed > 0:
        # Wait for killed runs to reach terminal state
        elapsed = 0.0
        while elapsed < wait_seconds:
            still_running = 0
            for did in dag_ids:
                still_running += len(await client.find_running_dag_runs(did))
            if still_running == 0:
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    return killed


