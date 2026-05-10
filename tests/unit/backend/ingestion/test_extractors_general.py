"""Unit tests for ingestion extractor dispatcher (platform-agnostic)."""

from unittest.mock import AsyncMock

from src.backend.ingestion.extractors import SUPPORTED_PLATFORMS, run_datahub_ingestion


def test_supported_platforms_contains_expected():
    assert {"postgres", "mysql", "oracle", "bigquery", "snowflake", "kafka"}.issubset(
        SUPPORTED_PLATFORMS
    )


async def test_unsupported_source_returns_error():
    datahub = AsyncMock()
    result = await run_datahub_ingestion(
        datahub=datahub,
        platform="unknown_source",
        locator={},
        identifier={},
        auth=None,
        dataset_urn="urn:li:dataset:x",
        run_id="test-run-id",
        dry_run=False,
    )
    assert result.entities_ingested == 0
    assert len(result.errors) == 1
    assert "Unsupported platform" in result.errors[0]


async def test_not_yet_implemented_source_returns_warning():
    datahub = AsyncMock()
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    result = await run_datahub_ingestion(
        datahub=datahub,
        platform="mysql",
        locator={"host": "x", "port": 3306},
        identifier={"database": "db"},
        auth=_auth,
        dataset_urn="urn:li:dataset:x",
        run_id="test-run-id",
    )
    assert result.entities_ingested == 0
    assert result.errors == []
    assert any("not yet implemented" in w for w in result.warnings)
