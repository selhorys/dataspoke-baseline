"""Measurer: poorly_documented — datasets whose description is shorter than 20 characters."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient


@register_measurer("poorly_documented")
async def measure(
    datasets: list[str],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[float, dict[str, Any]]:
    """Return count and breakdown of datasets with a description shorter than 20 chars.

    ``db`` is accepted for signature uniformity but not used by this measurer.
    """
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    affected: list[dict[str, Any]] = []
    for urn in datasets:
        props = await datahub.get_aspect(urn, DatasetPropertiesClass)
        desc = getattr(props, "description", None) or "" if props else ""
        if len(desc) < 20:
            affected.append(
                {
                    "urn": urn,
                    "category": "short_description",
                    "detail": {"length": len(desc), "value": desc},
                }
            )

    return float(len(affected)), {
        "dataset_count": len(datasets),
        "datasets": affected,
    }
