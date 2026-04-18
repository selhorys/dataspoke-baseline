"""Unit tests for generation workflow params."""

from src.workflows.generation import GenerationParams


def test_params():
    params = GenerationParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
