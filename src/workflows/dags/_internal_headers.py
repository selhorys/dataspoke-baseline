"""Common HTTP headers for Airflow DAG tasks calling /internal/* endpoints.

The ``X-Internal-Token`` header value is read from the
``DATASPOKE_INTERNAL_TOKEN`` env var **at DAG parse time**. The Airflow
scheduler re-parses DAG files on ``dag_dir_list_interval`` (default 60s
in dev, see ``helm-charts/dataspoke/values-dev.yaml``), so rotating the
secret propagates on the next parse cycle — it is NOT picked up mid-run
by already-queued task instances.

Ensure the Airflow scheduler / worker / triggerer pods have the same
``DATASPOKE_INTERNAL_TOKEN`` as the API; otherwise the API returns 401
UNAUTHORIZED.
"""
from __future__ import annotations

import os


def internal_headers() -> dict[str, str]:
    """Headers required by DataSpoke /internal/* endpoints."""
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": os.environ.get("DATASPOKE_INTERNAL_TOKEN", ""),
    }
