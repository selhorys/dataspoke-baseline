from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.auth.dependencies import require_common
from src.api.schemas.common import parse_sort
from src.api.dependencies import get_kestra_client, get_validation_service
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
from src.shared.db.models import Event, ValidationConfig, ValidationResult
from src.shared.exceptions import EntityNotFoundError
from src.workflows._common import urn_to_workflow_id
from src.workflows.kestra.client import KestraClient

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
async def get_validation_configs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfigListResponse:
    order_by = parse_sort(sort, {"created_at": ValidationConfig.created_at}, None)
    configs, total_count = await service.list_configs(
        offset=offset, limit=limit, status_filter=status_filter, order_by=order_by
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
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResultListResponse:
    order_by = parse_sort(sort, {"measured_at": ValidationResult.measured_at}, None)
    results, total_count = await service.get_results(
        dataset_urn, from_dt=from_time, to_dt=to_time, offset=offset, limit=limit, order_by=order_by
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
                dimension_details=r.dimension_details,
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
async def post_validation_run(
    dataset_urn: str,
    body: RunValidationRequest,
    service: ValidationService = Depends(get_validation_service),
    kestra: KestraClient = Depends(get_kestra_client),
) -> RunResultResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("validation_config", dataset_urn)
    label_value = f"validation-{urn_to_workflow_id(dataset_urn)}"
    await kestra.check_no_duplicate(
        "validation", "workflow_id", label_value, "VALIDATION_RUNNING"
    )
    execution = await kestra.trigger_and_wait(
        "validation",
        inputs={
            "callback_base_url": "http://localhost:8000",
            "dataset_urn": dataset_urn,
            "config_id": "",
            "dry_run": str(body.dry_run).lower(),
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
