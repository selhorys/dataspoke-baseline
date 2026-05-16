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
    """Parameters for a singleton metagen pipeline run.

    - dataset_urns: optional list of DataHub dataset URNs to scope the run.
      When omitted, MetagenService.run() enumerates all in-scope datasets
      from the global conf's dataset_filter intersected with enabled boundaries.
    - dry_run: when True, compute and return proposals without persisting.
    """

    dataset_urns: list[str] | None = None
    dry_run: bool = False
