"""Vector search request/response models for the search API."""

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse


class ColumnInfo(BaseModel):
    name: str = Field(description="Column name as it appears in the dataset schema")
    type: str = Field(description="Data type of the column, e.g. 'VARCHAR', 'INTEGER', 'TIMESTAMP'")
    sample_values: list[str] = Field(default=[], description="Representative sample values from this column to aid query generation")


class JoinPath(BaseModel):
    target_urn: str = Field(description="DataHub URN of the dataset this join path leads to")
    join_keys: list[str] = Field(default=[], description="Column names that form the join key, e.g. ['user_id', 'customer_id']")


class SqlContext(BaseModel):
    columns: list[ColumnInfo] = Field(default=[], description="Schema columns available in this dataset for SQL generation context")
    join_paths: list[JoinPath] = Field(default=[], description="Known join paths to related datasets")
    sample_query: str | None = Field(default=None, description="Example SQL query illustrating typical usage of this dataset")


class SearchResultItem(BaseModel):
    urn: str = Field(description="DataHub URN uniquely identifying this dataset")
    name: str = Field(description="Human-readable dataset name")
    platform: str = Field(description="Data platform where this dataset lives, e.g. 'postgres', 'bigquery'")
    description: str | None = Field(default=None, description="Dataset description from DataHub, if available")
    tags: list[str] = Field(default=[], description="Tags associated with this dataset in DataHub")
    owners: list[str] = Field(default=[], description="Owner identifiers (email or URN) for this dataset")
    quality_score: int | None = Field(default=None, description="Quality score (0–100) from the most recent metric measurement, null if not yet measured")
    score: float = Field(description="Semantic similarity score (0.0–1.0) between the search query and this dataset")
    sql_context: SqlContext | None = Field(default=None, description="SQL generation context with schema and join information, included when the request includes sql_context=true")


class SearchResponse(PaginatedResponse):
    datasets: list[SearchResultItem] = Field(default=[], description="Ranked list of datasets matching the search query")


class ReindexResponse(SingleResponse):
    status: str = Field(description="Reindex operation status, e.g. 'started' or 'completed'")
    message: str = Field(default="", description="Additional details about the reindex operation")
