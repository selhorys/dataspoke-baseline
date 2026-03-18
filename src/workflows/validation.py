"""Validation workflow — parameters and flow ID constant.

Orchestration is handled by the Kestra flow definition in flows/validation.yaml.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "validation"


@dataclass
class ValidationParams:
    dataset_urn: str
    config_id: str | None = None
    dry_run: bool = False
