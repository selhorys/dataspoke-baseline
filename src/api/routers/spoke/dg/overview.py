from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_dg
from src.api.dependencies import get_overview_service
from src.api.schemas.overview import (
    GraphEdgeResponse,
    GraphNodeResponse,
    MedallionSummaryResponse,
    OverviewResponse,
    OverviewSnapshotResponse,
    PatchOverviewRequest,
)
from src.backend.overview.service import OverviewService

router = APIRouter(
    prefix="/overview",
    tags=["dg/overview"],
    dependencies=[Depends(require_dg)],
)


@router.get("", response_model=OverviewSnapshotResponse)
async def get_overview(
    service: OverviewService = Depends(get_overview_service),
) -> OverviewSnapshotResponse:
    snapshot = await service.get_overview()
    return OverviewSnapshotResponse(
        nodes=[
            GraphNodeResponse(id=n.id, type=n.type, label=n.label, metadata=n.metadata)
            for n in snapshot.nodes
        ],
        edges=[
            GraphEdgeResponse(source=e.source, target=e.target, type=e.type, metadata=e.metadata)
            for e in snapshot.edges
        ],
        medallion=MedallionSummaryResponse(
            bronze=snapshot.medallion.bronze,
            silver=snapshot.medallion.silver,
            gold=snapshot.medallion.gold,
        ),
        blind_spots=snapshot.blind_spots,
        stats={
            "total_datasets": snapshot.stats.total_datasets,
            "monitored_datasets": snapshot.stats.monitored_datasets,
            "avg_quality_score": snapshot.stats.avg_quality_score,
            "issues_count": snapshot.stats.issues_count,
        },
    )


@router.get("/attr", response_model=OverviewResponse)
async def get_overview_attr(
    service: OverviewService = Depends(get_overview_service),
) -> OverviewResponse:
    config = await service.get_config()
    return OverviewResponse(
        layout=config.layout,
        color_by=config.color_by,
        filters=config.filters,
    )


@router.patch("/attr", response_model=OverviewResponse)
async def patch_overview_attr(
    body: PatchOverviewRequest,
    service: OverviewService = Depends(get_overview_service),
) -> OverviewResponse:
    config = await service.patch_config(
        layout=body.layout,
        color_by=body.color_by,
        filters=body.filters,
    )
    return OverviewResponse(
        layout=config.layout,
        color_by=config.color_by,
        filters=config.filters,
    )
