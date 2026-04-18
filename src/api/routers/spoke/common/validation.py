"""Cross-dataset validation list and detail views.

Per-dataset operations (attr CRUD, result, method/run, event) live under the
canonical /spoke/common/data/{dataset_urn}/attr/validation/ surface.
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_validation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.mappers import validation_config_response
from src.api.schemas.validation import ValidationConfigListResponse, ValidationConfigResponse
from src.backend.validation.service import ValidationService
from src.shared.db.models import ValidationConfig
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/validation",
    tags=["common/validation"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=ValidationConfigListResponse)
async def get_validation_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    is_active_filter: bool | None = Query(default=None, alias="is_active"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigListResponse:
    """List validation configs with optional active filter and pagination.

    Returns all validation configs across datasets. Use the `is_active` query
    parameter to filter by scheduling status. Sort by `created_at_asc` or
    `created_at_desc`.
    """
    order_by = parse_sort(sort, {"created_at": ValidationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, is_active_filter=is_active_filter, order_by=order_by
    )
    return ValidationConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        configs=[validation_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=ValidationConfigResponse)
async def get_validation_config(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    """Retrieve a single validation config by dataset URN.

    Returns the complete config including all rules, schedule, owner, and
    timestamps. Returns 404 if no config exists for the given URN.
    """
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    return validation_config_response(config)
