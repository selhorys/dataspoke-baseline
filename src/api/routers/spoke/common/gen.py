"""Cross-dataset generation list and detail views.

Per-dataset operations (attr CRUD, result, method/generate, method/apply, event)
live under the canonical /spoke/common/data/{dataset_urn}/attr/gen/ surface.
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_generation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.generation import GenerationConfigListResponse, GenerationConfigResponse
from src.backend.generation.service import GenerationService
from src.shared.db.models import GenerationConfig
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/gen",
    tags=["common/gen"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=GenerationConfigListResponse)
async def get_gen_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigListResponse:
    """List generation configs with optional status filter and pagination."""
    order_by = parse_sort(sort, {"created_at": GenerationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, status_filter=status_filter, order_by=order_by
    )
    return GenerationConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        configs=[GenerationConfigResponse.model_validate(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=GenerationConfigResponse)
async def get_gen_config(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    """Retrieve a single generation config by dataset URN."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    return GenerationConfigResponse.model_validate(config)
