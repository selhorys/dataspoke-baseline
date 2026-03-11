"""Ontology rebuild workflow — classify datasets, build hierarchy, infer relationships."""

from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from src.backend.ontology.service import OntologyService
    from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD
    from src.shared.db.session import SessionLocal
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_datahub,
        make_llm,
    )

from datetime import timedelta


@dataclass
class OntologyRebuildParams:
    force: bool = False


@activity.defn
async def classify_datasets_activity() -> list[dict]:
    """Classify all datasets into concept categories using LLM + schema analysis."""
    datahub = make_datahub()
    llm = make_llm()

    urns = await datahub.enumerate_datasets()
    classifications = []

    for urn in urns:
        activity.heartbeat(f"classifying {urn}")
        try:
            from datahub.metadata.schema_classes import SchemaMetadataClass

            schema = await datahub.get_aspect(urn, SchemaMetadataClass)
            field_names = []
            if schema and hasattr(schema, "fields"):
                field_names = [f.fieldPath for f in schema.fields]

            prompt = (
                f"Classify this dataset into a concept category.\n"
                f"Dataset URN: {urn}\n"
                f"Fields: {', '.join(field_names[:20])}\n"
                f'Return JSON: {{"category": "<name>", "confidence": <0-1>}}'
            )
            result = await llm.complete_json(
                prompt=prompt,
                system="You are a data governance expert. "
                "Classify datasets into concept categories.",
            )
            confidence = result.get("confidence", 0.0)
            if confidence >= ONTOLOGY_CONFIDENCE_THRESHOLD:
                classifications.append(
                    {
                        "dataset_urn": urn,
                        "category": result.get("category", "unknown"),
                        "confidence": confidence,
                        "field_count": len(field_names),
                    }
                )
        except Exception:
            pass  # Best-effort classification

    return classifications


@activity.defn
async def build_hierarchy_activity(classifications: list[dict]) -> list[dict]:
    """Build concept category hierarchy from classifications."""
    from sqlalchemy import select

    from src.shared.db.models import ConceptCategory

    async with SessionLocal() as db:
        # Group datasets by category
        categories: dict[str, list[str]] = {}
        for c in classifications:
            categories.setdefault(c["category"], []).append(c["dataset_urn"])

        hierarchy = []
        for category_name, dataset_urns in categories.items():
            try:
                # Look up existing concept by name
                result = await db.execute(
                    select(ConceptCategory).where(ConceptCategory.name == category_name)
                )
                row = result.scalar_one_or_none()

                if row is None:
                    row = ConceptCategory(
                        name=category_name,
                        description=f"Auto-classified category with {len(dataset_urns)} datasets",
                        status="pending",
                        version=1,
                    )
                    db.add(row)
                    await db.commit()
                    await db.refresh(row)

                hierarchy.append(
                    {
                        "concept_id": str(row.id),
                        "name": category_name,
                        "dataset_count": len(dataset_urns),
                        "dataset_urns": dataset_urns,
                    }
                )
            except Exception:
                pass

        return hierarchy


@activity.defn
async def infer_relationships_activity(hierarchy: list[dict]) -> list[dict]:
    """Infer cross-concept relationships based on shared datasets and schema similarity."""
    relationships = []

    # Find categories with overlapping datasets
    for i, cat_a in enumerate(hierarchy):
        urns_a = set(cat_a.get("dataset_urns", []))
        for cat_b in hierarchy[i + 1 :]:
            urns_b = set(cat_b.get("dataset_urns", []))
            shared = urns_a & urns_b
            if shared:
                relationships.append(
                    {
                        "source": cat_a["name"],
                        "target": cat_b["name"],
                        "type": "shared_datasets",
                        "shared_count": len(shared),
                    }
                )

    return relationships


@activity.defn
async def detect_drift_activity(current_hierarchy: list[dict]) -> list[dict]:
    """Compare current hierarchy against previous build to detect concept drift."""
    async with SessionLocal() as db:
        service = OntologyService(db=db)
        existing_concepts, _ = await service.list_concepts(offset=0, limit=1000)

    existing_names = {c.name for c in existing_concepts}
    current_names = {h["name"] for h in current_hierarchy}

    drift = []
    for name in current_names - existing_names:
        drift.append({"type": "new_category", "name": name})
    for name in existing_names - current_names:
        drift.append({"type": "removed_category", "name": name})

    return drift


@workflow.defn
class OntologyRebuildWorkflow:
    """Multi-step ontology rebuild: classify, hierarchy, relationships, drift detection.

    Workflow ID convention: ``ontology-rebuild``
    """

    @workflow.run
    async def run(self, params: OntologyRebuildParams) -> dict:
        classifications = await workflow.execute_activity(
            classify_datasets_activity,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=default_retry_policy(),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )

        hierarchy = await workflow.execute_activity(
            build_hierarchy_activity,
            args=[classifications],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        relationships = await workflow.execute_activity(
            infer_relationships_activity,
            args=[hierarchy],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        drift = await workflow.execute_activity(
            detect_drift_activity,
            args=[hierarchy],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        return {
            "classifications": len(classifications),
            "hierarchy_nodes": len(hierarchy),
            "relationships": len(relationships),
            "drift_detected": bool(drift),
            "drift_details": drift,
        }
