from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_validation_service
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.validation import (
    PatchValidationConfigRequest,
    RunResultResponse,
    RunValidationRequest,
    ValidationConfigListResponse,
    ValidationConfigResponse,
    ValidationResultListResponse,
    ValidationResultResponse,
)
from src.backend.validation.service import ValidationService
from src.shared.exceptions import EntityNotFoundError

router = APIRouter(
    prefix="/validation",
    tags=["common/validation"],
    dependencies=[Depends(require_common)],
)


def _config_response(c) -> ValidationConfigResponse:  # noqa: ANN001
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


@router.get("", response_model=ValidationConfigListResponse)
async def list_validation_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigListResponse:
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, status_filter=status_filter
    )
    return ValidationConfigListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        configs=[_config_response(c) for c in configs],
    )


@router.get("/{dataset_urn}", response_model=ValidationConfigResponse)
async def get_validation_config(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    return _config_response(config)


@router.get("/{dataset_urn}/attr", response_model=ValidationConfigResponse)
async def get_validation_config_attr(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    return _config_response(config)


@router.patch("/{dataset_urn}/attr", response_model=ValidationConfigResponse)
async def patch_validation_config_attr(
    dataset_urn: str,
    body: PatchValidationConfigRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return _config_response(config)


@router.get("/{dataset_urn}/attr/result", response_model=ValidationResultListResponse)
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


@router.post("/{dataset_urn}/method/run", response_model=RunResultResponse)
async def run_validation(
    dataset_urn: str,
    body: RunValidationRequest,
    service: ValidationService = Depends(get_validation_service),
) -> RunResultResponse:
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
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
