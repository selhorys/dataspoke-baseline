from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/search",
    tags=["common/search"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def search(q: str = "", sql_context: bool = False) -> None:
    raise _501


@router.post("/method/reindex")
async def reindex(dataset_urn: str = "") -> None:
    raise _501
