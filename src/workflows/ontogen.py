"""Ontogen workflow — Pydantic parameter models.

Orchestration is handled by the Airflow DAG definitions in:
  - dags/ontogen_hourly.py / ontogen_daily.py / ontogen_weekly.py (tier)

Manual API runs go through `POST /spoke/common/ontogen/method/run`, which calls
OntogenService.run() synchronously in-process. Business logic lives in
src/backend/ontogen/service.py.
Activity endpoint: POST /internal/activities/ontogen/run

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Concurrency Guards
"""

from __future__ import annotations

from pydantic import BaseModel


class OntogenRunParams(BaseModel):
    """Parameters for a single ontogen pipeline run.

    - tier: optional schedule tier (hourly/daily/weekly). When supplied by a
      tier DAG, the /internal/activities/ontogen/run activity compares it
      against the singleton conf's schedule_tier and short-circuits on
      mismatch (no Redis lock, no inference).
    - dry_run: when True, compute and return proposals without persisting.
    - prompt_md: optional Markdown override for the system prompt; when omitted,
      the service falls back to ontogen_config.default_run_prompt.
    """

    tier: str | None = None
    dry_run: bool = False
    prompt_md: str | None = None
