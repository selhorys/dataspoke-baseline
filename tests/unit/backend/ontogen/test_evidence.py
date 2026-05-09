"""Unit tests for src/backend/ontogen/evidence.py — gather_evidence.

Covers:
  related_documents populated from searchAcrossEntities
  cap enforced at DOCUMENT_EVIDENCE_CAP_PER_DATASET
  orFilters syntax used (not deprecated filters)
  No DATA_PRODUCT entity type queried

Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
      spec/DATAHUB_INTEGRATION.md §Document Aspects — orFilters, types=DOCUMENT
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metagen.cross_data import DOCUMENT_EVIDENCE_CAP_PER_DATASET
from src.backend.ontogen.evidence import gather_evidence

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)"


def _make_conf(
    max_manual_queries: int = 0,
    max_system_queries: int = 0,
) -> MagicMock:
    """Minimal OntogenConfig mock."""
    conf = MagicMock()
    conf.max_manual_queries_per_dataset = max_manual_queries
    conf.max_system_queries_per_dataset = max_system_queries
    return conf


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
    conf = _make_conf()

    evidence = await gather_evidence(_DATASET_URN, datahub, conf)

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
    conf = _make_conf()

    evidence = await gather_evidence(_DATASET_URN, datahub, conf)

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
    conf = _make_conf()

    await gather_evidence(_DATASET_URN, datahub, conf)

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
    conf = _make_conf(max_manual_queries=0, max_system_queries=0)

    await gather_evidence(_DATASET_URN, datahub, conf)

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
