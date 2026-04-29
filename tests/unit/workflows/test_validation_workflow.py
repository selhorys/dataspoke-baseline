"""Unit tests for validation workflow params."""

from src.workflows.validation import ValidationRunParams


def test_params_defaults():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
    assert params.partition is None


def test_params_with_partition():
    params = ValidationRunParams(
        dataset_urn="urn:li:dataset:test",
        partition={"load_date": "2025-03-10"},
    )
    assert params.partition == {"load_date": "2025-03-10"}


def test_params_dry_run_default():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test")
    assert params.dry_run is False


def test_params_dry_run_true():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test", dry_run=True)
    assert params.dry_run is True
