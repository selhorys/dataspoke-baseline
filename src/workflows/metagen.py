"""Metagen workflow — Pydantic parameter models.

Orchestration is handled by the Airflow DAG definitions in:
  - dags/metagen.py (on-demand)
  - dags/metagen_hourly.py / metagen_daily.py / metagen_weekly.py (tier)

Business logic lives in src/backend/metagen/service.py.
Activity endpoint: POST /internal/activities/metagen/run

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""

from __future__ import annotations

from pydantic import BaseModel


class MetagenRunParams(BaseModel):
    """Parameters for a single metagen pipeline run.

    - dataset_urn: fully-qualified DataHub URN for the target dataset (required).
    - dry_run: when True, compute and return proposals without persisting.
    """

    dataset_urn: str
    dry_run: bool = False
