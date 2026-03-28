"""API-wired integration test fixtures.

Extends the root ``tests/integration/conftest.py`` (inherited automatically
by pytest).  Provides fixtures specific to REST-based testing so that spot
and story tests get a ready-to-use auth header dict without boilerplate.

API-wired tests assume a host-mode DataSpoke runtime is already running
**with test mode enabled** (``DATASPOKE_TEST_MODE=true``).  Start it via::

    ./dev_env/dataspoke-test-mode.sh --skip-migrate --no-reload &

The ``require_server`` fixture verifies three things at session start:

1. The server is running and healthy (``GET /health``).
2. ``DATASPOKE_TEST_MODE`` is set in the environment — without it, Kestra
   activity endpoints use real LLM/Qdrant/cache/notification clients, which
   will fail or produce non-deterministic results.
3. Required Kestra flows are registered (e.g., ``ingestion-config-sync``).
"""

import os

import httpx
import pytest

from tests.integration.conftest import _auth_headers

@pytest.fixture(scope="session", autouse=True)
def require_server():
    """Fail fast if the host-mode DataSpoke server is not running in test mode.

    Checks three conditions:
    1. Server liveness via ``GET /health``.
    2. ``DATASPOKE_TEST_MODE`` is set — without it, activity endpoints use
       real external clients and tests will fail non-deterministically.
    3. Required Kestra flows are registered (currently only
       ``ingestion-config-sync``).
    """
    # -- Check test mode env var --
    test_mode = os.environ.get("DATASPOKE_TEST_MODE", "").lower()
    if test_mode not in ("true", "1", "yes"):
        pytest.fail(
            "DATASPOKE_TEST_MODE is not set. API-wired tests require test-mode "
            "stubs (LLM, Qdrant, cache, notification). "
            "Start with: ./dev_env/dataspoke-test-mode.sh"
        )

    # -- Check server health --
    port = os.environ.get("DATASPOKE_API_PORT", "8000")
    try:
        resp = httpx.get(f"http://localhost:{port}/health", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        pytest.fail(
            f"DataSpoke server not running on localhost:{port}. "
            "Start with: ./dev_env/dataspoke-test-mode.sh"
        )

    # Verify ingestion flow is registered (the only flow registered at startup)
    kestra_url = os.environ.get("DATASPOKE_KESTRA_URL", "http://localhost:9205")
    kestra_ns = os.environ.get("DATASPOKE_KESTRA_NAMESPACE", "dataspoke")
    kestra_user = os.environ.get("DATASPOKE_KESTRA_USER", "")
    kestra_pass = os.environ.get("DATASPOKE_KESTRA_PASSWORD", "")

    auth = (kestra_user, kestra_pass) if kestra_user else None
    try:
        resp = httpx.get(
            f"{kestra_url}/api/v1/flows/search",
            params={"namespace": kestra_ns, "size": 100},
            auth=auth,
            timeout=10.0,
        )
        resp.raise_for_status()
        registered = {f["id"] for f in resp.json().get("results", [])}
    except Exception as exc:
        pytest.fail(
            f"Cannot query Kestra at {kestra_url}: {exc}"
        )

    if "ingestion-config-sync" not in registered:
        pytest.fail(
            "Kestra flow 'ingestion-config-sync' not registered. "
            "Restart with: ./dev_env/dataspoke-test-mode.sh"
        )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """JWT auth headers for API-wired test requests."""
    return _auth_headers()
