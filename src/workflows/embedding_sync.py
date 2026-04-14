"""Embedding sync workflow — parameters and flow ID constant.

Orchestration is handled by the Airflow DAG definition in dags/embedding_sync.py.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "embedding-sync"


@dataclass
class EmbeddingSyncParams:
    mode: str = "full"  # "full" or "single"
    dataset_urn: str | None = None
