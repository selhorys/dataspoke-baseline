"""Unit tests for DI provider return types and auth dependencies."""

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.internal import require_internal_token
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


# ── require_internal_token ────────────────────────────────────────────────────


class TestRequireInternalToken:
    """Tests for the shared-secret dependency guarding /internal/* endpoints."""

    async def test_missing_header_with_token_configured_returns_401(
        self, monkeypatch
    ) -> None:
        """When the header is absent and internal_token is set → 401 UNAUTHORIZED."""
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "internal_token", "secret-token")
        monkeypatch.setattr(internal_mod.settings, "internal_token", "secret-token")

        with pytest.raises(HTTPException) as exc_info:
            await require_internal_token(x_internal_token=None)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == "UNAUTHORIZED"

    async def test_wrong_header_value_returns_401(self, monkeypatch) -> None:
        """When the header is present but wrong → 401 UNAUTHORIZED (constant-time compare)."""
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "internal_token", "correct-secret")
        monkeypatch.setattr(internal_mod.settings, "internal_token", "correct-secret")

        with pytest.raises(HTTPException) as exc_info:
            await require_internal_token(x_internal_token="wrong-secret")

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == "UNAUTHORIZED"

    async def test_correct_header_passes(self, monkeypatch) -> None:
        """When the header matches settings.internal_token → no exception raised."""
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        token = "my-valid-internal-token"
        monkeypatch.setattr(settings_mod.settings, "internal_token", token)
        monkeypatch.setattr(internal_mod.settings, "internal_token", token)

        # Should complete without raising
        result = await require_internal_token(x_internal_token=token)
        assert result is None

    async def test_blank_internal_token_setting_returns_503(self, monkeypatch) -> None:
        """When settings.internal_token is blank → 503 INTERNAL_AUTH_NOT_CONFIGURED.

        Blank token means the operator has not configured the secret, which is
        a server-side misconfiguration rather than a client auth error.
        """
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "internal_token", "")
        monkeypatch.setattr(internal_mod.settings, "internal_token", "")

        with pytest.raises(HTTPException) as exc_info:
            await require_internal_token(x_internal_token="any-value")

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error_code"] == "INTERNAL_AUTH_NOT_CONFIGURED"

    async def test_blank_internal_token_setting_with_no_header_also_returns_503(
        self, monkeypatch
    ) -> None:
        """Blank settings.internal_token → 503 even when no header is sent."""
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "internal_token", "")
        monkeypatch.setattr(internal_mod.settings, "internal_token", "")

        with pytest.raises(HTTPException) as exc_info:
            await require_internal_token(x_internal_token=None)

        assert exc_info.value.status_code == 503

    async def test_compare_digest_not_bypassable_via_empty_string(
        self, monkeypatch
    ) -> None:
        """An empty string header must be rejected even when the token is set.

        secrets.compare_digest("", "secret") is False; ensure we don't short-circuit.
        """
        import src.api.auth.internal as internal_mod
        import src.shared.settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "internal_token", "real-token")
        monkeypatch.setattr(internal_mod.settings, "internal_token", "real-token")

        with pytest.raises(HTTPException) as exc_info:
            await require_internal_token(x_internal_token="")

        assert exc_info.value.status_code == 401
