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
            "List of validation rules. Each rule is a dict with at minimum `rule_id` and `type` keys.\n\n"
            "**Common fields** (all types): `rule_id` (unique ID), `type`, "
            "`partition` (optional: `{\"field\": \"...\", \"order\": \"desc\"}`)\n\n"
            "**Structure by type:**\n"
            "- **freshness**: `lookback_interval` (e.g. `\"24 hours\"`), `last_modified_field` (column name)\n"
            "- **volume**: `metric` (`\"row_count\"`), `condition` (`{\"type\": \"between\", \"min\": N, \"max\": N}`)\n"
            "- **field**: `field` (column name), `metric` (`\"null_count\"`, `\"distinct_count\"`, etc.), "
            "`condition` (`{\"type\": \"less_than_or_equal_to\", \"value\": N}`)\n"
            "- **schema**: `fields` (list of `{\"field\": \"col\", \"type\": \"VARCHAR\"}`), "
            "`compatibility` (`\"superset\"` or `\"exact\"`)\n"
            "- **sql**: `statement` (SQL query returning a scalar), "
            "`condition` (`{\"type\": \"equal_to\", \"value\": 0}`)\n"
            "- **custom**: `subtype` (e.g. `\"sql_timeseries\"`), `sql`, `partition` (list), "
            "`order` (list), `values` (list), optional `ml_validation` config"
        )
    )
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic validation runs, e.g. '0 6 * * *' for daily at 06:00 UTC. Required when is_active is true.")
    is_active: bool = Field(default=False, description="Whether the validation config is active and scheduled to run")
    owner: str = Field(description="Owner identifier (email or user URN) responsible for this validation config")

    model_config = {
        "json_schema_extra": {
            "example": {
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
                "rules": [
                    {
                        "rule_id": "r-fresh-001",
                        "type": "freshness",
                        "lookback_interval": "24 hours",
                        "last_modified_field": "updated_at",
                        "partition": {"field": "updated_at", "order": "desc"},
                    },
                    {
                        "rule_id": "r-vol-001",
                        "type": "volume",
                        "metric": "row_count",
                        "condition": {"type": "between", "min": 10, "max": 10000},
                    },
                    {
                        "rule_id": "r-field-001",
                        "type": "field",
                        "field": "list_price",
                        "metric": "null_count",
                        "condition": {"type": "less_than_or_equal_to", "value": 0},
                    },
                    {
                        "rule_id": "r-schema-001",
                        "type": "schema",
                        "fields": [
                            {"field": "isbn", "type": "VARCHAR"},
                            {"field": "title", "type": "VARCHAR"},
                            {"field": "list_price", "type": "NUMERIC"},
                        ],
                        "compatibility": "superset",
                    },
                    {
                        "rule_id": "r-sql-001",
                        "type": "sql",
                        "statement": "SELECT COUNT(*) FROM catalog.title_master WHERE list_price <= 0",
                        "condition": {"type": "equal_to", "value": 0},
                    },
                    {
                        "rule_id": "r-custom-ts-001",
                        "type": "custom",
                        "subtype": "sql_timeseries",
                        "description": "Daily volume and null-rate trend for anomaly detection",
                        "sql": (
                            "SELECT updated_at::date AS day, COUNT(*) AS row_count, "
                            "SUM(CASE WHEN list_price IS NULL THEN 1 ELSE 0 END)::float / COUNT(*) AS null_rate "
                            "FROM catalog.title_master GROUP BY day"
                        ),
                        "partition": ["day"],
                        "order": ["day"],
                        "values": ["row_count", "null_rate"],
                        "ml_validation": {
                            "targets": ["null_rate"],
                            "model": "range",
                            "lookback_partitions": 30,
                        },
                    },
                ],
                "schedule_cron": "0 6 * * *",
                "is_active": True,
                "owner": "de-lead@imazon.com",
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

    model_config = {
        "json_schema_extra": {
            "example": {
                "rules": [
                    {
                        "rule_id": "r-fresh-001",
                        "type": "freshness",
                        "lookback_interval": "12 hours",
                        "last_modified_field": "updated_at",
                    },
                ],
                "schedule_cron": "0 */6 * * *",
                "is_active": True,
            }
        }
    }


class RunValidationRequest(BaseModel):
    partition: dict[str, Any] | None = Field(
        default=None,
        description="Optional partition filter for incremental validation. Example: {\"date\": \"2024-01-15\"} or {\"partition_id\": 42}"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "partition": {"updated_at": "2026-04-04"},
            }
        }
    }


class ValidationConfigResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the validation config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    rules: list[dict[str, Any]] = Field(description="List of validation rule definitions")
    schedule_cron: str | None = Field(description="Cron expression for scheduled runs")
    is_active: bool = Field(description="Whether scheduled validation runs are enabled")
    owner: str = Field(description="Owner identifier responsible for this validation config")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")

    model_config = {
        "json_schema_extra": {
            "example": {
                "resp_time": "2026-04-05T10:00:00Z",
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
                "rules": [
                    {"rule_id": "r-fresh-001", "type": "freshness", "lookback_interval": "24 hours", "last_modified_field": "updated_at"},
                    {"rule_id": "r-vol-001", "type": "volume", "metric": "row_count", "condition": {"type": "between", "min": 10, "max": 10000}},
                ],
                "schedule_cron": "0 6 * * *",
                "is_active": True,
                "owner": "de-lead@imazon.com",
                "created_at": "2026-04-01T06:00:00Z",
                "updated_at": "2026-04-04T06:00:00Z",
            }
        }
    }


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

    model_config = {
        "json_schema_extra": {
            "example": {
                "resp_time": "2026-04-05T10:00:00Z",
                "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
                "rule_id": "r-field-001",
                "partition": {"updated_at": "2026-04-04"},
                "values": {"null_count": 0},
                "validation": {"null_count": True},
                "assertion_result": "SUCCESS",
                "issues": [],
                "run_id": "kestra-exec-20260404-001",
                "measured_at": "2026-04-04T06:05:12Z",
            }
        }
    }


class ValidationResultListResponse(PaginatedResponse):
    results: list[ValidationResultResponse] = Field(default=[], description="Page of validation result records")


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Kestra execution ID for this validation run")
    status: str = Field(description="Execution outcome, e.g. 'success'")
    total: int = Field(default=0, description="Total number of rules evaluated")
    passed: int = Field(default=0, description="Number of rules that passed")
    failed: int = Field(default=0, description="Number of rules that failed")
    errored: int = Field(default=0, description="Number of rules that encountered an error")

    model_config = {
        "json_schema_extra": {
            "example": {
                "resp_time": "2026-04-05T10:00:00Z",
                "run_id": "kestra-exec-20260404-001",
                "status": "success",
                "total": 6,
                "passed": 5,
                "failed": 1,
                "errored": 0,
            }
        }
    }
