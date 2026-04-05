"""Dataset summary and attributes response models."""

from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse


class QualityScoreResponse(BaseModel):
    overall_score: float = Field(description="Composite quality score (0.0–1.0) aggregated across all quality dimensions")
    dimensions: dict[str, float] = Field(default={}, description="Per-dimension quality scores. Keys are dimension names (e.g. 'completeness', 'freshness'), values are scores (0.0–1.0).")
    dimension_details: dict[str, dict[str, Any]] | None = Field(default=None, description="Detailed breakdown per dimension. Keys match dimension names; values contain metric-specific details such as rule results and thresholds.")


class DatasetResponse(SingleResponse):
    urn: str = Field(description="DataHub URN uniquely identifying this dataset, e.g. 'urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)'")
    name: str = Field(description="Human-readable dataset name")
    platform: str = Field(description="Data platform where this dataset lives, e.g. 'postgres', 'bigquery'")
    description: str | None = Field(default=None, description="Dataset description from DataHub, if available")
    owners: list[str] = Field(default=[], description="Owner identifiers (email or URN) for this dataset")
    tags: list[str] = Field(default=[], description="Tags associated with this dataset in DataHub")


class DatasetListResponse(PaginatedResponse):
    datasets: list[DatasetResponse] = Field(default=[], description="Page of dataset records")


class DatasetAttributesResponse(SingleResponse):
    urn: str = Field(description="DataHub URN uniquely identifying this dataset")
    column_count: int = Field(description="Total number of columns in this dataset's schema")
    fields: list[str] = Field(default=[], description="List of column names in the dataset schema")
    owners: list[str] = Field(default=[], description="Owner identifiers (email or URN) for this dataset")
    tags: list[str] = Field(default=[], description="Tags associated with this dataset in DataHub")
    description: str | None = Field(default=None, description="Dataset description from DataHub, if available")
    quality_score: QualityScoreResponse | None = Field(default=None, description="Most recent quality score for this dataset, null if no measurements have been taken")
