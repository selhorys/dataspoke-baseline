"""Airflow test utilities for integration tests.

Provides:
- ``ALL_DAG_IDS`` — the DAG IDs DataSpoke registers in Airflow.
- ``.env.dev`` loading so AirflowClient factories pick up cluster connection vars.
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load helm-charts/.env.dev into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env.dev"
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
