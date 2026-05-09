"""Api-wired integration test fixtures.

All api-wired tests hit the in-cluster API via nginx-ingress.
Prerequisites: `./dev_env/dataspoke-test-mode.sh --skip-build` must be running.
"""

import asyncio
import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import datahub as datahub_util
from tests.integration.util import dataspoke_db


def _ingress_url() -> str:
    """Return the ingress base URL, resolved at fixture time."""
    domain = os.environ["DATASPOKE_DEV_INGRESS_DOMAIN"]
    return f"http://app.{domain}"


@pytest.fixture(scope="session", autouse=True)
def require_server() -> None:
    """Assert that the test-mode API server is reachable and properly configured.

    Checks:
    1. DATASPOKE_TEST_MODE env var is set to 'true'.
    2. GET /health returns 200.
    3. POST /admin/dags/verify returns 200 (using admin JWT auth).

    If any check fails, the test session is aborted with a clear message.
    """
    test_mode = os.environ.get("DATASPOKE_TEST_MODE", "")
    if test_mode.lower() != "true":
        pytest.fail(
            "DATASPOKE_TEST_MODE is not set to 'true'. "
            "Run: set -a && source dev_env/.env && set +a && "
            "DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/ "
            "(set -a is required — dev_env/.env has no `export` prefixes, so a bare "
            "`source` does not propagate vars to the pytest subprocess) "
            "or start the server with: ./dev_env/dataspoke-test-mode.sh --skip-build"
        )

    base_url = _ingress_url()

    # Check liveness — /health has no /api/v1 prefix (mounted at root)
    try:
        resp = httpx.get(f"{base_url}/health", timeout=10.0)
        if resp.status_code != 200:
            pytest.fail(
                f"GET /health returned {resp.status_code}. "
                "Server not running? Try: ./dev_env/dataspoke-test-mode.sh --skip-build"
            )
    except httpx.ConnectError as exc:
        pytest.fail(
            f"Cannot connect to API at {base_url}: {exc}. "
            "Try: ./dev_env/dataspoke-test-mode.sh --skip-build"
        )

    # Obtain admin token and verify DAGs
    try:
        token_resp = httpx.post(
            f"{base_url}/api/v1/auth/token",
            json={"email": "admin", "password": "admin"},
            timeout=10.0,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
    except Exception as exc:
        pytest.fail(f"Cannot obtain admin token: {exc}")

    try:
        verify_resp = httpx.post(
            f"{base_url}/api/v1/admin/dags/verify",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        if verify_resp.status_code != 200:
            pytest.fail(
                f"POST /admin/dags/verify returned {verify_resp.status_code}: {verify_resp.text}. "
                "DAGs may not be registered. Try: ./dev_env/dataspoke-test-mode.sh --skip-build"
            )
    except Exception as exc:
        pytest.fail(f"POST /admin/dags/verify failed: {exc}")

    yield  # type: ignore[misc]


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient]:
    """Function-scoped async HTTP client pointing at the ingress URL."""
    base_url = _ingress_url()
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def admin_token(require_server) -> str:  # noqa: ARG001
    """Session-scoped admin JWT access token."""
    base_url = _ingress_url()
    resp = httpx.post(
        f"{base_url}/api/v1/auth/token",
        json={"email": "admin", "password": "admin"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token: str) -> dict[str, str]:
    """Session-scoped Authorization header dict for admin user."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def internal_headers() -> dict[str, str]:
    """Session-scoped X-Internal-Token header dict for internal routes."""
    token = os.environ["DATASPOKE_INTERNAL_TOKEN"]
    return {"X-Internal-Token": token}


@pytest.fixture(autouse=True)
def purge_urns(request: pytest.FixtureRequest) -> None:
    """Hard-purge DataSpoke state for URNs declared by the test module.

    A test module opts in by declaring `URNS_TO_PURGE: list[str]` at module
    level. For each URN we:
      1. Hard-delete rows from every `dataspoke.*` operational table.
      2. Hard-delete every DataSpoke-emitted DataHub assertion attached to it
         (including its assertionRunEvent timeseries).

    Lets a single test re-run cleanly after an aborted prior run without
    requiring a full `reset-all`.
    """
    urns: list[str] = getattr(request.module, "URNS_TO_PURGE", [])
    if not urns:
        return
    for urn in urns:
        asyncio.run(dataspoke_db.purge_urn(urn))
        datahub_util.hard_delete_dataspoke_assertions_for_dataset(urn)
