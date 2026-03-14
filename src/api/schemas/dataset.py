"""Dataset summary and attributes response models."""

from typing import Any

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class QualityScoreResponse(BaseModel):
    overall_score: float
    dimensions: dict[str, float] = {}
    dimension_details: dict[str, dict[str, Any]] | None = None


class DatasetResponse(SingleResponse):
    urn: str
    name: str
    platform: str
    description: str | None = None
    owners: list[str] = []
    tags: list[str] = []


class DatasetListResponse(PaginatedResponse):
    datasets: list[DatasetResponse] = []


class DatasetAttributesResponse(SingleResponse):
    urn: str
    column_count: int
    fields: list[str] = []
    owners: list[str] = []
    tags: list[str] = []
    description: str | None = None
    quality_score: QualityScoreResponse | None = None
