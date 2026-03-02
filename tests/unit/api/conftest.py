"""Shared test fixtures for DataSpoke API unit tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.auth.jwt import create_access_token
from src.api.main import app


@pytest.fixture
async def client() -> AsyncClient:
    """Async HTTP client backed by the ASGI app — no running server needed."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


def make_token(groups: list[str], subject: str = "test-user") -> str:
    """Create a real (signed) access token for the given groups."""
    token, _ = create_access_token(subject=subject, groups=groups, email=f"{subject}@test.com")
    return token


def auth_headers(groups: list[str], subject: str = "test-user") -> dict[str, str]:
    """Return Authorization header dict for the given groups."""
    return {"Authorization": f"Bearer {make_token(groups, subject)}"}
