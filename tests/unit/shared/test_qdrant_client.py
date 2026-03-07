"""Unit tests for Qdrant client wrapper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from qdrant_client.models import PointStruct, ScoredPoint

from src.shared.vector.client import QdrantManager


@pytest.fixture
def mock_qdrant():
    with patch("src.shared.vector.client.AsyncQdrantClient") as mock_cls:
        instance = AsyncMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def manager(mock_qdrant):
    return QdrantManager(host="localhost", port=6333, api_key="")


async def test_ensure_collection_creates_if_missing(manager, mock_qdrant) -> None:
    collections_resp = MagicMock()
    collections_resp.collections = []
    mock_qdrant.get_collections.return_value = collections_resp

    await manager.ensure_collection("test_col", vector_size=384)
    mock_qdrant.create_collection.assert_awaited_once()
    call_kwargs = mock_qdrant.create_collection.call_args
    assert call_kwargs.kwargs["collection_name"] == "test_col"


async def test_ensure_collection_noop_if_exists(manager, mock_qdrant) -> None:
    existing = MagicMock()
    existing.name = "test_col"
    collections_resp = MagicMock()
    collections_resp.collections = [existing]
    mock_qdrant.get_collections.return_value = collections_resp

    await manager.ensure_collection("test_col")
    mock_qdrant.create_collection.assert_not_awaited()


async def test_upsert_calls_client(manager, mock_qdrant) -> None:
    points = [PointStruct(id="1", vector=[0.1, 0.2], payload={"key": "val"})]
    await manager.upsert("col", points)
    mock_qdrant.upsert.assert_awaited_once_with(collection_name="col", points=points)


async def test_search_returns_scored_points(manager, mock_qdrant) -> None:
    scored = MagicMock(spec=ScoredPoint)
    query_result = MagicMock()
    query_result.points = [scored]
    mock_qdrant.query_points.return_value = query_result

    result = await manager.search("col", [0.1, 0.2, 0.3], limit=5)
    assert result == [scored]


async def test_search_with_filters(manager, mock_qdrant) -> None:
    query_result = MagicMock()
    query_result.points = []
    mock_qdrant.query_points.return_value = query_result

    await manager.search("col", [0.1], filters={"platform": "snowflake"})
    call_kwargs = mock_qdrant.query_points.call_args.kwargs
    assert call_kwargs["query_filter"] is not None


async def test_delete_calls_client(manager, mock_qdrant) -> None:
    await manager.delete("col", ["id1", "id2"])
    mock_qdrant.delete.assert_awaited_once_with(
        collection_name="col", points_selector=["id1", "id2"]
    )


async def test_check_connectivity(manager, mock_qdrant) -> None:
    mock_qdrant.get_collections.return_value = MagicMock()
    result = await manager.check_connectivity()
    assert result is True


async def test_check_connectivity_failure(manager, mock_qdrant) -> None:
    mock_qdrant.get_collections.side_effect = Exception("connection refused")
    result = await manager.check_connectivity()
    assert result is False
