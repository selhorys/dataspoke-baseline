"""Unit tests for DI provider return types."""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_datahub, get_db, get_llm, get_qdrant, get_redis
from src.shared.cache.client import RedisClient


class TestInfraProviders:
    @patch("src.api.dependencies.DataHubClient")
    def test_get_datahub_returns_client(self, mock_cls: object) -> None:
        client = get_datahub()
        assert client is not None

    def test_get_redis_returns_client(self) -> None:
        client = get_redis()
        assert isinstance(client, RedisClient)

    @patch("src.api.dependencies.QdrantManager")
    def test_get_qdrant_returns_manager(self, mock_cls: object) -> None:
        manager = get_qdrant()
        assert manager is not None

    @patch("src.api.dependencies.LLMClient")
    def test_get_llm_returns_client(self, mock_cls: object) -> None:
        client = get_llm()
        assert client is not None

    @pytest.mark.asyncio
    async def test_get_db_yields_session(self) -> None:
        gen = get_db()
        session = await gen.__anext__()
        assert isinstance(session, AsyncSession)
        # Clean up the generator
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
