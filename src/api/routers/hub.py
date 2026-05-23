"""DataHub pass-through proxy routes.

Forwards requests to DataHub GMS without wrapping responses — clients
receive DataHub's native JSON/GraphQL payloads.
"""

import httpx
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_db
from src.shared.exceptions import DataHubUnavailableError, StorageUnavailableError

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


async def _get_datahub_connection(db: AsyncSession) -> tuple[str, str]:
    """Return (gms_url, token) from peripheral_config.

    Raises StorageUnavailableError (→ 503) when unconfigured.
    """
    from src.backend.admin.datahub_secret import get_datahub_token
    from src.backend.admin.peripheral_service import get_peripheral_config

    dto = await get_peripheral_config(db, "datahub")
    token = get_datahub_token()
    # Must match the predicate in src/api/dependencies.py get_datahub — both guards
    # must stay in sync: dto present AND non-empty token required.
    if dto is None or not token:
        raise StorageUnavailableError("datahub peripheral not configured")
    return dto.gms_url, token


def _build_upstream_headers(request: Request, token: str) -> dict[str, str]:
    """Build headers for the upstream DataHub request.

    Strips hop-by-hop headers and the caller's Authorization (DataSpoke auth
    is already validated).  Injects the DataHub service token when configured.
    """
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "authorization"
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
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
async def hub_graphql(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Proxy GraphQL queries to DataHub GMS."""
    gms_url, token = await _get_datahub_connection(db)
    body = await request.body()
    headers = _build_upstream_headers(request, token)
    headers["content-type"] = "application/json"

    return await _proxy(
        "POST",
        f"{gms_url}/api/graphql",
        headers=headers,
        content=body,
        params="",
    )


@router.api_route(
    "/openapi/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def hub_openapi(
    request: Request,
    path: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Proxy REST requests to DataHub GMS OpenAPI surface."""
    gms_url, token = await _get_datahub_connection(db)
    body = await request.body()
    headers = _build_upstream_headers(request, token)

    return await _proxy(
        request.method,
        f"{gms_url}/openapi/{path}",
        headers=headers,
        content=body,
        params=request.url.query,
    )
