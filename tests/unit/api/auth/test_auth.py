"""Unit tests for auth endpoints: /api/v1/auth/token, /refresh, /revoke."""

import pytest
from httpx import AsyncClient

from src.api.routers.auth import _get_user_groups, _verify_credentials
from tests.unit.api.conftest import auth_headers

AUTH_TOKEN = "/api/v1/auth/token"
AUTH_REFRESH = "/api/v1/auth/token/refresh"
AUTH_REVOKE = "/api/v1/auth/token/revoke"


# ── Fixture: enable stub auth (patch settings for tests that need a working login) ───


@pytest.fixture(autouse=True)
def enable_stub_auth(monkeypatch):
    """Enable stub auth so existing tests that POST /auth/token can succeed.

    All tests in this module run with ``settings.enable_stub_auth=True`` unless
    they override this fixture explicitly (e.g., the stub-gate tests below).
    """
    import src.api.routers.auth as auth_mod
    import src.shared.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "enable_stub_auth", True)
    monkeypatch.setattr(auth_mod.settings, "enable_stub_auth", True)


# ── Fake Redis double for revocation tests ─────────────────────────────────────


class _FakeRedis:
    """In-memory dict-backed Redis fake. Supports get/set/set_nx with optional TTL."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._store[key] = value

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 300) -> bool:
        """Set key only if it does not exist. Returns True if set, False if already exists."""
        if key in self._store:
            return False
        self._store[key] = value
        return True

    def is_empty(self) -> bool:
        return len(self._store) == 0


async def test_valid_login_returns_access_token(client: AsyncClient) -> None:
    # spec: API.md §Token Strategy — access token lifetime is 15 minutes
    _ACCESS_TOKEN_SECONDS = 15 * 60  # 900 seconds per spec/API.md §Token Strategy
    response = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    # token_type is impl-pinned; spec/API.md does not mandate it — spec gap surfaced 2026-05-01
    assert body["expires_in"] == _ACCESS_TOKEN_SECONDS, (
        f"expires_in must be {_ACCESS_TOKEN_SECONDS}s (15 min) per spec/API.md §Token Strategy, "
        f"got {body['expires_in']}"
    )


async def test_valid_login_sets_refresh_cookie(client: AsyncClient) -> None:
    """POST /auth/token sets a refresh token cookie with required security attributes.

    spec: API.md §Token Strategy — refresh token stored in HttpOnly cookie.
    spec: API.md §Known Limitations (Current Stub) — HTTP-only cookie mandated.
    spec: API.md §Token Strategy — refresh token lifetime is 7 days (604800 seconds).
    """
    _REFRESH_TTL_SECONDS = 7 * 24 * 3600  # 604800 seconds per spec/API.md §Token Strategy
    response = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
    assert response.status_code == 200
    assert "refresh_token" in response.cookies

    # Inspect the raw Set-Cookie header for security attributes
    set_cookie = response.headers.get("set-cookie", "")
    assert "refresh_token" in set_cookie, "set-cookie header must contain refresh_token"
    set_cookie_lower = set_cookie.lower()

    # spec: API.md §Known Limitations (Current Stub) — HttpOnly is the only mandated cookie flag
    assert "httponly" in set_cookie_lower, (
        "Refresh cookie must be HttpOnly per spec/API.md §Known Limitations (Current Stub)"
    )

    # spec: API.md §Token Strategy — refresh token lifetime is 7 days = 604800 seconds
    assert f"max-age={_REFRESH_TTL_SECONDS}" in set_cookie_lower, (
        f"Refresh cookie Max-Age must be {_REFRESH_TTL_SECONDS}s (7 days) "
        "per spec/API.md §Token Strategy"
    )

    # samesite and path are impl-pinned; spec/API.md does not mandate them — spec gap surfaced 2026-05-01


async def test_access_token_carries_groups_and_email(client: AsyncClient) -> None:
    """Issued access token payload contains sub, email, groups, exp, iat.

    spec: API.md §JWT Claims — access-token payload: sub (user uuid), email,
    groups (array of user-group identifiers), exp, iat.
    The admin account receives all groups (admin, de, da, dg) per spec/API.md §Known Limitations.
    """
    import jwt as pyjwt

    from src.api.config import settings as api_settings

    response = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
    assert response.status_code == 200
    raw_token = response.json()["access_token"]

    # Decode without verification is safe here — we already trust the server issued it.
    # We use the known secret to do a full verification to also confirm signature.
    payload = pyjwt.decode(
        raw_token,
        api_settings.jwt_secret_key,
        algorithms=[api_settings.jwt_algorithm],
    )

    # spec: API.md §JWT Claims — sub must be set
    assert "sub" in payload and payload["sub"], (
        "Access token must carry 'sub' claim per spec/API.md §JWT Claims"
    )

    # spec: API.md §JWT Claims — email claim must be present
    assert "email" in payload, "Access token must carry 'email' claim per spec/API.md §JWT Claims"
    # Admin login uses "admin" as email in stub identity store
    assert payload["email"] == "admin", (
        "Access token 'email' must match admin login email per spec/API.md §JWT Claims"
    )

    # spec: API.md §JWT Claims — groups is an array; admin gets all groups
    assert "groups" in payload, "Access token must carry 'groups' claim per spec/API.md §JWT Claims"
    assert isinstance(payload["groups"], list), "'groups' must be a list"
    assert "admin" in payload["groups"], (
        "Admin account must have 'admin' in groups per spec/API.md §Known Limitations"
    )

    # spec: API.md §JWT Claims — exp and iat must be present
    assert "exp" in payload, "Access token must carry 'exp' claim per spec/API.md §JWT Claims"
    assert "iat" in payload, "Access token must carry 'iat' claim per spec/API.md §JWT Claims"


async def test_invalid_credentials_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        AUTH_TOKEN, json={"email": "admin", "password": "wrong-password"}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error_code"] == "UNAUTHORIZED"


async def test_refresh_without_cookie_returns_401(client: AsyncClient) -> None:
    # get_redis now calls get_runtime_config(db) — override it so no DB is needed.
    # The subject under test is the missing-cookie guard, not Redis or runtime config.
    from unittest.mock import AsyncMock

    from src.api.dependencies import get_redis
    from src.api.main import app

    app.dependency_overrides[get_redis] = lambda: AsyncMock()
    try:
        response = await client.post(AUTH_REFRESH)
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_refresh_with_valid_cookie_returns_new_token(client: AsyncClient) -> None:
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        # First get a refresh cookie
        login_resp = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
        assert login_resp.status_code == 200
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie is not None

        # Use the refresh cookie to get a new access token
        client.cookies.set("refresh_token", refresh_cookie)
        refresh_resp = await client.post(AUTH_REFRESH)
        assert refresh_resp.status_code == 200
        body = refresh_resp.json()
        assert "access_token" in body
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_revoke_clears_cookie(client: AsyncClient) -> None:
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        # Login to get a refresh token
        login_resp = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
        refresh_cookie = login_resp.cookies.get("refresh_token")

        # Revoke
        client.cookies.set("refresh_token", refresh_cookie)
        revoke_resp = await client.post(AUTH_REVOKE)
        assert revoke_resp.status_code == 204
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_auth_required_route_without_token_returns_401(client: AsyncClient) -> None:
    """Accessing a protected route without a token must return 401."""
    response = await client.get("/api/v1/spoke/common/ingestion")
    assert response.status_code == 401


async def test_wrong_group_returns_403(client: AsyncClient) -> None:
    """A user without 'dg' group accessing /spoke/dg/* must get 403."""
    headers = auth_headers(groups=["de"])
    response = await client.get("/api/v1/spoke/dg/metric", headers=headers)
    assert response.status_code == 403


async def test_admin_group_can_access_dg_routes(client: AsyncClient) -> None:
    """Admin users bypass group-tier restrictions."""
    from unittest.mock import AsyncMock, MagicMock

    from src.api.dependencies import get_datahub, get_db, get_redis
    from src.api.main import app

    mock_session = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(side_effect=[count_result, rows_result])

    async def _mock_db():
        yield mock_session

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[get_datahub] = lambda: AsyncMock()
    app.dependency_overrides[get_redis] = lambda: AsyncMock()
    try:
        headers = auth_headers(groups=["admin"])
        response = await client.get("/api/v1/spoke/dg/metric", headers=headers)
        # 200 means route was reached (admin auth passed); service returns paginated list
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_datahub, None)
        app.dependency_overrides.pop(get_redis, None)


async def test_valid_group_can_access_common_routes(client: AsyncClient) -> None:
    """Any valid group member can access /spoke/common/* routes.

    /spoke/common/ingestion depends on get_ingestion_service → get_datahub.
    get_datahub now reads from DB-backed peripheral_config; we override it here
    so the auth guard (not peripheral config) is the subject under test.

    spec: API.md §Group-to-Route Access Control — any valid group may access /spoke/common/*.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.api.dependencies import get_datahub, get_db, get_redis
    from src.api.main import app

    mock_session = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []

    async def _mock_db():
        yield mock_session

    # Override get_redis so its get_runtime_config(db) call does not consume
    # execute results that belong to the ingestion service query.
    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[get_datahub] = lambda: AsyncMock()
    app.dependency_overrides[get_redis] = lambda: AsyncMock()
    try:
        for group in ["de", "da", "dg"]:
            mock_session.execute = AsyncMock(side_effect=[count_result, rows_result])
            headers = auth_headers(groups=[group])
            response = await client.get("/api/v1/spoke/common/ingestion", headers=headers)
            assert response.status_code == 200, (
                f"Expected 200 for group={group}, got {response.status_code}"
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_datahub, None)
        app.dependency_overrides.pop(get_redis, None)


# ── Redis-backed revocation tests ──────────────────────────────────────────────


async def test_revoke_then_refresh_returns_401_revoked(client: AsyncClient) -> None:
    """After POST /auth/token/revoke, refreshing the same cookie returns 401 UNAUTHORIZED.

    spec: API.md §Known Limitations — Redis-backed token revocation; fail-closed.
    spec: API.md §Application Error Codes — UNAUTHORIZED (401) for token missing/expired/malformed.
    Note: message text (e.g. 'revoked') is impl-defined; only error_code is spec-mandated.
    """
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        # Obtain a real refresh cookie
        login_resp = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
        assert login_resp.status_code == 200
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie is not None

        # Revoke the token — Redis should now hold the revocation key
        client.cookies.set("refresh_token", refresh_cookie)
        revoke_resp = await client.post(AUTH_REVOKE)
        assert revoke_resp.status_code == 204

        # Trying to refresh with the revoked cookie must fail
        # spec: API.md §Application Error Codes — UNAUTHORIZED (401)
        refresh_resp = await client.post(AUTH_REFRESH)
        assert refresh_resp.status_code == 401
        body = refresh_resp.json()
        assert body["error_code"] == "UNAUTHORIZED"
        # Message text is impl-defined; spec only mandates error_code == UNAUTHORIZED
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_token_rotation_rejects_old_cookie(client: AsyncClient) -> None:
    """After a successful refresh (token rotation), the OLD cookie must be rejected.

    Token rotation is current impl best-practice; spec/API.md is silent on rotation enforcement.
    spec: API.md §Application Error Codes — UNAUTHORIZED (401) for token missing/expired/malformed.
    Note: message text is impl-defined; spec only mandates status_code == 401 and
    error_code == UNAUTHORIZED.
    """
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        # Login — get first refresh cookie
        login_resp = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
        assert login_resp.status_code == 200
        old_cookie = login_resp.cookies.get("refresh_token")
        assert old_cookie is not None

        # First refresh — rotates the token; old cookie goes into the revocation store
        client.cookies.set("refresh_token", old_cookie)
        refresh1 = await client.post(AUTH_REFRESH)
        assert refresh1.status_code == 200

        # Second refresh attempt with the OLD cookie must be rejected.
        # Refresh1 mutated client.cookies to the rotated token; restore old.
        # spec: API.md §Application Error Codes — UNAUTHORIZED (401)
        client.cookies.set("refresh_token", old_cookie)
        refresh2 = await client.post(AUTH_REFRESH)
        assert refresh2.status_code == 401
        body = refresh2.json()
        assert body["error_code"] == "UNAUTHORIZED"
        # Message text is impl-defined; spec only mandates error_code == UNAUTHORIZED
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_revoke_without_cookie_is_noop(client: AsyncClient) -> None:
    """POST /auth/token/revoke with no cookie returns 204 and leaves Redis empty."""
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        revoke_resp = await client.post(AUTH_REVOKE)
        assert revoke_resp.status_code == 204
        assert fake_redis.is_empty()
    finally:
        app.dependency_overrides.pop(get_redis, None)


async def test_revoke_with_invalid_cookie_is_noop(client: AsyncClient) -> None:
    """POST /auth/token/revoke with a garbage cookie does not fail and does not write to Redis."""
    from src.api.dependencies import get_redis
    from src.api.main import app

    fake_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        client.cookies.set("refresh_token", "this.is.not.a.valid.jwt")
        revoke_resp = await client.post(AUTH_REVOKE)
        assert revoke_resp.status_code == 204
        assert fake_redis.is_empty()
    finally:
        app.dependency_overrides.pop(get_redis, None)


# ── Redis failure → 503 (fail-closed) ─────────────────────────────────────────


class _FailingRedis:
    """Redis fake where every call raises RedisError — simulates a dead cache."""

    async def get(self, key: str) -> str | None:
        import redis.exceptions
        raise redis.exceptions.RedisError("connection refused")

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 300) -> bool:
        import redis.exceptions
        raise redis.exceptions.RedisError("connection refused")


async def test_refresh_redis_failure_returns_503(client: AsyncClient) -> None:
    """When Redis raises RedisError during revocation check, refresh must return 503."""
    from src.api.dependencies import get_redis
    from src.api.main import app

    # Obtain a valid refresh cookie using a working fake first.
    working_redis = _FakeRedis()
    app.dependency_overrides[get_redis] = lambda: working_redis
    try:
        login_resp = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
        assert login_resp.status_code == 200
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie is not None
    finally:
        app.dependency_overrides.pop(get_redis, None)

    # Now swap in the failing Redis and attempt the refresh.
    app.dependency_overrides[get_redis] = lambda: _FailingRedis()
    try:
        client.cookies.set("refresh_token", refresh_cookie)
        refresh_resp = await client.post(AUTH_REFRESH)
        assert refresh_resp.status_code == 503
        body = refresh_resp.json()
        assert body["error_code"] == "STORAGE_UNAVAILABLE"
    finally:
        app.dependency_overrides.pop(get_redis, None)


# ── Stub-auth gate tests ───────────────────────────────────────────────────────


async def test_stub_auth_disabled_rejects_correct_admin_creds(
    client: AsyncClient, monkeypatch
) -> None:
    """With enable_stub_auth=False, POST /auth/token with correct admin creds → 401.

    Even valid credentials must be rejected when the stub-auth gate is off.
    This guards against accidentally enabling stub credentials in production.
    """
    import src.api.routers.auth as auth_mod
    import src.shared.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "enable_stub_auth", False)
    monkeypatch.setattr(auth_mod.settings, "enable_stub_auth", False)

    response = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
    assert response.status_code == 401
    body = response.json()
    assert body["error_code"] == "UNAUTHORIZED"


async def test_stub_auth_disabled_rejects_verify_credentials() -> None:
    """_verify_credentials returns False when enable_stub_auth is False."""
    import src.api.routers.auth as auth_mod
    import src.shared.settings as settings_mod

    original = settings_mod.settings.enable_stub_auth
    settings_mod.settings.enable_stub_auth = False
    auth_mod.settings.enable_stub_auth = False
    try:
        result = _verify_credentials("admin", "admin")
        assert result is False, "_verify_credentials should return False when stub auth is disabled"
    finally:
        settings_mod.settings.enable_stub_auth = original
        auth_mod.settings.enable_stub_auth = original


async def test_stub_auth_disabled_returns_empty_groups() -> None:
    """_get_user_groups returns [] when enable_stub_auth is False."""
    import src.api.routers.auth as auth_mod
    import src.shared.settings as settings_mod

    original = settings_mod.settings.enable_stub_auth
    settings_mod.settings.enable_stub_auth = False
    auth_mod.settings.enable_stub_auth = False
    try:
        groups = _get_user_groups("admin")
        assert groups == [], "_get_user_groups should return [] when stub auth is disabled"
    finally:
        settings_mod.settings.enable_stub_auth = original
        auth_mod.settings.enable_stub_auth = original


async def test_stub_auth_enabled_accepts_correct_admin_creds(client: AsyncClient) -> None:
    """With enable_stub_auth=True (from autouse fixture), POST /auth/token → 200.

    This is the positive counterpart to the disabled test: the autouse fixture
    already sets enable_stub_auth=True, so this test confirms that toggling the
    flag to True restores normal stub-auth behavior.
    """
    response = await client.post(AUTH_TOKEN, json={"email": "admin", "password": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
