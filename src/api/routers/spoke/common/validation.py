from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth.dependencies import require_common

router = APIRouter(
    prefix="/validation",
    tags=["common/validation"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("")
async def list_validation_configs() -> None:
    raise _501


@router.get("/{dataset_urn}")
async def get_validation_config(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr")
async def get_validation_config_attr(dataset_urn: str) -> None:
    raise _501


@router.patch("/{dataset_urn}/attr")
async def patch_validation_config_attr(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/attr/result")
async def get_validation_result(dataset_urn: str) -> None:
    raise _501


@router.post("/{dataset_urn}/method/run")
async def run_validation(dataset_urn: str) -> None:
    raise _501


@router.get("/{dataset_urn}/event")
async def get_validation_events(dataset_urn: str) -> None:
    raise _501
