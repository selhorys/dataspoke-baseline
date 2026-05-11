"""Unit tests for src/backend/ontogen/validator.py.

Spec: spec/feature/BACKEND.md §Ontogen validator rules (8-rule table).
Each group defends one rule (or helper) from the spec invariant —
NOT from current line-by-line impl behaviour.

Groups:
  A – SCHEMA short-circuit
  B – SLUG_FORMAT / DOUBLE_UNDERSCORE
  C – DUP_ID
  D – UNKNOWN_NODE_REF
  E – UNKNOWN_EDGE_REF
  F – CONF_OUT_OF_RANGE
  G – MISSING_DATASET_URNS
  H – OUT_OF_SCOPE_URN
  I – DUP_TRIPLE
  J – build_ontogen_validate_tool
  K – partition_clean_rows
  L – Field-level bounds (security hardening)
"""

import pytest
import pydantic

from src.backend.ontogen.models import (
    OntogenLLMEdge,
    OntogenLLMNode,
    OntogenLLMOutput,
    OntogenLLMTriple,
)
from src.backend.ontogen.validator import (
    ValidationError,
    build_ontogen_validate_tool,
    partition_clean_rows,
    validate_ontogen_output,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — inline payloads per feedback_test_readability.md
# ─────────────────────────────────────────────────────────────────────────────

_SCOPE = frozenset(["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"])

# A minimal *valid* OntogenLLMOutput as a plain dict
def _valid_payload() -> dict:
    return {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {
                "label": "has item",
                "id": "has-item",
                "confidence_score": 0.8,
            }
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has-item",
                "object_node_id": "order",
                "confidence_score": 0.7,
            }
        ],
    }


def _valid_output() -> OntogenLLMOutput:
    return OntogenLLMOutput.model_validate(_valid_payload())


# ─────────────────────────────────────────────────────────────────────────────
# Group A: SCHEMA short-circuit
# ─────────────────────────────────────────────────────────────────────────────


def test_schema_failure_returns_single_schema_error() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'Pydantic shape of OntogenLLMOutput → SCHEMA'.
    A payload with 'nodes' set to a string (not a list) violates the Pydantic shape contract.
    The function must return exactly one error with code=SCHEMA and path=''.

    Note: {} passes model_validate because nodes/edges/triples all have default_factory=list.
    A true SCHEMA violation requires an invalid type, such as nodes='not-a-list'.
    """
    payload = {"nodes": "not-a-list"}  # nodes must be a list — this is a shape violation
    errors = validate_ontogen_output(payload, _SCOPE)
    # SCHEMA error short-circuits: exactly one error
    assert len(errors) == 1
    assert errors[0].code == "SCHEMA"
    assert errors[0].path == ""


def test_schema_failure_short_circuits_other_rules() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SCHEMA short-circuit.
    A payload that would ALSO fail SLUG_FORMAT (uppercase id) but has invalid shape
    must return only the SCHEMA error — no additional rule codes should fire.
    The SCHEMA rule runs first and returns immediately without evaluating semantic rules.
    """
    # nodes have invalid shape (missing required 'name') — triggers SCHEMA
    # the id 'Order' would also fail SLUG_FORMAT if evaluated, but it must NOT be
    payload = {
        "nodes": [{"id": "Order", "confidence_score": 0.9}],  # 'name' missing → SCHEMA
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    assert len(errors) == 1
    assert errors[0].code == "SCHEMA"
    # Specifically: SLUG_FORMAT must not appear
    assert all(e.code != "SLUG_FORMAT" for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# Group B: SLUG_FORMAT / DOUBLE_UNDERSCORE
# ─────────────────────────────────────────────────────────────────────────────


def test_slug_uppercase_fires_slug_format() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'node.id matches ^[a-z0-9][a-z0-9_-]*$; SLUG_FORMAT'.
    A node id with uppercase letter 'Order' does not match the slug regex.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "Order",  # uppercase → SLUG_FORMAT
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) >= 1
    assert slug_errors[0].path == "nodes[0].id"


def test_slug_starts_with_hyphen_fires_slug_format() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    id='-foo' starts with a hyphen which is not matched by ^[a-z0-9][a-z0-9_-]*$.
    """
    payload = {
        "nodes": [
            {
                "name": "Foo",
                "id": "-foo",  # starts with hyphen → SLUG_FORMAT
                "confidence_score": 0.5,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) >= 1
    assert slug_errors[0].path == "nodes[0].id"


def test_double_underscore_fires_double_underscore_code() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'no __; DOUBLE_UNDERSCORE'.
    id='order__line' contains __ and must produce DOUBLE_UNDERSCORE, NOT SLUG_FORMAT.
    The validator explicitly separates the two codes.
    """
    payload = {
        "nodes": [
            {
                "name": "Order Line",
                "id": "order__line",  # double underscore → DOUBLE_UNDERSCORE
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    codes = [e.code for e in errors]
    assert "DOUBLE_UNDERSCORE" in codes
    assert "SLUG_FORMAT" not in codes


def test_valid_slug_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    id='order-line_v1' matches ^[a-z0-9][a-z0-9_-]*$ (lowercase, hyphen, underscore, digit).
    No SLUG_FORMAT or DOUBLE_UNDERSCORE error must appear.
    """
    payload = {
        "nodes": [
            {
                "name": "Order Line",
                "id": "order-line_v1",  # valid slug
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code in ("SLUG_FORMAT", "DOUBLE_UNDERSCORE")]
    assert slug_errors == []


def test_edge_id_slug_format_also_checked() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'edge.id matches ^[a-z0-9][a-z0-9_-]*$; SLUG_FORMAT'.
    The same slug rules apply to edge.id; a violation must set path='edges[i].id'.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {
                "label": "has item",
                "id": "HAS-ITEM",  # uppercase → SLUG_FORMAT on edges
                "confidence_score": 0.8,
            }
        ],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) >= 1
    assert slug_errors[0].path == "edges[0].id"


def test_none_id_skipped_by_slug_check() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT / DOUBLE_UNDERSCORE.
    id=None is a legitimate LLM omission (service always re-slugs from name).
    Validator must NOT fire SLUG_FORMAT or DOUBLE_UNDERSCORE for None ids.
    Defends against false-positive on valid omitted-id pattern.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": None,  # LLM omitted id — valid omission
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {
                "label": "has item",
                "id": None,  # LLM omitted id — valid omission
                "confidence_score": 0.8,
            }
        ],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code in ("SLUG_FORMAT", "DOUBLE_UNDERSCORE")]
    assert slug_errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Group C: DUP_ID
# ─────────────────────────────────────────────────────────────────────────────


def test_duplicate_node_id_fires_dup_id() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'No duplicate ids within nodes; DUP_ID'.
    Two nodes both with id='foo' must fire DUP_ID on the second node (index 1).
    """
    payload = {
        "nodes": [
            {
                "name": "Foo One",
                "id": "foo",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "name": "Foo Two",
                "id": "foo",  # duplicate → DUP_ID
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    dup_errors = [e for e in errors if e.code == "DUP_ID"]
    assert len(dup_errors) >= 1
    assert dup_errors[0].path == "nodes[1].id"


def test_duplicate_edge_id_fires_dup_id() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'No duplicate ids within edges; DUP_ID'.
    Two edges both with id='rel' must fire DUP_ID on the second edge (index 1).
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "relates to", "id": "rel", "confidence_score": 0.8},
            {"label": "relates to again", "id": "rel", "confidence_score": 0.7},  # duplicate
        ],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    dup_errors = [e for e in errors if e.code == "DUP_ID"]
    assert len(dup_errors) >= 1
    assert dup_errors[0].path == "edges[1].id"


def test_none_id_nodes_not_treated_as_duplicates() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — DUP_ID.
    Two nodes both with id=None must NOT fire DUP_ID.
    id=None is a legitimate LLM omission; None values must be excluded from
    the deduplication set (F1 reviewer fix — critical contract).
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": None,  # first omitted id
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "name": "Item",
                "id": None,  # second omitted id — must NOT be a duplicate
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    dup_errors = [e for e in errors if e.code == "DUP_ID"]
    assert dup_errors == [], (
        f"Two nodes with id=None must not trigger DUP_ID; got: {dup_errors}"
    )


def test_none_id_edges_not_treated_as_duplicates() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — DUP_ID.
    Two edges both with id=None must NOT fire DUP_ID.
    Same contract as for nodes: None is excluded from deduplication.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "relates to", "id": None, "confidence_score": 0.8},
            {"label": "also relates to", "id": None, "confidence_score": 0.7},
        ],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    dup_errors = [e for e in errors if e.code == "DUP_ID"]
    assert dup_errors == [], (
        f"Two edges with id=None must not trigger DUP_ID; got: {dup_errors}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group D: UNKNOWN_NODE_REF
# ─────────────────────────────────────────────────────────────────────────────


def test_triple_subject_unknown_fires_unknown_node_ref() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'Every triple.subject_node_id resolves
    to a node in the payload; UNKNOWN_NODE_REF'.
    subject_node_id='ghost' is not in nodes → code='UNKNOWN_NODE_REF',
    path='triples[0].subject_node_id'.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "ghost",  # not in nodes → UNKNOWN_NODE_REF
                "edge_id": "has-item",
                "object_node_id": "order",
                "confidence_score": 0.7,
            }
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    ref_errors = [e for e in errors if e.code == "UNKNOWN_NODE_REF"]
    assert len(ref_errors) >= 1
    assert ref_errors[0].path == "triples[0].subject_node_id"


def test_triple_object_unknown_fires_unknown_node_ref() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'Every triple.object_node_id resolves
    to a node in the payload; UNKNOWN_NODE_REF'.
    object_node_id='phantom' is not in nodes → UNKNOWN_NODE_REF on object path.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has-item",
                "object_node_id": "phantom",  # not in nodes → UNKNOWN_NODE_REF
                "confidence_score": 0.7,
            }
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    ref_errors = [e for e in errors if e.code == "UNKNOWN_NODE_REF"]
    assert len(ref_errors) >= 1
    assert ref_errors[0].path == "triples[0].object_node_id"


def test_triple_with_valid_node_refs_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — UNKNOWN_NODE_REF.
    Triple whose both subject and object resolve to nodes in the payload must not fire
    UNKNOWN_NODE_REF.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "name": "Item",
                "id": "item",
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "order",  # valid
                "edge_id": "has-item",
                "object_node_id": "item",   # valid
                "confidence_score": 0.7,
            }
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    ref_errors = [e for e in errors if e.code == "UNKNOWN_NODE_REF"]
    assert ref_errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Group E: UNKNOWN_EDGE_REF
# ─────────────────────────────────────────────────────────────────────────────


def test_triple_edge_unknown_fires_unknown_edge_ref() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'Every triple.edge_id resolves to an
    edge in the payload; UNKNOWN_EDGE_REF'.
    edge_id='ghost-edge' is not in edges → code='UNKNOWN_EDGE_REF',
    path='triples[0].edge_id'.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "ghost-edge",   # not in edges → UNKNOWN_EDGE_REF
                "object_node_id": "order",
                "confidence_score": 0.7,
            }
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    ref_errors = [e for e in errors if e.code == "UNKNOWN_EDGE_REF"]
    assert len(ref_errors) >= 1
    assert ref_errors[0].path == "triples[0].edge_id"


def test_triple_with_valid_edge_ref_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — UNKNOWN_EDGE_REF.
    Triple whose edge_id resolves to an edge in the payload must not fire UNKNOWN_EDGE_REF.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has-item",  # valid edge ref
                "object_node_id": "order",
                "confidence_score": 0.7,
            }
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    ref_errors = [e for e in errors if e.code == "UNKNOWN_EDGE_REF"]
    assert ref_errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Group F: CONF_OUT_OF_RANGE
# ─────────────────────────────────────────────────────────────────────────────


def test_valid_confidence_in_range_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'confidence_score ∈ [0.0, 1.0]; CONF_OUT_OF_RANGE'.
    Values 0.0, 0.5, and 1.0 are all within the valid range and must not produce
    CONF_OUT_OF_RANGE errors.
    """
    for score in [0.0, 0.5, 1.0]:
        payload = {
            "nodes": [
                {
                    "name": "Order",
                    "id": "order",
                    "confidence_score": score,
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
                }
            ],
            "edges": [],
            "triples": [],
        }
        errors = validate_ontogen_output(payload, _SCOPE)
        conf_errors = [e for e in errors if e.code == "CONF_OUT_OF_RANGE"]
        assert conf_errors == [], f"score={score} must not produce CONF_OUT_OF_RANGE; got {conf_errors}"


def test_confidence_at_boundary_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'confidence_score ∈ [0.0, 1.0]'.
    Boundary values 0.0 and 1.0 are inclusive per Pydantic Field(ge=0.0, le=1.0).
    Neither value must produce CONF_OUT_OF_RANGE.
    """
    for score in [0.0, 1.0]:
        payload = {
            "nodes": [
                {
                    "name": "Order",
                    "id": "order",
                    "confidence_score": score,
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
                }
            ],
            "edges": [],
            "triples": [],
        }
        errors = validate_ontogen_output(payload, _SCOPE)
        conf_errors = [e for e in errors if e.code == "CONF_OUT_OF_RANGE"]
        assert conf_errors == [], (
            f"boundary score={score} must pass CONF_OUT_OF_RANGE check; got {conf_errors}"
        )


def test_confidence_out_of_range_direct_dict_yields_schema_error() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — CONF_OUT_OF_RANGE / SCHEMA interaction.

    Note: confidence_score has Field(ge=0.0, le=1.0) in OntogenLLMNode, so passing
    confidence_score=1.5 in a dict to validate_ontogen_output first hits Pydantic's
    model_validate, which raises PydanticValidationError before the semantic rules run.
    The validator therefore returns code='SCHEMA', not 'CONF_OUT_OF_RANGE'.

    The belt-and-suspenders semantic CONF_OUT_OF_RANGE check exists for the edge case
    where with_structured_output coerces rather than rejects (architect's note in plan §F6).
    Confirming here that Pydantic always catches it first in the normal dict-validate path.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 1.5,  # out of [0.0, 1.0] — Pydantic raises first
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    # Pydantic catches it → SCHEMA (not CONF_OUT_OF_RANGE)
    assert len(errors) == 1
    assert errors[0].code == "SCHEMA"


# ─────────────────────────────────────────────────────────────────────────────
# Group G: MISSING_DATASET_URNS
# ─────────────────────────────────────────────────────────────────────────────


def test_node_with_empty_dataset_urns_fires_missing() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'node.dataset_urns is non-empty;
    MISSING_DATASET_URNS'.
    A node with dataset_urns=[] must produce code='MISSING_DATASET_URNS' with
    path='nodes[i].dataset_urns'.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": [],  # empty → MISSING_DATASET_URNS
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    missing_errors = [e for e in errors if e.code == "MISSING_DATASET_URNS"]
    assert len(missing_errors) >= 1
    assert missing_errors[0].path == "nodes[0].dataset_urns"


def test_node_with_non_empty_dataset_urns_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — MISSING_DATASET_URNS.
    A node with a non-empty dataset_urns list of in-scope URNs must not fire
    MISSING_DATASET_URNS.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    missing_errors = [e for e in errors if e.code == "MISSING_DATASET_URNS"]
    assert missing_errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Group H: OUT_OF_SCOPE_URN
# ─────────────────────────────────────────────────────────────────────────────


def test_node_with_out_of_scope_urn_fires_out_of_scope() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'every entry ∈ in-scope dataset URNs;
    OUT_OF_SCOPE_URN'.
    A node URN not present in in_scope_urns must produce code='OUT_OF_SCOPE_URN'.
    The path must include the URN index: 'nodes[i].dataset_urns[j]'.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.OTHER,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    }
    # in_scope_urns does NOT contain the URN above
    errors = validate_ontogen_output(payload, _SCOPE)
    oos_errors = [e for e in errors if e.code == "OUT_OF_SCOPE_URN"]
    assert len(oos_errors) >= 1
    assert oos_errors[0].path == "nodes[0].dataset_urns[0]"


def test_node_with_all_in_scope_urns_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OUT_OF_SCOPE_URN.
    All URNs present in in_scope_urns must not produce OUT_OF_SCOPE_URN.
    """
    scope = frozenset([
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog,PROD)",
    ])
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": [
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)",
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog,PROD)",
                ],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, scope)
    oos_errors = [e for e in errors if e.code == "OUT_OF_SCOPE_URN"]
    assert oos_errors == []


def test_mixed_in_scope_and_out_of_scope_urns_one_error() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OUT_OF_SCOPE_URN.
    When a node has two URNs — one in-scope and one out-of-scope — exactly one
    OUT_OF_SCOPE_URN error must be produced pointing at the out-of-scope URN index.
    """
    scope = frozenset(["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"])
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": [
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)",    # in-scope [0]
                    "urn:li:dataset:(urn:li:dataPlatform:postgres,db.OTHER,PROD)",     # out-of-scope [1]
                ],
            }
        ],
        "edges": [],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, scope)
    oos_errors = [e for e in errors if e.code == "OUT_OF_SCOPE_URN"]
    assert len(oos_errors) == 1
    assert oos_errors[0].path == "nodes[0].dataset_urns[1]"


# ─────────────────────────────────────────────────────────────────────────────
# Group I: DUP_TRIPLE
# ─────────────────────────────────────────────────────────────────────────────


def test_duplicate_triple_fires_dup_triple() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'No duplicate (subject_node_id, edge_id,
    object_node_id) triples; DUP_TRIPLE'.
    Two triples with the same (subject, edge, object) tuple must produce code='DUP_TRIPLE'
    on the second triple.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "name": "Item",
                "id": "item",
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8}
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has-item",
                "object_node_id": "item",
                "confidence_score": 0.7,
            },
            {
                "subject_node_id": "order",   # same tuple → DUP_TRIPLE
                "edge_id": "has-item",
                "object_node_id": "item",
                "confidence_score": 0.6,
            },
        ],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    dup_errors = [e for e in errors if e.code == "DUP_TRIPLE"]
    assert len(dup_errors) >= 1


def test_different_triples_pass() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — DUP_TRIPLE.
    Three triples with distinct (subject, edge, object) combinations must not fire DUP_TRIPLE.
    """
    scope = frozenset([
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog,PROD)",
    ])
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "name": "Item",
                "id": "item",
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog,PROD)"],
            },
            {
                "name": "Customer",
                "id": "customer",
                "confidence_score": 0.7,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [
            {"label": "has item", "id": "has-item", "confidence_score": 0.8},
            {"label": "placed by", "id": "placed-by", "confidence_score": 0.7},
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has-item",
                "object_node_id": "item",
                "confidence_score": 0.7,
            },
            {
                "subject_node_id": "order",
                "edge_id": "placed-by",
                "object_node_id": "customer",
                "confidence_score": 0.6,
            },
            {
                "subject_node_id": "item",
                "edge_id": "has-item",
                "object_node_id": "order",
                "confidence_score": 0.5,
            },
        ],
    }
    errors = validate_ontogen_output(payload, scope)
    dup_errors = [e for e in errors if e.code == "DUP_TRIPLE"]
    assert dup_errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Group J: build_ontogen_validate_tool
# ─────────────────────────────────────────────────────────────────────────────


async def test_tool_returns_ok_true_on_valid_payload() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — build_ontogen_validate_tool.
    A LangChain StructuredTool wrapping the validator with a known in_scope_urns set,
    invoked with a valid payload dict, must return {'ok': True, 'errors': []}.
    """
    tool = build_ontogen_validate_tool(_SCOPE)
    result = await tool.ainvoke({
        "payload": {
            "nodes": [
                {
                    "name": "Order",
                    "id": "order",
                    "confidence_score": 0.9,
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
                }
            ],
            "edges": [],
            "triples": [],
        }
    })
    assert result["ok"] is True
    assert result["errors"] == []


async def test_tool_returns_ok_false_with_errors_on_invalid() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — build_ontogen_validate_tool.
    A payload that fires SLUG_FORMAT must cause the tool to return
    {'ok': False, 'errors': [{'path': 'nodes[0].id', 'code': 'SLUG_FORMAT', 'message': ...}]}.
    """
    tool = build_ontogen_validate_tool(_SCOPE)
    result = await tool.ainvoke({
        "payload": {
            "nodes": [
                {
                    "name": "Order",
                    "id": "Order",  # uppercase → SLUG_FORMAT
                    "confidence_score": 0.9,
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
                }
            ],
            "edges": [],
            "triples": [],
        }
    })
    assert result["ok"] is False
    assert len(result["errors"]) >= 1
    slug_err = next((e for e in result["errors"] if e["code"] == "SLUG_FORMAT"), None)
    assert slug_err is not None
    assert slug_err["path"] == "nodes[0].id"
    assert "message" in slug_err


async def test_tool_closes_over_in_scope_urns() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — build_ontogen_validate_tool.
    Two tools built with different in_scope_urns sets must evaluate the same payload
    differently: the tool whose scope includes the URN returns ok=True; the other
    returns ok=False with OUT_OF_SCOPE_URN.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"
    tool_with_scope = build_ontogen_validate_tool(frozenset([urn]))
    tool_without_scope = build_ontogen_validate_tool(frozenset())  # empty scope

    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": [urn],
            }
        ],
        "edges": [],
        "triples": [],
    }

    result_with = await tool_with_scope.ainvoke({"payload": payload})
    result_without = await tool_without_scope.ainvoke({"payload": payload})

    assert result_with["ok"] is True, (
        f"Tool with matching scope must return ok=True; got: {result_with}"
    )
    assert result_without["ok"] is False, (
        f"Tool with empty scope must return ok=False; got: {result_without}"
    )
    oos_errors = [e for e in result_without["errors"] if e["code"] == "OUT_OF_SCOPE_URN"]
    assert len(oos_errors) >= 1


def test_tool_name_and_description_set() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — build_ontogen_validate_tool.
    The returned StructuredTool must have name='ontogen_validate' and a non-empty description.
    No length-pinning on description — the contract only requires it to be present.
    """
    tool = build_ontogen_validate_tool(_SCOPE)
    assert tool.name == "ontogen_validate"
    assert isinstance(tool.description, str)
    assert len(tool.description) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Group K: partition_clean_rows
# ─────────────────────────────────────────────────────────────────────────────


def test_partition_no_errors_passes_through() -> None:
    """Spec: BACKEND.md §Ontogen validator rules / LLM Inference Loop — partition_clean_rows.
    With an empty errors list, all rows must survive; dropped_count must be 0.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Order", id="order", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"])
        ],
        edges=[OntogenLLMEdge(label="has item", id="has-item", confidence_score=0.8)],
        triples=[
            OntogenLLMTriple(subject_node_id="order", edge_id="has-item",
                             object_node_id="order", confidence_score=0.7)
        ],
    )
    cleaned, dropped = partition_clean_rows(output, [])
    assert dropped == 0
    assert len(cleaned.nodes) == 1
    assert len(cleaned.edges) == 1
    assert len(cleaned.triples) == 1


def test_partition_drops_bad_node_only() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows.
    A node that fails SLUG_FORMAT (path='nodes[0].id') must be removed.
    dropped_count must equal 1.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Bad Order", id=None, confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"])
        ],
        edges=[],
        triples=[],
    )
    errors = [
        ValidationError(path="nodes[0].id", code="SLUG_FORMAT", message="bad id")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    assert dropped == 1
    assert len(cleaned.nodes) == 0


def test_partition_cascade_drops_triple_referencing_bad_node() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows cascade.
    When a node is dropped due to a SLUG_FORMAT error, any triple referencing that
    node as subject_node_id must also be dropped (cascade).
    The flat drop count must be node + cascaded triple = 2.
    Defends the architect's flat-count recommendation.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Bad Node", id="bad-node", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
            OntogenLLMNode(name="Good Node", id="good-node", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
        ],
        edges=[OntogenLLMEdge(label="relates to", id="relates-to", confidence_score=0.8)],
        triples=[
            # This triple references bad-node as subject → should cascade-drop
            OntogenLLMTriple(subject_node_id="bad-node", edge_id="relates-to",
                             object_node_id="good-node", confidence_score=0.7),
        ],
    )
    errors = [
        ValidationError(path="nodes[0].id", code="SLUG_FORMAT", message="bad slug")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    # 1 bad node + 1 cascaded triple = 2 dropped total
    assert dropped == 2
    assert len(cleaned.nodes) == 1
    assert cleaned.nodes[0].id == "good-node"
    assert len(cleaned.triples) == 0


def test_partition_cascade_drops_triple_referencing_bad_object_node() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows cascade.
    A triple referencing a dropped node as object_node_id must also be cascade-dropped.
    Flat count: 1 (bad node) + 1 (cascaded triple) = 2.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Good Node", id="good-node", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
            OntogenLLMNode(name="Bad Object", id="bad-object", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
        ],
        edges=[OntogenLLMEdge(label="relates to", id="relates-to", confidence_score=0.8)],
        triples=[
            # This triple references bad-object as object → should cascade-drop
            OntogenLLMTriple(subject_node_id="good-node", edge_id="relates-to",
                             object_node_id="bad-object", confidence_score=0.7),
        ],
    )
    errors = [
        ValidationError(path="nodes[1].id", code="SLUG_FORMAT", message="bad object slug")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    assert dropped == 2
    assert len(cleaned.nodes) == 1
    assert cleaned.nodes[0].id == "good-node"
    assert len(cleaned.triples) == 0


def test_partition_cascade_drops_triple_referencing_bad_edge() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows cascade.
    A triple referencing a dropped edge must be cascade-dropped.
    Flat count: 1 (bad edge) + 1 (cascaded triple) = 2.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Order", id="order", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
        ],
        edges=[
            OntogenLLMEdge(label="bad edge", id="bad-edge", confidence_score=0.8),
        ],
        triples=[
            # This triple references bad-edge → should cascade-drop
            OntogenLLMTriple(subject_node_id="order", edge_id="bad-edge",
                             object_node_id="order", confidence_score=0.7),
        ],
    )
    errors = [
        ValidationError(path="edges[0].id", code="DOUBLE_UNDERSCORE", message="double underscore in edge id")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    assert dropped == 2
    assert len(cleaned.edges) == 0
    assert len(cleaned.triples) == 0


def test_partition_schema_error_drops_everything_and_counts() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows SCHEMA handling.
    When errors list contains a SCHEMA error, the payload shape is untrustworthy.
    All rows must be dropped; dropped_count = original total row count.
    Defends the F2 reviewer fix.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="N1", id="n1", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
            OntogenLLMNode(name="N2", id="n2", confidence_score=0.8,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
        ],
        edges=[OntogenLLMEdge(label="rel", id="rel", confidence_score=0.7)],
        triples=[
            OntogenLLMTriple(subject_node_id="n1", edge_id="rel",
                             object_node_id="n2", confidence_score=0.6),
        ],
    )
    errors = [
        ValidationError(path="", code="SCHEMA", message="missing required field")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    # 2 nodes + 1 edge + 1 triple = 4 total dropped
    assert dropped == 4
    assert len(cleaned.nodes) == 0
    assert len(cleaned.edges) == 0
    assert len(cleaned.triples) == 0


def test_partition_independent_triple_error_drops_only_triple() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — partition_clean_rows.
    A DUP_TRIPLE error path (e.g. 'triples[1]') drops only that triple.
    Its referenced node and edge are clean and must not be dropped.
    dropped_count must equal 1.
    """
    output = OntogenLLMOutput(
        nodes=[
            OntogenLLMNode(name="Order", id="order", confidence_score=0.9,
                           dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"]),
        ],
        edges=[OntogenLLMEdge(label="has item", id="has-item", confidence_score=0.8)],
        triples=[
            OntogenLLMTriple(subject_node_id="order", edge_id="has-item",
                             object_node_id="order", confidence_score=0.7),
            OntogenLLMTriple(subject_node_id="order", edge_id="has-item",
                             object_node_id="order", confidence_score=0.6),  # duplicate
        ],
    )
    errors = [
        ValidationError(path="triples[1]", code="DUP_TRIPLE", message="duplicate triple")
    ]
    cleaned, dropped = partition_clean_rows(output, errors)
    assert dropped == 1
    assert len(cleaned.nodes) == 1
    assert len(cleaned.edges) == 1
    assert len(cleaned.triples) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Group L: Field-level bounds (security hardening F6)
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_node_id_rejected_by_pydantic() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OntogenLLMNode field constraints.
    OntogenLLMNode with id='x' * 201 must raise pydantic.ValidationError.
    The max_length=200 constraint on OntogenLLMNode.id prevents oversized LLM-supplied ids.
    """
    with pytest.raises(pydantic.ValidationError):
        OntogenLLMNode(
            name="order",
            id="x" * 201,  # exceeds max_length=200
            confidence_score=0.9,
            dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
        )


def test_oversized_dataset_urn_rejected_by_pydantic() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OntogenLLMNode field constraints.
    A URN string of length 1025 exceeds the per-URN max_length=1024 constraint
    and must raise pydantic.ValidationError.
    """
    with pytest.raises(pydantic.ValidationError):
        OntogenLLMNode(
            name="order",
            id="order",
            confidence_score=0.9,
            dataset_urns=["x" * 1025],  # exceeds per-URN max_length=1024
        )


def test_too_many_dataset_urns_rejected_by_pydantic() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OntogenLLMNode field constraints.
    dataset_urns list with 101 entries exceeds list max_length=100
    and must raise pydantic.ValidationError.
    """
    with pytest.raises(pydantic.ValidationError):
        OntogenLLMNode(
            name="order",
            id="order",
            confidence_score=0.9,
            dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"] * 101,
        )


def test_too_many_nodes_rejected_by_pydantic() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — OntogenLLMOutput field constraints.
    OntogenLLMOutput with 501 nodes exceeds list max_length=500
    and must raise pydantic.ValidationError.
    """
    minimal_node = OntogenLLMNode(
        name="n",
        id=None,
        confidence_score=0.5,
        dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)"],
    )
    with pytest.raises(pydantic.ValidationError):
        OntogenLLMOutput(
            nodes=[minimal_node] * 501,  # exceeds max_length=500
            edges=[],
            triples=[],
        )
