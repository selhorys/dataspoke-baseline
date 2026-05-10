"""Unit tests for the DataHub pass-through proxy routes."""

from unittest.mock import AsyncMock, patch

import httpx
from httpx import AsyncClient

from tests.unit.api.conftest import auth_headers

_DE_HEADERS = auth_headers(groups=["de"])
_GRAPHQL_URL = "/api/v1/hub/graphql"
_OPENAPI_BASE = "/api/v1/hub/openapi"


def _mock_response(
    status_code: int = 200,
    content: bytes = b'{"data": "ok"}',
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response."""
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers or {"content-type": "application/json"},
    )


# ── GraphQL proxy ─────────────────────────────────────────────────────────────


async def test_graphql_proxy_forwards_request(client: AsyncClient) -> None:
    mock_resp = _mock_response(content=b'{"data":{"listDatasets":[]}}')
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _GRAPHQL_URL,
            content=b'{"query":"{ listDatasets { total } }"}',
            headers={**_DE_HEADERS, "content-type": "application/json"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"data": {"listDatasets": []}}
    mock_client.request.assert_called_once()
    call_kwargs = mock_client.request.call_args
    assert call_kwargs[0][0] == "POST"
    assert "/api/graphql" in call_kwargs[0][1]


async def test_graphql_proxy_forwards_datahub_token(client: AsyncClient) -> None:
    mock_resp = _mock_response()
    with (
        patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls,
        patch("src.api.routers.hub.settings") as mock_settings,
    ):
        mock_settings.datahub_gms_url = "http://gms:8080"
        mock_settings.datahub_token = "dh-secret-token"
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await client.post(
            _GRAPHQL_URL,
            content=b"{}",
            headers=_DE_HEADERS,
        )

    call_kwargs = mock_client.request.call_args
    forwarded_headers = call_kwargs[1]["headers"]
    assert forwarded_headers["authorization"] == "Bearer dh-secret-token"


async def test_graphql_proxy_handles_datahub_error(client: AsyncClient) -> None:
    mock_resp = _mock_response(status_code=500, content=b'{"error":"internal"}')
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _GRAPHQL_URL,
            content=b"{}",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 500
    assert resp.json() == {"error": "internal"}


async def test_graphql_proxy_handles_connect_error(client: AsyncClient) -> None:
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.side_effect = httpx.ConnectError("Connection refused")
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _GRAPHQL_URL,
            content=b"{}",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "DATAHUB_UNAVAILABLE"


async def test_graphql_proxy_handles_timeout(client: AsyncClient) -> None:
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.side_effect = httpx.TimeoutException("timed out")
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            _GRAPHQL_URL,
            content=b"{}",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "DATAHUB_UNAVAILABLE"


# ── OpenAPI proxy ─────────────────────────────────────────────────────────────


async def test_openapi_proxy_get(client: AsyncClient) -> None:
    mock_resp = _mock_response(content=b'{"entities":[]}')
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(
            f"{_OPENAPI_BASE}/v3/entity/dataset",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 200
    call_kwargs = mock_client.request.call_args
    assert call_kwargs[0][0] == "GET"
    assert "/openapi/v3/entity/dataset" in call_kwargs[0][1]


async def test_openapi_proxy_post(client: AsyncClient) -> None:
    mock_resp = _mock_response(status_code=201, content=b'{"urn":"urn:li:dataset:1"}')
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.post(
            f"{_OPENAPI_BASE}/v3/entity/dataset",
            content=b'{"aspect":"value"}',
            headers={**_DE_HEADERS, "content-type": "application/json"},
        )

    assert resp.status_code == 201
    call_kwargs = mock_client.request.call_args
    assert call_kwargs[0][0] == "POST"


async def test_openapi_proxy_preserves_query_params(client: AsyncClient) -> None:
    mock_resp = _mock_response()
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(
            f"{_OPENAPI_BASE}/v3/entity/dataset?type=dataset&count=10",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 200
    call_kwargs = mock_client.request.call_args
    target_url = call_kwargs[0][1]
    assert "type=dataset" in target_url
    assert "count=10" in target_url


async def test_openapi_proxy_preserves_status_code(client: AsyncClient) -> None:
    mock_resp = _mock_response(status_code=404, content=b'{"error":"not found"}')
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(
            f"{_OPENAPI_BASE}/v3/entity/dataset/unknown",
            headers=_DE_HEADERS,
        )

    assert resp.status_code == 404


# ── Auth requirement ──────────────────────────────────────────────────────────


async def test_hub_graphql_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(_GRAPHQL_URL, content=b"{}")
    assert resp.status_code == 401


async def test_hub_openapi_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(f"{_OPENAPI_BASE}/v3/entity/dataset")
    assert resp.status_code == 401
