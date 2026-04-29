"""Evidence gatherer for the Ontology Generation inference pipeline.

For each dataset, this module fetches:
  - Canonical aspects (schemaMetadata, datasetProperties, globalTags,
    glossaryTerms, upstreamLineage, usageStats)
  - UC4-approved editable aspects (editableDatasetProperties.description,
    editableSchemaMetadata, dataProductProperties on intersecting dataProducts)
  - Bounded DataHub Query lists (MANUAL up to max_manual_queries_per_dataset;
    SYSTEM multi-asset joins up to max_system_queries_per_dataset)

All DataHub failures are best-effort — a failure returns reduced evidence and
logs a WARNING.  The inference pipeline continues with the available data.

Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
      spec/DATAHUB_INTEGRATION.md §Query Aspects
"""

import logging
from typing import Any

from src.shared.datahub.client import DataHubClient
from src.shared.db.models import OntogenConfig

logger = logging.getLogger(__name__)


async def gather_evidence(
    dataset_urn: str,
    datahub: DataHubClient,
    conf: OntogenConfig,
) -> dict[str, Any]:
    """Gather all evidence for *dataset_urn* needed by the LLM inference step.

    Returns a dict with the following keys (all best-effort; missing if
    unavailable):

    - dataset_name (str)
    - description (str) — from ``datasetProperties``
    - platform (str)
    - schema_fields (list[dict]) — each has fieldPath, nativeDataType, description
    - tags (list[str]) — DataHub tag URNs
    - glossary_terms (list[str]) — DataHub glossary-term URNs
    - upstream_urns (list[str])
    - usage_stats (dict) — from ``datasetUsageStatistics`` (latest timeseries)
    - editable_description (str | None) — from ``editableDatasetProperties``
    - editable_field_descriptions (list[dict]) — approved column descs
    - queries (list[dict]) — MANUAL + SYSTEM queries (capped)
    """
    evidence: dict[str, Any] = {}

    # ── Canonical aspects ─────────────────────────────────────────────────

    try:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        props = await datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        if props:
            evidence["dataset_name"] = getattr(props, "name", "") or ""
            evidence["description"] = getattr(props, "description", "") or ""
            # Extract platform from the qualified name if available
            qn = getattr(props, "qualifiedName", None) or ""
            evidence["qualified_name"] = qn
    except Exception:
        logger.warning(
            "ontogen_evidence_dataset_properties_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )

    try:
        from datahub.metadata.schema_classes import SchemaMetadataClass

        schema_meta = await datahub.get_aspect(dataset_urn, SchemaMetadataClass)
        if schema_meta and hasattr(schema_meta, "fields"):
            evidence["schema_fields"] = [
                {
                    "fieldPath": getattr(f, "fieldPath", ""),
                    "nativeDataType": getattr(f, "nativeDataType", "") or "",
                    "description": getattr(f, "description", "") or "",
                }
                for f in schema_meta.fields
            ]
        else:
            evidence["schema_fields"] = []
    except Exception:
        logger.warning(
            "ontogen_evidence_schema_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("schema_fields", [])

    try:
        from datahub.metadata.schema_classes import GlobalTagsClass

        global_tags = await datahub.get_aspect(dataset_urn, GlobalTagsClass)
        if global_tags and hasattr(global_tags, "tags"):
            evidence["tags"] = [str(t.tag) for t in global_tags.tags]
        else:
            evidence["tags"] = []
    except Exception:
        logger.warning(
            "ontogen_evidence_tags_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("tags", [])

    try:
        from datahub.metadata.schema_classes import GlossaryTermsClass

        glossary_terms = await datahub.get_aspect(dataset_urn, GlossaryTermsClass)
        if glossary_terms and hasattr(glossary_terms, "terms"):
            evidence["glossary_terms"] = [str(t.urn) for t in glossary_terms.terms]
        else:
            evidence["glossary_terms"] = []
    except Exception:
        logger.warning(
            "ontogen_evidence_glossary_terms_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("glossary_terms", [])

    try:
        from datahub.metadata.schema_classes import UpstreamLineageClass

        upstream_lineage = await datahub.get_aspect(dataset_urn, UpstreamLineageClass)
        if upstream_lineage and hasattr(upstream_lineage, "upstreams"):
            evidence["upstream_urns"] = [
                str(u.dataset)
                for u in upstream_lineage.upstreams
                if hasattr(u, "dataset")
            ]
        else:
            evidence["upstream_urns"] = []
    except Exception:
        logger.warning(
            "ontogen_evidence_lineage_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("upstream_urns", [])

    try:
        from datahub.metadata.schema_classes import DatasetUsageStatisticsClass

        usage_list = await datahub.get_timeseries(dataset_urn, DatasetUsageStatisticsClass, limit=1)
        if usage_list:
            u = usage_list[0]
            evidence["usage_stats"] = {
                "unique_user_count": getattr(u, "uniqueUserCount", 0),
                "total_sql_queries": getattr(u, "totalSqlQueries", 0),
            }
    except Exception:
        logger.warning(
            "ontogen_evidence_usage_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )

    # ── UC4-approved editable aspects ─────────────────────────────────────

    try:
        from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

        editable_props = await datahub.get_aspect(dataset_urn, EditableDatasetPropertiesClass)
        if editable_props:
            evidence["editable_description"] = getattr(editable_props, "description", None)
    except Exception:
        logger.warning(
            "ontogen_evidence_editable_props_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )

    try:
        from datahub.metadata.schema_classes import EditableSchemaMetadataClass

        editable_schema = await datahub.get_aspect(dataset_urn, EditableSchemaMetadataClass)
        if editable_schema and hasattr(editable_schema, "editableSchemaFieldInfo"):
            editable_fields = [
                {
                    "fieldPath": getattr(f, "fieldPath", ""),
                    "description": getattr(f, "description", "") or "",
                }
                for f in editable_schema.editableSchemaFieldInfo
                if getattr(f, "description", None)
            ]
            evidence["editable_field_descriptions"] = editable_fields
        else:
            evidence["editable_field_descriptions"] = []
    except Exception:
        logger.warning(
            "ontogen_evidence_editable_schema_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("editable_field_descriptions", [])

    # ── DataHub Query entities ────────────────────────────────────────────

    max_manual = conf.max_manual_queries_per_dataset
    max_system = conf.max_system_queries_per_dataset

    queries: list[dict[str, Any]] = []

    if max_manual > 0:
        try:
            manual_queries = await _fetch_queries(datahub, dataset_urn, source="MANUAL")
            # MANUAL: take up to cap, no subject-count restriction
            manual_queries = manual_queries[:max_manual]
            queries.extend(manual_queries)
        except Exception:
            logger.warning(
                "ontogen_evidence_manual_queries_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )

    if max_system > 0:
        try:
            system_queries = await _fetch_queries(datahub, dataset_urn, source="SYSTEM")
            # SYSTEM: only multi-asset joins (len(subjects) >= 2)
            # Sort joins-first by subject count desc, then lastModified desc
            system_queries = [q for q in system_queries if len(q.get("subjects", [])) >= 2]
            system_queries.sort(
                key=lambda q: (-len(q.get("subjects", [])), -(q.get("last_modified") or 0))
            )
            system_queries = system_queries[:max_system]
            queries.extend(system_queries)
        except Exception:
            logger.warning(
                "ontogen_evidence_system_queries_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )

    if queries:
        evidence["queries"] = queries

    return evidence


async def _fetch_queries(
    datahub: DataHubClient,
    dataset_urn: str,
    source: str,
) -> list[dict[str, Any]]:
    """List DataHub Query entities for *dataset_urn* filtered by *source*.

    Uses ``DataHubGraph.execute_graphql`` with the ``listQueries`` query.
    Returns a list of dicts with keys: name, statement, source, subjects,
    last_modified.

    Best-effort — caller must wrap in try/except.
    """
    gql = """
    query listQueriesByDataset($input: ListQueriesInput!) {
        listQueries(input: $input) {
            total
            start
            count
            queries {
                urn
                properties {
                    name
                    description
                    statement { value language }
                    source
                    lastModified { time }
                }
                subjects {
                    dataset { urn }
                }
            }
        }
    }
    """
    variables: dict[str, Any] = {
        "input": {
            "start": 0,
            "count": 100,
            "filters": [
                {"field": "entities", "value": dataset_urn},
                {"field": "source", "value": source},
            ],
        }
    }

    # DataHubClient wraps execute_graphql
    result = await datahub._with_retry(
        datahub._graph.execute_graphql, gql, variables=variables
    )

    queries_data = (result or {}).get("listQueries", {}).get("queries", [])
    out: list[dict[str, Any]] = []
    for q in queries_data:
        props = q.get("properties") or {}
        subjects_raw = q.get("subjects") or []
        subject_urns = [
            s["dataset"]["urn"]
            for s in subjects_raw
            if s.get("dataset") and s["dataset"].get("urn")
        ]
        stmt = (props.get("statement") or {}).get("value", "")
        last_mod_ms: int = (props.get("lastModified") or {}).get("time") or 0
        out.append(
            {
                "urn": q.get("urn", ""),
                "name": props.get("name") or "",
                "description": props.get("description") or "",
                "statement": stmt,
                "source": props.get("source", source),
                "subjects": subject_urns,
                "last_modified": last_mod_ms,
            }
        )
    return out
