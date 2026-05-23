"""Unit tests for DI provider return types and auth dependencies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.internal import require_internal_token
from src.api.dependencies import get_datahub, get_db, get_redis, get_vector


def _fake_request(**state: object):
    """Return a stand-in Request object exposing .app.state.<key> attributes.

    Providers like get_datahub() read request.app.state.X; we don't need a real
    Starlette Request for that — SimpleNamespace lookups suffice.
    """
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


class TestInfraProviders:
    async def test_get_datahub_returns_client(self) -> None:
        """get_datahub(db) constructs a DataHubClient from peripheral_config + secret.

        The function is now async and reads from DB/K8s rather than app.state.
        We patch get_peripheral_config and get_datahub_token at the source module
        level because both are imported lazily inside get_datahub.

        spec: plan/scalable-beaming-hamster.md — get_datahub is per-request factory.
        spec: API.md §DataHub client — constructed from peripheral_config + K8s secret.
        """
        from unittest.mock import AsyncMock, patch

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

    def test_get_redis_returns_client(self) -> None:
        sentinel = object()
        assert get_redis(_fake_request(redis=sentinel)) is sentinel

    def test_get_vector_returns_manager(self) -> None:
        sentinel = object()
        assert get_vector(_fake_request(vector=sentinel)) is sentinel

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


# ── Group-to-Route Access Control: admin routes require 'admin' group ─────────


class TestAdminGroupEnforcement:
    """Tests that /admin/* routes are restricted to tokens with 'admin' in groups.

    spec: API.md §Group-to-Route Access Control — /admin/* requires 'admin' group exclusively.
    spec: API.md §Application Error Codes — FORBIDDEN (403) for valid token; wrong group claim.
    """

    async def test_dg_only_token_cannot_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A token with groups=['dg'] (no admin) hitting /admin/dags/verify must get 403.

        spec: API.md §Group-to-Route Access Control — /admin/* accessible to admins only.
        spec: API.md §Application Error Codes — FORBIDDEN (403) for valid token; groups
        claim does not satisfy route requirement.
        """
        from tests.unit.api.conftest import auth_headers

        # Mint a real token with only 'dg' — no 'admin' claim
        # spec: API.md §JWT Claims — groups is the claim enforced against URI tier
        dg_only_headers = auth_headers(groups=["dg"])

        response = await client.post(
            "/api/v1/admin/dags/verify",
            headers=dg_only_headers,
        )
        assert response.status_code == 403, (
            f"Token with groups=['dg'] must be rejected with 403 on /admin/dags/verify "
            f"per spec/API.md §Group-to-Route Access Control, got {response.status_code}"
        )

    async def test_de_only_token_cannot_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A token with groups=['de'] (no admin) hitting /admin/dags/verify must get 403.

        spec: API.md §Group-to-Route Access Control — /admin/* requires 'admin' claim exclusively.
        """
        from tests.unit.api.conftest import auth_headers

        de_only_headers = auth_headers(groups=["de"])

        response = await client.post(
            "/api/v1/admin/dags/verify",
            headers=de_only_headers,
        )
        assert response.status_code == 403, (
            f"Token with groups=['de'] must be rejected with 403 on /admin/dags/verify "
            f"per spec/API.md §Group-to-Route Access Control, got {response.status_code}"
        )

    async def test_admin_token_can_access_admin_dags_verify(
        self, client: AsyncClient
    ) -> None:
        """A token with groups=['admin'] passes the auth guard on /admin/dags/verify.

        spec: API.md §Admin Role — 'admin' group bypasses group-tier restrictions.
        We inject an AsyncMock AirflowClient so the route can complete without a
        real Airflow instance, giving a 200 response. The key assertion is that
        the 'admin' group is not rejected with 403.
        """
        from unittest.mock import AsyncMock

        from src.api.dependencies import get_airflow_client
        from src.api.main import app
        from tests.unit.api.conftest import auth_headers

        # Inject a stub Airflow client that returns an empty DAG list
        mock_airflow = AsyncMock()
        mock_airflow.list_dags = AsyncMock(return_value=[])
        app.dependency_overrides[get_airflow_client] = lambda: mock_airflow

        try:
            admin_headers = auth_headers(groups=["admin"])
            response = await client.post(
                "/api/v1/admin/dags/verify",
                headers=admin_headers,
            )
            # Must not be 403 — admin group must pass the auth guard
            # spec: API.md §Admin Role — 'admin' group bypasses group-tier restrictions
            assert response.status_code != 403, (
                f"Admin token must not get 403 on /admin/dags/verify "
                f"per spec/API.md §Admin Role, got {response.status_code}"
            )
            # With the stub returning [], verify_dags returns a 200 with the expected shape
            assert response.status_code == 200
        finally:
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
