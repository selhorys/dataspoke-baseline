"""Health score aggregation — compute per-department quality scores."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.scoring import compute_quality_score
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import DepartmentMapping


class DepartmentHealth:
    """Aggregated health metrics for a single department."""

    __slots__ = ("department", "avg_score", "dataset_count", "worst_datasets")

    def __init__(
        self,
        department: str,
        avg_score: float,
        dataset_count: int,
        worst_datasets: list[dict[str, Any]],
    ) -> None:
        self.department = department
        self.avg_score = avg_score
        self.dataset_count = dataset_count
        self.worst_datasets = worst_datasets


async def aggregate_health_scores(
    datahub: DataHubClient,
    db: AsyncSession,
    cache: RedisClient | None = None,
) -> dict[str, DepartmentHealth]:
    """Enumerate all datasets, compute quality scores, group by department.

    1. Enumerate all datasets from DataHub
    2. For each dataset, compute quality score
    3. Look up owner from OwnershipClass
    4. Map owner_urn -> department via department_mapping table
    5. Aggregate per-department: avg score, dataset count, worst datasets
    """
    from datahub.metadata.schema_classes import OwnershipClass

    datasets = await datahub.enumerate_datasets()
    if not datasets:
        return {}

    # Load department mappings
    result = await db.execute(select(DepartmentMapping))
    mappings = {row.owner_urn: row.department for row in result.scalars().all()}

    # Collect per-department scores
    dept_scores: dict[str, list[dict[str, Any]]] = {}

    for urn in datasets:
        try:
            score = await compute_quality_score(datahub, urn, cache=cache)
        except Exception:
            continue

        # Determine department from dataset owner
        ownership = await datahub.get_aspect(urn, OwnershipClass)
        owner_urn = None
        if ownership and getattr(ownership, "owners", []):
            owner_urn = ownership.owners[0].owner

        department = mappings.get(owner_urn, "Unknown") if owner_urn else "Unknown"

        entry = {"urn": urn, "score": score.overall_score}
        dept_scores.setdefault(department, []).append(entry)

    # Aggregate
    result_map: dict[str, DepartmentHealth] = {}
    for dept, entries in dept_scores.items():
        avg = sum(e["score"] for e in entries) / len(entries)
        sorted_entries = sorted(entries, key=lambda e: e["score"])
        worst = sorted_entries[:5]
        result_map[dept] = DepartmentHealth(
            department=dept,
            avg_score=round(avg, 2),
            dataset_count=len(entries),
            worst_datasets=worst,
        )

    return result_map
