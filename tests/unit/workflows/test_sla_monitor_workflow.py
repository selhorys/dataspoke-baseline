"""Unit tests for SLA monitor workflow params and flow ID."""

from src.workflows.sla_monitor import FLOW_ID, SLAMonitorParams


def test_flow_id():
    assert FLOW_ID == "sla-monitor"


def test_params_defaults():
    params = SLAMonitorParams(dataset_urn="urn:test", sla_target={"freshness_hours": 24})
    assert params.dataset_urn == "urn:test"
    assert params.sla_target == {"freshness_hours": 24}
    assert params.alert_recipients == []


def test_params_with_recipients():
    params = SLAMonitorParams(
        dataset_urn="urn:test",
        sla_target={"freshness_hours": 24},
        alert_recipients=["ops@example.com"],
    )
    assert params.alert_recipients == ["ops@example.com"]
