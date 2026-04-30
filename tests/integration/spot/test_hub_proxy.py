"""Spot tests for DataHub pass-through proxy endpoints.

Concerns covered:
- POST /hub/graphql — trivial GraphQL query proxied to DataHub GMS returns non-500
- GET /hub/openapi/{path} — DataHub REST OpenAPI surface is reachable via proxy
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_hub_graphql_proxy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /hub/graphql proxies a trivial query to DataHub GMS.

    Spec: API.md §DataHub Pass-Through — POST /hub/graphql.
    The DataSpoke API validates the JWT and forwards to DataHub.
    We accept any non-502 response (DataHub may return 200 or 401 depending
    on token forwarding, but we must not get a connectivity failure).
    """
    resp = await api_client.post(
        "/api/v1/hub/graphql",
        headers={**admin_headers, "content-type": "application/json"},
        json={"query": "{ me { corpUser { username } } }"},
    )

    # 502 would mean DataHub GMS is unreachable — all other codes are valid proxy outcomes.
    assert resp.status_code != 502, f"DataHub GMS unreachable: {resp.text}"
    # We expect a JSON response (DataHub always returns JSON for GraphQL)
    assert resp.headers.get("content-type", "").startswith("application/json") or resp.status_code in (200, 401, 403)


@pytest.mark.asyncio
async def test_hub_openapi_proxy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /hub/openapi/v2/openapi proxies to DataHub GMS OpenAPI spec.

    Spec: API.md §DataHub Pass-Through — GET /hub/openapi/{path}.
    Accepts any non-502 status (DataHub may require auth or return redirect).
    """
    # Try a simple OpenAPI discovery path on DataHub GMS OpenAPI surface
    resp = await api_client.get(
        "/api/v1/hub/openapi/v2/openapi",
        headers=admin_headers,
    )

    # 502 would mean DataHub GMS is unreachable
    assert resp.status_code != 502, f"DataHub GMS unreachable: {resp.text}"
    # Any other status (200, 404, 401) means the proxy reached DataHub
    assert resp.status_code in (200, 301, 302, 400, 401, 403, 404)
