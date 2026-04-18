"""Cross-dataset ingestion list and detail views.

Per-dataset operations (attr CRUD, method/run, event) live under the
canonical /spoke/common/data/{dataset_urn}/attr/ingestion/ surface.
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_ingestion_service
from src.api.schemas.common import parse_sort
from src.api.schemas.ingestion import IngestionConfigListResponse, IngestionConfigResponse
from src.api.schemas.mappers import ingestion_config_response
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import IngestionConfig
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/ingestion",
    tags=["common/ingestion"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=IngestionConfigListResponse)
async def get_ingestion_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigListResponse:
    """List ingestion configs with optional status filter and pagination."""
    order_by = parse_sort(sort, {"created_at": IngestionConfig.created_at}, None)
    configs, total = await service.list_configs(offset, limit, status_filter, order_by=order_by)
    return IngestionConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        configs=[ingestion_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=IngestionConfigResponse)
async def get_ingestion_config(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Retrieve a single ingestion config by dataset URN."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return ingestion_config_response(config)
