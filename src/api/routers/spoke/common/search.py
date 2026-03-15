from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_search_service
from src.api.schemas.search import ReindexResponse, SearchResponse, SearchResultItem, SqlContext
from src.backend.search.service import SearchService
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/search",
    tags=["common/search"],
    dependencies=[Depends(require_common)],
)


@router.get("", response_model=SearchResponse)
async def get_search(
    q: str = Query(..., min_length=1),
    sql_context: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    result = await service.search(q=q, sql_context=sql_context, offset=offset, limit=limit)

    datasets = [
        SearchResultItem(
            urn=d["urn"],
            name=d["name"],
            platform=d["platform"],
            description=d.get("description"),
            tags=d.get("tags", []),
            owners=d.get("owners", []),
            quality_score=d.get("quality_score"),
            score=d["score"],
            sql_context=SqlContext(**d["sql_context"]) if d.get("sql_context") else None,
        )
        for d in result["datasets"]
    ]

    return SearchResponse(
        datasets=datasets,
        offset=result["offset"],
        limit=result["limit"],
        total_count=result["total_count"],
    )


@router.post("/method/reindex", response_model=ReindexResponse)
async def post_search_reindex(
    dataset_urn: str = Query(..., min_length=1),
    service: SearchService = Depends(get_search_service),
) -> ReindexResponse:
    try:
        result = await service.reindex(dataset_urn=dataset_urn)
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset '{dataset_urn}' not found in DataHub",
        )
    return ReindexResponse(status=result["status"], message=result["message"])
