from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/hub",
    tags=["hub"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail="DataHub pass-through proxy is not yet implemented.",
)


@router.post("/graphql")
async def hub_graphql(_request: Request) -> None:
    # TODO: proxy to DATASPOKE_DATAHUB_GMS_URL/api/graphql
    raise _501


@router.api_route("/openapi/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def hub_openapi(_request: Request, path: str) -> None:
    # TODO: proxy to DATASPOKE_DATAHUB_GMS_URL/openapi/{path}
    raise _501
