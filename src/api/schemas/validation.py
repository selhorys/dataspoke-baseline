"""Validation config CRUD, run, and result models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import AssertionResult


class CreateValidationConfigRequest(BaseModel):
    dataset_urn: str = Field(description="DataHub URN of the dataset to validate, e.g. 'urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)'")
    rules: list[dict[str, Any]] = Field(
        description=(
            "List of validation rules. Each rule is a dict with at minimum a 'type' key.\n"
            "Supported types: freshness, volume, field, schema, sql, custom.\n"
            "Example: [{\"type\": \"freshness\", \"max_age_hours\": 24}, "
            "{\"type\": \"volume\", \"min_rows\": 100}]"
        )
    )
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic validation runs, e.g. '0 6 * * *' for daily at 06:00 UTC. Required when is_active is true.")
    is_active: bool = Field(default=False, description="Whether the validation config is active and scheduled to run")
    owner: str = Field(description="Owner identifier (email or user URN) responsible for this validation config")

    model_config = {
        "json_schema_extra": {
            "example": {
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)",
                "rules": [
                    {"type": "freshness", "max_age_hours": 24},
                    {"type": "volume", "min_rows": 100},
                    {"type": "field", "column": "order_id", "not_null": True},
                ],
                "schedule_cron": "0 6 * * *",
                "is_active": True,
                "owner": "analyst@example.com",
            }
        }
    }

    @model_validator(mode="after")
    def validate_is_active_schedule_cron(self) -> "CreateValidationConfigRequest":
        if self.is_active and not self.schedule_cron:
            raise ValueError("schedule_cron is required when is_active is true")
        return self


class PatchValidationConfigRequest(BaseModel):
    rules: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Updated list of validation rules. Replaces the entire rule set.\n"
            "Supported types: freshness, volume, field, schema, sql, custom."
        )
    )
    schedule_cron: str | None = Field(default=None, description="Updated cron expression for periodic runs.")
    is_active: bool | None = Field(default=None, description="Set to true to activate scheduling (schedule_cron must be provided in the same request), false to pause.")

    @model_validator(mode="after")
    def validate_is_active_schedule_cron(self) -> "PatchValidationConfigRequest":
        if self.is_active is True and self.schedule_cron is None:
            raise ValueError(
                "schedule_cron must be provided in the same patch when setting is_active to true"
            )
        return self


class RunValidationRequest(BaseModel):
    partition: dict[str, Any] | None = Field(
        default=None,
        description="Optional partition filter for incremental validation. Example: {\"date\": \"2024-01-15\"} or {\"partition_id\": 42}"
    )


class ValidationConfigResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the validation config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    rules: list[dict[str, Any]] = Field(description="List of validation rule definitions")
    schedule_cron: str | None = Field(description="Cron expression for scheduled runs")
    is_active: bool = Field(description="Whether scheduled validation runs are enabled")
    owner: str = Field(description="Owner identifier responsible for this validation config")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class ValidationConfigListResponse(PaginatedResponse):
    configs: list[ValidationConfigResponse] = Field(default=[], description="Page of validation config records")


class ValidationResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the validation result")
    dataset_urn: str = Field(description="DataHub URN of the validated dataset")
    rule_id: str = Field(description="Identifier of the rule that produced this result")
    partition: dict[str, Any] = Field(description="Partition context for this result, e.g. {\"date\": \"2024-01-15\"}")
    values: dict[str, Any] = Field(description="Measured values collected during validation, keyed by metric name")
    validation: dict[str, bool] | None = Field(default=None, description="Per-check pass/fail mapping, keyed by check name")
    assertion_result: AssertionResult = Field(description="Overall assertion outcome: SUCCESS, FAILURE, or ERROR")
    issues: list[dict[str, Any]] = Field(default=[], description="List of issue details when assertion_result is FAILURE or ERROR")
    run_id: str = Field(description="Kestra execution ID for the run that produced this result")
    measured_at: datetime = Field(description="UTC timestamp when the measurement was taken")


class ValidationResultListResponse(PaginatedResponse):
    results: list[ValidationResultResponse] = Field(default=[], description="Page of validation result records")


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Kestra execution ID for this validation run")
    status: str = Field(description="Execution outcome, e.g. 'success'")
    total: int = Field(default=0, description="Total number of rules evaluated")
    passed: int = Field(default=0, description="Number of rules that passed")
    failed: int = Field(default=0, description="Number of rules that failed")
    errored: int = Field(default=0, description="Number of rules that encountered an error")
