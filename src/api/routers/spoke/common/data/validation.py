"""Validation sub-resource handlers.

Routes served:
  GET    /data/{dataset_urn}/attr/validation/conf
  PUT    /data/{dataset_urn}/attr/validation/conf
  PATCH  /data/{dataset_urn}/attr/validation/conf
  DELETE /data/{dataset_urn}/attr/validation/conf
  POST   /data/{dataset_urn}/attr/validation/result
  GET    /data/{dataset_urn}/attr/validation/result
  GET    /data/{dataset_urn}/event/validation

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Data Resource (validation rows).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.auth.dependencies import AuthContext, require_writer
from src.api.dependencies import get_validation_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.validation import (
    PatchValidationConfRequest,
    PostValidationResultRequest,
    PutValidationConfRequest,
    ValidationConfResponse,
    ValidationResultListResponse,
    ValidationResultRow,
)
from src.backend.validation.service import ValidationService
from src.shared.db.models import Event
from src.shared.exceptions import EntityNotFoundError

sub_router = APIRouter()


# ── Conf CRUD ─────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfResponse)
async def get_data_validation_conf(
    dataset_urn: DatasetUrnPath,
    service: ValidationService = Depends(get_validation_service),
) -> ValidationConfResponse:
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("config", dataset_urn)
    return ValidationConfResponse.model_validate(config)


@sub_router.put(
    "/{dataset_urn}/attr/validation/conf",
    response_model=ValidationConfResponse,
    status_code=status.HTTP_200_OK,
)
async def put_data_validation_conf(
    dataset_urn: DatasetUrnPath,
    body: PutValidationConfRequest,
    response: Response,
    service: ValidationService = Depends(get_validation_service),
    _writer: AuthContext = Depends(require_writer),
) -> ValidationConfResponse:
    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        description=body.description,
        variables=[v.model_dump() for v in body.variables],
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return ValidationConfResponse.model_validate(config)


@sub_router.patch("/{dataset_urn}/attr/validation/conf", response_model=ValidationConfResponse)
async def patch_data_validation_conf(
    dataset_urn: DatasetUrnPath,
    body: PatchValidationConfRequest,
    service: ValidationService = Depends(get_validation_service),
    _writer: AuthContext = Depends(require_writer),
) -> ValidationConfResponse:
    patch = body.model_dump(exclude_unset=True)
    config = await service.patch_config(dataset_urn, patch)
    return ValidationConfResponse.model_validate(config)


@sub_router.delete(
    "/{dataset_urn}/attr/validation/conf",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_data_validation_conf(
    dataset_urn: DatasetUrnPath,
    service: ValidationService = Depends(get_validation_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    await service.delete_config(dataset_urn)


# ── Results ───────────────────────────────────────────────────────────────────


@sub_router.post(
    "/{dataset_urn}/attr/validation/result",
    response_model=ValidationResultRow,
    status_code=status.HTTP_201_CREATED,
)
async def post_data_validation_result(
    dataset_urn: DatasetUrnPath,
    body: PostValidationResultRequest,
    service: ValidationService = Depends(get_validation_service),
    _writer: AuthContext = Depends(require_writer),
) -> ValidationResultRow:
    record = await service.record_result(
        dataset_urn=dataset_urn,
        data_time=body.data_time,
        score=body.score,
        variables=body.variables,
    )
    return ValidationResultRow.model_validate(record)


@sub_router.get(
    "/{dataset_urn}/attr/validation/result",
    response_model=ValidationResultListResponse,
)
async def get_data_validation_result(
    dataset_urn: DatasetUrnPath,
    from_time: datetime | None = Query(default=None, alias="from"),
    until_time: datetime | None = Query(default=None, alias="until"),
    limit: int = Query(default=1000, ge=1, le=10000),
    service: ValidationService = Depends(get_validation_service),
) -> ValidationResultListResponse:
    results, total_count = await service.get_results(
        dataset_urn,
        from_dt=from_time,
        until_dt=until_time,
        limit=limit,
    )
    return ValidationResultListResponse(
        offset=0,
        limit=limit,
        total_count=total_count,
        results=[ValidationResultRow.model_validate(r) for r in results],
    )


# ── Events ────────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/event/validation", response_model=EventListResponse)
async def get_data_validation_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: ValidationService = Depends(get_validation_service),
) -> EventListResponse:
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events(
        dataset_urn,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=str(e["id"]),
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e.get("detail", {}),
                occurred_at=e["occurred_at"],
            )
            for e in events
        ],
    )
