"""Validation config CRUD, run, and result models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateValidationConfigRequest(BaseModel):
    dataset_urn: str
    rules: list[dict[str, Any]]
    schedule_cron: str | None = None
    is_active: bool = False
    owner: str

    @model_validator(mode="after")
    def validate_is_active_schedule_cron(self) -> "CreateValidationConfigRequest":
        if self.is_active and not self.schedule_cron:
            raise ValueError("schedule_cron is required when is_active is true")
        return self


class PatchValidationConfigRequest(BaseModel):
    rules: list[dict[str, Any]] | None = None
    schedule_cron: str | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_is_active_schedule_cron(self) -> "PatchValidationConfigRequest":
        if self.is_active is True and self.schedule_cron is None:
            raise ValueError(
                "schedule_cron must be provided in the same patch when setting is_active to true"
            )
        return self


class RunValidationRequest(BaseModel):
    partition: dict[str, Any] | None = None


class ValidationConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    rules: list[dict[str, Any]]
    schedule_cron: str | None
    is_active: bool
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
