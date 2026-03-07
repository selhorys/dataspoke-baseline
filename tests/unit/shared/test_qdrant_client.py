"""Unit tests for QdrantManager — no real Qdrant connection needed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import StorageUnavailableError


@pytest.fixture
def mock_qdrant():
    with patch("src.shared.vector.client.AsyncQdrantClient") as cls:
        instance = AsyncMock()
        cls.return_value = instance
        yield instance


def _make_manager(mock_qdrant):
    from src.shared.vector.client import QdrantManager

    return QdrantManager(host="localhost", port=6333, grpc_port=6334, api_key="")


@pytest.mark.asyncio
async def test_ensure_collection_creates_when_missing(mock_qdrant):

    collections_resp = MagicMock()
    collections_resp.collections = []
    mock_qdrant.get_collections.return_value = collections_resp

    mgr = _make_manager(mock_qdrant)
    await mgr.ensure_collection("test_col", vector_size=1536)

    mock_qdrant.create_collection.assert_awaited_once()
    call_kwargs = mock_qdrant.create_collection.call_args
    assert call_kwargs.kwargs["collection_name"] == "test_col"


@pytest.mark.asyncio
async def test_ensure_collection_skips_existing(mock_qdrant):
    col = MagicMock()
    col.name = "test_col"
    collections_resp = MagicMock()
    collections_resp.collections = [col]
    mock_qdrant.get_collections.return_value = collections_resp

    mgr = _make_manager(mock_qdrant)
    await mgr.ensure_collection("test_col", vector_size=1536)

    mock_qdrant.create_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert(mock_qdrant):
    from qdrant_client import models

    mgr = _make_manager(mock_qdrant)
    points = [models.PointStruct(id="p1", vector=[0.1] * 1536, payload={"text": "hello"})]
    await mgr.upsert("test_col", points)

    mock_qdrant.upsert.assert_awaited_once_with(collection_name="test_col", points=points)


@pytest.mark.asyncio
async def test_delete(mock_qdrant):
    mgr = _make_manager(mock_qdrant)
    await mgr.delete("test_col", ids=["p1", "p2"])

    mock_qdrant.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_raises_storage_unavailable_on_error(mock_qdrant):
    mock_qdrant.upsert.side_effect = Exception("connection refused")

    mgr = _make_manager(mock_qdrant)
    with pytest.raises(StorageUnavailableError, match="Qdrant upsert failed"):
        await mgr.upsert("col", [])


@pytest.mark.asyncio
async def test_ensure_collection_raises_storage_unavailable_on_error(mock_qdrant):
    mock_qdrant.get_collections.side_effect = Exception("timeout")

    mgr = _make_manager(mock_qdrant)
    with pytest.raises(StorageUnavailableError, match="Qdrant ensure_collection failed"):
        await mgr.ensure_collection("col", 1536)


@pytest.mark.asyncio
async def test_search_returns_points(mock_qdrant):
    scored_point = MagicMock()
    query_response = MagicMock()
    query_response.points = [scored_point]
    mock_qdrant.query_points.return_value = query_response

    mgr = _make_manager(mock_qdrant)
    result = await mgr.search("test_col", vector=[0.1] * 1536, limit=5)

    assert result == [scored_point]
    mock_qdrant.query_points.assert_awaited_once()
    call_kwargs = mock_qdrant.query_points.call_args.kwargs
    assert call_kwargs["collection_name"] == "test_col"
    assert call_kwargs["limit"] == 5
    assert call_kwargs["query_filter"] is None


@pytest.mark.asyncio
async def test_search_with_filters(mock_qdrant):
    query_response = MagicMock()
    query_response.points = []
    mock_qdrant.query_points.return_value = query_response

    mgr = _make_manager(mock_qdrant)
    await mgr.search("test_col", vector=[0.1] * 1536, filters={"platform": "snowflake"})

    call_kwargs = mock_qdrant.query_points.call_args.kwargs
    assert call_kwargs["query_filter"] is not None


@pytest.mark.asyncio
async def test_search_raises_storage_unavailable_on_error(mock_qdrant):
    mock_qdrant.query_points.side_effect = Exception("timeout")

    mgr = _make_manager(mock_qdrant)
    with pytest.raises(StorageUnavailableError, match="Qdrant search failed"):
        await mgr.search("col", vector=[0.1] * 1536)


@pytest.mark.asyncio
async def test_close(mock_qdrant):
    mgr = _make_manager(mock_qdrant)
    await mgr.close()
    mock_qdrant.close.assert_awaited_once()
