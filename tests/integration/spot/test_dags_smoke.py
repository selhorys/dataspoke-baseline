"""Spot smoke tests for Airflow DAG availability.

Concerns covered:
- All expected DataSpoke DAGs are listed by Airflow REST API
- No expected DAG has an import (parse) error

Both tests use a function-scoped AirflowClient created locally to avoid
session-scoped event-loop binding errors. End-to-end DAG-trigger-and-wait
runs belong in the api-wired UC tests, not in spot.
"""

import os

import pytest

from src.workflows.airflow.client import AirflowClient
from src.workflows.registry import ALL_DAG_IDS


def _airflow_client() -> AirflowClient:
    return AirflowClient(
        base_url=os.environ["DATASPOKE_DEV_AIRFLOW_URL"],
        username=os.environ.get("DATASPOKE_DEV_AIRFLOW_USER", "admin"),
        password=os.environ.get("DATASPOKE_DEV_AIRFLOW_PASSWORD", "admin"),
    )


@pytest.mark.asyncio
async def test_all_expected_dags_registered() -> None:
    """All expected DataSpoke DAGs must be listed by Airflow."""
    client = _airflow_client()
    try:
        dags = await client.list_dags()
    finally:
        await client.close()

    loaded_ids = {d.get("dag_id") for d in dags}
    missing = sorted(ALL_DAG_IDS - loaded_ids)
    assert missing == [], f"Missing DAGs from Airflow: {missing}"


@pytest.mark.asyncio
async def test_no_dag_import_errors() -> None:
    """No expected DAG has an import error.

    Airflow exposes import errors via /api/v2/importErrors. Iterating the listed
    DAGs and checking `has_import_errors` (or the import-errors endpoint) catches
    silently broken DAG files that would otherwise pass list_dags() but fail at
    parse time.
    """
    client = _airflow_client()
    try:
        dags = await client.list_dags()
    finally:
        await client.close()

    broken = [
        d.get("dag_id")
        for d in dags
        if d.get("dag_id") in ALL_DAG_IDS and d.get("has_import_errors") is True
    ]
    assert broken == [], f"DAGs with import errors: {broken}"
