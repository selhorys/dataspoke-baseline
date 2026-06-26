"""Api-wired integration test fixtures.

All api-wired tests hit the in-cluster API via nginx-ingress. Prerequisites:
`./helm-charts/bin/install.sh --profile dev --components api --skip-build` must be running.

The server/auth fixtures (`require_server`, `admin_token`, `admin_headers`,
`internal_headers`) are inherited from the parent `tests/integration/conftest.py`.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Iterator

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import datahub as datahub_util
from tests.integration.util import dataspoke_db


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


@pytest.fixture(autouse=True)
def purge_urns(request: pytest.FixtureRequest) -> Iterator[None]:
    """Hard-purge DataSpoke state for URNs declared by the test module.

    A test module opts in by declaring `URNS_TO_PURGE: list[str]` at module
    level. For each URN we:
      1. Hard-delete rows from every `dataspoke.*` operational table.
      2. Hard-delete every DataSpoke-emitted DataHub assertion attached to it
         (including its assertionRunEvent timeseries).

    Runs both before and after the test — the pre-purge lets a single test
    re-run cleanly after an aborted prior run; the post-purge keeps the dev
    DataHub UI free of soft-deleted leftovers (an in-test `DELETE` only sets
    `status.removed=true`, leaving the assertion entity and its run events).
    """
    urns: list[str] = getattr(request.module, "URNS_TO_PURGE", [])
    if not urns:
        yield
        return
    for urn in urns:
        asyncio.run(dataspoke_db.purge_urn(urn))
        datahub_util.hard_delete_dataspoke_assertions_for_dataset(urn)
    yield
    for urn in urns:
        asyncio.run(dataspoke_db.purge_urn(urn))
        datahub_util.hard_delete_dataspoke_assertions_for_dataset(urn)
