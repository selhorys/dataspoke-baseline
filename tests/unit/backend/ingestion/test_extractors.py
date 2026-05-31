"""Unit tests for src/backend/ingestion/extractors.py — registry and dispatch.

Covers run_extractor dispatch behavior:
- Unregistered source.type → IngestionResult with errors (not a raise)
- Registered 'postgres' type is present in the registry
- run_extractor passes through the result from the extractor function

Spec: spec/feature/BACKEND.md §Custom Extractor Authoring Contract
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.ingestion.extractors import IngestionResult, run_extractor


# ── Registry ──────────────────────────────────────────────────────────────────


class TestExtractorRegistry:
    def test_postgres_is_registered(self) -> None:
        """'postgres' extractor is in the registry — it is the only baseline type.

        Spec: BACKEND.md §Custom Extractor Authoring Contract — 'This release ships a
        postgres extractor only.'
        """
        from src.backend.ingestion.extractors import _EXTRACTOR_REGISTRY

        assert "postgres" in _EXTRACTOR_REGISTRY

    def test_kafka_is_not_registered(self) -> None:
        """'kafka' is not registered in the baseline — fork-and-extend path only.

        Spec: BACKEND.md §Custom Extractor Authoring Contract — 'Kafka, mysql, oracle,
        bigquery, snowflake: Fork-and-extend'.
        """
        from src.backend.ingestion.extractors import _EXTRACTOR_REGISTRY

        assert "kafka" not in _EXTRACTOR_REGISTRY


# ── run_extractor dispatch ────────────────────────────────────────────────────


class TestRunExtractor:
    @pytest.mark.asyncio
    async def test_unregistered_type_returns_error_result_not_raise(self) -> None:
        """run_extractor for an unregistered source.type returns IngestionResult with errors.

        Spec: BACKEND.md §Active-custom run pipeline — extractor dispatch; unregistered
        type returns an error result so the run pipeline can record INGESTION.FAIL.
        """
        recipe = {"source": {"type": "oracle", "config": {}}}
        result = await run_extractor(
            datahub=MagicMock(),
            source_id="some-source-id",
            recipe=recipe,
            dry_run=False,
            run_id="test-run-id",
        )
        assert isinstance(result, IngestionResult)
        assert result.entities_ingested == 0
        assert len(result.errors) > 0
        assert "oracle" in result.errors[0].lower() or "no" in result.errors[0].lower()

    @pytest.mark.asyncio
    async def test_registered_type_calls_extractor_function(self) -> None:
        """run_extractor calls the registered extractor function for a known type.

        Spec: BACKEND.md §Custom Extractor Authoring Contract — 'Signature: async
        (datahub, source_id, recipe, dry_run, run_id) -> IngestionResult'.
        """
        expected = IngestionResult(
            entities_ingested=3,
            emitted_urns=["urn:1", "urn:2", "urn:3"],
            errors=[],
            warnings=[],
        )
        mock_extractor = AsyncMock(return_value=expected)

        from src.backend.ingestion import extractors as _ext

        original_registry = dict(_ext._EXTRACTOR_REGISTRY)
        _ext._EXTRACTOR_REGISTRY["testtype"] = mock_extractor

        try:
            recipe = {"source": {"type": "testtype", "config": {}}}
            result = await run_extractor(
                datahub=MagicMock(),
                source_id="src-123",
                recipe=recipe,
                dry_run=True,
                run_id="run-456",
            )
            mock_extractor.assert_awaited_once()
            assert result.entities_ingested == 3
        finally:
            _ext._EXTRACTOR_REGISTRY.clear()
            _ext._EXTRACTOR_REGISTRY.update(original_registry)

    @pytest.mark.asyncio
    async def test_empty_source_type_returns_error_result(self) -> None:
        """Empty source.type in recipe → error result (no extractor registered for '').

        Spec: BACKEND.md §Active-custom run pipeline.
        """
        recipe = {"source": {"type": "", "config": {}}}
        result = await run_extractor(
            datahub=MagicMock(),
            source_id="src",
            recipe=recipe,
            dry_run=False,
            run_id="run",
        )
        assert len(result.errors) > 0
