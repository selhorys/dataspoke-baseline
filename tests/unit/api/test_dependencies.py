"""Unit tests for DI provider return types."""

from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_datahub, get_db, get_llm, get_qdrant, get_redis


class TestInfraProviders:
    @patch("src.api.dependencies.DataHubClient")
    def test_get_datahub_returns_client(self, mock_cls: object) -> None:
        client = get_datahub()
        assert client is not None

    @patch("src.api.dependencies.RedisClient")
    def test_get_redis_returns_client(self, mock_cls: object) -> None:
        client = get_redis()
        assert client is not None

    @patch("src.api.dependencies.QdrantManager")
    def test_get_qdrant_returns_manager(self, mock_cls: object) -> None:
        manager = get_qdrant()
        assert manager is not None

    @patch("src.api.dependencies.LLMClient")
    def test_get_llm_returns_client(self, mock_cls: object) -> None:
        client = get_llm()
        assert client is not None

    @patch("src.api.dependencies.SessionLocal")
    async def test_get_db_yields_session(self, mock_session_local: object) -> None:
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

        gen = get_db()
        session = await gen.__anext__()
        assert session is mock_session
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
