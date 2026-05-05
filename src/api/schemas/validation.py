"""Validation config CRUD, run, and result models."""

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import AssertionResult

_VALID_TIERS = frozenset({"hourly", "daily", "weekly"})
_FRESHNESS_VALID_SOURCES = frozenset({"datahub_operation", "datahub_profile", "query"})
_VOLUME_VALID_SOURCES = frozenset({"datahub_profile", "query"})

# Mirrors the canonical _IDENTIFIER_RE in src/backend/validation/timeseries.py.
# Kept as a separate constant to avoid cross-layer imports (API schema → backend).
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def _validate_rules_source(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the ``source`` field on freshness and volume rules.

    Raises ValueError describing every invalid rule found.
    """
    errors: list[str] = []
    for i, rule in enumerate(rules):
        rule_type = rule.get("type", "")
        source = rule.get("source")
        if rule_type == "freshness":
            if source is not None:
                if source not in _FRESHNESS_VALID_SOURCES:
                    errors.append(
                        f"rules[{i}] (freshness): source must be one of "
                        f"{sorted(_FRESHNESS_VALID_SOURCES)}, got {source!r}"
                    )
                elif source == "query":
                    last_modified_field = rule.get("last_modified_field")
                    if not last_modified_field:
                        errors.append(
                            f"rules[{i}] (freshness): last_modified_field is required"
                            " when source='query'"
                        )
                    elif not _IDENTIFIER_RE.match(last_modified_field):
                        errors.append(
                            f"rules[{i}] (freshness): last_modified_field"
                            f" {last_modified_field!r} is not a valid SQL identifier"
                            r" (\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z)"
                        )
        elif rule_type == "volume":
            if source is not None and source not in _VOLUME_VALID_SOURCES:
                errors.append(
                    f"rules[{i}] (volume): source must be one of "
                    f"{sorted(_VOLUME_VALID_SOURCES)}, got {source!r}"
                )
        elif source is not None:
            errors.append(
                f"rules[{i}] ({rule_type}): source is reserved for freshness/volume rules only"
            )

    if errors:
        raise ValueError("; ".join(errors))
    return rules


class CreateValidationConfigRequest(BaseModel):
    rules: list[dict[str, Any]] = Field(
        max_length=200,
        description=(
            "List of validation rules. Each rule is a dict with at minimum `rule_id` and `type` keys.\n\n"
            "**Common fields** (all types): `rule_id` (unique ID), `type`, "
            "`partition` (optional: `{\"field\": \"...\", \"order\": \"desc\"}`)\n\n"
            "**Structure by type:**\n"
            "- **freshness**: `lookback_interval` (e.g. `\"24 hours\"`), "
            "`source` (optional: `\"datahub_operation\"` [default] | `\"datahub_profile\"` | `\"query\"`). "
            "When `source=query`: `last_modified_field` (required, SQL identifier), "
            "`filter` (optional SQL WHERE fragment).\n"
            "- **volume**: `condition` (`{\"type\": \"between\", \"min\": N, \"max\": N}`), "
            "`source` (optional: `\"datahub_profile\"` [default] | `\"query\"`). "
            "When `source=query`: `filter` (optional SQL WHERE fragment).\n"
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
    schedule_tier: str | None = Field(default=None, description="Schedule tier for periodic validation runs: 'hourly', 'daily', or 'weekly'. Required when is_enabled is true.")
    is_enabled: bool = Field(default=False, description="Whether the validation config is enabled and scheduled to run")
    owner: str = Field(description="Owner identifier (email or user URN) responsible for this validation config")

    model_config = {
        "json_schema_extra": {
            "example": {
                "rules": [
                    {
                        "rule_id": "r-fresh-001",
                        "type": "freshness",
                        "source": "datahub_operation",
                        "lookback_interval": "24 hours",
                        "partition": {"field": "updated_at", "order": "desc"},
                    },
                    {
                        "rule_id": "r-fresh-002",
                        "type": "freshness",
                        "source": "query",
                        "lookback_interval": "24 hours",
                        "last_modified_field": "updated_at",
                        "filter": "tenant_id = 'imazon'",
                    },
                    {
                        "rule_id": "r-vol-001",
                        "type": "volume",
                        "source": "datahub_profile",
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
                "schedule_tier": "daily",
                "is_enabled": True,
                "owner": "de-lead@imazon.com",
            }
        }
    }

    @field_validator("rules", mode="after")
    @classmethod
    def validate_rules_source(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_rules_source(v)

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_is_enabled_schedule_tier(self) -> "CreateValidationConfigRequest":
        if self.is_enabled and not self.schedule_tier:
            raise ValueError("schedule_tier is required when is_enabled is true")
        return self


class PatchValidationConfigRequest(BaseModel):
    rules: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Updated list of validation rules. Replaces the entire rule set.\n"
            "Supported types: freshness, volume, field, schema, sql, custom.\n"
            "For freshness/volume rules, the optional `source` field controls the data source "
            "(see CreateValidationConfigRequest for allowed values and extra required fields)."
        )
    )
    schedule_tier: str | None = Field(default=None, description="Updated schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'.")
    is_enabled: bool | None = Field(default=None, description="Set to true to enable scheduling (schedule_tier must be provided in the same request), false to pause.")

    @field_validator("rules", mode="after")
    @classmethod
    def validate_rules_source(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if v is None:
            return v
        return _validate_rules_source(v)

    @field_validator("schedule_tier")
    @classmethod
    def validate_schedule_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_TIERS:
            raise ValueError(f"schedule_tier must be one of {sorted(_VALID_TIERS)}, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_is_enabled_schedule_tier(self) -> "PatchValidationConfigRequest":
        if self.is_enabled is True and self.schedule_tier is None:
            raise ValueError(
                "schedule_tier must be provided in the same patch when setting is_enabled to true"
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
                "schedule_tier": "daily",
                "is_enabled": True,
            }
        }
    }


class RunValidationRequest(BaseModel):
    partition: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional partition filter for incremental validation. "
            "Example: {\"date\": \"2024-01-15\"} or {\"partition_id\": 42}"
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "No-write evaluation — powers the Online Verifier for coding agents. "
            "When true, rules are evaluated but results are not persisted."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "partition": {"updated_at": "2026-04-04"},
                "dry_run": False,
            }
        }
    }


class ValidationConfigResponse(SingleResponse):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "resp_time": "2026-04-05T10:00:00Z",
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
                "rules": [
                    {"rule_id": "r-fresh-001", "type": "freshness", "lookback_interval": "24 hours", "last_modified_field": "updated_at"},
                    {"rule_id": "r-vol-001", "type": "volume", "metric": "row_count", "condition": {"type": "between", "min": 10, "max": 10000}},
                ],
                "schedule_tier": "daily",
                "is_enabled": True,
                "owner": "de-lead@imazon.com",
                "created_at": "2026-04-01T06:00:00Z",
                "updated_at": "2026-04-04T06:00:00Z",
            }
        },
    )

    id: str = Field(description="Unique identifier of the validation config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    rules: list[dict[str, Any]] = Field(description="List of validation rule definitions")
    schedule_tier: str | None = Field(description="Schedule tier for periodic runs: 'hourly', 'daily', or 'weekly'")
    is_enabled: bool = Field(description="Whether scheduled validation runs are enabled")
    owner: str = Field(description="Owner identifier responsible for this validation config")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> str:
        return v if isinstance(v, str) else str(v)


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
    run_id: str = Field(description="Airflow DAG run ID for the run that produced this result")
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
                "run_id": "airflow-run-20260404-001",
                "measured_at": "2026-04-04T06:05:12Z",
            }
        }
    }


class ValidationResultListResponse(PaginatedResponse):
    results: list[ValidationResultResponse] = Field(default=[], description="Page of validation result records")


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Airflow DAG run ID for this validation run")
    status: str = Field(description="Execution outcome, e.g. 'success'")
    total: int = Field(default=0, description="Total number of rules evaluated")
    passed: int = Field(default=0, description="Number of rules that passed")
    failed: int = Field(default=0, description="Number of rules that failed")
    errored: int = Field(default=0, description="Number of rules that encountered an error")

    model_config = {
        "json_schema_extra": {
            "example": {
                "resp_time": "2026-04-05T10:00:00Z",
                "run_id": "airflow-run-20260404-001",
                "status": "success",
                "total": 6,
                "passed": 5,
                "failed": 1,
                "errored": 0,
            }
        }
    }
