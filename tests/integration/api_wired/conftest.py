"""API-wired integration test fixtures.

Extends the root ``tests/integration/conftest.py`` (inherited automatically
by pytest).  Provides fixtures specific to REST-based testing so that spot
and story tests get a ready-to-use auth header dict without boilerplate.

API-wired tests assume the in-cluster DataSpoke API is deployed and
accessible via nginx-ingress **with test mode enabled** (``DATASPOKE_TEST_MODE=true``).
Start it via::

    ./dev_env/dataspoke-test-mode.sh

The ``require_server`` fixture verifies three things at session start:

1. The server is running and healthy (``GET /health``).
2. ``DATASPOKE_TEST_MODE`` is set in the environment — without it, Airflow
   activity endpoints use real LLM/Qdrant/cache/notification clients, which
   will fail or produce non-deterministic results.
3. Expected Airflow DAGs are loaded (verified via ``GET /api/v2/dags``).
"""

import os

import httpx
import pytest

from tests.integration.conftest import _auth_headers

# DAGs that must be loaded in Airflow for the in-cluster API to function
_REQUIRED_DAG_IDS = frozenset({
    "ingestion",
    "generation",
    "metrics",
    "embedding-sync",
    "ontology-rebuild",
})


@pytest.fixture(scope="session", autouse=True)
def require_server():
    """Fail fast if the in-cluster DataSpoke API is not running in test mode.

    Checks three conditions:
    1. Server liveness via ``GET /health``.
    2. ``DATASPOKE_TEST_MODE`` is set — without it, activity endpoints use
       real external clients and tests will fail non-deterministically.
    3. Required Airflow DAGs are loaded.
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
    domain = os.environ.get("DATASPOKE_DEV_INGRESS_DOMAIN", "")
    api_base = (
        f"http://app.{domain}"
        if domain
        else f"http://localhost:{os.environ.get('DATASPOKE_API_PORT', '8002')}"
    )
    try:
        resp = httpx.get(f"{api_base}/health", timeout=5.0)
        resp.raise_for_status()
    except Exception:
        pytest.fail(
            f"DataSpoke API not reachable at {api_base}. "
            "Deploy with: ./dev_env/dataspoke-test-mode.sh"
        )

    # -- Verify Airflow DAGs are loaded --
    airflow_url = os.environ.get("DATASPOKE_AIRFLOW_URL", "http://localhost:8080")
    airflow_user = os.environ.get("DATASPOKE_AIRFLOW_USER", "")
    airflow_pass = os.environ.get("DATASPOKE_AIRFLOW_PASSWORD", "")

    auth = (airflow_user, airflow_pass) if airflow_user else None
    try:
        resp = httpx.get(
            f"{airflow_url}/api/v2/dags",
            params={"limit": 100},
            auth=auth,
            timeout=10.0,
        )
        resp.raise_for_status()
        loaded_ids = {d["dag_id"] for d in resp.json().get("dags", [])}
    except Exception as exc:
        pytest.fail(f"Cannot query Airflow at {airflow_url}: {exc}")

    missing = _REQUIRED_DAG_IDS - loaded_ids
    if missing:
        pytest.fail(
            f"Airflow DAGs not loaded: {', '.join(sorted(missing))}. "
            "Ensure the in-cluster API is deployed: ./dev_env/dataspoke-test-mode.sh"
        )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """JWT auth headers for API-wired test requests."""
    return _auth_headers()
