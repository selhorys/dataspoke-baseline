"""Ingestion config CRUD and run request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.ingestion import (
    NoAuth,
    SourceType,
    validate_source_fields,
)


class CreateIngestionConfigRequest(BaseModel):
    dataset_urn: str
    source_type: SourceType
    locator: dict[str, Any]
    identifier: dict[str, Any]
    auth: dict[str, Any] | None = None
    is_active: bool = False
    schedule_cron: str | None = None
    enrichment_sources: dict[str, Any] | None = None
    custom_extractors: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "CreateIngestionConfigRequest":
        if self.is_active and not self.schedule_cron:
            raise ValueError("schedule_cron is required when is_active is true")
        validate_source_fields(
            self.source_type, self.locator, self.identifier, self.auth
        )
        return self


class PatchIngestionConfigRequest(BaseModel):
    source_type: SourceType | None = None
    locator: dict[str, Any] | None = None
    identifier: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    is_active: bool | None = None
    schedule_cron: str | None = None
    enrichment_sources: dict[str, Any] | None = None
    custom_extractors: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "PatchIngestionConfigRequest":
        # Only validate when is_active is explicitly set to true in this patch.
        if self.is_active is True and self.schedule_cron is None:
            raise ValueError(
                "schedule_cron must be provided in the same patch when setting is_active to true"
            )
        # Per-source_type sub-field validation only when source_type is present.
        if self.source_type is not None:
            from src.shared.models.ingestion import SOURCE_TYPE_REGISTRY

            locator_cls, identifier_cls, auth_cls = SOURCE_TYPE_REGISTRY[
                self.source_type
            ]
            if self.locator is not None:
                locator_cls.model_validate(self.locator)
            if self.identifier is not None:
                identifier_cls.model_validate(self.identifier)
            if self.auth is not None:
                auth_cls.model_validate(self.auth)
        return self


class RunIngestionRequest(BaseModel):
    dry_run: bool = False


class IngestionConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    source_type: str
    locator: dict[str, Any]
    identifier: dict[str, Any]
    auth: dict[str, Any] | None
    is_active: bool
    schedule_cron: str | None
    enrichment_sources: dict[str, Any] | None
    custom_extractors: dict[str, Any] | None
    kestra_flow_namespace: str | None
    kestra_flow_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionConfigListResponse(PaginatedResponse):
    configs: list[IngestionConfigResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
