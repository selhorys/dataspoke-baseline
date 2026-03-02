from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth.dependencies import require_dg

router = APIRouter(
    prefix="/overview",
    tags=["dg/overview"],
    dependencies=[Depends(require_dg)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def get_overview() -> None:
    raise _501


@router.get("/attr")
async def get_overview_attr() -> None:
    raise _501


@router.patch("/attr")
async def patch_overview_attr() -> None:
    raise _501
