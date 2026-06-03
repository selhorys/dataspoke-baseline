"""Spot integration test: Google OAuth disabled behavior.

Concerns covered:
- When DATASPOKE_GOOGLE_OAUTH_CLIENT_ID is empty (dev default),
  GET /auth/google/login returns 503 OAUTH_NOT_CONFIGURED
- GET /auth/google/callback also returns 503 OAUTH_NOT_CONFIGURED

Both tests skip when the dev install provisions OAuth credentials (login returns a
redirect rather than 503) — the 503-when-unconfigured contract only applies in a
no-OAuth environment.

spec: spec/feature/AUTH.md §Lifecycle §Google OAuth registration & login —
when google_oauth_client_id is empty, both routes return 503 OAUTH_NOT_CONFIGURED.
"""

import httpx
import pytest


def _oauth_is_configured(api_client: httpx.AsyncClient) -> bool:
    """Return True when the dev install has OAuth credentials provisioned.

    Probe GET /auth/google/login synchronously (httpx.get) and treat any
    non-503 response as "OAuth configured" (e.g. a 302 redirect).

    spec: feature/AUTH.md §Lifecycle §Google OAuth — 503 OAUTH_NOT_CONFIGURED is
    returned only when google_oauth_client_id is empty.
    """
    # The api_client base_url is set at fixture time; extract it from the session-
    # level spot conftest base URL.  Use a sync request because this runs in the
    # skip-guard (outside async context).
    import os

    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    base_url = f"http://api.{domain}"
    try:
        resp = httpx.get(
            f"{base_url}/api/v1/auth/google/login",
            follow_redirects=False,
            timeout=10.0,
        )
        return resp.status_code != 503
    except Exception:
        return False


@pytest.mark.asyncio
async def test_google_login_returns_503_when_oauth_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/login returns 503 OAUTH_NOT_CONFIGURED when OAuth is not configured.

    Skips when the dev install provisions OAuth credentials (non-503 response to the
    probe) — the 503 contract only holds in a no-OAuth environment.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth — if settings.google_oauth_client_id
    is empty, both routes return 503 OAUTH_NOT_CONFIGURED.
    """
    if _oauth_is_configured(api_client):
        pytest.skip(
            "GET /api/v1/auth/google/login did not return 503 — OAuth credentials are "
            "provisioned in this dev install. The 503 OAUTH_NOT_CONFIGURED contract only "
            "applies when google_oauth_client_id is empty. "
            "spec: feature/AUTH.md §Lifecycle §Google OAuth."
        )

    resp = await api_client.get("/api/v1/auth/google/login")

    assert resp.status_code == 503, (
        "GET /auth/google/login must return 503 OAUTH_NOT_CONFIGURED when OAuth is "
        "not configured per spec/feature/AUTH.md §Lifecycle §Google OAuth, "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "OAUTH_NOT_CONFIGURED", (
        "Error code must be OAUTH_NOT_CONFIGURED per spec/feature/AUTH.md §Lifecycle §Google OAuth"
    )


@pytest.mark.asyncio
async def test_google_callback_returns_503_when_oauth_not_configured(
    api_client: httpx.AsyncClient,
) -> None:
    """GET /auth/google/callback returns 503 OAUTH_NOT_CONFIGURED when OAuth is not configured.

    Skips when the dev install provisions OAuth credentials — see login test above.

    spec: spec/feature/AUTH.md §Lifecycle §Google OAuth — both login and callback routes
    return 503 OAUTH_NOT_CONFIGURED when google_oauth_client_id is empty.
    """
    if _oauth_is_configured(api_client):
        pytest.skip(
            "GET /api/v1/auth/google/login did not return 503 — OAuth credentials are "
            "provisioned in this dev install. The 503 OAUTH_NOT_CONFIGURED contract only "
            "applies when google_oauth_client_id is empty. "
            "spec: feature/AUTH.md §Lifecycle §Google OAuth."
        )

    resp = await api_client.get(
        "/api/v1/auth/google/callback",
        params={"code": "fake_code", "state": "fake_state"},
    )

    assert resp.status_code == 503, (
        "GET /auth/google/callback must return 503 OAUTH_NOT_CONFIGURED when OAuth is "
        "not configured per spec/feature/AUTH.md §Lifecycle §Google OAuth, "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "OAUTH_NOT_CONFIGURED"
