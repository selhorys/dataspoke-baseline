"""Validation config CRUD, run, and result models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateValidationConfigRequest(BaseModel):
    dataset_urn: str
    rules: list[dict[str, Any]]
    schedule: dict[str, Any] | None = None
    owner: str


class PatchValidationConfigRequest(BaseModel):
    rules: list[dict[str, Any]] | None = None
    schedule: dict[str, Any] | None = None
    status: str | None = None


class RunValidationRequest(BaseModel):
    partition: dict[str, Any] | None = None


class ValidationConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    rules: list[dict[str, Any]]
    schedule: dict[str, Any] | None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class ValidationConfigListResponse(PaginatedResponse):
    configs: list[ValidationConfigResponse] = []


class ValidationResultResponse(SingleResponse):
    id: str
    dataset_urn: str
    rule_id: str
    partition: dict[str, Any]
    values: dict[str, Any]
    validation: dict[str, bool] | None = None
    assertion_result: str
    issues: list[dict[str, Any]] = []
    run_id: str
    measured_at: datetime


class ValidationResultListResponse(PaginatedResponse):
    results: list[ValidationResultResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
