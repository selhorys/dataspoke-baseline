"""Spot tests for DataHub pass-through proxy endpoints.

Concerns covered:
- POST /hub/graphql — { me { corpUser { username } } } returns 200 with body shape
- GET /hub/openapi/{path} — DataHub REST OpenAPI surface is reachable via proxy (200)
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_hub_graphql_proxy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /hub/graphql proxies a trivial introspection query to DataHub GMS.

    spec: API.md §DataHub Pass-Through — POST /hub/graphql proxies DataHub GraphQL queries.
    The DataSpoke API validates the JWT and forwards to DataHub with the configured
    datahub_token. In test-mode with a valid datahub_token the GraphQL 'me' query
    must return 200 with body['data']['me']['corpUser'] present.
    A 401/403 from DataHub indicates a token forwarding problem (misconfigured
    DATASPOKE_TEST_DATAHUB_TOKEN), which is surfaced as a test failure.
    A 502 means DataHub GMS is unreachable — infra failure.
    """
    resp = await api_client.post(
        "/api/v1/hub/graphql",
        headers={**admin_headers, "content-type": "application/json"},
        json={"query": "{ me { corpUser { username } } }"},
    )

    # 502 means DataHub GMS is unreachable — infra must be healthy per TESTING.md §Prerequisites
    assert resp.status_code != 502, (
        f"DataHub GMS unreachable (502) — run ./helm-charts/bin/health-check.sh and reinstall "
        f"datahub subsystem if needed. Response: {resp.text}"
    )

    # spec: API.md §DataHub Pass-Through — proxy returns DataHub's response verbatim
    # In test-mode with a valid datahub_token, DataHub returns 200 with GraphQL body shape.
    assert resp.status_code == 200, (
        f"Expected 200 from DataHub GraphQL proxy. "
        f"401/403 indicates a token forwarding problem (check DATASPOKE_TEST_DATAHUB_TOKEN). "
        f"Status: {resp.status_code}, Body: {resp.text}"
    )

    body = resp.json()
    # DataHub GraphQL response must contain 'data' key with 'me.corpUser' shape
    assert "data" in body, f"GraphQL response must contain 'data' key, got: {body}"
    assert "me" in body["data"], (
        f"GraphQL 'me' field missing from response data: {body['data']}"
    )
    assert "corpUser" in body["data"]["me"], (
        f"GraphQL 'me.corpUser' field missing: {body['data']['me']}"
    )


@pytest.mark.asyncio
async def test_hub_openapi_proxy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /hub/openapi/{path} proxies to DataHub GMS REST OpenAPI surface — expects 200.

    spec: API.md §DataHub Pass-Through — GET /hub/openapi/{path} proxies DataHub REST
    OpenAPI endpoints (all methods). The DataHub GMS entity list endpoint at
    /openapi/v3/entity/dataset is a lightweight probe that returns 200 when DataHub is
    healthy and the datahub_token is configured correctly.
    A 502 means DataHub GMS is unreachable — infra failure.
    A 401/403 indicates a token forwarding problem (misconfigured DATASPOKE_TEST_DATAHUB_TOKEN).
    """
    # Use the DataHub v3 entity list endpoint — publicly accessible via the REST OpenAPI surface
    resp = await api_client.get(
        "/api/v1/hub/openapi/v3/entity/dataset",
        params={"count": "1"},
        headers=admin_headers,
    )

    # 502 means DataHub GMS is unreachable — infra must be healthy per TESTING.md §Prerequisites
    assert resp.status_code != 502, (
        f"DataHub GMS unreachable (502) — run ./helm-charts/bin/health-check.sh. "
        f"Response: {resp.text}"
    )

    # spec: API.md §DataHub Pass-Through — proxy returns DataHub's response verbatim
    # In test-mode with a valid datahub_token, this endpoint must return 200.
    assert resp.status_code == 200, (
        f"Expected 200 from DataHub REST OpenAPI proxy at /openapi/v3/entity/dataset. "
        f"401/403 indicates a token forwarding problem (check DATASPOKE_TEST_DATAHUB_TOKEN). "
        f"Status: {resp.status_code}, Body: {resp.text[:200]}"
    )
