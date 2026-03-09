"""Validation config CRUD, run, and result models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateValidationConfigRequest(BaseModel):
    dataset_urn: str
    rules: dict[str, Any]
    schedule: str | None = None
    owner: str


class PatchValidationConfigRequest(BaseModel):
    rules: dict[str, Any] | None = None
    schedule: str | None = None
    status: str | None = None


class RunValidationRequest(BaseModel):
    dry_run: bool = False


class ValidationConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    rules: dict[str, Any]
    schedule: str | None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class ValidationConfigListResponse(PaginatedResponse):
    configs: list[ValidationConfigResponse] = []


class ValidationResultResponse(SingleResponse):
    id: str
    config_id: str
    dataset_urn: str
    passed: bool
    violations: list[dict[str, Any]] = []
    executed_at: datetime


class ValidationResultListResponse(PaginatedResponse):
    results: list[ValidationResultResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
