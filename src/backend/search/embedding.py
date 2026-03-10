"""Embedding text builder and vector generation for dataset metadata."""

from src.shared.datahub.client import DataHubClient
from src.shared.llm.client import LLMClient


def build_embedding_text(
    name: str,
    description: str | None = None,
    fields: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
    lineage_context: list[str] | None = None,
) -> str:
    """Concatenate dataset metadata into a single text for embedding.

    Pure function, separated for testability.
    """
    parts: list[str] = [name]

    if description:
        parts.append(description)

    if fields:
        field_strs = []
        for f in fields:
            fname = f.get("name", "")
            fdesc = f.get("description", "")
            field_strs.append(f"{fname}: {fdesc}" if fdesc else fname)
        if field_strs:
            parts.append(f"Fields: {', '.join(field_strs)}")

    if tags:
        parts.append(f"Tags: {', '.join(tags)}")

    if lineage_context:
        parts.append(f"Lineage: upstream of {', '.join(lineage_context)}")

    return "\n".join(parts)


def _extract_name_from_urn(urn: str) -> str:
    """Extract a human-readable dataset name from a DataHub URN."""
    # urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)
    try:
        inner = urn.split("(", 1)[1].rstrip(")")
        parts = inner.split(",")
        return parts[1] if len(parts) >= 2 else urn
    except (IndexError, ValueError):
        return urn


def _extract_platform_from_urn(urn: str) -> str:
    """Extract the platform name from a DataHub dataset URN."""
    try:
        inner = urn.split("(", 1)[1].rstrip(")")
        platform_part = inner.split(",")[0]
        return platform_part.split(":")[-1]
    except (IndexError, ValueError):
        return "unknown"


async def generate_embedding(
    llm: LLMClient,
    datahub: DataHubClient,
    dataset_urn: str,
) -> tuple[list[float], dict]:
    """Fetch dataset metadata from DataHub and generate a vector embedding.

    Returns (embedding_vector, payload_metadata) where payload_metadata
    contains fields to store alongside the vector in Qdrant.
    """
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        GlobalTagsClass,
        OwnershipClass,
        SchemaMetadataClass,
    )

    props = await datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
    schema_meta = await datahub.get_aspect(dataset_urn, SchemaMetadataClass)
    global_tags = await datahub.get_aspect(dataset_urn, GlobalTagsClass)
    ownership = await datahub.get_aspect(dataset_urn, OwnershipClass)

    # Extract metadata
    name = _extract_name_from_urn(dataset_urn)
    description: str | None = None
    if props:
        name = getattr(props, "name", None) or name
        description = getattr(props, "description", None)

    fields: list[dict[str, str]] = []
    if schema_meta and hasattr(schema_meta, "fields"):
        for field in schema_meta.fields:
            fields.append(
                {
                    "name": field.fieldPath,
                    "description": getattr(field, "description", "") or "",
                }
            )

    tags: list[str] = []
    if global_tags and hasattr(global_tags, "tags"):
        tags = [str(t.tag).removeprefix("urn:li:tag:") for t in global_tags.tags]

    owners: list[str] = []
    if ownership and hasattr(ownership, "owners"):
        owners = [str(o.owner) for o in ownership.owners]

    # Get upstream lineage for context
    upstream_urns = await datahub.get_upstream_lineage(dataset_urn)
    lineage_names = [_extract_name_from_urn(u) for u in upstream_urns] if upstream_urns else None

    text = build_embedding_text(
        name=name,
        description=description,
        fields=fields if fields else None,
        tags=tags if tags else None,
        lineage_context=lineage_names,
    )

    embedding = await llm.embed(text)

    platform = _extract_platform_from_urn(dataset_urn)
    payload = {
        "dataset_urn": dataset_urn,
        "platform": platform,
        "tags": tags,
        "owners": owners,
        "quality_score": None,
        "has_pii": any("pii" in t.lower() for t in tags),
    }

    return embedding, payload
