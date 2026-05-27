"""Cross-dataset ingestion list view — /spoke/ingestion.

Per-dataset operations (attr CRUD, method/run, event) live under the
canonical /spoke/common/data/{dataset_urn}/... surface.

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Ingestion (/spoke/ingestion).
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_authenticated
from src.api.dependencies import get_ingestion_service
from src.api.schemas.common import parse_sort
from src.api.schemas.ingestion import IngestionConfigListResponse, IngestionConfigResponse
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import IngestionConfig

router = APIRouter(
    prefix="/ingestion",
    tags=["ingestion"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("", response_model=IngestionConfigListResponse)
async def get_ingestion_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigListResponse:
    """List ingestion attributes across datasets (paginated, filterable).

    Each row combines dataset identity with the ingestion attributes stored
    under common/data/{dataset_urn}/attr/ingestion/*.
    """
    order_by = parse_sort(sort, {"created_at": IngestionConfig.created_at}, None)
    configs, total = await service.list_configs(offset, limit, status_filter, order_by=order_by)
    return IngestionConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        configs=[IngestionConfigResponse.model_validate(c) for c in configs],
    )
