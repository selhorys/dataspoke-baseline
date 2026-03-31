"""Validation workflow — parameters for the periodic validation flow contract.

Ad-hoc validation runs execute directly (no Kestra detour).
Periodic/scheduled validation runs are handled by dynamically generated
``validation-periodic-*`` flows (via ``validation-config-sync``).

This module provides the typing contract for periodic flow callbacks.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationParams:
    dataset_urn: str
    partition: dict[str, Any] = field(default_factory=dict)
