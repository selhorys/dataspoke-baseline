"""Airflow test utilities for integration tests.

Provides:
- ``ALL_DAG_IDS`` — the DAG IDs DataSpoke registers in Airflow.
- ``.env.dev`` loading so AirflowClient factories pick up cluster connection vars.
"""

from pathlib import Path

from tests.integration.util.env_file import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3])


# DAG IDs that DataSpoke registers in Airflow
ALL_DAG_IDS = frozenset(
    [
        "ingestion",
        "generation",
        "embedding-sync",
        "metrics",
        "ontology-rebuild",
    ]
)
