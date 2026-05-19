"""Measurer: doc-health — counts datasets with complete table and column documentation.

A dataset scores 1.0 iff:
  - Its resolved table description (EditableDatasetProperties overlays
    DatasetProperties) is non-empty after stripping whitespace.
  - Every column in its resolved schema (EditableSchemaMetadata overlays
    SchemaMetadata) carries a non-empty description.

A dataset with no schema metadata scores 0.0 because "every column" cannot be
satisfied when the column set is unknown.

Spec: spec/feature/BACKEND.md §Metrics Service — doc-health
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers.registry import register_measurer
from src.shared.datahub.client import DataHubClient


@register_measurer("doc-health")
async def measure(
    datasets: list[str],
    metric_conf: dict[str, Any],
    *,
    datahub: DataHubClient,
    db: AsyncSession,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return doc-health values and a breakdown of undocumented datasets.

    Parameters
    ----------
    datasets:
        Dataset URNs to measure.
    metric_conf:
        Unused for this metric type (must be ``{}``).
    datahub:
        DataHubClient for DatasetProperties / SchemaMetadata aspect reads.
    db:
        AsyncSession — accepted for signature uniformity, not used here.

    Returns
    -------
    tuple[dict[str, float], dict]
        ``(values, breakdown)`` where values has keys ``total`` and
        ``doc_health``; breakdown lists only datasets with score < 1.0.
    """
    total = len(datasets)
    doc_health = 0.0
    failed_datasets: list[dict[str, Any]] = []

    aspects_map = await datahub.get_dataset_documentation_aspects(datasets)

    for urn in datasets:
        aspects = aspects_map[urn]

        # Resolve table description: editable overlay wins when non-empty
        table_desc = aspects.editable_table_description or aspects.table_description
        has_table_desc = bool(table_desc and table_desc.strip())

        if not aspects.field_descriptions:
            # No schema metadata (or empty schema) — cannot satisfy "every column described"
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "missing_table_description": not has_table_desc,
                        "missing_column_descriptions": [],
                    },
                }
            )
            continue

        # Merge field descriptions: base first, editable overlay wins
        field_descs: dict[str, str] = dict(aspects.field_descriptions)
        for fp, desc in aspects.editable_field_descriptions.items():
            if desc:
                field_descs[fp] = desc

        missing_columns: list[str] = [
            fp for fp, desc in field_descs.items() if not (desc and desc.strip())
        ]

        if has_table_desc and not missing_columns:
            doc_health += 1.0
        else:
            failed_datasets.append(
                {
                    "urn": urn,
                    "detail": {
                        "missing_table_description": not has_table_desc,
                        "missing_column_descriptions": missing_columns,
                    },
                }
            )

    values: dict[str, float] = {
        "total": float(total),
        "doc_health": float(doc_health),
    }
    breakdown: dict[str, Any] = {
        "dataset_count": total,
        "datasets": failed_datasets,
    }
    return values, breakdown
