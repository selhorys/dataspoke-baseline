"""Evidence gatherer for the Ontology Generation inference pipeline.

For each dataset, this module fetches the unified six-aspect proofread boundary
shared with UC4:
  - datasetProperties
  - schemaMetadata
  - editableDatasetProperties (description — UC4-approved editable aspect)
  - editableSchemaMetadata (per-field descriptions — UC4-approved)
  - glossaryTerms
  - documentInfo on document entities whose relatedAssets reference the dataset
    (capped at _DOCUMENT_EVIDENCE_CAP_PER_DATASET)

All DataHub failures are best-effort — a failure returns reduced evidence and
logs a WARNING.  The inference pipeline continues with the available data.

Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
      spec/DATAHUB_INTEGRATION.md §Document Aspects
      spec/USE_CASE_en.md §UC3 Inputs
"""

import logging
from typing import Any

from src.shared.datahub.client import DataHubClient
from src.shared.datahub.documents import fetch_related_documents

logger = logging.getLogger(__name__)


async def gather_evidence(
    dataset_urn: str,
    datahub: DataHubClient,
) -> dict[str, Any]:
    """Gather all evidence for *dataset_urn* needed by the LLM inference step.

    Returns a dict with the following keys (all best-effort; missing if
    unavailable):

    - dataset_name (str)
    - description (str) — from ``datasetProperties``
    - platform (str)
    - schema_fields (list[dict]) — each has fieldPath, nativeDataType, description
    - glossary_terms (list[str]) — DataHub glossary-term URNs
    - editable_description (str | None) — from ``editableDatasetProperties``
    - editable_field_descriptions (list[dict]) — approved column descs
    - related_documents (list[dict]) — documents whose relatedAssets reference this dataset
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

    # ── Related document entities ─────────────────────────────────────────

    try:
        docs = await fetch_related_documents(dataset_urn, datahub)
        evidence["related_documents"] = docs
    except Exception:
        logger.warning(
            "ontogen_evidence_related_documents_failed",
            extra={"dataset_urn": dataset_urn},
            exc_info=True,
        )
        evidence.setdefault("related_documents", [])

    return evidence


