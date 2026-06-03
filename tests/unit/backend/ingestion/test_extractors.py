"""Unit tests for src/backend/ingestion/extractors.py — registry, dispatch, and dry-run.

Covers run_extractor dispatch behavior:
- Unregistered source.type → IngestionResult with errors (not a raise)
- Registered 'postgres' type is present in the registry
- run_extractor passes through the result from the extractor function

Covers _extract_postgres dry-run contract:
- dry_run=True → emitted_urns=[], entities_ingested=0, no emit_aspect calls
- dry_run=False → emitted_urns non-empty, emit_aspect called per discovered table

Spec: spec/feature/BACKEND.md §Custom Extractor Authoring Contract
Spec: spec/feature/BACKEND.md §Active-custom run pipeline
Spec: spec/USE_CASE_en.md §UC1 Case 2
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── PostgreSQL extractor dry-run contract ─────────────────────────────────────


def _make_asyncpg_row(schema: str, table: str, column: str, col_num: int) -> MagicMock:
    """Build a fake asyncpg.Record-like MagicMock for a single column."""
    row: MagicMock = MagicMock()
    row.__getitem__ = lambda self, key: {  # type: ignore[assignment]
        "table_schema": schema,
        "table_name": table,
        "column_name": column,
        "data_type": "text",
        "ordinal_position": col_num,
        "is_nullable": "YES",
        "column_comment": None,
        "table_comment": None,
    }[key]
    row.get = lambda key, default=None: {
        "table_schema": schema,
        "table_name": table,
        "column_name": column,
        "data_type": "text",
        "ordinal_position": col_num,
        "is_nullable": "YES",
        "column_comment": None,
        "table_comment": None,
    }.get(key, default)
    return row


_POSTGRES_RECIPE: dict[str, Any] = {
    "source": {
        "type": "postgres",
        "config": {
            "host_port": "localhost:5432",
            "database": "testdb",
            "username": "user",
            "password": "pw",
            "env": "DEV",
        },
    }
}

_FAKE_ROWS = [
    _make_asyncpg_row("public", "orders", "id", 1),
    _make_asyncpg_row("public", "orders", "amount", 2),
    _make_asyncpg_row("public", "users", "id", 1),
]


class TestPostgresExtractorDryRunContract:
    """Spec: spec/USE_CASE_en.md §UC1 Case 2 — dry run emits nothing.

    Spec: BACKEND.md §Active-custom run pipeline — 'aspect emission is skipped
    on dry_run'.

    The postgres extractor must return emitted_urns=[] on dry_run=True while
    still discovering schema (no connection error, status not 'error').
    """

    @pytest.mark.asyncio
    async def test_dry_run_emits_no_urns(self) -> None:
        """dry_run=True → emitted_urns is empty, entities_ingested is 0.

        UC1 Case 2: POST /sources/{id}/method/run with dry_run=True must return
        detail.emitted_urns_count == 0.
        """
        mock_datahub = MagicMock()
        mock_datahub.emit_aspect = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=_FAKE_ROWS)
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", return_value=mock_conn):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-dry",
                recipe=_POSTGRES_RECIPE,
                dry_run=True,
                run_id="run-dry",
            )

        assert result.emitted_urns == [], (
            f"dry_run=True must produce emitted_urns=[] but got {result.emitted_urns!r}"
        )
        assert result.entities_ingested == 0, (
            f"dry_run=True must produce entities_ingested=0 but got {result.entities_ingested}"
        )
        mock_datahub.emit_aspect.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_returns_no_errors_for_valid_schema(self) -> None:
        """dry_run=True on a reachable schema returns status-success semantics (no errors).

        The caller (service._run_inner) relies on result.errors being empty to
        classify the run as 'success'; a dry run that always returns errors would
        break that contract.
        """
        mock_datahub = MagicMock()
        mock_datahub.emit_aspect = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=_FAKE_ROWS)
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", return_value=mock_conn):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-dry-ok",
                recipe=_POSTGRES_RECIPE,
                dry_run=True,
                run_id="run-dry-ok",
            )

        assert result.errors == []

    @pytest.mark.asyncio
    async def test_real_run_emits_catalog_datasets_with_origin_emitted(self) -> None:
        """dry_run=False → emitted_urns is non-empty for a schema that yields datasets.

        This guards against regressions that would break the real-run mapping path.
        Spec: BACKEND.md §Active-custom run pipeline step 7 — 'upsert emitted URNs
        into ingestion_source_dataset (origin=emitted, non-dry-run)'.
        """
        mock_datahub = MagicMock()
        mock_datahub.emit_aspect = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=_FAKE_ROWS)
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", return_value=mock_conn):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-real",
                recipe=_POSTGRES_RECIPE,
                dry_run=False,
                run_id="run-real",
            )

        # _FAKE_ROWS covers two distinct tables: public.orders and public.users.
        assert len(result.emitted_urns) == 2, (
            f"Expected 2 emitted URNs (orders + users) but got {result.emitted_urns!r}"
        )
        assert result.entities_ingested == 2
        assert result.errors == []
        # emit_aspect must have been called (aspects per table + containers).
        mock_datahub.emit_aspect.assert_called()

    @pytest.mark.asyncio
    async def test_dry_run_connection_failure_returns_error(self) -> None:
        """dry_run=True with a connection failure returns IngestionResult with errors.

        Connection check is still exercised on dry run — the result surfaces errors
        rather than raising, consistent with the real-run error path.
        """
        mock_datahub = MagicMock()

        with patch("asyncpg.connect", side_effect=OSError("Connection refused")):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-fail",
                recipe=_POSTGRES_RECIPE,
                dry_run=True,
                run_id="run-fail",
            )

        assert result.emitted_urns == []
        assert result.entities_ingested == 0
        assert any("connection" in e.lower() or "postgresql" in e.lower() for e in result.errors)
