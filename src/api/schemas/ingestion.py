"""Ingestion config CRUD and run request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateIngestionConfigRequest(BaseModel):
    dataset_urn: str
    sources: dict[str, Any]
    deep_spec_enabled: bool = False
    schedule: str | None = None
    owner: str


class PatchIngestionConfigRequest(BaseModel):
    sources: dict[str, Any] | None = None
    deep_spec_enabled: bool | None = None
    schedule: str | None = None
    status: str | None = None


class RunIngestionRequest(BaseModel):
    dry_run: bool = False


class IngestionConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    sources: dict[str, Any]
    deep_spec_enabled: bool
    schedule: str | None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class IngestionConfigListResponse(PaginatedResponse):
    configs: list[IngestionConfigResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
