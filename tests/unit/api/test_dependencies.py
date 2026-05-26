"""Unit tests for DI provider return types and auth dependencies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.internal import require_internal_token
from src.api.dependencies import get_datahub, get_db, get_notification, get_redis, get_vector
from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO


def _fake_request(**state: object):
    """Return a stand-in Request object exposing .app.state.<key> attributes.

    Providers like get_redis() read request.app.state.redis; we don't need a real
    Starlette Request for that — SimpleNamespace lookups suffice.
    """
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _rc(**overrides) -> RuntimeConfigDTO:
    """Build a RuntimeConfigDTO with RUNTIME_CONFIG_DEFAULTS + overrides."""
    return RuntimeConfigDTO(**{**RUNTIME_CONFIG_DEFAULTS, **overrides})


class TestInfraProviders:
    async def test_get_datahub_returns_client(self) -> None:
        """get_datahub(db) constructs a DataHubClient from peripheral_config + secret.

        The function is now async and reads from DB/K8s rather than app.state.
        We patch get_peripheral_config and get_datahub_token at the source module
        level because both are imported lazily inside get_datahub.

        spec: plan/scalable-beaming-hamster.md — get_datahub is per-request factory.
        spec: API.md §DataHub client — constructed from peripheral_config + K8s secret.
        """
        from src.backend.admin.peripheral_service import DatahubConfigDTO
        from src.shared.datahub.client import DataHubClient

        _fake_dto = DatahubConfigDTO(gms_url="http://gms-test:8080", kafka_brokers="k:9092")
        mock_db = AsyncMock()

        with (
            patch(
                "src.backend.admin.peripheral_service.get_peripheral_config",
                AsyncMock(return_value=_fake_dto),
            ),
            patch(
                "src.backend.admin.datahub_secret.get_datahub_token",
                new=lambda: "test-token",
            ),
        ):
            result = await get_datahub(db=mock_db)

        assert isinstance(result, DataHubClient), (
            "get_datahub must return a DataHubClient when peripheral is configured"
        )

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


# ── get_redis stub/real branching ─────────────────────────────────────────────


class TestGetRedisProvider:
    """get_redis returns StubRedisClient when rc.stub_redis_client=True, app.state.redis otherwise.

    spec: src/workflows/_common.py — make_redis_client(stub=...) factory contract.
    spec: src/api/dependencies.py — get_redis consults RuntimeConfigDTO.stub_redis_client.
    """

    @pytest.mark.asyncio
    async def test_get_redis_returns_stub_when_stub_flag_true(self) -> None:
        """get_redis returns StubRedisClient when rc.stub_redis_client=True.

        spec: src/workflows/_common.py — make_redis_client(stub=True) → StubRedisClient.
        spec: src/api/dependencies.py — stub_redis_client=True → StubRedisClient().
        """
        from src.workflows._stubs import StubRedisClient

        mock_db = AsyncMock()
        fake_rc = _rc(stub_redis_client=True)
        sentinel_redis = object()
        request = _fake_request(redis=sentinel_redis)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_redis(request=request, db=mock_db)

        assert isinstance(result, StubRedisClient), (
            f"Expected StubRedisClient when stub_redis_client=True; got {type(result).__name__}. "
            "spec: src/api/dependencies.py get_redis."
        )

    @pytest.mark.asyncio
    async def test_get_redis_returns_app_state_when_stub_flag_false(self) -> None:
        """get_redis returns request.app.state.redis when rc.stub_redis_client=False.

        spec: src/workflows/_common.py — make_redis_client(stub=False) → real RedisClient.
        spec: src/api/dependencies.py — stub_redis_client=False → app.state.redis.
        """
        mock_db = AsyncMock()
        fake_rc = _rc(stub_redis_client=False)
        sentinel_redis = object()
        request = _fake_request(redis=sentinel_redis)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_redis(request=request, db=mock_db)

        assert result is sentinel_redis, (
            "get_redis must return the pooled redis from app.state when stub_redis_client=False."
        )


# ── get_vector stub/real branching ────────────────────────────────────────────


class TestGetVectorProvider:
    """get_vector returns StubPgVectorManager when rc.stub_pgvector_manager=True.

    spec: src/workflows/_common.py — make_pgvector_manager(stub=...) factory contract.
    spec: src/api/dependencies.py — get_vector consults RuntimeConfigDTO.stub_pgvector_manager.
    """

    @pytest.mark.asyncio
    async def test_get_vector_returns_stub_when_stub_flag_true(self) -> None:
        """get_vector returns StubPgVectorManager when rc.stub_pgvector_manager=True.

        spec: src/workflows/_common.py — make_pgvector_manager(stub=True) → StubPgVectorManager.
        spec: src/api/dependencies.py — stub_pgvector_manager=True → StubPgVectorManager().
        """
        from src.workflows._stubs import StubPgVectorManager

        mock_db = AsyncMock()
        fake_rc = _rc(stub_pgvector_manager=True)
        sentinel_vector = object()
        request = _fake_request(vector=sentinel_vector)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_vector(request=request, db=mock_db)

        assert isinstance(result, StubPgVectorManager), (
            f"Expected StubPgVectorManager when stub_pgvector_manager=True; got {type(result).__name__}. "
            "spec: src/api/dependencies.py get_vector."
        )

    @pytest.mark.asyncio
    async def test_get_vector_returns_app_state_when_stub_flag_false(self) -> None:
        """get_vector returns request.app.state.vector when rc.stub_pgvector_manager=False.

        spec: src/workflows/_common.py — make_pgvector_manager(stub=False) → real PgVectorManager.
        spec: src/api/dependencies.py — stub_pgvector_manager=False → app.state.vector.
        """
        mock_db = AsyncMock()
        fake_rc = _rc(stub_pgvector_manager=False)
        sentinel_vector = object()
        request = _fake_request(vector=sentinel_vector)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_vector(request=request, db=mock_db)

        assert result is sentinel_vector, (
            "get_vector must return the pooled vector manager from app.state when stub_pgvector_manager=False."
        )


# ── get_notification stub/real branching ──────────────────────────────────────


class TestGetNotificationProvider:
    """get_notification returns StubNotificationService when rc.stub_notification_service=True.

    spec: src/workflows/_common.py — make_notification_service(stub=...) factory contract.
    spec: src/api/dependencies.py — get_notification consults RuntimeConfigDTO.stub_notification_service.
    """

    @pytest.mark.asyncio
    async def test_get_notification_returns_stub_when_stub_flag_true(self) -> None:
        """get_notification returns StubNotificationService when rc.stub_notification_service=True.

        spec: src/workflows/_common.py — make_notification_service(stub=True) → StubNotificationService.
        spec: src/api/dependencies.py — stub_notification_service=True → StubNotificationService().
        """
        from src.workflows._stubs import StubNotificationService

        mock_db = AsyncMock()
        fake_rc = _rc(stub_notification_service=True)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_notification(db=mock_db)

        assert isinstance(result, StubNotificationService), (
            f"Expected StubNotificationService when stub_notification_service=True; got {type(result).__name__}. "
            "spec: src/api/dependencies.py get_notification."
        )

    @pytest.mark.asyncio
    async def test_get_notification_returns_real_when_stub_flag_false(self) -> None:
        """get_notification returns NotificationService when rc.stub_notification_service=False.

        spec: src/workflows/_common.py — make_notification_service(stub=False) → real NotificationService.
        spec: src/api/dependencies.py — stub_notification_service=False → NotificationService().
        """
        from src.shared.notifications.service import NotificationService

        mock_db = AsyncMock()
        fake_rc = _rc(stub_notification_service=False)

        with patch(
            "src.backend.admin.config_service.get_runtime_config",
            new=AsyncMock(return_value=fake_rc),
        ):
            result = await get_notification(db=mock_db)

        assert isinstance(result, NotificationService), (
            f"Expected NotificationService when stub_notification_service=False; got {type(result).__name__}. "
            "spec: src/api/dependencies.py get_notification."
        )


# ── Role-to-Route Access Control: admin routes require Admin role ──────────────


class TestAdminGroupEnforcement:
    """Tests that /admin/* routes are restricted to users with Admin role.

    Role is DB-backed; the JWT carries only sub (user UUID) and email.
    Tests override require_authenticated / require_admin to inject a known
    AuthContext without a real database.

    spec: API.md §Role-to-Route Access Control — /admin/* requires Admin role.
    spec: API.md §Application Error Codes — FORBIDDEN (403) for valid token; wrong role.
    """

    async def test_non_admin_role_cannot_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A user with role=Editor hitting /admin/dags/verify must get 403.

        spec: API.md §Role-to-Route Access Control — /admin/* requires Admin role.
        spec: API.md §Application Error Codes — FORBIDDEN (403) for valid token; wrong role.
        """
        from unittest.mock import MagicMock

        from src.api.auth.dependencies import require_authenticated, require_admin
        from src.api.main import app
        from src.backend.auth.privilege import AuthContext

        mock_user = MagicMock()
        mock_user.id = __import__("uuid").uuid4()
        mock_user.role = "Editor"
        editor_ctx = AuthContext(user=mock_user, effective_role="Editor")

        app.dependency_overrides[require_authenticated] = lambda: editor_ctx
        try:
            response = await client.post("/api/v1/admin/dags/verify")
            assert response.status_code == 403, (
                f"Editor role must be rejected with 403 on /admin/dags/verify "
                f"per spec/API.md §Role-to-Route Access Control, got {response.status_code}"
            )
        finally:
            app.dependency_overrides.pop(require_authenticated, None)

    async def test_reader_role_cannot_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A user with role=Reader hitting /admin/dags/verify must get 403.

        spec: API.md §Role-to-Route Access Control — /admin/* requires Admin role.
        """
        from unittest.mock import MagicMock

        from src.api.auth.dependencies import require_authenticated
        from src.api.main import app
        from src.backend.auth.privilege import AuthContext

        mock_user = MagicMock()
        mock_user.id = __import__("uuid").uuid4()
        mock_user.role = "Reader"
        reader_ctx = AuthContext(user=mock_user, effective_role="Reader")

        app.dependency_overrides[require_authenticated] = lambda: reader_ctx
        try:
            response = await client.post("/api/v1/admin/dags/verify")
            assert response.status_code == 403, (
                f"Reader role must be rejected with 403 on /admin/dags/verify "
                f"per spec/API.md §Role-to-Route Access Control, got {response.status_code}"
            )
        finally:
            app.dependency_overrides.pop(require_authenticated, None)

    async def test_admin_role_can_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A user with role=Admin passes the auth guard on /admin/dags/verify.

        spec: API.md §Admin Role — Admin role grants access to /admin/* routes.
        We inject an AsyncMock AirflowClient and an Admin AuthContext so the route
        can complete without a real database or Airflow instance.
        """
        from unittest.mock import AsyncMock, MagicMock

        from src.api.auth.dependencies import require_authenticated, require_admin
        from src.api.dependencies import get_airflow_client
        from src.api.main import app
        from src.backend.auth.privilege import AuthContext

        mock_user = MagicMock()
        mock_user.id = __import__("uuid").uuid4()
        mock_user.role = "Admin"
        admin_ctx = AuthContext(user=mock_user, effective_role="Admin")

        mock_airflow = AsyncMock()
        mock_airflow.list_dags = AsyncMock(return_value=[])
        app.dependency_overrides[require_authenticated] = lambda: admin_ctx
        app.dependency_overrides[require_admin] = lambda: admin_ctx
        app.dependency_overrides[get_airflow_client] = lambda: mock_airflow

        try:
            response = await client.post("/api/v1/admin/dags/verify")
            # Must not be 403 — Admin role must pass the auth guard
            # spec: API.md §Admin Role — Admin role grants access to /admin/* routes
            assert response.status_code != 403, (
                f"Admin role must not get 403 on /admin/dags/verify "
                f"per spec/API.md §Admin Role, got {response.status_code}"
            )
            assert response.status_code == 200
        finally:
            app.dependency_overrides.pop(require_authenticated, None)
            app.dependency_overrides.pop(require_admin, None)
            app.dependency_overrides.pop(get_airflow_client, None)


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
