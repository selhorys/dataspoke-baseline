"""Cross-dataset validation list view — /spoke/common/validation.

Per-dataset operations (attr CRUD, result, method/run, event) live under the
canonical /spoke/common/data/{dataset_urn}/... surface.

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Validation (/spoke/common/validation).
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_validation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.validation import ValidationConfigListResponse, ValidationConfigResponse
from src.backend.validation.service import ValidationService
from src.shared.db.models import ValidationConfig

router = APIRouter(
    prefix="/validation",
    tags=["common/validation"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=ValidationConfigListResponse)
async def get_validation_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    is_enabled_filter: bool | None = Query(default=None, alias="is_enabled"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigListResponse:
    """List validation attributes across datasets (paginated, filterable by is_enabled).

    Each row aggregates the per-dataset attr/validation/* (conf and latest result).
    """
    order_by = parse_sort(sort, {"created_at": ValidationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, is_enabled_filter=is_enabled_filter, order_by=order_by
    )
    return ValidationConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        configs=[ValidationConfigResponse.model_validate(c) for c in configs],
    )
