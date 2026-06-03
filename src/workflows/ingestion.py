"""Ingestion workflow — parameter models and schedule tier helpers.

Manual runs call IngestionService.run() directly via the API route.
Periodic active ingestion is handled by static Airflow DAGs keyed by schedule
tier (hourly, daily, weekly). The /internal/activities/ingestion/list-active
endpoint calls IngestionService.list_active_sources_for_tier(tier) directly.
The full DataHub→DataSpoke reconciliation sweep is handled by the
datahub-sync-hourly DAG, which calls POST /internal/activities/ingestion/sync
once per hour.

Spec: spec/feature/BACKEND.md §Ingestion Workflow, §DAG Catalogue
"""

from __future__ import annotations

from pydantic import BaseModel


class IngestionSyncParams(BaseModel):
    """Parameters for the ingestion sync activity (no inputs required)."""

    pass
