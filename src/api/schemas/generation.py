"""Generation config CRUD, generate, and apply models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class CreateGenerationConfigRequest(BaseModel):
    dataset_urn: str
    target_type: str
    template: dict[str, Any] = {}
    owner: str


class PatchGenerationConfigRequest(BaseModel):
    target_type: str | None = None
    template: dict[str, Any] | None = None
    status: str | None = None


class RunGenerationRequest(BaseModel):
    dry_run: bool = False


class ApplyGenerationRequest(BaseModel):
    result_id: str
    confirm: bool = True


class GenerationConfigResponse(SingleResponse):
    id: str
    dataset_urn: str
    target_type: str
    template: dict[str, Any]
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class GenerationConfigListResponse(PaginatedResponse):
    configs: list[GenerationConfigResponse] = []


class GenerationResultResponse(SingleResponse):
    id: str
    config_id: str
    dataset_urn: str
    generated_content: dict[str, Any] = {}
    applied: bool = False
    created_at: datetime


class GenerationResultListResponse(PaginatedResponse):
    results: list[GenerationResultResponse] = []
