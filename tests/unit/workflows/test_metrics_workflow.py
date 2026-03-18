"""Unit tests for metrics workflow params and flow ID."""

from src.workflows.metrics import FLOW_ID, MetricsParams


def test_flow_id():
    assert FLOW_ID == "metrics"


def test_params_defaults():
    params = MetricsParams(metric_id="metric-1")
    assert params.metric_id == "metric-1"
    assert params.aggregate is False
    assert params.dry_run is False


def test_params_with_aggregate():
    params = MetricsParams(metric_id="metric-1", aggregate=True, dry_run=True)
    assert params.aggregate is True
    assert params.dry_run is True
