"""Spot integration test fixtures.

All spot tests hit the in-cluster API via nginx-ingress. Prerequisites:
`./helm-charts/bin/install.sh --profile dev --components api --skip-build` must be running.

The server/auth fixtures (`require_server`, `admin_token`, `admin_headers`,
`internal_headers`) are inherited from the parent `tests/integration/conftest.py`.
"""

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio


def _ingress_url() -> str:
    """Return the ingress base URL, resolved at fixture time."""
    domain = os.environ["DATASPOKE_KUBE_INGRESS_DOMAIN"]
    return f"http://api.{domain}"


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient]:
    """Function-scoped async HTTP client pointing at the ingress URL."""
    base_url = _ingress_url()
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        yield client


@pytest.fixture
def silence_api_health_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``IngestionService._report_api_health`` a no-op for this test.

    Opt in with ``pytestmark = pytest.mark.usefixtures("silence_api_health_report")`` from
    any module that drives ``IngestionService.sync()`` **in-process without owning the
    ``datahub-api`` health row**.

    Two reasons, and both matter:

    1. **Isolation of the in-process sweeps.** ``sync()`` writes the ``datahub-api``
       ``peripheral_health`` row as a side effect. That row is dev-cluster-wide singleton
       state, so a module that drives several sweeps and has no snapshot/restore around them
       would leave it holding whatever its last sweep produced. Only
       ``test_datahub_api_health.py`` owns that row — it snapshots and verifiably restores it
       — so every other sweep-driving module silences its own **in-process** sweeps instead.
    2. **Readable failures.** The reporter opens the module-level
       ``src.shared.db.session.SessionLocal``, which resolves to ``localhost:5432`` outside
       the cluster. The write therefore fails, is swallowed by design, and logs a ~40-line
       ``OSError: Connect call failed ('127.0.0.1', 5432)`` traceback per sweep. On a green
       run that is invisible; on a failing one it occupies the output tail and hides the
       assertion that actually failed.

    Patched at the ``IngestionService`` method rather than at ``SessionLocal`` so a real
    ``SessionLocal`` user elsewhere in the same test is unaffected.

    **Scope limit — this fixture cannot silence a REST-driven sweep.** ``monkeypatch`` binds
    in the *pytest* process. ``test_internal_activities.py`` also drives the sweep by POSTing
    ``/internal/activities/ingestion/sync`` (10 call sites), which executes inside the API pod
    where this patch does not exist, so that module still moves the singleton row. Two
    consequences, both accepted rather than worked around:

    - Nothing here becomes vacuous. No assertion in any opted-in module reads
      ``peripheral_health``, so a stray write cannot make one of their checks pass falsely;
      only reason 2 (log noise) is fully delivered for those calls, and reason 1 is delivered
      only for the in-process half.
    - The row is left dirty at session end. Files run in name order, so
      ``test_datahub_api_health.py`` sorts *before* ``test_internal_activities.py``: its
      verified restore does happen, and is then clobbered by the later REST-driven sweeps.
      A test that needs a known ``datahub-api`` row must therefore establish it itself
      (``test_datahub_api_health.py`` does — it snapshots and asserts its own restore) rather
      than assume a clean baseline from this fixture.
    """
    from src.backend.ingestion.service import IngestionService

    async def _noop(self: IngestionService, status: str, error: str | None = None) -> None:
        return None

    monkeypatch.setattr(IngestionService, "_report_api_health", _noop)
