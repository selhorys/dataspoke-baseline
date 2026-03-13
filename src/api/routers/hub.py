"""DataHub pass-through proxy routes.

Forwards requests to DataHub GMS without wrapping responses — clients
receive DataHub's native JSON/GraphQL payloads.
"""

import httpx
from fastapi import APIRouter, Depends, Request, Response

from src.api.auth.dependencies import require_common
from src.api.config import settings
from src.shared.exceptions import DataHubUnavailableError

router = APIRouter(
    prefix="/hub",
    tags=["hub"],
    dependencies=[Depends(require_common)],
)

_PROXY_TIMEOUT = 30.0

# Headers that must not be forwarded between hops (RFC 2616 §13.5.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _build_upstream_headers(request: Request) -> dict[str, str]:
    """Build headers for the upstream DataHub request.

    Strips hop-by-hop headers and the caller's Authorization (DataSpoke auth
    is already validated).  Injects the DataHub service token when configured.
    """
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "authorization"
    }
    if settings.datahub_token:
        headers["authorization"] = f"Bearer {settings.datahub_token}"
    return headers


def _filter_response_headers(response: httpx.Response) -> dict[str, str]:
    """Return only safe response headers (drop hop-by-hop)."""
    return {k: v for k, v in response.headers.items() if k.lower() not in _HOP_BY_HOP}


async def _proxy(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: bytes,
    params: str,
) -> Response:
    """Send request to DataHub and return the raw response."""
    target = f"{url}?{params}" if params else url
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            resp = await client.request(
                method,
                target,
                content=content,
                headers=headers,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise DataHubUnavailableError(f"DataHub GMS unreachable: {exc}") from exc

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_filter_response_headers(resp),
    )


@router.post("/graphql")
async def hub_graphql(request: Request) -> Response:
    """Proxy GraphQL queries to DataHub GMS."""
    body = await request.body()
    headers = _build_upstream_headers(request)
    headers["content-type"] = "application/json"

    return await _proxy(
        "POST",
        f"{settings.datahub_gms_url}/api/graphql",
        headers=headers,
        content=body,
        params="",
    )


@router.api_route(
    "/openapi/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def hub_openapi(request: Request, path: str) -> Response:
    """Proxy REST requests to DataHub GMS OpenAPI surface."""
    body = await request.body()
    headers = _build_upstream_headers(request)

    return await _proxy(
        request.method,
        f"{settings.datahub_gms_url}/openapi/{path}",
        headers=headers,
        content=body,
        params=request.url.query,
    )
