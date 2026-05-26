"""Spot integration test: /internal/admin/bootstrap idempotency.

Concerns covered:
- First call to /internal/admin/bootstrap returns created=True (or is already done — created=False)
- Second consecutive call returns created=False (idempotency)
- Bootstrap endpoint requires X-Internal-Token header (401 without it)

spec: plan §Bootstrap — first-admin bootstrap = built-in default dataspoke/dataspoke.
spec: spec/API.md §Internal — /internal/* routes gated by X-Internal-Token.
"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """Two consecutive calls to /internal/admin/bootstrap: first may create, second always returns created=False.

    spec: plan §Bootstrap — if no Admin exists, creates the bootstrap admin (created=True);
    if any Admin already exists, returns 200 with {created: false} (idempotent no-op).
    """
    # First call — may already have been bootstrapped; both cases are valid
    first = await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )
    assert first.status_code == 200, f"First bootstrap call must return 200: {first.text}"
    first_body = first.json()
    assert "created" in first_body, "bootstrap response must include 'created' boolean"
    assert isinstance(first_body["created"], bool)

    # Second call must always return created=False (Admin exists from first call or prior)
    second = await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )
    assert second.status_code == 200, f"Second bootstrap call must return 200: {second.text}"
    assert second.json()["created"] is False, (
        "Second bootstrap call must return created=False (idempotent) "
        "per plan §Bootstrap"
    )


@pytest.mark.asyncio
async def test_bootstrap_requires_internal_token(
    api_client: httpx.AsyncClient,
) -> None:
    """/internal/admin/bootstrap without X-Internal-Token returns 401.

    spec: spec/API.md §Internal — /internal/* gated by X-Internal-Token shared-secret header.
    """
    resp = await api_client.post(
        "/internal/admin/bootstrap",
        # No X-Internal-Token — must be rejected
        content="{}",
    )
    assert resp.status_code == 401, (
        f"/internal/admin/bootstrap without X-Internal-Token must return 401 "
        f"per spec/API.md §Internal, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_bootstrap_admin_can_login(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """After bootstrap, dataspoke/dataspoke credentials work for login.

    spec: plan §Bootstrap — built-in default dataspoke/dataspoke (parallel to DataHub's datahub/datahub).
    """
    # Ensure bootstrap has run
    await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )

    # Login as bootstrap admin
    login = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "dataspoke", "password": "dataspoke"},
    )
    assert login.status_code == 200, (
        f"Bootstrap admin dataspoke/dataspoke must be able to login per plan §Bootstrap, "
        f"got {login.status_code}: {login.text}"
    )
    assert "access_token" in login.json()
