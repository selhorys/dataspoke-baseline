"""Unit tests for embedding sync workflow params."""

from src.workflows.embedding_sync import EmbeddingSyncParams


def test_params_defaults():
    params = EmbeddingSyncParams()
    assert params.mode == "full"
    assert params.dataset_urn is None


def test_params_single():
    params = EmbeddingSyncParams(mode="single", dataset_urn="urn:li:dataset:test")
    assert params.mode == "single"
    assert params.dataset_urn == "urn:li:dataset:test"
