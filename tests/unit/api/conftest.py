"""Shared test fixtures for DataSpoke API unit tests."""

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.auth.jwt import create_access_token
from src.api.main import app


_STATE_ATTRS = ("datahub", "redis", "vector", "llm", "airflow")


@pytest.fixture
async def client() -> AsyncClient:
    """Async HTTP client backed by the ASGI app — no running server needed.

    The ASGI transport does not run the app's lifespan, so ``app.state``
    is empty by default. We pre-populate it with harmless MagicMocks so
    providers like ``get_redis(request)`` succeed; tests that exercise
    a specific client should still use ``app.dependency_overrides``.

    The mocks are torn down after each test so they don't leak across
    modules and silently mask a missing dependency override.
    """
    for attr in _STATE_ATTRS:
        setattr(app.state, attr, MagicMock(name=attr))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac
    finally:
        for attr in _STATE_ATTRS:
            if hasattr(app.state, attr):
                delattr(app.state, attr)


def make_token(groups: list[str], subject: str = "test-user") -> str:
    """Create a real (signed) access token for the given groups."""
    token, _ = create_access_token(subject=subject, groups=groups, email=f"{subject}@test.com")
    return token


def auth_headers(groups: list[str], subject: str = "test-user") -> dict[str, str]:
    """Return Authorization header dict for the given groups."""
    return {"Authorization": f"Bearer {make_token(groups, subject)}"}
