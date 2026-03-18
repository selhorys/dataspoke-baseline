"""SLA monitor workflow — parameters and flow ID constant.

Orchestration is handled by the Kestra flow definition in flows/sla_monitor.yaml.
The Kestra flow uses a Schedule trigger (cron) for periodic execution.
Business logic is in the internal activity endpoints.
"""

from dataclasses import dataclass, field

FLOW_ID = "sla-monitor"


@dataclass
class SLAMonitorParams:
    dataset_urn: str
    sla_target: dict
    alert_recipients: list[str] = field(default_factory=list)
