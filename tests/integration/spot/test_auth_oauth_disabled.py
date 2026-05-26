"""Spot integration test: Google OAuth disabled behavior.

Concerns covered:
- When DATASPOKE_GOOGLE_OAUTH_CLIENT_ID is empty (dev default),
  GET /auth/google/login returns 503 OAUTH_NOT_CONFIGURED
- GET /auth/google/callback also returns 503 OAUTH_NOT_CONFIGURED

spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
when google_oauth_client_id is empty, both routes return 503 OAUTH_NOT_CONFIGURED.
"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_google_login_returns_503_when_oauth_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/login returns 503 OAUTH_NOT_CONFIGURED in dev env (no OAuth credentials).

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth — if settings.google_oauth_client_id
    is empty, both routes return 503 OAUTH_NOT_CONFIGURED.
    """
    resp = await api_client.get("/api/v1/auth/google/login")

    assert resp.status_code == 503, (
        f"GET /auth/google/login must return 503 OAUTH_NOT_CONFIGURED when OAuth is not configured "
        f"per spec/feature/AUTH.md §Lifecycle §Google OAuth, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "OAUTH_NOT_CONFIGURED", (
        "Error code must be OAUTH_NOT_CONFIGURED per spec/feature/AUTH.md §Lifecycle §Google OAuth"
    )


@pytest.mark.asyncio
async def test_google_callback_returns_503_when_oauth_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/callback returns 503 OAUTH_NOT_CONFIGURED in dev env.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth — both login and callback routes
    return 503 OAUTH_NOT_CONFIGURED when google_oauth_client_id is empty.
    """
    resp = await api_client.get(
        "/api/v1/auth/google/callback",
        params={"code": "fake_code", "state": "fake_state"},
    )

    assert resp.status_code == 503, (
        f"GET /auth/google/callback must return 503 OAUTH_NOT_CONFIGURED when OAuth is not configured "
        f"per spec/feature/AUTH.md §Lifecycle §Google OAuth, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "OAUTH_NOT_CONFIGURED"
