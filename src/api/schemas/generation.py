"""Generation config CRUD, generate, and apply models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.enums import ApprovalStatus, GenerationConfigStatus


class CreateGenerationConfigRequest(BaseModel):
    dataset_urn: str = Field(description="DataHub URN of the dataset to generate metadata for, e.g. 'urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)'")
    target_fields: dict[str, Any] = Field(
        description="Fields to generate metadata for. Keys are field paths, values are generation config per field. Example: {\"description\": {\"strategy\": \"llm\"}, \"tags\": {\"strategy\": \"llm\", \"max_tags\": 5}}"
    )
    code_refs: dict[str, Any] | None = Field(
        default=None,
        description="References to source code for context-aware generation. Example: {\"repo\": \"github.com/org/repo\", \"paths\": [\"dbt/models/orders.sql\"]}"
    )
    schedule_cron: str | None = Field(default=None, description="Cron expression for periodic generation runs, e.g. '0 6 * * *' for daily at 06:00 UTC")
    owner: str = Field(description="Owner identifier (email or user URN) responsible for this generation config")

    model_config = {
        "json_schema_extra": {
            "example": {
                "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)",
                "target_fields": {
                    "description": {"strategy": "llm"},
                    "tags": {"strategy": "llm", "max_tags": 5},
                },
                "code_refs": {"repo": "github.com/org/repo", "paths": ["dbt/models/orders.sql"]},
                "schedule_cron": "0 6 * * *",
                "owner": "de@example.com",
            }
        }
    }


class PatchGenerationConfigRequest(BaseModel):
    target_fields: dict[str, Any] | None = Field(
        default=None,
        description="Updated fields to generate metadata for. Replaces the entire target_fields map."
    )
    code_refs: dict[str, Any] | None = Field(
        default=None,
        description="Updated source code references for context-aware generation."
    )
    schedule_cron: str | None = Field(default=None, description="Updated cron expression for periodic generation runs.")
    status: GenerationConfigStatus | None = Field(default=None, description="Updated config status. Currently only 'draft' is supported.")


class RunGenerationRequest(BaseModel):
    dry_run: bool = Field(default=False, description="When true, simulate generation and return proposals without persisting or applying them")


class ApplyGenerationRequest(BaseModel):
    result_id: str = Field(description="Identifier of the GenerationResult to apply to DataHub")
    confirm: bool = Field(default=True, description="Must be true to confirm and execute the apply operation")


class GenerationConfigResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the generation config")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    target_fields: dict[str, Any] = Field(description="Fields to generate metadata for with their generation config")
    code_refs: dict[str, Any] | None = Field(description="Source code references used for context-aware generation")
    schedule_cron: str | None = Field(description="Cron expression for scheduled generation runs")
    status: GenerationConfigStatus = Field(description="Config lifecycle status. 'draft' indicates the config has not been executed yet.")
    owner: str = Field(description="Owner identifier responsible for this generation config")
    created_at: datetime = Field(description="UTC timestamp when the config was created")
    updated_at: datetime = Field(description="UTC timestamp of the most recent update")


class GenerationConfigListResponse(PaginatedResponse):
    configs: list[GenerationConfigResponse] = Field(default=[], description="Page of generation config records")


class GenerationResultResponse(SingleResponse):
    id: str = Field(description="Unique identifier of the generation result")
    dataset_urn: str = Field(description="DataHub URN of the dataset")
    proposals: dict[str, Any] = Field(default={}, description="Generated metadata proposals keyed by field name. Values are the proposed metadata values.")
    similar_diffs: list[dict[str, Any]] = Field(default=[], description="Similar historical metadata changes retrieved via vector search for context")
    approval_status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, description="Review status of the generated proposals: 'pending' (awaiting review) or 'approved' (applied to DataHub)")
    run_id: str = Field(description="Airflow DAG run ID for the run that produced this result")
    generated_at: datetime = Field(description="UTC timestamp when the proposals were generated")
    applied_at: datetime | None = Field(default=None, description="UTC timestamp when the proposals were applied to DataHub, null if not yet applied")


class GenerationResultListResponse(PaginatedResponse):
    results: list[GenerationResultResponse] = Field(default=[], description="Page of generation result records")


class RunResultResponse(SingleResponse):
    run_id: str = Field(description="Airflow DAG run ID for this generation run")
    status: str = Field(description="Execution status returned by Airflow, e.g. 'running' or 'success'")
    detail: dict[str, Any] = Field(default={}, description="Additional execution metadata returned by Airflow")
