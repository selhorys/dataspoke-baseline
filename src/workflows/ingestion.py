"""Ingestion workflow — parameters and flow ID constant.

Orchestration is handled by the Kestra flow definition in flows/ingestion.yaml.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "ingestion"


@dataclass
class IngestionParams:
    dataset_urn: str
    dry_run: bool = False
