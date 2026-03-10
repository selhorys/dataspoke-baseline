"""Unit tests for SourceCodeAnalyzer."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.generation.analyzer import SourceCodeAnalyzer


@pytest.fixture
def llm():
    return AsyncMock()


@pytest.fixture
def analyzer(llm):
    return SourceCodeAnalyzer(llm=llm)


# ── diff_similar_tables ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diff_similar_tables_overlapping_fields(analyzer):
    current_schema = [
        {"fieldPath": "id", "nativeDataType": "int"},
        {"fieldPath": "name", "nativeDataType": "varchar"},
        {"fieldPath": "email", "nativeDataType": "varchar"},
    ]
    similar_schemas = [
        {
            "urn": "urn:li:dataset:similar1",
            "fields": [
                {"fieldPath": "id", "nativeDataType": "int"},
                {"fieldPath": "name", "nativeDataType": "varchar"},
                {"fieldPath": "phone", "nativeDataType": "varchar"},
            ],
        }
    ]

    diffs = await analyzer.diff_similar_tables(current_schema, similar_schemas)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["similar_urn"] == "urn:li:dataset:similar1"
    assert len(diff["overlapping_fields"]) == 2
    assert diff["current_only"] == ["email"]
    assert diff["similar_only"] == ["phone"]
    assert diff["type_mismatches"] == []


@pytest.mark.asyncio
async def test_diff_similar_tables_no_overlap(analyzer):
    current_schema = [
        {"fieldPath": "a", "nativeDataType": "int"},
    ]
    similar_schemas = [
        {
            "urn": "urn:li:dataset:disjoint",
            "fields": [
                {"fieldPath": "x", "nativeDataType": "int"},
            ],
        }
    ]

    diffs = await analyzer.diff_similar_tables(current_schema, similar_schemas)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["overlapping_fields"] == []
    assert diff["current_only"] == ["a"]
    assert diff["similar_only"] == ["x"]


@pytest.mark.asyncio
async def test_diff_similar_tables_type_mismatch(analyzer):
    current_schema = [
        {"fieldPath": "amount", "nativeDataType": "float"},
    ]
    similar_schemas = [
        {
            "urn": "urn:li:dataset:mismatch",
            "fields": [
                {"fieldPath": "amount", "nativeDataType": "decimal"},
            ],
        }
    ]

    diffs = await analyzer.diff_similar_tables(current_schema, similar_schemas)
    assert len(diffs) == 1
    assert len(diffs[0]["type_mismatches"]) == 1
    mismatch = diffs[0]["type_mismatches"][0]
    assert mismatch["fieldPath"] == "amount"
    assert mismatch["current_type"] == "float"
    assert mismatch["similar_type"] == "decimal"


# ── analyze ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_with_code_refs(analyzer, llm):
    mock_items = [
        MagicMock(content={"name": "main.py", "path": "src/main.py", "type": "file", "sha": "abc"}),
    ]

    with patch("src.backend.ingestion.extractors.GitHubExtractor") as MockExtractor:
        extractor_instance = AsyncMock()
        extractor_instance.extract = AsyncMock(return_value=mock_items)
        MockExtractor.return_value = extractor_instance

        llm.complete_json = AsyncMock(
            return_value={"user_id": "Unique user identifier", "email": "User email address"}
        )

        code_refs = {"owner": "org", "repo": "app", "token": "tok"}
        schema_fields = [
            {"fieldPath": "user_id", "nativeDataType": "int", "description": ""},
            {"fieldPath": "email", "nativeDataType": "varchar", "description": ""},
        ]

        result = await analyzer.analyze(code_refs, schema_fields)
        assert "user_id" in result
        assert "email" in result
        llm.complete_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_empty_code_refs(analyzer, llm):
    with patch("src.backend.ingestion.extractors.GitHubExtractor") as MockExtractor:
        extractor_instance = AsyncMock()
        extractor_instance.extract = AsyncMock(return_value=[])
        MockExtractor.return_value = extractor_instance

        result = await analyzer.analyze({"owner": "org", "repo": "app", "token": "tok"}, [])
        assert result == {}
        llm.complete_json.assert_not_awaited()
