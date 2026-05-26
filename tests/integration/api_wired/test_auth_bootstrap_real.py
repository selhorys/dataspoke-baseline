"""API-wired integration test: /internal/admin/bootstrap creates corpuser in DataHub.

Concerns covered:
- POST /internal/admin/bootstrap creates the bootstrap admin (dataspoke/dataspoke)
- The bootstrap admin's corpuser (urn:li:corpuser:dataspoke) exists in DataHub after bootstrap
- Bootstrap is idempotent — re-running returns created=False but leaves DataHub corpuser intact

spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence —
      bootstrap admin is mirrored to DataHub just like a registered user.
spec: plan §Bootstrap — first-admin bootstrap = built-in default dataspoke/dataspoke.
spec: spec/API.md §Internal — /internal/* routes gated by X-Internal-Token.
"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_bootstrap_admin_corpuser_exists_in_datahub(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    datahub_client,
) -> None:
    """POST /internal/admin/bootstrap: bootstrap admin's corpuser lands in DataHub.

    spec: spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence —
          every managed user, including the bootstrap admin, is mirrored into DataHub
          as a corpuser with the corpUserInfo aspect.
    spec: plan §Bootstrap — built-in default dataspoke/dataspoke.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass

    # Ensure bootstrap has run (idempotent — safe to call even if already done)
    bootstrap_resp = await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )
    assert bootstrap_resp.status_code == 200, (
        f"POST /internal/admin/bootstrap must return 200: {bootstrap_resp.text}"
    )
    body = bootstrap_resp.json()
    assert "created" in body, "bootstrap response must include 'created' boolean"

    # The bootstrap admin uses the special login name "dataspoke"
    # Per plan §Bootstrap, the corpuser URN is urn:li:corpuser:dataspoke
    corpuser_urn = "urn:li:corpuser:dataspoke"
    aspect = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)

    assert aspect is not None, (
        f"Bootstrap admin corpuser {corpuser_urn} must exist in DataHub after bootstrap "
        f"per spec/feature/AUTH.md §DataHub Mirror Semantics §Mirror create sequence. "
        f"Check that bootstrap correctly mirrors the admin user."
    )
    assert aspect.active is True, (
        "Bootstrap admin corpuser must have active=True in corpUserInfo aspect"
    )


@pytest.mark.asyncio
async def test_bootstrap_idempotent_corpuser_survives_second_call(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    datahub_client,
) -> None:
    """Two calls to /internal/admin/bootstrap: second returns created=False; corpuser still exists.

    spec: plan §Bootstrap — if any Admin already exists, returns {created: false} (idempotent no-op).
    spec: spec/feature/AUTH.md §DataHub Mirror Semantics — corpuser must not be deleted by idempotent call.
    """
    from datahub.metadata.schema_classes import CorpUserInfoClass

    # First call (may already be done)
    first = await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )
    assert first.status_code == 200, f"First bootstrap call failed: {first.text}"

    # Second call must be idempotent
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

    # DataHub corpuser must still exist after second call
    corpuser_urn = "urn:li:corpuser:dataspoke"
    aspect = await datahub_client.get_aspect(corpuser_urn, CorpUserInfoClass)
    assert aspect is not None, (
        f"Bootstrap admin corpuser {corpuser_urn} must still exist after idempotent bootstrap call "
        f"per spec/feature/AUTH.md §DataHub Mirror Semantics"
    )


@pytest.mark.asyncio
async def test_bootstrap_admin_can_login_after_bootstrap(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """After bootstrap, dataspoke/dataspoke credentials produce a valid access token.

    spec: plan §Bootstrap — built-in default dataspoke/dataspoke credentials.
    spec: spec/API.md §Auth — POST /auth/token issues access_token + sets refresh cookie.
    """
    # Ensure bootstrap has run
    await api_client.post(
        "/internal/admin/bootstrap",
        headers=internal_headers,
        content="{}",
    )

    login = await api_client.post(
        "/api/v1/auth/token",
        json={"email": "dataspoke", "password": "dataspoke"},
    )
    assert login.status_code == 200, (
        f"Bootstrap admin dataspoke/dataspoke must be able to login per plan §Bootstrap, "
        f"got {login.status_code}: {login.text}"
    )
    body = login.json()
    assert "access_token" in body, "Login response must include access_token"
    assert body.get("token_type") == "bearer", "token_type must be bearer"
