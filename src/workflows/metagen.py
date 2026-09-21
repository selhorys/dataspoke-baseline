"""Metagen workflow — Pydantic parameter models.

Orchestration is handled by the Airflow DAG definitions in:
  - dags/metagen_hourly.py / metagen_daily.py / metagen_weekly.py (tier)

Manual API runs go through `POST /spoke/metagen/conf/{conf_id}/method/run`,
which calls MetagenService.run() synchronously in-process for that one conf.
Business logic lives in src/backend/metagen/service.py.
Activity endpoint: POST /internal/activities/metagen/run

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""

from __future__ import annotations

from pydantic import BaseModel


class MetagenRunParams(BaseModel):
    """Optional scope and dry-run flag for a metagen run.

    The tier DAGs supply the tier on the activity body
    (`src/api/routers/internal/activities.MetagenRunRequest.tier`); the activity
    then fans out across all enabled metagen confs whose schedule_tier matches.
    Each conf runs under its own per-conf lock; the dataset scope for each conf
    is determined server-side from that conf's dataset_filter intersected with
    enabled boundaries.

    - dataset_urns: optional list of DataHub dataset URNs to further scope the
      run. When omitted, the full per-conf dataset_filter applies.
    - dry_run: when True, compute and return proposals without persisting.
    """

    dataset_urns: list[str] | None = None
    dry_run: bool = False
