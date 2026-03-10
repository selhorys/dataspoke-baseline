"""Vector search request/response models aligned with OpenAPI spec."""

from pydantic import BaseModel

from src.api.schemas.common import PaginatedResponse, SingleResponse


class ColumnInfo(BaseModel):
    name: str
    type: str
    sample_values: list[str] = []


class JoinPath(BaseModel):
    target_urn: str
    join_keys: list[str] = []


class SqlContext(BaseModel):
    columns: list[ColumnInfo] = []
    join_paths: list[JoinPath] = []
    sample_query: str | None = None


class SearchResultItem(BaseModel):
    urn: str
    name: str
    platform: str
    description: str | None = None
    tags: list[str] = []
    owners: list[str] = []
    quality_score: int | None = None
    score: float
    sql_context: SqlContext | None = None


class SearchResponse(PaginatedResponse):
    datasets: list[SearchResultItem] = []


class ReindexResponse(SingleResponse):
    status: str
    message: str = ""
