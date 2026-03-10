"""Generation config CRUD, generate, and apply models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateGenerationConfigRequest(BaseModel):
    dataset_urn: str
    target_fields: dict[str, Any]
    code_refs: dict[str, Any] | None = None
    schedule: str | None = None
    owner: str


class PatchGenerationConfigRequest(BaseModel):
    target_fields: dict[str, Any] | None = None
    code_refs: dict[str, Any] | None = None
    schedule: str | None = None
    status: str | None = None


class RunGenerationRequest(BaseModel):
    dry_run: bool = False


class ApplyGenerationRequest(BaseModel):
    result_id: str
    confirm: bool = True


class GenerationConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    target_fields: dict[str, Any]
    code_refs: dict[str, Any] | None
    schedule: str | None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class GenerationConfigListResponse(PaginatedResponse):
    configs: list[GenerationConfigResponse] = []


class GenerationResultResponse(SingleResponse):
    id: str
    dataset_urn: str
    proposals: dict[str, Any] = {}
    similar_diffs: list[dict[str, Any]] = []
    approval_status: str = "pending"
    run_id: str
    generated_at: datetime
    applied_at: datetime | None = None


class GenerationResultListResponse(PaginatedResponse):
    results: list[GenerationResultResponse] = []


class RunResultResponse(SingleResponse):
    run_id: str
    status: str
    detail: dict[str, Any] = {}
