"""Cross-dataset metadata generation list view — /spoke/common/metagen.

A single route: GET /spoke/common/metagen — paginated list of the latest
metagen result per dataset. Replaces the deleted gen.py.

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: require_common.
Spec: API.md §Metadata Generation (/spoke/common/metagen).
"""

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_metagen_service
from src.api.schemas.metagen import MetagenListItem, MetagenListResponse
from src.backend.metagen.service import MetagenService

router = APIRouter(
    prefix="/metagen",
    tags=["common/metagen"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=MetagenListResponse)
async def get_metagen_list(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service: MetagenService = Depends(get_metagen_service),
) -> MetagenListResponse:
    """List metadata generation attributes across all datasets.

    Each row aggregates the per-dataset metagen state (latest result +
    field_status).  Use the canonical
    /data/{dataset_urn}/attr/metagen/* surface for per-dataset detail.
    """
    results, total = await service.list_metagen(offset=offset, limit=limit)
    return MetagenListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        results=[
            MetagenListItem(
                dataset_urn=r.dataset_urn,
                run_id=r.run_id,
                proposals=r.proposals,
                field_status=r.field_status,
                generated_at=r.generated_at,
                last_reviewed_at=r.last_reviewed_at,
            )
            for r in results
        ],
    )
