import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.dependencies import get_redis, get_validation_service
from src.api.schemas.common import parse_sort
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
from src.shared.cache.client import RedisClient
from src.shared.db.models import Event, ValidationConfig, ValidationResult
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
        periodic=c.periodic,
        owner=c.owner,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=ValidationConfigListResponse)
async def get_validation_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    periodic_filter: bool | None = Query(default=None, alias="periodic"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigListResponse:
    order_by = parse_sort(sort, {"created_at": ValidationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, periodic_filter=periodic_filter, order_by=order_by
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
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    partition: str | None = Query(default=None),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResultListResponse:
    order_by = parse_sort(sort, {"measured_at": ValidationResult.measured_at}, None)
    partition_filter: dict | None = None
    if partition:
        try:
            partition_filter = json.loads(partition)
        except Exception:
            partition_filter = None

    results, total_count = await service.get_results(
        dataset_urn,
        from_dt=from_time,
        to_dt=to_time,
        partition_filter=partition_filter,
        offset=offset,
        limit=limit,
        order_by=order_by,
    )
    return ValidationResultListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        results=[
            ValidationResultResponse(
                id=r.id,
                dataset_urn=r.dataset_urn,
                rule_id=r.rule_id,
                partition=r.partition,
                values=r.values,
                validation=r.validation,
                assertion_result=r.assertion_result,
                issues=r.issues,
                run_id=r.run_id,
                measured_at=r.measured_at,
            )
            for r in results
        ],
    )


@router.post("/{dataset_urn}/method/run", response_model=RunResultResponse)
async def post_validation_run(
    dataset_urn: str,
    body: RunValidationRequest,
    service: ValidationService = Depends(get_validation_service),
    cache: RedisClient = Depends(get_redis),
) -> RunResultResponse:
    from src.backend.validation.service import run_validation_with_lock

    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    result = await run_validation_with_lock(service, cache, dataset_urn, partition=body.partition)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        total=result.total,
        passed=result.passed,
        failed=result.failed,
        errored=result.errored,
    )


@router.get("/{dataset_urn}/event", response_model=EventListResponse)
async def get_validation_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: ValidationService = Depends(get_validation_service),
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
