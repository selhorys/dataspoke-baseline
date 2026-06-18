"""Cross-dataset validation list view — GET /spoke/validation.

Per-dataset operations (attr CRUD, result, event) live under the
canonical /spoke/common/data/{dataset_urn}/... surface.

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Validation (/spoke/validation).
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_authenticated
from src.api.dependencies import get_validation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.validation import ValidationListItem, ValidationListResponse
from src.backend.validation.service import ValidationService
from src.shared.db.models import ValidationConfig

router = APIRouter(
    prefix="/validation",
    tags=["validation"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("", response_model=ValidationListResponse)
async def get_validation(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    removed: bool | None = Query(default=None),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationListResponse:
    """List validation attributes across datasets (paginated, filterable by removed status).

    Each row aggregates per-dataset attr/validation/* (conf description + variable count
    + latest result data_time and score).  Default ordering: updated_at DESC.
    """
    order_by = parse_sort(sort, {"updated_at": ValidationConfig.updated_at}, None)
    items, total_count = await service.list_configs(
        offset=offset,
        limit=limit,
        removed_filter=removed,
        order_by=order_by,
    )
    return ValidationListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        validations=[ValidationListItem.model_validate(item) for item in items],
    )
