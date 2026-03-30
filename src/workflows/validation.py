"""Validation workflow — parameters and flow ID constant.

Orchestration is handled by the Kestra flow definition in flows/validation.yaml.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass, field
from typing import Any

FLOW_ID = "validation"


@dataclass
class ValidationParams:
    dataset_urn: str
    partition: dict[str, Any] = field(default_factory=dict)
