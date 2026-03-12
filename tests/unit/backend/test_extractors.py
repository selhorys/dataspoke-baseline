"""Unit tests for ingestion extractors."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ingestion.extractors import (
    EXTRACTOR_REGISTRY,
    ConfluenceExtractor,
    ExcelExtractor,
    GitHubExtractor,
    SqlLogExtractor,
)
from src.shared.exceptions import DataSpokeError

# ── Registry ──────────────────────────────────────────────────────────────────


def test_extractor_registry_maps_all_types():
    assert set(EXTRACTOR_REGISTRY.keys()) == {"confluence", "github", "excel", "sql_log"}


# ── ConfluenceExtractor ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confluence_extractor_returns_descriptions():
    extractor = ConfluenceExtractor()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {
                "title": "Dataset Overview",
                "body": {"storage": {"value": "<p>This is a test</p>"}},
                "_links": {"webui": "/pages/123"},
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("src.backend.ingestion.extractors.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await extractor.extract(
            {
                "base_url": "https://wiki.example.com",
                "space_key": "DS",
                "username": "user",
                "api_token": "token",
            }
        )

    assert len(result) == 1
    assert result[0].metadata_type == "description"
    assert result[0].content["title"] == "Dataset Overview"
    assert "test" in result[0].content["body"]


@pytest.mark.asyncio
async def test_confluence_extractor_invalid_config():
    extractor = ConfluenceExtractor()
    with pytest.raises(DataSpokeError, match="missing config keys"):
        await extractor.extract({"base_url": "https://wiki.example.com"})


# ── GitHubExtractor ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_extractor_returns_code_refs():
    extractor = GitHubExtractor()
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "name": "schema.sql",
            "path": "db/schema.sql",
            "type": "file",
            "sha": "abc123",
            "html_url": "https://github.com/org/repo/blob/main/db/schema.sql",
        }
    ]
    mock_response.raise_for_status = MagicMock()

    with patch("src.backend.ingestion.extractors.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await extractor.extract({"owner": "org", "repo": "repo", "token": "ghp_test"})

    assert len(result) == 1
    assert result[0].metadata_type == "code_ref"
    assert result[0].content["name"] == "schema.sql"
    assert result[0].content["path"] == "db/schema.sql"


@pytest.mark.asyncio
async def test_github_extractor_invalid_config():
    extractor = GitHubExtractor()
    with pytest.raises(DataSpokeError, match="missing config keys"):
        await extractor.extract({"owner": "org"})


# ── ExcelExtractor ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_excel_extractor_returns_column_mapping_csv():
    csv_content = "column_name,data_type,description\nid,integer,Primary key\nname,text,User name"
    encoded = base64.b64encode(csv_content.encode()).decode()

    extractor = ExcelExtractor()
    result = await extractor.extract({"file_content": encoded, "file_name": "schema.csv"})

    assert len(result) == 2
    assert result[0].metadata_type == "column_mapping"
    assert result[0].content["column_name"] == "id"
    assert result[1].content["column_name"] == "name"


@pytest.mark.asyncio
async def test_excel_extractor_missing_config():
    extractor = ExcelExtractor()
    with pytest.raises(DataSpokeError, match="requires 'file_path' or 'file_content'"):
        await extractor.extract({})


# ── SqlLogExtractor ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sql_log_extractor_returns_lineage_edges():
    extractor = SqlLogExtractor()
    result = await extractor.extract(
        {
            "queries": [
                "SELECT * FROM catalog.title_master "
                "JOIN orders.order_header "
                "ON catalog.title_master.id = orders.order_header.title_id"
            ]
        }
    )

    assert len(result) == 1
    assert result[0].metadata_type == "lineage_edge"
    tables = result[0].content["tables"]
    assert "catalog.title_master" in tables
    assert "orders.order_header" in tables


@pytest.mark.asyncio
async def test_sql_log_extractor_missing_queries():
    extractor = SqlLogExtractor()
    with pytest.raises(DataSpokeError, match="requires 'queries'"):
        await extractor.extract({})


@pytest.mark.asyncio
async def test_sql_log_extractor_invalid_queries_type():
    extractor = SqlLogExtractor()
    with pytest.raises(DataSpokeError, match="must be a list"):
        await extractor.extract({"queries": "SELECT 1"})


@pytest.mark.asyncio
async def test_sql_log_extractor_simple_select():
    extractor = SqlLogExtractor()
    result = await extractor.extract({"queries": ["SELECT a, b FROM my_table WHERE a > 1"]})
    assert len(result) == 1
    assert "my_table" in result[0].content["tables"]
