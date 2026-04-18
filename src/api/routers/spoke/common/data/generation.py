"""Generation sub-resource handlers: /data/{dataset_urn}/attr/gen/*"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_airflow_client, get_generation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.generation import (
    ApplyGenerationRequest,
    CreateGenerationConfigRequest,
    GenerationConfigResponse,
    GenerationResultListResponse,
    GenerationResultResponse,
    PatchGenerationConfigRequest,
)
from src.api.schemas.generation import RunResultResponse as GenerationRunResultResponse
from src.backend.generation.service import GenerationService
from src.shared.db.models import Event, GenerationResult
from src.shared.exceptions import EntityNotFoundError
from src.shared.settings import settings
from src.workflows._common import urn_to_workflow_id
from src.workflows.airflow.client import AirflowClient

sub_router = APIRouter()


@sub_router.get("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def get_data_gen_conf(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    """Retrieve the generation config embedded within the dataset resource."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    return GenerationConfigResponse.model_validate(config)


@sub_router.put("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def put_data_gen_conf(
    dataset_urn: str,
    body: CreateGenerationConfigRequest,
    response: Response,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    """Create or replace the generation config for the dataset (upsert)."""
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        target_fields=body.target_fields,
        code_refs=body.code_refs,
        schedule_cron=body.schedule_cron,
        owner=body.owner,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return GenerationConfigResponse.model_validate(config)


@sub_router.patch("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def patch_data_gen_conf(
    dataset_urn: str,
    body: PatchGenerationConfigRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    """Partially update the generation config for the dataset."""
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return GenerationConfigResponse.model_validate(config)


@sub_router.delete("/{dataset_urn}/attr/gen/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_data_gen_conf(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> None:
    """Delete the generation config for the dataset."""
    await service.delete_config(dataset_urn)


@sub_router.get("/{dataset_urn}/attr/gen/result", response_model=GenerationResultListResponse)
async def get_data_gen_result(
    dataset_urn: str,
    latest: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationResultListResponse:
    """List generation results for the dataset; pass latest=true for the most recent only."""
    effective_limit = 1 if latest else limit
    effective_offset = 0 if latest else offset
    order_by = parse_sort(sort, {"generated_at": GenerationResult.generated_at}, None)
    results, total_count = await service.get_results(
        dataset_urn,
        from_dt=from_time,
        to_dt=to_time,
        offset=effective_offset,
        limit=effective_limit,
        order_by=order_by,
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


@sub_router.post("/{dataset_urn}/attr/gen/method/generate", response_model=GenerationRunResultResponse)
async def post_data_gen_generate(
    dataset_urn: str,
    airflow: AirflowClient = Depends(get_airflow_client),
) -> GenerationRunResultResponse:
    """Trigger AI metadata generation for the dataset via the data sub-resource."""
    workflow_id = f"generation-{urn_to_workflow_id(dataset_urn)}"
    await airflow.check_no_duplicate(
        "generation", "workflow_id", workflow_id, "GENERATION_RUNNING"
    )
    dag_run = await airflow.trigger_and_wait(
        "generation",
        conf={
            "callback_base_url": settings.airflow_callback_base_url,
            "dataset_urn": dataset_urn,
            "workflow_id": workflow_id,
        },
    )
    conf_out = dag_run.conf or {}
    return GenerationRunResultResponse(
        run_id=conf_out.get("run_id", dag_run.dag_run_id),
        status=conf_out.get("status", dag_run.state.value),
        detail=conf_out.get("detail", {}),
    )


@sub_router.post("/{dataset_urn}/attr/gen/method/apply", response_model=GenerationRunResultResponse)
async def post_data_gen_apply(
    dataset_urn: str,
    body: ApplyGenerationRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationRunResultResponse:
    """Apply a previously generated metadata proposal to the dataset via the data sub-resource."""
    result = await service.apply(dataset_urn, body.result_id)
    return GenerationRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@sub_router.get("/{dataset_urn}/attr/gen/event", response_model=EventListResponse)
async def get_data_gen_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> EventListResponse:
    """List generation events for the dataset with time range and pagination."""
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
