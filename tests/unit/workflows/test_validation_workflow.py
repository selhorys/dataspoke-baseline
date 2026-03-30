"""Unit tests for validation workflow params and flow ID."""

from src.workflows.validation import FLOW_ID, ValidationParams


def test_flow_id():
    assert FLOW_ID == "validation"


def test_params_defaults():
    params = ValidationParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
    assert params.partition == {}


def test_params_with_partition():
    params = ValidationParams(
        dataset_urn="urn:li:dataset:test",
        partition={"load_date": "2025-03-10"},
    )
    assert params.partition == {"load_date": "2025-03-10"}
