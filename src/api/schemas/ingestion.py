"""Ingestion config CRUD and run request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateIngestionConfigRequest(BaseModel):
    dataset_urn: str
    source_type: str
    location: dict[str, Any]
    periodic: bool = False
    schedule: str | None = None
    enrichment_sources: dict[str, Any] | None = None
    custom_extractors: dict[str, Any] | None = None

    @model_validator(mode="after")
    def schedule_required_when_periodic(self) -> "CreateIngestionConfigRequest":
        if self.periodic and not self.schedule:
            raise ValueError("schedule is required when periodic is true")
        return self


class PatchIngestionConfigRequest(BaseModel):
    source_type: str | None = None
    location: dict[str, Any] | None = None
    periodic: bool | None = None
    schedule: str | None = None
    enrichment_sources: dict[str, Any] | None = None
    custom_extractors: dict[str, Any] | None = None
    status: str | None = None

    @model_validator(mode="after")
    def schedule_required_when_periodic(self) -> "PatchIngestionConfigRequest":
        # Only validate when periodic is explicitly set to true in this patch.
        # We cannot know the existing DB state here, so we require schedule to
        # also be present in the same patch when periodic is being set to true.
        if self.periodic is True and self.schedule is None:
            raise ValueError(
                "schedule must be provided in the same patch when setting periodic to true"
            )
        return self


class RunIngestionRequest(BaseModel):
    dry_run: bool = False


class IngestionConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    source_type: str
    location: dict[str, Any]
    periodic: bool
    schedule: str | None
    enrichment_sources: dict[str, Any] | None
    custom_extractors: dict[str, Any] | None
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionConfigListResponse(PaginatedResponse):
    configs: list[IngestionConfigResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
