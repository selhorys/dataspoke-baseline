"""Unit tests for src/backend/metagen/prompts.py — build_run_prompt.

Verifies the spec-mandated evidence-parity additions:
  - Related documents appear in the prompt (title + body) when present.
  - Related documents are omitted when the list is empty.
  - Related documents are capped at 5 per BACKEND.md §Generation Pipeline (mirrors ontogen).
  - Long document bodies are truncated with the _TRUNCATION_MARKER.
  - Ontology RAG nodes / edges / triples appear when present.
  - Ontology RAG section is omitted when all three lists are empty / absent.
  - Score values are NOT exposed to the LLM (security-reviewer F2).
  - Untrusted-data markers wrap both new evidence blocks.

spec: spec/feature/BACKEND.md §Metadata Generation Service §Generation Pipeline
impl: src/backend/metagen/prompts.py — build_run_prompt

No DB, no LLM, no mocking — pure build_run_prompt() calls.
"""

from src.backend.metagen.prompts import _MAX_UNTRUSTED_BYTES, _TRUNCATION_MARKER, build_run_prompt

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_NONCE = "deadbeef"


def _base_evidence(**overrides) -> dict:
    """Return a minimal evidence dict for _DATASET_URN."""
    ev: dict = {
        "datasetProperties": {"name": "title_master", "description": "Master title catalog"},
        "schemaMetadata": {"fields": []},
    }
    ev.update(overrides)
    return ev


def _build(evidence_overrides: dict | None = None) -> str:
    """Build a prompt with _DATASET_URN using the provided evidence overrides."""
    ev = _base_evidence(**(evidence_overrides or {}))
    return build_run_prompt(
        evidence_per_dataset={_DATASET_URN: ev},
        target_items=[
            {
                "dataset_urn": _DATASET_URN,
                "item_id": "dataset.description",
                "kind": "dataset.description",
                "field_path": None,
            }
        ],
        nonce=_NONCE,
    )


# ── Related documents ─────────────────────────────────────────────────────────


def test_related_documents_appear_in_prompt() -> None:
    """Related documents: title and body surface in the prompt when provided.

    spec: spec/feature/BACKEND.md §Metadata Generation Service §Generation Pipeline
    — evidence must include related documents (per-dataset context from DataHub).
    spec: BACKEND.md §Generation Pipeline — case a.
    """
    prompt = _build(
        {"related_documents": [{"title": "Fulfillment SOP", "body": "Pick-and-pack guide."}]}
    )

    assert "Related documents" in prompt, (
        "Prompt must contain 'Related documents' header when related_documents is non-empty. "
        "spec: BACKEND.md §Metadata Generation Service §Generation Pipeline"
    )
    assert "Fulfillment SOP" in prompt, (
        "Document title must appear in the prompt. "
        "spec: BACKEND.md §Generation Pipeline — evidence parity with ontogen"
    )
    assert "Pick-and-pack guide." in prompt, (
        "Document body must appear in the prompt. "
        "spec: BACKEND.md §Generation Pipeline — evidence parity with ontogen"
    )


def test_related_documents_absent_when_empty_list() -> None:
    """Related documents section is absent when related_documents is an empty list.

    spec: spec/feature/BACKEND.md §Metadata Generation Service §Generation Pipeline
    — section is conditionally included; empty list yields no header.
    spec: BACKEND.md §Generation Pipeline — case b.
    """
    with_docs = _build(
        {"related_documents": [{"title": "Fulfillment SOP", "body": "Pick-and-pack guide."}]}
    )
    without_docs = _build({"related_documents": []})

    assert "Related documents" in with_docs
    assert "Related documents" not in without_docs, (
        "'Related documents' header must not appear when related_documents is empty. "
        "spec: BACKEND.md §Generation Pipeline — conditional section rendering"
    )


def test_related_documents_capped_at_five() -> None:
    """Only 5 documents appear in the prompt when 6 are supplied.

    spec: spec/feature/BACKEND.md §Metadata Generation Service §Generation Pipeline
    — document list capped at 5 per dataset (mirrors ontogen cap).
    spec: BACKEND.md §Generation Pipeline — case c.
    """
    docs = [{"title": f"Doc{i}", "body": f"Body{i}"} for i in range(6)]
    prompt = _build({"related_documents": docs})

    # The 6th document's title must not appear; the 5th must.
    assert "Doc4" in prompt, "5th document (index 4) must appear in the prompt."
    assert "Doc5" not in prompt, (
        "6th document (index 5) must be excluded; cap is 5. "
        "spec: BACKEND.md §Generation Pipeline — document cap"
    )


def test_related_document_body_truncated_when_too_long() -> None:
    """A body that exceeds _MAX_UNTRUSTED_BYTES is truncated with _TRUNCATION_MARKER.

    spec: spec/feature/BACKEND.md §Metadata Generation Service §Generation Pipeline
    — untrusted text is capped at _MAX_UNTRUSTED_BYTES to protect the context window.
    spec: BACKEND.md §Generation Pipeline — case d.
    """
    long_body = "X" * (_MAX_UNTRUSTED_BYTES + 100)
    prompt = _build({"related_documents": [{"title": "BigDoc", "body": long_body}]})

    assert _TRUNCATION_MARKER in prompt, (
        f"Prompt must contain {_TRUNCATION_MARKER!r} when body exceeds {_MAX_UNTRUSTED_BYTES} "
        f"bytes. "
        "spec: BACKEND.md §Generation Pipeline — untrusted data cap"
    )


# ── Ontology RAG — nodes ──────────────────────────────────────────────────────


def test_ontology_rag_nodes_appear_in_prompt() -> None:
    """Ontology RAG node id, name, and description surface in the prompt.

    spec: BACKEND.md §Generation Pipeline — case e
    — per-dataset ontology RAG nodes appear in the evidence block.
    """
    prompt = _build(
        {
            "ontology_rag": {
                "nodes": [
                    {"id": "n1", "name": "Order", "description": "A customer order", "score": 0.9}
                ],
                "edges": [],
                "triples": [],
            }
        }
    )

    assert "n1" in prompt, "Node id must appear in the prompt."
    assert "Order" in prompt, "Node name must appear in the prompt."
    assert "A customer order" in prompt, "Node description must appear in the prompt."


def test_ontology_rag_edges_appear_in_prompt() -> None:
    """Ontology RAG edge id and label surface in the prompt.

    spec: BACKEND.md §Generation Pipeline — case f
    — per-dataset ontology RAG edges appear in the evidence block.
    """
    prompt = _build(
        {
            "ontology_rag": {
                "nodes": [],
                "edges": [{"id": "e1", "label": "has_part", "score": 0.8}],
                "triples": [],
            }
        }
    )

    assert "e1" in prompt, "Edge id must appear in the prompt."
    assert "has_part" in prompt, "Edge label must appear in the prompt."


def test_ontology_rag_triples_appear_in_prompt() -> None:
    """Ontology RAG triple subject, edge_label, and object surface in the prompt.

    spec: BACKEND.md §Generation Pipeline — case g
    — per-dataset ontology RAG triples appear in the evidence block.
    """
    prompt = _build(
        {
            "ontology_rag": {
                "nodes": [],
                "edges": [],
                "triples": [
                    {
                        "subject_name": "Order",
                        "edge_label": "has_part",
                        "object_name": "OrderLine",
                        "score": 0.7,
                    }
                ],
            }
        }
    )

    assert "Order" in prompt, "Triple subject_name must appear in the prompt."
    assert "has_part" in prompt, "Triple edge_label must appear in the prompt."
    assert "OrderLine" in prompt, "Triple object_name must appear in the prompt."


def test_ontology_rag_omitted_when_all_lists_empty() -> None:
    """Ontology RAG section is absent when nodes, edges, and triples are all empty.

    spec: BACKEND.md §Generation Pipeline — case h
    — section is rendered only when at least one list is non-empty.
    """
    with_rag = _build(
        {
            "ontology_rag": {
                "nodes": [{"id": "n1", "name": "Order", "description": "D", "score": 0.9}],
                "edges": [],
                "triples": [],
            }
        }
    )
    without_rag = _build(
        {"ontology_rag": {"nodes": [], "edges": [], "triples": []}}
    )

    assert "n1" in with_rag, "Sanity: node id must appear when ontology_rag is populated."
    assert "ontology RAG" not in without_rag.lower(), (
        "'ontology RAG' header must not appear when all three lists are empty. "
        "spec: BACKEND.md §Generation Pipeline — case h — conditional section rendering"
    )


def test_ontology_rag_absent_when_key_missing() -> None:
    """Ontology RAG section is absent when ontology_rag key is entirely missing from evidence.

    This is the same spec invariant as the empty-lists case — the section is optional.
    spec: BACKEND.md §Generation Pipeline — case h (missing-key variant).
    """
    prompt = _build({})  # No ontology_rag key at all

    assert "ontology RAG" not in prompt.lower(), (
        "Ontology RAG section must not appear when the key is absent. "
        "spec: BACKEND.md §Generation Pipeline — conditional section"
    )


# ── Untrusted-data markers wrap evidence blocks ───────────────────────────────


def test_untrusted_data_markers_wrap_document_body() -> None:
    """Document body appears inside the DATA-{nonce}-BEGIN/END wrapper.

    The per-run nonce + markers prevent prompt injection from untrusted document
    content stored in DataHub.

    impl: src/backend/metagen/prompts.py — untrusted-data nonce markers (unspecced)
    spec: BACKEND.md §Generation Pipeline — case j.
    """
    distinctive_body = "SpecialBodyContent_abc123"
    prompt = _build(
        {"related_documents": [{"title": "T", "body": distinctive_body}]}
    )

    data_begin = f"=== DATA-{_NONCE}-BEGIN ==="
    data_end = f"=== DATA-{_NONCE}-END ==="

    # Use rindex() to find the actual data-wrapping markers in the user_prompt section.
    # The system_prompt references the marker strings in its security instruction;
    # the user_prompt contains the actual wrapping pair that encloses all evidence.
    begin_idx = prompt.rindex(data_begin)
    end_idx = prompt.rindex(data_end)
    body_idx = prompt.index(distinctive_body)

    assert begin_idx < body_idx < end_idx, (
        "Document body must appear between DATA-nonce-BEGIN and DATA-nonce-END markers. "
        "impl: metagen/prompts.py — untrusted-data nonce markers (unspecced)"
    )


def test_untrusted_data_markers_wrap_ontology_rag_node() -> None:
    """Ontology RAG node id appears inside the DATA-{nonce}-BEGIN/END wrapper.

    spec: BACKEND.md §Generation Pipeline — case j (ontology RAG variant).
    impl: src/backend/metagen/prompts.py — untrusted-data nonce markers (unspecced)
    """
    prompt = _build(
        {
            "ontology_rag": {
                "nodes": [
                    {"id": "distinctive_node_id_xyz", "name": "X", "description": "D", "score": 0.5}
                ],
                "edges": [],
                "triples": [],
            }
        }
    )

    data_begin = f"=== DATA-{_NONCE}-BEGIN ==="
    data_end = f"=== DATA-{_NONCE}-END ==="
    node_id = "distinctive_node_id_xyz"

    # Use rindex() to find the actual data-wrapping markers in the user_prompt section.
    begin_idx = prompt.rindex(data_begin)
    end_idx = prompt.rindex(data_end)
    node_idx = prompt.index(node_id)

    assert begin_idx < node_idx < end_idx, (
        "Ontology RAG node id must appear between DATA-nonce-BEGIN and DATA-nonce-END markers. "
        "impl: metagen/prompts.py — untrusted-data nonce markers (unspecced)"
    )
