"""Ontogen workflow — Pydantic parameter models.

Orchestration is handled by the Airflow DAG definitions in:
  - dags/ontogen.py (on-demand)
  - dags/ontogen_hourly.py / ontogen_daily.py / ontogen_weekly.py (tier)

Business logic lives in src/backend/ontogen/service.py.
Activity endpoint: POST /internal/activities/ontogen/run

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""

from __future__ import annotations

from pydantic import BaseModel


class OntogenRunParams(BaseModel):
    """Parameters for a single ontogen pipeline run.

    - tier: optional schedule tier (hourly/daily/weekly). When provided by a
      tier DAG, the OntogenService.run() implementation checks the singleton
      conf's schedule_tier and short-circuits if they don't match.
    - dry_run: when True, compute and return proposals without persisting.
    - prompt_md: optional Markdown override for the system prompt; when omitted,
      the service falls back to ontogen_config.default_run_prompt.
    """

    tier: str | None = None
    dry_run: bool = False
    prompt_md: str | None = None
