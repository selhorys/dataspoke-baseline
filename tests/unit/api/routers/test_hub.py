"""Unit tests for the DataHub pass-through proxy routes.

The hub router now reads the DataHub connection from peripheral_config (DB-backed)
and the K8s secret, not from settings.  All tests stub out both
``get_peripheral_config`` and ``get_datahub_token`` at the module boundary in
``src.api.routers.hub`` to avoid real DB/K8s calls in unit tests.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient

from src.api.dependencies import get_db
from src.api.main import app
from src.backend.admin.peripheral_service import DatahubConfigDTO
from tests.unit.api.conftest import auth_headers

_DE_HEADERS = auth_headers(groups=["de"])
_GRAPHQL_URL = "/api/v1/hub/graphql"
_OPENAPI_BASE = "/api/v1/hub/openapi"

_FAKE_GMS_URL = "http://gms-test:8080"
_FAKE_TOKEN = "dh-test-token"
_FAKE_DTO = DatahubConfigDTO(gms_url=_FAKE_GMS_URL, kafka_brokers="kafka-test:9092")


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


def _db_override():
    """Dependency override that yields a mock AsyncSession."""
    db = AsyncMock()
    result = AsyncMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    async def _gen():
        yield db

    return _gen


@pytest.fixture(autouse=True)
def _stub_datahub_connection():
    """Stub out peripheral_config read and token read for every hub test.

    The hub router calls ``get_peripheral_config(db, "datahub")`` and
    ``get_datahub_token()`` inside ``_get_datahub_connection()``, both are
    imported lazily inside the function body.  Since lazy imports resolve
    from the source module at call time, we patch at the source module level.

    We also override ``get_db`` so tests do not open a real DB connection.
    """
    app.dependency_overrides[get_db] = _db_override()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=_FAKE_DTO),
        ),
        patch(
            "src.backend.admin.datahub_secret.get_datahub_token",
            new=lambda: _FAKE_TOKEN,
        ),
    ):
        yield
    app.dependency_overrides.pop(get_db, None)


# ── GraphQL proxy ─────────────────────────────────────────────────────────────


async def test_graphql_proxy_forwards_request(client: AsyncClient) -> None:
    """GraphQL proxy forwards POST to DataHub GMS /api/graphql.

    spec: API.md §Hub routes — /hub/graphql proxies to DataHub GMS.
    """
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
    """GraphQL proxy injects the DataHub token from peripheral_config into upstream headers.

    spec: API.md §Hub routes — token forwarded from DB-backed peripheral config.
    """
    mock_resp = _mock_response()
    with patch("src.api.routers.hub.httpx.AsyncClient") as mock_cls:
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
    # _FAKE_TOKEN is injected by the autouse fixture via get_datahub_token stub
    assert forwarded_headers["authorization"] == f"Bearer {_FAKE_TOKEN}"


async def test_graphql_proxy_handles_datahub_error(client: AsyncClient) -> None:
    """GraphQL proxy forwards DataHub 500 responses unchanged.

    spec: API.md §Hub routes — proxy passes DataHub error responses through.
    """
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
    """GraphQL proxy returns 502 DATAHUB_UNAVAILABLE on connection error.

    spec: API.md §Hub routes — connect error maps to DATAHUB_UNAVAILABLE.
    """
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
    """GraphQL proxy returns 502 DATAHUB_UNAVAILABLE on timeout.

    spec: API.md §Hub routes — timeout maps to DATAHUB_UNAVAILABLE.
    """
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
    """OpenAPI proxy forwards GET to DataHub GMS OpenAPI surface.

    spec: API.md §Hub routes — /hub/openapi/{path} proxies to DataHub OpenAPI.
    """
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
    """OpenAPI proxy forwards POST to DataHub GMS OpenAPI surface.

    spec: API.md §Hub routes — /hub/openapi/{path} proxies POST.
    """
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
    """OpenAPI proxy appends query parameters to the upstream URL.

    spec: API.md §Hub routes — query params forwarded verbatim.
    """
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
    """OpenAPI proxy forwards the upstream status code unchanged.

    spec: API.md §Hub routes — DataHub status codes forwarded as-is.
    """
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
    """GraphQL proxy returns 401 for unauthenticated requests.

    spec: API.md §Authentication — hub routes require valid JWT.
    """
    resp = await client.post(_GRAPHQL_URL, content=b"{}")
    assert resp.status_code == 401


async def test_hub_openapi_requires_auth(client: AsyncClient) -> None:
    """OpenAPI proxy returns 401 for unauthenticated requests.

    spec: API.md §Authentication — hub routes require valid JWT.
    """
    resp = await client.get(f"{_OPENAPI_BASE}/v3/entity/dataset")
    assert resp.status_code == 401
