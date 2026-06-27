"""Unit tests for src/backend/ingestion/extractors.py — registry, dispatch, and dry-run.

Covers run_extractor dispatch behavior:
- Unregistered source.type → IngestionResult with errors (not a raise)
- Registered 'postgres' type is present in the registry
- run_extractor passes through the result from the extractor function

Covers _extract_postgres discovered/emitted contract:
- dry_run=True → discovered_urns populated (the "would emit" plan), emitted_urns=[],
  no emit_aspect calls
- dry_run=False → emitted_urns non-empty, emit_aspect called per discovered table,
  emitted_urns ⊆ discovered_urns
- Early-return paths (connection failure / no rows / no tables matched / unregistered
  type) → discovered_urns == [] and emitted_urns == []

Spec: spec/API.md §POST /spoke/ingestion/sources/{id}/method/run — detail carries
  discovered_urns / emitted_urns (emitted ⊆ discovered; discovered present on dry-run + real)
Spec: spec/feature/BACKEND.md §Custom Extractor Authoring Contract
Spec: spec/feature/BACKEND.md §Active-custom run pipeline
Spec: spec/USE_CASE_en.md §UC1 Case 2
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ingestion.extractors import (
    IngestionResult,
    _make_dataset_urn,
    run_extractor,
)

# ── _parse_env_from_config: recipe env vs configured default_env ──────────────


class TestParseEnvFromConfig:
    """A recipe omitting ``source.config.env`` adopts the configured default_env.

    The DataHub peripheral's ``default_env`` is the fabric/env applied to ingested
    datasets when an ingestion recipe does not pin ``env``.  When the recipe DOES
    pin ``env``, the recipe value wins.

    spec: spec/feature/BACKEND.md §Ingestion Service — recipe omits ``env`` → extractor
        falls back to the peripheral's configured ``default_env``.
    spec: spec/API.md §Admin (/admin/peripherals/datahub) — ``default_env`` applied when an
        ingestion recipe omits ``env``.
    """

    def test_recipe_omitting_env_adopts_configured_default_env(self) -> None:
        from src.backend.ingestion.extractors import _parse_env_from_config

        # No "env" key in config → the configured default_env is used.
        result = _parse_env_from_config({"database": "imazon"}, default_env="PROD")
        assert result == "PROD", (
            "A recipe without 'env' must adopt the configured default_env. "
            "spec: spec/feature/BACKEND.md §Ingestion Service."
        )

    def test_recipe_env_overrides_configured_default_env(self) -> None:
        from src.backend.ingestion.extractors import _parse_env_from_config

        # Explicit recipe env wins over the configured default.
        result = _parse_env_from_config({"env": "QA"}, default_env="PROD")
        assert result == "QA", (
            "An explicit recipe 'env' must override the configured default_env. "
            "spec: spec/feature/BACKEND.md §Ingestion Service."
        )

    def test_default_env_fallback_is_dev_when_caller_omits_it(self) -> None:
        from src.backend.ingestion.extractors import _parse_env_from_config

        # When neither the recipe nor the caller supplies a fabric, 'DEV' is the
        # documented baseline default.
        # spec: spec/API.md §Admin (/admin/peripherals/datahub) — factory default_env → DEV.
        assert _parse_env_from_config({}) == "DEV"


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
        # Unregistered type discovers nothing and emits nothing.
        # Spec: API.md — discovered_urns is the "would emit" plan; an unrunnable
        # extractor never reaches discovery, so both lists are empty.
        assert result.discovered_urns == []
        assert result.emitted_urns == []
        assert len(result.errors) > 0
        assert "oracle" in result.errors[0].lower() or "no" in result.errors[0].lower()

    @pytest.mark.asyncio
    async def test_registered_type_calls_extractor_function(self) -> None:
        """run_extractor calls the registered extractor function for a known type.

        Spec: BACKEND.md §Custom Extractor Authoring Contract — 'Signature: async
        (datahub, source_id, recipe, dry_run, run_id) -> IngestionResult'.
        """
        expected = IngestionResult(
            discovered_urns=["urn:1", "urn:2", "urn:3"],
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
            assert result.emitted_urns == ["urn:1", "urn:2", "urn:3"]
            assert result.discovered_urns == ["urn:1", "urn:2", "urn:3"]
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

# Discovered URNs are built from the recipe's database/env with the SAME helper the
# extractor uses for emission, so discovered and emitted URN strings are identical.
# Spec: API.md §method/run — discovered_urns are dataset URNs passing the filter,
# and emitted_urns ⊆ discovered_urns.
_DB = _POSTGRES_RECIPE["source"]["config"]["database"]
_ENV = _POSTGRES_RECIPE["source"]["config"]["env"]
_EXPECTED_DISCOVERED = {
    _make_dataset_urn("postgres", f"{_DB}.public.orders", _ENV),
    _make_dataset_urn("postgres", f"{_DB}.public.users", _ENV),
}


class TestPostgresExtractorDryRunContract:
    """Spec: spec/USE_CASE_en.md §UC1 Case 2 — dry run emits nothing but DOES discover.

    Spec: spec/API.md §method/run — 'discovered_urns (dataset URNs passing the filter
    — the "would emit" plan, present on both dry-run and real runs); emitted_urns
    (dataset URNs actually written to DataHub; empty with count 0 on a dry-run)'.
    Spec: BACKEND.md §Active-custom run pipeline — 'aspect emission is skipped on dry_run'.

    The postgres extractor must return emitted_urns=[] on dry_run=True while populating
    discovered_urns with the filtered table URNs (no connection error, status not 'error').
    """

    @pytest.mark.asyncio
    async def test_dry_run_discovers_tables_but_emits_no_urns(self) -> None:
        """dry_run=True → discovered_urns has the filtered table URNs, emitted_urns is [].

        UC1 Case 2: POST /sources/{id}/method/run with dry_run=True returns
        detail.discovered_urns_count >= the table count and detail.emitted_urns_count == 0.
        Spec: API.md §method/run — discovered present on dry-run; emitted empty on dry-run.
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

        # Dry-run discovers the two distinct tables (public.orders, public.users).
        assert set(result.discovered_urns) == _EXPECTED_DISCOVERED, (
            f"dry_run=True must discover the filtered table URNs {_EXPECTED_DISCOVERED!r}; "
            f"got {result.discovered_urns!r}. "
            "spec: API.md §method/run — discovered_urns present on dry-run."
        )
        assert result.emitted_urns == [], (
            f"dry_run=True must produce emitted_urns=[] but got {result.emitted_urns!r}. "
            "spec: API.md §method/run — dry-run emits nothing."
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
    async def test_real_run_emits_subset_of_discovered(self) -> None:
        """dry_run=False → emitted_urns non-empty and emitted_urns ⊆ discovered_urns.

        This guards against regressions that would break the real-run mapping path.
        Spec: API.md §method/run — emitted_urns ⊆ discovered_urns; both populated on a
        real run that yields datasets.
        Spec: BACKEND.md §Active-custom run pipeline — emitted URNs are upserted into
        ingestion_source_dataset with derivation=emitted on a non-dry run.
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
        assert set(result.emitted_urns) == _EXPECTED_DISCOVERED, (
            f"Expected 2 emitted URNs (orders + users) but got {result.emitted_urns!r}"
        )
        # Discovered set equals the emitted set here (every discovered table emitted ok).
        assert set(result.discovered_urns) == _EXPECTED_DISCOVERED
        # Core invariant: emitted ⊆ discovered. spec: API.md §method/run.
        assert set(result.emitted_urns).issubset(set(result.discovered_urns)), (
            f"emitted_urns must be a subset of discovered_urns; "
            f"emitted={result.emitted_urns!r} discovered={result.discovered_urns!r}. "
            "spec: API.md §method/run."
        )
        # Discovered and emitted URNs use the SAME _make_dataset_urn format.
        assert all(
            u in result.discovered_urns for u in result.emitted_urns
        ), "emitted URN strings must match discovered URN strings exactly"
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

        # Connection failure is an early return: nothing discovered, nothing emitted.
        # spec: API.md §method/run — discovered_urns is the post-filter plan; an
        # unreachable source never reaches discovery.
        assert result.discovered_urns == []
        assert result.emitted_urns == []
        assert any("connection" in e.lower() or "postgresql" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty_discovered(self) -> None:
        """A reachable DB with no columns → discovered_urns == [] and emitted_urns == [].

        Spec: API.md §method/run — discovered_urns is the set of dataset URNs passing the
        filter; with no tables there is nothing to discover.
        """
        mock_datahub = MagicMock()
        mock_datahub.emit_aspect = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])  # empty information_schema
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", return_value=mock_conn):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-empty",
                recipe=_POSTGRES_RECIPE,
                dry_run=False,
                run_id="run-empty",
            )

        assert result.discovered_urns == []
        assert result.emitted_urns == []
        mock_datahub.emit_aspect.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tables_matched_filter_returns_empty_discovered(self) -> None:
        """schema_pattern that excludes every table → discovered_urns == [].

        The rows exist (public.orders, public.users) but the schema_pattern filter
        rejects them all, so nothing is discovered and nothing is emitted.
        Spec: API.md §method/run — discovered_urns are only the URNs passing the
        schema_pattern filter.
        """
        mock_datahub = MagicMock()
        mock_datahub.emit_aspect = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=_FAKE_ROWS)
        mock_conn.close = AsyncMock()

        # schema_pattern allows only a schema that does not exist in _FAKE_ROWS.
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    **_POSTGRES_RECIPE["source"]["config"],
                    "schema_pattern": {"allow": ["^catalog$"]},
                },
            }
        }

        with patch("asyncpg.connect", return_value=mock_conn):
            result = await run_extractor(
                datahub=mock_datahub,
                source_id="src-nomatch",
                recipe=recipe,
                dry_run=False,
                run_id="run-nomatch",
            )

        assert result.discovered_urns == [], (
            f"No table passes the ^catalog$ filter, so discovered_urns must be empty; "
            f"got {result.discovered_urns!r}. spec: API.md §method/run."
        )
        assert result.emitted_urns == []
        mock_datahub.emit_aspect.assert_not_called()
