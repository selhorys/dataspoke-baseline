"""Integration-layer "auth headers for a principal" utility.

``login_headers`` performs a live ``POST /api/v1/auth/token`` against a running
API server and returns a Bearer ``Authorization`` header for the named principal.
This is the integration layer's single mechanism for obtaining auth headers.

Three auth-header mechanisms exist across the test suite and are deliberately
kept separate — do NOT try to unify them, they target different substrates:

  - unit (offline JWT sign): ``tests/unit/api/conftest.py:make_token`` signs a JWT
    in-process and relies on a mock DB row to resolve the principal — no server,
    no real DB.
  - integration (live server): this helper logs in to a running API and returns a
    real, server-minted access token.
  - api-token (opaque PAT): the api-token DB-seed path persists an opaque personal
    access token to the DB — an opaque PAT, not a short-lived JWT.

Each speaks to a different substrate (mock-DB vs live-server vs opaque PAT), so
they stay as three separate paths.
"""

import httpx


def login_headers(base_url: str, email: str, password: str) -> dict[str, str]:
    """Log in via ``POST {base_url}/api/v1/auth/token`` and return a Bearer header.

    Returns ``{"Authorization": "Bearer <access_token>"}`` for the given
    principal. Raises ``httpx.HTTPStatusError`` if the login fails.
    """
    resp = httpx.post(
        f"{base_url}/api/v1/auth/token",
        json={"email": email, "password": password},
        timeout=10.0,
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
