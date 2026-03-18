from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_ingestion_service, get_kestra_client
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    IngestionConfigListResponse,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import Event, IngestionConfig
from src.shared.exceptions import EntityNotFoundError
from src.workflows._common import urn_to_workflow_id
from src.workflows.kestra.client import KestraClient

router = APIRouter(
    prefix="/ingestion",
    tags=["common/ingestion"],
    dependencies=[Depends(require_common)],
)


def _config_response(c) -> IngestionConfigResponse:  # noqa: ANN001
    return IngestionConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),
        dataset_urn=c.dataset_urn,
        sources=c.sources,
        deep_spec_enabled=c.deep_spec_enabled,
        schedule=c.schedule,
        status=c.status,
        owner=c.owner,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=IngestionConfigListResponse)
async def get_ingestion_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigListResponse:
    order_by = parse_sort(sort, {"created_at": IngestionConfig.created_at}, None)
    configs, total = await service.list_configs(offset, limit, status_filter, order_by=order_by)
    return IngestionConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        configs=[_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=IngestionConfigResponse)
async def get_ingestion_config(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return _config_response(config)


@router.get("/{dataset_urn}/attr", response_model=IngestionConfigResponse)
async def get_ingestion_config_attr(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return _config_response(config)


@router.patch("/{dataset_urn}/attr", response_model=IngestionConfigResponse)
async def patch_ingestion_config_attr(
    dataset_urn: str,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _config_response(config)


@router.post("/{dataset_urn}/method/run", response_model=RunResultResponse)
async def post_ingestion_run(
    dataset_urn: str,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
    kestra: KestraClient = Depends(get_kestra_client),
) -> RunResultResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    label_value = f"ingestion-{urn_to_workflow_id(dataset_urn)}"
    await kestra.check_no_duplicate(
        "ingestion", "workflow_id", label_value, "INGESTION_RUNNING"
    )
    import uuid

    execution = await kestra.trigger_and_wait(
        "ingestion",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": dataset_urn,
            "dry_run": str(body.dry_run).lower(),
            "run_id": str(uuid.uuid4()),
        },
        labels={"workflow_id": label_value},
    )
    outputs = execution.outputs or {}
    return RunResultResponse(
        run_id=outputs.get("run_id", execution.id),
        status=outputs.get("status", execution.status.value),
        detail=outputs.get("detail", {}),
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_ingestion_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events(
        dataset_urn, offset, limit, from_time, to_time, order_by=order_by
    )
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=e["id"],
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e["detail"],
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )
