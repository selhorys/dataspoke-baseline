from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_generation_service, get_kestra_client
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.generation import (
    ApplyGenerationRequest,
    GenerationConfigListResponse,
    GenerationConfigResponse,
    GenerationResultListResponse,
    GenerationResultResponse,
    PatchGenerationConfigRequest,
    RunResultResponse,
)
from src.backend.generation.service import GenerationService
from src.shared.db.models import Event, GenerationConfig, GenerationResult
from src.shared.exceptions import EntityNotFoundError
from src.shared.settings import settings
from src.workflows._common import urn_to_workflow_id
from src.workflows.kestra.client import KestraClient

router = APIRouter(
    prefix="/gen",
    tags=["common/gen"],
    dependencies=[Depends(require_common)],
)


def _config_response(c) -> GenerationConfigResponse:  # noqa: ANN001
    return GenerationConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),
        dataset_urn=c.dataset_urn,
        target_fields=c.target_fields,
        code_refs=c.code_refs,
        schedule=c.schedule,
        status=c.status,
        owner=c.owner,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=GenerationConfigListResponse)
async def get_gen_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigListResponse:
    order_by = parse_sort(sort, {"created_at": GenerationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, status_filter=status_filter, order_by=order_by
    )
    return GenerationConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        configs=[_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=GenerationConfigResponse)
async def get_gen_config(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    return _config_response(config)


@router.get("/{dataset_urn}/attr", response_model=GenerationConfigResponse)
async def get_gen_config_attr(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    return _config_response(config)


@router.patch("/{dataset_urn}/attr", response_model=GenerationConfigResponse)
async def patch_gen_config_attr(
    dataset_urn: str,
    body: PatchGenerationConfigRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _config_response(config)


@router.get("/{dataset_urn}/attr/result", response_model=GenerationResultListResponse)
async def get_gen_result(
    dataset_urn: str,
    latest: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationResultListResponse:
    effective_limit = 1 if latest else limit
    effective_offset = 0 if latest else offset
    order_by = parse_sort(sort, {"generated_at": GenerationResult.generated_at}, None)
    results, total_count = await service.get_results(
        dataset_urn, from_dt=from_time, to_dt=to_time, offset=effective_offset, limit=effective_limit, order_by=order_by
    )
    return GenerationResultListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        results=[
            GenerationResultResponse(
                id=r.id,
                dataset_urn=r.dataset_urn,
                proposals=r.proposals,
                similar_diffs=r.similar_diffs,
                approval_status=r.approval_status,
                run_id=r.run_id,
                generated_at=r.generated_at,
                applied_at=r.applied_at,
            )
            for r in results
        ],
    )


@router.post("/{dataset_urn}/method/generate", response_model=RunResultResponse)
async def post_gen_generate(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
    kestra: KestraClient = Depends(get_kestra_client),
) -> RunResultResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    label_value = f"generation-{urn_to_workflow_id(dataset_urn)}"
    await kestra.check_no_duplicate(
        "generation", "workflow_id", label_value, "GENERATION_RUNNING"
    )
    execution = await kestra.trigger_and_wait(
        "generation",
        inputs={
            "callback_base_url": settings.kestra_callback_base_url,
            "dataset_urn": dataset_urn,
        },
        labels={"workflow_id": label_value},
    )
    outputs = execution.outputs or {}
    return RunResultResponse(
        run_id=outputs.get("run_id", execution.id),
        status=outputs.get("status", execution.status.value),
        detail=outputs.get("detail", {}),
    )


@router.post("/{dataset_urn}/method/apply", response_model=RunResultResponse)
async def post_gen_apply(
    dataset_urn: str,
    body: ApplyGenerationRequest,
    service: GenerationService = Depends(get_generation_service),
) -> RunResultResponse:
    result = await service.apply(dataset_urn, body.result_id)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_gen_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> EventListResponse:
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events(
        dataset_urn, offset=offset, limit=limit, from_dt=from_time, to_dt=to_time, order_by=order_by
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
