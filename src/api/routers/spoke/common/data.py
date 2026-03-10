from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status

from src.api.auth.dependencies import require_common
from src.api.dependencies import (
    get_dataset_service,
    get_generation_service,
    get_ingestion_service,
    get_validation_service,
)
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse, QualityScoreResponse
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
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.api.schemas.validation import (
    CreateValidationConfigRequest,
    PatchValidationConfigRequest,
    RunValidationRequest,
    ValidationConfigResponse,
    ValidationResultListResponse,
    ValidationResultResponse,
)
from src.api.schemas.validation import RunResultResponse as ValidationRunResultResponse
from src.backend.dataset.service import DatasetService
from src.backend.generation.service import GenerationService
from src.backend.ingestion.service import IngestionService
from src.backend.validation.service import ValidationService
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/data",
    tags=["common/data"],
    dependencies=[Depends(require_common)],
)

_501 = HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


# ── Dataset resource ───────────────────────────────────────────────────────────


@router.get("/{dataset_urn}", response_model=DatasetResponse)
async def get_dataset(
    dataset_urn: str,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetResponse:
    summary = await service.get_summary(dataset_urn)
    return DatasetResponse(
        urn=summary.urn,
        name=summary.name,
        platform=summary.platform,
        description=summary.description,
        owners=summary.owners,
        tags=summary.tags,
    )


@router.get("/{dataset_urn}/attr", response_model=DatasetAttributesResponse)
async def get_dataset_attr(
    dataset_urn: str,
    service: DatasetService = Depends(get_dataset_service),
) -> DatasetAttributesResponse:
    attrs = await service.get_attributes(dataset_urn)
    quality = None
    if attrs.quality_score is not None:
        quality = QualityScoreResponse(
            overall_score=attrs.quality_score.overall_score,
            dimensions=attrs.quality_score.dimensions,
        )
    return DatasetAttributesResponse(
        urn=attrs.urn,
        column_count=attrs.column_count,
        fields=attrs.fields,
        owners=attrs.owners,
        tags=attrs.tags,
        description=attrs.description,
        quality_score=quality,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_dataset_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: DatasetService = Depends(get_dataset_service),
) -> EventListResponse:
    events, total_count = await service.get_events(dataset_urn, offset, limit, from_time, to_time)
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=e.id,
                entity_type=e.entity_type,
                entity_id=e.entity_id,
                event_type=e.event_type,
                status=e.status,
                detail=e.detail,
                occurred_at=e.occurred_at,
            )
            for e in events
        ],
    )


# ── Ingestion ─────────────────────────────────────────────────────────────────


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


@router.get("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def get_ingestion_conf(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("ingestion_config", dataset_urn)
    return _config_response(config)


@router.put("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def put_ingestion_conf(
    dataset_urn: str,
    body: CreateIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    config = await service.upsert_config(
        dataset_urn=dataset_urn,
        sources=body.sources,
        deep_spec_enabled=body.deep_spec_enabled,
        schedule=body.schedule,
        owner=body.owner,
    )
    return _config_response(config)


@router.patch("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def patch_ingestion_conf(
    dataset_urn: str,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _config_response(config)


@router.delete("/{dataset_urn}/attr/ingestion/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ingestion_conf(
    dataset_urn: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> None:
    await service.delete_config(dataset_urn)


@router.post("/{dataset_urn}/attr/ingestion/method/run", response_model=RunResultResponse)
async def run_ingestion(
    dataset_urn: str,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> RunResultResponse:
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/attr/ingestion/event", response_model=EventListResponse)
async def get_ingestion_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    events, total_count = await service.get_events(dataset_urn, offset, limit, from_time, to_time)
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


# ── Validation ────────────────────────────────────────────────────────────────


def _validation_config_response(c) -> ValidationConfigResponse:  # noqa: ANN001
    return ValidationConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),
        dataset_urn=c.dataset_urn,
        rules=c.rules,
        schedule=c.schedule,
        sla_target=c.sla_target,
        status=c.status,
        owner=c.owner,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def get_validation_conf(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    return _validation_config_response(config)


@router.put("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def put_validation_conf(
    dataset_urn: str,
    body: CreateValidationConfigRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    config = await service.upsert_config(
        dataset_urn=dataset_urn,
        rules=body.rules,
        schedule=body.schedule,
        sla_target=body.sla_target,
        owner=body.owner,
    )
    return _validation_config_response(config)


@router.patch("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def patch_validation_conf(
    dataset_urn: str,
    body: PatchValidationConfigRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _validation_config_response(config)


@router.delete("/{dataset_urn}/attr/validation/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_validation_conf(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> None:
    await service.delete_config(dataset_urn)


@router.get("/{dataset_urn}/attr/validation/result", response_model=ValidationResultListResponse)
async def get_validation_result(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResultListResponse:
    results, total_count = await service.get_results(
        dataset_urn, from_dt=from_time, to_dt=to_time, offset=offset, limit=limit
    )
    return ValidationResultListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        results=[
            ValidationResultResponse(
                id=r.id,
                dataset_urn=r.dataset_urn,
                quality_score=r.quality_score,
                dimensions=r.dimensions,
                issues=r.issues,
                anomalies=r.anomalies,
                recommendations=r.recommendations,
                alternatives=r.alternatives,
                run_id=r.run_id,
                measured_at=r.measured_at,
            )
            for r in results
        ],
    )


@router.post(
    "/{dataset_urn}/attr/validation/method/run", response_model=ValidationRunResultResponse
)
async def run_validation(
    dataset_urn: str,
    body: RunValidationRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationRunResultResponse:
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return ValidationRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/attr/validation/event", response_model=EventListResponse)
async def get_validation_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: ValidationService = Depends(get_validation_service),
) -> EventListResponse:
    events, total_count = await service.get_events(
        dataset_urn, offset=offset, limit=limit, from_dt=from_time, to_dt=to_time
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


# ── Generation ────────────────────────────────────────────────────────────────


def _generation_config_response(c) -> GenerationConfigResponse:  # noqa: ANN001
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


@router.get("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def get_gen_conf(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("generation_config", dataset_urn)
    return _generation_config_response(config)


@router.put("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def put_gen_conf(
    dataset_urn: str,
    body: CreateGenerationConfigRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    config = await service.upsert_config(
        dataset_urn=dataset_urn,
        target_fields=body.target_fields,
        code_refs=body.code_refs,
        schedule=body.schedule,
        owner=body.owner,
    )
    return _generation_config_response(config)


@router.patch("/{dataset_urn}/attr/gen/conf", response_model=GenerationConfigResponse)
async def patch_gen_conf(
    dataset_urn: str,
    body: PatchGenerationConfigRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _generation_config_response(config)


@router.delete("/{dataset_urn}/attr/gen/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gen_conf(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> None:
    await service.delete_config(dataset_urn)


@router.get("/{dataset_urn}/attr/gen/result", response_model=GenerationResultListResponse)
async def get_gen_result(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationResultListResponse:
    results, total_count = await service.get_results(
        dataset_urn, from_dt=from_time, to_dt=to_time, offset=offset, limit=limit
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


@router.post("/{dataset_urn}/attr/gen/method/generate", response_model=GenerationRunResultResponse)
async def run_gen(
    dataset_urn: str,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationRunResultResponse:
    result = await service.generate(dataset_urn)
    return GenerationRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.post("/{dataset_urn}/attr/gen/method/apply", response_model=GenerationRunResultResponse)
async def apply_gen(
    dataset_urn: str,
    body: ApplyGenerationRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GenerationRunResultResponse:
    result = await service.apply(dataset_urn, body.result_id)
    return GenerationRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/attr/gen/event", response_model=EventListResponse)
async def get_gen_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: GenerationService = Depends(get_generation_service),
) -> EventListResponse:
    events, total_count = await service.get_events(
        dataset_urn, offset=offset, limit=limit, from_dt=from_time, to_dt=to_time
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


# ── WebSocket: validation progress stream ─────────────────────────────────────


@router.websocket("/{dataset_urn}/stream/validation")
async def stream_validation(dataset_urn: str, websocket: WebSocket) -> None:
    """Stub WebSocket — immediately closes with 1011 (internal error / not implemented)."""
    await websocket.accept()
    await websocket.close(code=1011, reason="not implemented")
