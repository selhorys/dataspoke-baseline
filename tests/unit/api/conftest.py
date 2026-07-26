"""Shared test fixtures for DataSpoke API unit tests."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.backend.auth.tokens import issue_access_token

_STATE_ATTRS = ("datahub", "redis", "vector", "airflow")

# Stable test user UUID used as the default authenticated principal.
# The mock DB session in the client fixture returns this user for any
# scalar_one_or_none() call, so JWTs produced by make_token() resolve
# to the Admin context through require_authenticated.
_TEST_USER_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")

# Session epoch carried by both the mocked principal row and the JWTs minted by
# make_token().  The bearer path compares the token's ``ses`` claim against
# ``users.session_epoch`` and rejects a mismatch (spec/feature/AUTH.md §Session
# epoch), so the two must agree for a fixture token to authenticate at all.
TEST_SESSION_EPOCH = 0


def _make_mock_user(role: str = "Admin") -> MagicMock:
    """Return a MagicMock shaped like a User ORM row."""
    u = MagicMock()
    u.id = _TEST_USER_ID
    u.email = "unit-test@example.com"
    u.name = "Unit Test User"
    u.role = role
    u.google_sub = None
    u.session_epoch = TEST_SESSION_EPOCH
    return u


async def capture_event_route_sql(client: AsyncClient, url: str) -> tuple[int, list[str]]:
    """Drive a DB-direct ``event`` route; return its status and its events queries' SQL.

    Several ``event`` routes build their query inline instead of delegating to a service
    (``src/api/routers/spoke/metagen.py`` ×2, ``src/api/routers/spoke/common/data/metagen.py``
    ×1), so there is no mockable service call to assert a filter against — the query itself
    is the only observable. This override yields a query-routing session that records every
    statement, and returns the ones touching ``dataspoke.events`` (the count query and the
    rows query) compiled to PostgreSQL SQL with literal binds, newest call last.

    Callers assert on the returned SQL, so a param the handler declares but never reads is
    visible as a missing predicate. See ``tests/unit/conftest.py`` ``compiled_sql`` for the
    rendering (and its documented fallback when a bound value will not inline).
    """
    from src.api.dependencies import get_db
    from tests.unit.conftest import route_db_execute

    captured: list[str] = []

    def _capture(sql: str) -> bool:
        """Record every statement, then decline so the routes below supply the result."""
        captured.append(sql)
        return False

    count_result = MagicMock()
    count_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []
    auth_result = MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user()

    mock_db = AsyncMock()
    # Route by SQL: auth user-lookup (users), the events count(), then the events rows.
    route_db_execute(
        mock_db,
        [(_capture, None), ("users", auth_result), ("count(", count_result)],
        default=rows_result,
    )

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        resp = await client.get(url, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    return resp.status_code, [sql for sql in captured if "dataspoke.events" in sql]


@pytest.fixture
async def client() -> AsyncClient:
    """Async HTTP client backed by the ASGI app — no running server needed.

    The ASGI transport does not run the app's lifespan, so ``app.state``
    is empty by default. We pre-populate it with harmless MagicMocks.

    Two baseline overrides prevent real infrastructure connections:
      - ``get_db``    → async generator yielding a mock session. The session's
                        ``execute()`` always returns a MagicMock whose
                        ``scalar_one_or_none()`` returns a mock Admin user, so
                        ``require_authenticated`` can resolve any JWT produced
                        by ``make_token()`` / ``auth_headers()``.
      - ``get_redis`` → AsyncMock (revocation checks return None = not revoked).

    Tests that check "no token → 401" still work: ``require_authenticated``
    raises ``AuthenticationError`` before touching the DB when ``credentials``
    is None.

    Tests that override ``get_db`` internally (e.g. to supply specific query
    results) MUST also include an entry that satisfies the user-lookup query
    performed by ``require_authenticated`` if they send an auth header.

    The mocks are torn down after each test so they don't leak across modules.
    """
    from src.api.dependencies import get_db, get_redis
    from src.backend.admin.config_service import (
        RUNTIME_CONFIG_DEFAULTS,
        RuntimeConfigDTO,
        get_runtime_config,
    )

    for attr in _STATE_ATTRS:
        setattr(app.state, attr, MagicMock(name=attr))

    mock_user = _make_mock_user(role="Admin")

    # Return mock_user for scalar_one_or_none (user lookup), 0 for scalar
    # (count queries), and [] for scalars().all() (list queries).
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_result.scalar.return_value = 0
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # not revoked

    async def _mock_db():
        yield mock_session

    default_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)

    app.dependency_overrides[get_db] = _mock_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_runtime_config] = lambda: default_rc

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_runtime_config, None)
        for attr in _STATE_ATTRS:
            if hasattr(app.state, attr):
                delattr(app.state, attr)


def make_token(subject: str | None = None, session_epoch: int = TEST_SESSION_EPOCH) -> str:
    """Create a real (signed) access token.

    ``subject`` must be a UUID string; a random UUID is generated when omitted.
    The token sub carries the UUID and no role claim — the principal's role is
    resolved from the DB row in ``require_authenticated`` (see
    ``src/backend/auth/privilege.py``). Tests that exercise role-gated endpoints
    set the role on the mocked ``User`` (or override ``require_authenticated`` /
    ``require_admin``) at their own call site.

    ``session_epoch`` rides on the token as the ``ses`` claim and defaults to the
    epoch the mocked principal row carries, so the bearer path's epoch gate
    passes. Pass a different value to exercise a superseded session.
    """
    user_id = uuid.UUID(subject) if subject else uuid.uuid4()
    token, _ = issue_access_token(user_id, f"{user_id}@test.example.com", session_epoch)
    return token


def auth_headers(subject: str | None = None) -> dict[str, str]:
    """Return an Authorization header dict for a freshly signed access token.

    The JWT carries no role or groups claim; authorization derives from the DB
    role of the resolved principal. The ``client`` fixture's DB mock returns an
    Admin ``User`` by default — tests needing a different role set it on that
    mocked ``User`` or override ``require_authenticated`` / ``require_admin``.
    """
    return {"Authorization": f"Bearer {make_token(subject=subject)}"}
