"""Unit tests for src/backend/ontogen/evidence.py — gather_evidence.

Covers:
  related_documents populated from searchAcrossEntities
  cap enforced at DOCUMENT_EVIDENCE_CAP_PER_DATASET
  orFilters syntax used (not deprecated filters)
  No DATA_PRODUCT entity type queried
  Absence: UpstreamLineage, DatasetUsageStatistics, GlobalTags, Query entities not fetched
  Presence: unified six-aspect set (datasetProperties, schemaMetadata,
            editableDatasetProperties, editableSchemaMetadata, glossaryTerms,
            related documents) all surfaced

Spec: spec/USE_CASE_en.md §UC3 Inputs
      spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
      spec/DATAHUB_INTEGRATION.md §Document Aspects — orFilters, types=DOCUMENT
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metagen.cross_data import DOCUMENT_EVIDENCE_CAP_PER_DATASET
from src.backend.ontogen.evidence import gather_evidence

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)"


def _make_search_result_item(
    urn: str,
    title: str,
    body: str,
    related_asset_urns: list[str],
    last_modified_ms: int = 1000,
) -> dict:
    """Build a searchAcrossEntities result item dict."""
    return {
        "entity": {
            "urn": urn,
            "info": {
                "title": title,
                "contents": {"text": body},
                "relatedAssets": [{"asset": {"urn": u}} for u in related_asset_urns],
                "lastModified": {"time": last_modified_ms},
            },
        }
    }


def _make_datahub_with_documents(doc_items: list[dict]) -> AsyncMock:
    """Return a mock DataHubClient whose _with_retry returns the given doc items
    for searchAcrossEntities; all get_aspect calls return None; get_timeseries returns [].
    """
    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])

    search_response = {"searchAcrossEntities": {"searchResults": doc_items}}
    datahub._graph = MagicMock()
    datahub._with_retry = AsyncMock(return_value=search_response)

    return datahub


# ── related_documents populated ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_includes_related_documents() -> None:
    """gather_evidence populates 'related_documents' with urn, title, body, related_assets.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — evidence includes documents
    whose relatedAssets reference the dataset; each entry has urn, title, body,
    related_assets.
    """
    doc_items = [
        _make_search_result_item(
            urn="urn:li:document:doc1",
            title="Doc One",
            body="## First document",
            related_asset_urns=[_DATASET_URN],
            last_modified_ms=3000,
        ),
        _make_search_result_item(
            urn="urn:li:document:doc2",
            title="Doc Two",
            body="## Second document",
            related_asset_urns=[_DATASET_URN],
            last_modified_ms=2000,
        ),
        _make_search_result_item(
            urn="urn:li:document:doc3",
            title="Doc Three",
            body="## Third document",
            related_asset_urns=[_DATASET_URN],
            last_modified_ms=1000,
        ),
    ]

    datahub = _make_datahub_with_documents(doc_items)

    evidence = await gather_evidence(_DATASET_URN, datahub)

    assert "related_documents" in evidence
    docs = evidence["related_documents"]
    assert len(docs) == 3

    for doc in docs:
        assert "urn" in doc
        assert "title" in doc
        assert "body" in doc
        assert "related_assets" in doc


# ── cap enforcement ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_cap_per_dataset_respected() -> None:
    """gather_evidence caps related_documents at DOCUMENT_EVIDENCE_CAP_PER_DATASET (=10).

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — document evidence is capped
    to prevent oversized LLM prompts.
    """
    # Return 15 documents (5 over cap)
    doc_items = [
        _make_search_result_item(
            urn=f"urn:li:document:doc{i}",
            title=f"Doc {i}",
            body=f"Body {i}",
            related_asset_urns=[_DATASET_URN],
            last_modified_ms=15000 - i * 100,
        )
        for i in range(15)
    ]

    datahub = _make_datahub_with_documents(doc_items)

    evidence = await gather_evidence(_DATASET_URN, datahub)

    docs = evidence.get("related_documents", [])
    assert len(docs) == DOCUMENT_EVIDENCE_CAP_PER_DATASET, (
        f"Expected {DOCUMENT_EVIDENCE_CAP_PER_DATASET} documents (cap), got {len(docs)}"
    )


# ── orFilters syntax ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_filters_by_related_assets() -> None:
    """gather_evidence filters the document search by relatedAssets containing the dataset URN.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — filtering on entityType: DOCUMENT
    and relatedAssets containing the dataset URN. The spec requires the filtering
    invariant, not a specific GraphQL field name (orFilters vs filters are both valid).
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    # Collect all variables dicts passed to _with_retry for the document search
    all_variables = [
        call_args[1].get("variables") or call_args[0][2]
        for call_args in datahub._with_retry.call_args_list
        if len(call_args[0]) > 2 or "variables" in call_args[1]
    ]

    # Find the document search variables (types contains DOCUMENT)
    doc_search_vars = [
        v
        for v in all_variables
        if isinstance(v, dict) and "DOCUMENT" in v.get("input", {}).get("types", [])
    ]

    assert doc_search_vars, (
        "No document searchAcrossEntities call found — _with_retry may not have been called"
    )

    for v in doc_search_vars:
        query_input = v["input"]

        # Flatten all filter clauses from both orFilters and filters (either is valid)
        all_clauses: list[dict] = []
        for clause in query_input.get("orFilters", []):
            all_clauses.extend(clause.get("and", []))
        for f in query_input.get("filters", []):
            all_clauses.append(f)

        # Spec invariant: at least one filter clause targets 'relatedAssets'
        related_assets_clauses = [c for c in all_clauses if c.get("field") == "relatedAssets"]
        assert related_assets_clauses, (
            f"Expected a filter clause on 'relatedAssets', found clauses: {all_clauses}"
        )

        # Spec invariant: the relatedAssets clause includes the dataset URN as a value
        all_filter_values = [v for c in related_assets_clauses for v in c.get("values", [])]
        assert _DATASET_URN in all_filter_values, (
            f"Expected {_DATASET_URN!r} in relatedAssets filter values, found: {all_filter_values}"
        )


# ── No DATA_PRODUCT lookup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_excludes_data_product_lookups() -> None:
    """gather_evidence never queries for DATA_PRODUCT entities.

    Spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke does not query
    dataProducts in the evidence-gathering phase.
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    # Inspect all _with_retry calls for DATA_PRODUCT in types
    for c in datahub._with_retry.call_args_list:
        # variables may be positional arg[2] or keyword arg
        variables = None
        if len(c[0]) > 2:
            variables = c[0][2]
        elif "variables" in c[1]:
            variables = c[1]["variables"]

        if variables is None:
            continue

        types_used = variables.get("input", {}).get("types", [])
        for t in types_used:
            assert "DATA_PRODUCT" not in str(t).upper() and "DATAPRODUCT" not in str(t).upper(), (
                f"Found DATA_PRODUCT in types during gather_evidence: {types_used}"
            )


# ── Absence: dropped aspects not fetched ─────────────────────────────────────


def _fetched_aspect_class_names(datahub_mock) -> list[str]:
    """Return the __name__ of every class passed as the second arg to get_aspect."""
    names = []
    for call in datahub_mock.get_aspect.call_args_list:
        args = call[0]
        if len(args) > 1:
            aspect_cls = args[1]
            # The class itself is passed (not an instance); use __name__ directly.
            name = getattr(aspect_cls, "__name__", str(aspect_cls))
            names.append(name)
    return names


@pytest.mark.asyncio
async def test_evidence_does_not_read_upstream_lineage() -> None:
    """gather_evidence never fetches UpstreamLineageClass.

    Spec anchor: spec/USE_CASE_en.md §UC3 Inputs — lineage (upstreamLineage)
    is absent from the unified six-aspect input set; it was dropped from the
    evidence boundary in the UC3/UC4 refactor.
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    for name in _fetched_aspect_class_names(datahub):
        assert "UpstreamLineage" not in name, (
            f"UpstreamLineageClass must not be fetched (UC3 input set excludes lineage); "
            f"found: {name}"
        )


@pytest.mark.asyncio
async def test_evidence_does_not_read_usage_stats() -> None:
    """gather_evidence never fetches DatasetUsageStatisticsClass.

    Spec anchor: spec/USE_CASE_en.md §UC3 Inputs — datasetUsageStatistics
    is absent from the unified six-aspect input set.
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    for name in _fetched_aspect_class_names(datahub):
        assert "UsageStatistics" not in name, (
            f"DatasetUsageStatisticsClass must not be fetched; found: {name}"
        )


@pytest.mark.asyncio
async def test_evidence_does_not_read_global_tags() -> None:
    """gather_evidence fetches GlossaryTermsClass but not GlobalTagsClass.

    Spec anchor: spec/USE_CASE_en.md §UC3 Inputs — glossaryTerms is in the
    unified six-aspect set; globalTags is not.
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    fetched_class_names = _fetched_aspect_class_names(datahub)

    # globalTags must not be fetched
    for name in fetched_class_names:
        assert "GlobalTags" not in name, (
            f"GlobalTagsClass must not be fetched (not in UC3 input set); found: {name}"
        )

    # glossaryTerms must be fetched
    assert any("GlossaryTerms" in name for name in fetched_class_names), (
        f"GlossaryTermsClass must be fetched (it is in the UC3 input set); "
        f"fetched: {fetched_class_names}"
    )


@pytest.mark.asyncio
async def test_evidence_does_not_fetch_query_entities() -> None:
    """gather_evidence never calls any DataHub method to list or fetch Query entities.

    Spec anchor: spec/USE_CASE_en.md §UC3 Inputs — DataHub Query entities
    (listQueries / queryProperties / querySubjects) are absent from the
    unified six-aspect input set.

    Two orthogonal guards:
    1. _with_retry: catches GraphQL-route Query lookups (listQueries etc.)
    2. get_aspect class names: catches get_aspect(urn, QueryPropertiesClass) regressions
       that would not surface in _with_retry call args.
    """
    datahub = _make_datahub_with_documents([])

    await gather_evidence(_DATASET_URN, datahub)

    # Guard 1: no Query-related GraphQL operations via _with_retry
    for call in datahub._with_retry.call_args_list:
        args_str = str(call)
        for keyword in ("listQueries", "queryProperties", "querySubjects", "QUERY"):
            assert keyword not in args_str, (
                f"Query-entity GraphQL call ({keyword!r}) must not be made during "
                f"evidence gathering; found in _with_retry call: {args_str[:200]}"
            )

    # Guard 2: no Query-related aspect class fetched via get_aspect
    fetched_classes = _fetched_aspect_class_names(datahub)
    assert not any("Query" in name for name in fetched_classes), (
        f"UC3 must not fetch any Query-related aspect via get_aspect; "
        f"got: {fetched_classes}"
    )


# ── Positive: unified six-aspect set present ─────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_includes_unified_aspect_set() -> None:
    """gather_evidence surfaces all six aspects from the unified input set.

    The six aspects are: datasetProperties, schemaMetadata,
    editableDatasetProperties, editableSchemaMetadata, glossaryTerms,
    and document entities (via relatedAssets).

    Spec anchor: spec/USE_CASE_en.md §UC3 Inputs; spec/feature/BACKEND.md
    §Ontology Generation Service §Inference Pipeline (step 4: Gather evidence).
    """
    from unittest.mock import MagicMock, AsyncMock

    # Build mocks for each of the six aspects
    props_mock = MagicMock()
    props_mock.name = "catalog.books"
    props_mock.description = "Books table"
    props_mock.qualifiedName = "catalog.books"

    schema_mock = MagicMock()
    field = MagicMock()
    field.fieldPath = "isbn"
    field.nativeDataType = "VARCHAR"
    field.description = "Book ISBN"
    schema_mock.fields = [field]

    editable_props_mock = MagicMock()
    editable_props_mock.description = "Approved description"

    editable_schema_mock = MagicMock()
    ef = MagicMock()
    ef.fieldPath = "isbn"
    ef.description = "Approved field desc"
    editable_schema_mock.editableSchemaFieldInfo = [ef]

    glossary_mock = MagicMock()
    term = MagicMock()
    term.urn = "urn:li:glossaryTerm:Book"
    glossary_mock.terms = [term]

    doc_item = _make_search_result_item(
        urn="urn:li:document:doc1",
        title="Doc One",
        body="Content",
        related_asset_urns=[_DATASET_URN],
    )

    # get_aspect is called with the class object as second arg (not an instance).
    # aspect_class.__name__ gives the actual class name for dispatch.
    def _get_aspect_side_effect(urn, aspect_class):
        class_name = getattr(aspect_class, "__name__", "")
        if "DatasetPropertiesClass" in class_name and "Editable" not in class_name:
            return props_mock
        if "SchemaMetadataClass" in class_name and "Editable" not in class_name:
            return schema_mock
        if "GlossaryTermsClass" in class_name:
            return glossary_mock
        if "EditableDatasetPropertiesClass" in class_name:
            return editable_props_mock
        if "EditableSchemaMetadataClass" in class_name:
            return editable_schema_mock
        return None

    datahub = AsyncMock()
    datahub.get_aspect = AsyncMock(side_effect=_get_aspect_side_effect)
    datahub.get_timeseries = AsyncMock(return_value=[])

    search_response = {"searchAcrossEntities": {"searchResults": [doc_item]}}
    datahub._graph = MagicMock()
    datahub._with_retry = AsyncMock(return_value=search_response)

    evidence = await gather_evidence(_DATASET_URN, datahub)

    # Each of the six aspects must appear in the evidence dict.
    # datasetProperties surfaces both dataset_name and description (both seeded above).
    assert "dataset_name" in evidence and "description" in evidence, (
        "datasetProperties evidence keys 'dataset_name' and 'description' must both be present"
    )
    assert "schema_fields" in evidence, "schemaMetadata evidence key missing"
    # editableDatasetProperties → editable_description; editableSchemaMetadata → editable_field_descriptions
    # Both are seeded above and must both appear.
    assert "editable_description" in evidence and "editable_field_descriptions" in evidence, (
        "Both 'editable_description' (from editableDatasetProperties) and "
        "'editable_field_descriptions' (from editableSchemaMetadata) must be present"
    )
    assert "glossary_terms" in evidence, "glossaryTerms evidence key missing"
    assert "related_documents" in evidence, "document entity evidence key missing"
