"""Unit tests for ingestion workflow params and flow ID."""

from src.workflows.ingestion import FLOW_ID, IngestionParams


def test_flow_id():
    assert FLOW_ID == "ingestion"


def test_params_defaults():
    params = IngestionParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
    assert params.dry_run is False


def test_params_dry_run():
    params = IngestionParams(dataset_urn="urn:li:dataset:test", dry_run=True)
    assert params.dry_run is True
