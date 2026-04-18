"""Validation sub-resource handlers: /data/{dataset_urn}/attr/validation/*"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_redis, get_validation_service
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.validation import (
    CreateValidationConfigRequest,
    PatchValidationConfigRequest,
    RunValidationRequest,
    ValidationConfigResponse,
    ValidationResultListResponse,
    ValidationResultResponse,
)
from src.api.schemas.validation import RunResultResponse as ValidationRunResultResponse
from src.backend.validation.service import ValidationService
from src.shared.cache.client import RedisClient
from src.shared.db.models import Event, ValidationResult
from src.shared.exceptions import EntityNotFoundError

sub_router = APIRouter()


@sub_router.get("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def get_data_validation_conf(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    """Retrieve the validation config embedded within the dataset resource."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    return ValidationConfigResponse.model_validate(config)


@sub_router.put("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def put_data_validation_conf(
    dataset_urn: str,
    body: CreateValidationConfigRequest,
    response: Response,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    """Create or replace the validation config for the dataset (upsert)."""
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        rules=body.rules,
        schedule_tier=body.schedule_tier,
        is_active=body.is_active,
        owner=body.owner,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return ValidationConfigResponse.model_validate(config)


@sub_router.patch("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfigResponse)
async def patch_data_validation_conf(
    dataset_urn: str,
    body: PatchValidationConfigRequest,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigResponse:
    """Partially update the validation config for the dataset."""
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return ValidationConfigResponse.model_validate(config)


@sub_router.delete("/{dataset_urn}/attr/validation/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_data_validation_conf(
    dataset_urn: str,
    service: ValidationService = Depends(get_validation_service),
) -> None:
    """Delete the validation config for the dataset."""
    await service.delete_config(dataset_urn)


@sub_router.get("/{dataset_urn}/attr/validation/result", response_model=ValidationResultListResponse)
async def get_data_validation_result(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    partition: str | None = Query(default=None),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResultListResponse:
    """List validation results for the dataset with time range, partition, and pagination."""
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


@sub_router.post(
    "/{dataset_urn}/attr/validation/method/run",
    response_model=ValidationRunResultResponse,
)
async def post_data_validation_run(
    dataset_urn: str,
    body: RunValidationRequest,
    service: ValidationService = Depends(get_validation_service),
    cache: RedisClient = Depends(get_redis),
) -> ValidationRunResultResponse:
    """Trigger a validation run for the dataset via the data sub-resource."""
    from src.backend.validation.service import run_validation_with_lock

    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    result = await run_validation_with_lock(service, cache, dataset_urn, partition=body.partition)
    return ValidationRunResultResponse(
        run_id=result.run_id,
        status=result.status,
        total=result.total,
        passed=result.passed,
        failed=result.failed,
        errored=result.errored,
    )


@sub_router.get("/{dataset_urn}/attr/validation/event", response_model=EventListResponse)
async def get_data_validation_events(
    dataset_urn: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: ValidationService = Depends(get_validation_service),
) -> EventListResponse:
    """List validation events for the dataset with time range and pagination."""
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
