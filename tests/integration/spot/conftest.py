"""Spot integration test fixtures.

All spot tests hit the in-cluster API via nginx-ingress. Prerequisites:
`./helm-charts/bin/install.sh --profile dev --components api --skip-build` must be running.

The server/auth fixtures (`require_server`, `admin_token`, `admin_headers`,
`internal_headers`) are inherited from the parent `tests/integration/conftest.py`.
"""

import os
from collections.abc import AsyncGenerator

import httpx
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
