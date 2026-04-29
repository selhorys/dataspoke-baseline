from fastapi import APIRouter, Depends

from src.api.auth.dependencies import require_dg
from src.api.dependencies import get_overview_service
from src.api.schemas.overview import (
    GraphEdgeResponse,
    GraphNodeResponse,
    MedallionSummaryResponse,
    OntologyGraphResponse,
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
    """Retrieve the governance overview snapshot."""
    snapshot = await service.get_overview()
    return OverviewSnapshotResponse(
        metric_values=snapshot.metric_values,
        per_dataset_breakdown=snapshot.per_dataset_breakdown,
        blind_spots=snapshot.blind_spots,
        ontology_graph=OntologyGraphResponse(
            nodes=[
                GraphNodeResponse(
                    id=n.id,
                    type=n.type,
                    label=n.label,
                    metadata=n.metadata,
                )
                for n in snapshot.ontology_graph.nodes
            ],
            edges=[
                GraphEdgeResponse(
                    source=e.source,
                    target=e.target,
                    type=e.type,
                    metadata=e.metadata,
                )
                for e in snapshot.ontology_graph.edges
            ],
        ),
        medallion=MedallionSummaryResponse(
            bronze=snapshot.medallion.bronze,
            silver=snapshot.medallion.silver,
            gold=snapshot.medallion.gold,
        ),
        ownership_topology=snapshot.ownership_topology,
    )


@router.get("/attr", response_model=OverviewResponse)
async def get_overview_attr(
    service: OverviewService = Depends(get_overview_service),
) -> OverviewResponse:
    """Retrieve the persisted display configuration (layout, color scheme, filters) for the overview."""
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
    """Partially update the overview display configuration."""
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
