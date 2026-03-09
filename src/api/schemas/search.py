"""Vector search request/response models."""

from typing import Any

from pydantic import BaseModel, Field

from src.api.schemas.common import PaginatedResponse, SingleResponse


class SearchRequest(BaseModel):
    q: str = Field(..., min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    platform: str | None = None
    has_pii: bool | None = None
    tags: list[str] = []


class SearchResultItem(BaseModel):
    urn: str
    name: str
    platform: str
    description: str | None = None
    score: float
    tags: list[str] = []
    metadata: dict[str, Any] = {}


class SearchResponse(PaginatedResponse):
    results: list[SearchResultItem] = []


class ReindexRequest(BaseModel):
    platform: str | None = None
    force: bool = False


class ReindexResponse(SingleResponse):
    task_id: str
    status: str
    message: str = ""
