"""Spot integration test fixtures.

All spot tests hit the in-cluster API via nginx-ingress.
Prerequisites: `./helm-charts/bin/install.sh --profile dev --components api --skip-build` must be running.
"""

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio


def _ingress_url() -> str:
    """Return the ingress base URL, resolved at fixture time."""
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    return f"http://app.{domain}"


@pytest.fixture(scope="session", autouse=True)
def require_server(runtime_conf) -> None:  # noqa: ARG001 — runtime_conf performs stub preflight
    """Assert that the API server is reachable, three of four stub fields (LLM excluded) are true via runtime_conf preflight, and DAGs are registered.

    Checks:
    1. GET /api/v1/admin/conf confirms stub_redis_client, stub_pgvector_manager, stub_notification_service are true (delegated to runtime_conf); stub_llm_client is intentionally excluded so real-LLM tests can run.
    2. GET /health returns 200.
    3. POST /admin/dags/verify returns 200 (using admin JWT auth).

    If any check fails, the test session is aborted with a clear message.
    """
    base_url = _ingress_url()

    # Check liveness — /health has no /api/v1 prefix (mounted at root)
    try:
        resp = httpx.get(f"{base_url}/health", timeout=10.0)
        if resp.status_code != 200:
            pytest.fail(
                f"GET /health returned {resp.status_code}. "
                "Server not running? Try: ./helm-charts/bin/install.sh --profile dev --components api --skip-build"
            )
    except httpx.ConnectError as exc:
        pytest.fail(
            f"Cannot connect to API at {base_url}: {exc}. "
            "Try: ./helm-charts/bin/install.sh --profile dev --components api --skip-build"
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
                "DAGs may not be registered. Try: ./helm-charts/bin/install.sh --profile dev --components api --skip-build"
            )
    except Exception as exc:
        pytest.fail(f"POST /admin/dags/verify failed: {exc}")

    yield  # type: ignore[misc]


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient, None]:
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
    token = os.environ["DATASPOKE_TEST_INTERNAL_TOKEN"]
    return {"X-Internal-Token": token}
