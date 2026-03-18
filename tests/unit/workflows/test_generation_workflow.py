"""Unit tests for generation workflow params and flow ID."""

from src.workflows.generation import FLOW_ID, GenerationParams


def test_flow_id():
    assert FLOW_ID == "generation"


def test_params():
    params = GenerationParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
