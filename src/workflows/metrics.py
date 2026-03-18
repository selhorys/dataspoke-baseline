"""Metrics collection workflow — parameters and flow ID constant.

Orchestration is handled by the Kestra flow definition in flows/metrics.yaml.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass

FLOW_ID = "metrics"


@dataclass
class MetricsParams:
    metric_id: str
    aggregate: bool = False
    dry_run: bool = False
