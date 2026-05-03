"""Ingestion sub-resource handlers: /data/{dataset_urn}/attr/ingestion/*
   and siblings: /data/{dataset_urn}/method/ingestion/run
                  /data/{dataset_urn}/event/ingestion

Handler naming: BACKEND.md §Route Handler Naming Convention.
Spec: API.md §Data Resource (lines 233–238).
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_ingestion_service
from src.api.schemas._paths import DatasetUrnPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    AuthSpec,
    CreateIngestionConfigRequest,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
    SecretRefSpec,
)
from src.backend.ingestion.secret_resolver import (
    SecretCollision,
    SecretRefNameForbidden,
    SecretRefNotFound,
    SecretResolverUnavailable,
    verify_secret_ref,
    write_secret_value,
)
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import Event
from src.shared.exceptions import (
    EntityNotFoundError,
    PreconditionFailedError,
    StorageUnavailableError,
)

sub_router = APIRouter()


def _vault_or_verify(auth: AuthSpec) -> AuthSpec:
    """Write or verify the Kubernetes Secret for ``auth``, then return the reference shape.

    Vault path (``auth.password`` is present): write the password to the Secret,
    then strip the password before the service layer sees it.

    Reference path (``auth.password`` is absent): verify the Secret + key exist.

    Raises:
        PreconditionFailedError: SecretCollision, SecretRefNotFound, or SecretRefNameForbidden.
        StorageUnavailableError: SecretResolverUnavailable (k8s config not loadable or API error).
    """
    assert auth.secret_ref is not None  # enforced by AuthSpec.enforce_matrix

    ref = auth.secret_ref

    if auth.password is not None:
        try:
            write_secret_value(
                name=ref.name,
                key=ref.key,
                value=auth.password,
                force_overwrite=ref.force_overwrite,
            )
        except SecretRefNameForbidden as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"SecretRefNameForbidden: {exc}",
            ) from exc
        except SecretCollision as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"Secret collision: {exc}",
            ) from exc
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(str(exc)) from exc
    else:
        try:
            verify_secret_ref(name=ref.name, key=ref.key)
        except SecretRefNameForbidden as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"SecretRefNameForbidden: {exc}",
            ) from exc
        except SecretRefNotFound as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"Secret reference not found: {exc}",
            ) from exc
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(str(exc)) from exc

    return AuthSpec(
        username=auth.username,
        secret_ref=SecretRefSpec(
            name=ref.name,
            key=ref.key,
        ),
    )


# ── Conf CRUD ─────────────────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def get_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Retrieve the ingestion config for a dataset."""
    config = await service.get_config(dataset_urn)
    if config is None:
        raise EntityNotFoundError("config", dataset_urn)
    return IngestionConfigResponse.model_validate(config)


@sub_router.put("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def put_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    body: CreateIngestionConfigRequest,
    response: Response,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Create or replace the ingestion config for a dataset (upsert).

    When ``auth`` is present, the handler either vaults the password (vault path)
    or verifies a pre-existing Secret (reference path) before persisting the
    reference shape.  Plaintext passwords are never forwarded to the service layer.
    """
    resolved_auth: AuthSpec | None = body.auth
    if resolved_auth is not None:
        resolved_auth = _vault_or_verify(resolved_auth)

    auth_dict: dict[str, Any] | None = None
    if resolved_auth is not None:
        # Persist the reference shape only: {username, secret_ref: {name, key}}.
        # password is never persisted; force_overwrite is a transient API-only field.
        auth_dict = {
            "username": resolved_auth.username,
            "secret_ref": {
                "name": resolved_auth.secret_ref.name,  # type: ignore[union-attr]
                "key": resolved_auth.secret_ref.key,  # type: ignore[union-attr]
            },
        }

    config, created = await service.upsert_config(
        dataset_urn=dataset_urn,
        mode=body.mode,
        platform=body.platform,
        locator=body.locator,
        identifier=body.identifier,
        auth=auth_dict,
        is_enabled=body.is_enabled,
        schedule_tier=body.schedule_tier,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return IngestionConfigResponse.model_validate(config)


@sub_router.patch("/{dataset_urn}/attr/ingestion/conf", response_model=IngestionConfigResponse)
async def patch_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    body: PatchIngestionConfigRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionConfigResponse:
    """Partially update the ingestion config for a dataset.

    When ``auth`` is present in the PATCH body, vault/verify is performed before
    the service layer write.  Fields not included in the PATCH body are preserved.
    """
    patch = body.model_dump(exclude_unset=True)

    if body.auth is not None:
        resolved_auth = _vault_or_verify(body.auth)
        # Persist the reference shape only: {username, secret_ref: {name, key}}.
        patch["auth"] = {
            "username": resolved_auth.username,
            "secret_ref": {
                "name": resolved_auth.secret_ref.name,  # type: ignore[union-attr]
                "key": resolved_auth.secret_ref.key,  # type: ignore[union-attr]
            },
        }

    config = await service.patch_config(dataset_urn, patch)
    return IngestionConfigResponse.model_validate(config)


@sub_router.delete(
    "/{dataset_urn}/attr/ingestion/conf", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_data_ingestion_conf(
    dataset_urn: DatasetUrnPath,
    service: IngestionService = Depends(get_ingestion_service),
) -> None:
    """Delete the ingestion config for a dataset."""
    await service.delete_config(dataset_urn)


# ── Run (sibling path) ────────────────────────────────────────────────────────


@sub_router.post("/{dataset_urn}/method/ingestion/run", response_model=RunResultResponse)
async def post_data_ingestion_run(
    dataset_urn: DatasetUrnPath,
    body: RunIngestionRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> RunResultResponse:
    """Execute the ingestion pipeline for a dataset.

    ?dry_run=true (in body) runs the extractor without emitting to DataHub.
    Concurrent runs return 409 INGESTION_RUNNING.
    """
    result = await service.run(dataset_urn, dry_run=body.dry_run)
    return RunResultResponse(
        run_id=result.run_id,
        status=result.status,
        detail=result.detail,
    )


# ── Events (sibling path) ─────────────────────────────────────────────────────


@sub_router.get("/{dataset_urn}/event/ingestion", response_model=EventListResponse)
async def get_data_ingestion_events(
    dataset_urn: DatasetUrnPath,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    """Ingestion event reports for a dataset (INGESTION.COMPLETE, INGESTION.FAIL)."""
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
