from pydantic import BaseModel

from src.shared.models.quality import QualityScore


class DatasetSummary(BaseModel):
    urn: str
    name: str
    platform: str
    description: str | None = None
    owners: list[str] = []
    tags: list[str] = []


class DatasetAttributes(BaseModel):
    urn: str
    column_count: int
    fields: list[str] = []
    owners: list[str] = []
    tags: list[str] = []
    description: str | None = None
    quality_score: QualityScore | None = None
