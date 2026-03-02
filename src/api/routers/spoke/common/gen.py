from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/gen",
    tags=["common/gen"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def list_gen_configs() -> None:
    raise _501


@router.get("/{dataset_urn}")
async def get_gen_config(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr")
async def get_gen_config_attr(dataset_urn: str) -> None:
    raise _501


@router.patch("/{dataset_urn}/attr")
async def patch_gen_config_attr(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/result")
async def get_gen_result(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/method/generate")
async def run_gen(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/method/apply")
async def apply_gen(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/event")
async def get_gen_events(dataset_urn: str) -> None:
    raise _501
