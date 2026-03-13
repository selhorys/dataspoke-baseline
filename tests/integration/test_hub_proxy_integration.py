"""Integration tests for hub proxy against real DataHub GMS.

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
"""

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from .conftest import _auth_headers, _datahub_gms_url, _resolve_datahub_token


@pytest_asyncio.fixture
async def http_client():
    """HTTP client with settings patched to point at dev-env DataHub."""
    from src.api.main import app

    token = _resolve_datahub_token()
    if not token:
        pytest.skip("Cannot obtain DataHub token (frontend unreachable)")

    with patch("src.api.routers.hub.settings") as mock_settings:
        mock_settings.datahub_gms_url = _datahub_gms_url
        mock_settings.datahub_token = token

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


_HEADERS = _auth_headers()
_GRAPHQL_URL = "/api/v1/hub/graphql"
_OPENAPI_BASE = "/api/v1/hub/openapi"


@pytest.mark.asyncio
async def test_graphql_proxy_queries_datahub(http_client):
    resp = await http_client.post(
        _GRAPHQL_URL,
        json={"query": "{ listDatasets(input: {start: 0, count: 1}) { total datasets { urn } } }"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body


@pytest.mark.asyncio
async def test_graphql_proxy_returns_datahub_errors(http_client):
    resp = await http_client.post(
        _GRAPHQL_URL,
        json={"query": "{ invalidField }"},
        headers=_HEADERS,
    )
    # DataHub returns 200 with errors array for invalid GraphQL
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" in body


@pytest.mark.asyncio
async def test_openapi_proxy_list_entities(http_client):
    resp = await http_client.get(
        f"{_OPENAPI_BASE}/v3/entity/dataset",
        headers=_HEADERS,
        params={"count": 1},
    )
    # DataHub v3 entity API returns 200
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openapi_proxy_404_for_unknown_entity(http_client):
    resp = await http_client.get(
        f"{_OPENAPI_BASE}/v3/entity/dataset/urn%3Ali%3Adataset%3A(urn%3Ali%3AdataPlatform%3Anone,nonexistent.table,PROD)",
        headers=_HEADERS,
    )
    # DataHub returns 404 for non-existent entities
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_hub_auth_required(http_client):
    resp = await http_client.post(
        _GRAPHQL_URL,
        json={"query": "{ __typename }"},
    )
    assert resp.status_code == 401
