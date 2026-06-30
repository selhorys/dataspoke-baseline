"""Dataset summary and attributes response models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse
from src.shared.models.ingestion import Mode


class QualityScoreResponse(BaseModel):
    overall_score: float = Field(
        description="Composite quality score (0.0–1.0) aggregated across all quality dimensions"
    )
    dimensions: dict[str, float] = Field(
        default={},
        description=(
            "Per-dimension quality scores. Keys are dimension names "
            "(e.g. 'completeness', 'freshness'), values are scores (0.0–1.0)."
        ),
    )
    dimension_details: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Detailed breakdown per dimension. Keys match dimension names; values "
            "contain metric-specific details such as rule results and thresholds."
        ),
    )


class DatasetResponse(SingleResponse):
    model_config = ConfigDict(from_attributes=True)

    urn: str = Field(
        description=(
            "DataHub URN uniquely identifying this dataset, e.g. "
            "'urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)'"
        )
    )
    name: str = Field(description="Human-readable dataset name")
    platform: str = Field(
        description="Data platform where this dataset lives, e.g. 'postgres', 'bigquery'"
    )
    description: str | None = Field(
        default=None, description="Dataset description from DataHub, if available"
    )
    owners: list[str] = Field(
        default=[], description="Owner identifiers (email or URN) for this dataset"
    )
    tags: list[str] = Field(
        default=[], description="Tags associated with this dataset in DataHub"
    )


class DatasetListIngestion(BaseModel):
    """One covering ingestion source for a dataset in the catalog list."""

    model_config = ConfigDict(from_attributes=True)

    source_id: str = Field(description="Id of the ingestion source that covers this dataset")
    name: str = Field(description="Display name of the covering ingestion source")
    mode: Mode = Field(description="Ingestion mode of the covering source")
    platform: str = Field(
        description="Data platform of the covering source (= recipe.source.type), e.g. 'postgres'"
    )


class DatasetListValidation(BaseModel):
    """Validation-coverage summary for one dataset in the catalog list."""

    model_config = ConfigDict(from_attributes=True)

    covered: bool = Field(
        description="True when a validation conf exists for this dataset"
    )


class DatasetListMetagenConf(BaseModel):
    """One enabled metagen conf whose dataset_filter matches the dataset."""

    model_config = ConfigDict(from_attributes=True)

    conf_id: str = Field(description="Id of the matching metadata-generation config")
    name: str = Field(description="Display name of the matching metadata-generation config")


class DatasetListItem(BaseModel):
    """One row in the cross-feature dataset catalog list."""

    model_config = ConfigDict(from_attributes=True)

    dataset_urn: str = Field(description="DataHub URN of the registered dataset")
    ingestion: list[DatasetListIngestion] = Field(
        default=[],
        description="Every ingestion source covering this dataset (empty when none cover it)",
    )
    validation: DatasetListValidation = Field(
        description="Validation-coverage summary for this dataset",
    )
    metagen: list[DatasetListMetagenConf] = Field(
        default=[],
        description="Enabled metagen confs whose filter matches this dataset (possibly empty)",
    )


class DatasetListResponse(PaginatedResponse):
    datasets: list[DatasetListItem] = Field(
        default=[], description="Page of registered datasets with cross-feature coverage"
    )


class DatasetAttributesResponse(SingleResponse):
    urn: str = Field(description="DataHub URN uniquely identifying this dataset")
    column_count: int = Field(description="Total number of columns in this dataset's schema")
    fields: list[str] = Field(default=[], description="List of column names in the dataset schema")
    owners: list[str] = Field(
        default=[], description="Owner identifiers (email or URN) for this dataset"
    )
    tags: list[str] = Field(
        default=[], description="Tags associated with this dataset in DataHub"
    )
    description: str | None = Field(
        default=None, description="Dataset description from DataHub, if available"
    )
    quality_score: QualityScoreResponse | None = Field(
        default=None,
        description=(
            "Most recent quality score for this dataset, null if no measurements have been taken"
        ),
    )
