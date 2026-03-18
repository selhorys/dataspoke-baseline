"""Unit tests for validation workflow params and flow ID."""

from src.workflows.validation import FLOW_ID, ValidationParams


def test_flow_id():
    assert FLOW_ID == "validation"


def test_params_defaults():
    params = ValidationParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
    assert params.config_id is None
    assert params.dry_run is False


def test_params_with_config():
    params = ValidationParams(dataset_urn="urn:li:dataset:test", config_id="cfg-1", dry_run=True)
    assert params.config_id == "cfg-1"
    assert params.dry_run is True
