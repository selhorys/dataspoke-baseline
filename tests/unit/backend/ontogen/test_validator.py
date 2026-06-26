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
    _check_slug,
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


def test_check_slug_rejects_uppercase() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    _check_slug (defence-in-depth layer) fires SLUG_FORMAT for an uppercase id 'Order'
    even though the model_validator normalises LLM-supplied ids upstream.
    This test pins the _check_slug contract directly — it is the low-level guard
    that protects against any caller that bypasses model_validate.
    """
    errors: list[ValidationError] = []
    _check_slug("nodes[0].id", "Order", errors)  # uppercase → SLUG_FORMAT
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) >= 1
    assert slug_errors[0].path == "nodes[0].id"


def test_check_slug_rejects_leading_hyphen() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    _check_slug fires SLUG_FORMAT for id='-foo' (leading hyphen) which does not match
    ^[a-z0-9_]{1,64}$.  Tests the defence-in-depth guard directly; model_validator
    normalises hyphens upstream but this layer remains the invariant sentinel.
    """
    errors: list[ValidationError] = []
    _check_slug("nodes[0].id", "-foo", errors)  # leading hyphen → SLUG_FORMAT
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) >= 1
    assert slug_errors[0].path == "nodes[0].id"


def test_check_slug_flags_double_underscore() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — 'no __; DOUBLE_UNDERSCORE'.
    _check_slug fires DOUBLE_UNDERSCORE (not SLUG_FORMAT) for id='order__line'.
    Tests the defence-in-depth guard directly; model_validator collapses __ → _
    upstream, but _check_slug remains the authoritative slug-invariant sentinel.
    """
    errors: list[ValidationError] = []
    _check_slug("nodes[0].id", "order__line", errors)  # double underscore → DOUBLE_UNDERSCORE
    codes = [e.code for e in errors]
    assert "DOUBLE_UNDERSCORE" in codes
    assert "SLUG_FORMAT" not in codes


def test_valid_node_slug_passes() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    id='order_line_v1' is a valid snake_case slug (^[a-z0-9_]{1,64}$) on the node path.
    No SLUG_FORMAT or DOUBLE_UNDERSCORE error must appear.
    """
    payload = {
        "nodes": [
            {
                "name": "Order Line",
                "id": "order_line_v1",  # valid snake_case slug
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {
                "label": "has edition",
                "id": "has_edition",  # valid snake_case slug on edge path
                "confidence_score": 0.8,
            }
        ],
        "triples": [],
    }
    errors = validate_ontogen_output(payload, _SCOPE)
    slug_errors = [e for e in errors if e.code in ("SLUG_FORMAT", "DOUBLE_UNDERSCORE")]
    assert slug_errors == []


def test_check_slug_applies_to_edge_path() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT applied to edge.id.
    _check_slug fires SLUG_FORMAT for edge id 'HAS-ITEM' (uppercase + hyphen).
    Tests the defence-in-depth guard directly on an edge path; model_validator
    normalises upstream but this sentinel layer must flag it regardless.
    """
    errors: list[ValidationError] = []
    _check_slug("edges[0].id", "HAS-ITEM", errors)  # uppercase → SLUG_FORMAT
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

    An id of 65 lowercase letters passes model_validate (max_length=200) and survives
    model_validator normalisation unchanged, but exceeds the 64-char cap in _SLUG_RE
    (^[a-z0-9_]{1,64}$) so _check_slug fires SLUG_FORMAT.  This is the canonical way to
    test the validator layer independently of the upstream normaliser.
    """
    oversized_id = "a" * 65  # passes model_validate, bypasses normaliser, fails _SLUG_RE
    tool = build_ontogen_validate_tool(_SCOPE)
    result = await tool.ainvoke({
        "payload": {
            "nodes": [
                {
                    "name": "Order",
                    "id": oversized_id,
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


# ─────────────────────────────────────────────────────────────────────────────
# Group M: model_validator / _check_slug new contract tests (plan §10)
# ─────────────────────────────────────────────────────────────────────────────


def test_hyphen_in_id_fails_check_slug() -> None:
    """Spec: BACKEND.md §Ontogen validator rules — SLUG_FORMAT.
    _check_slug rejects 'has-edition' because hyphen is not in ^[a-z0-9_]{1,64}$.
    This is the spec-mandated defence-in-depth guard for slug-format enforcement.
    """
    errors: list[ValidationError] = []
    _check_slug("nodes[0].id", "has-edition", errors)
    slug_errors = [e for e in errors if e.code == "SLUG_FORMAT"]
    assert len(slug_errors) == 1, (
        "SLUG_FORMAT must fire for 'has-edition' (hyphen not allowed in snake_case slugs). "
        "spec: BACKEND.md §Ontogen validator rules — id regex ^[a-z0-9_]{1,64}$"
    )
    assert slug_errors[0].path == "nodes[0].id"


def test_model_validator_normalizes_node_id() -> None:
    """Spec: BACKEND.md §Ontology Generation Service §Inference Pipeline — model_validator.
    OntogenLLMOutput.model_validate normalises node id='Order Line' (spaced, mixed case)
    to snake_case 'order_line' before any field validators run.
    spec: plan §3 — server-side normalisation at schema boundary via @model_validator(mode='before')
    """
    result = OntogenLLMOutput.model_validate({
        "nodes": [
            {
                "id": "Order Line",
                "name": "Order Line",
                "confidence_score": 1.0,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    })
    assert result.nodes[0].id == "order_line", (
        f"model_validator must normalise 'Order Line' → 'order_line'; "
        f"got {result.nodes[0].id!r}. "
        "spec: plan §3 — server-side slug normalisation"
    )


def test_model_validator_normalizes_edge_id() -> None:
    """Spec: BACKEND.md §Ontology Generation Service §Inference Pipeline — model_validator.
    OntogenLLMOutput.model_validate normalises edge id='Has Edition' (spaced, mixed case)
    to snake_case 'has_edition'.
    spec: plan §3 — model_validator normalises both node and edge id fields
    """
    result = OntogenLLMOutput.model_validate({
        "nodes": [],
        "edges": [
            {
                "id": "Has Edition",
                "label": "Has Edition",
                "confidence_score": 0.9,
            }
        ],
        "triples": [],
    })
    assert result.edges[0].id == "has_edition", (
        f"model_validator must normalise 'Has Edition' → 'has_edition'; "
        f"got {result.edges[0].id!r}. "
        "spec: plan §3 — server-side slug normalisation for edge ids"
    )


def test_model_validator_remaps_triples() -> None:
    """Spec: BACKEND.md §Ontology Generation Service §Inference Pipeline — model_validator.
    When node id='Order Line' normalises to 'order_line' and edge id='Has Edition' normalises
    to 'has_edition', any triple referencing the original pre-normalisation ids must be
    rewritten to the post-normalisation ids.
    spec: plan §3 — triple subject/edge/object refs rewritten via id_remap dict
    """
    result = OntogenLLMOutput.model_validate({
        "nodes": [
            {
                "id": "Order Line",
                "name": "Order Line",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [
            {
                "id": "Has Edition",
                "label": "Has Edition",
                "confidence_score": 0.8,
            }
        ],
        "triples": [
            {
                "subject_node_id": "Order Line",  # original pre-normalisation id
                "edge_id": "Has Edition",         # original pre-normalisation id
                "object_node_id": "Order Line",   # original pre-normalisation id
                "confidence_score": 0.7,
            }
        ],
    })
    assert result.nodes[0].id == "order_line"
    assert result.edges[0].id == "has_edition"
    triple = result.triples[0]
    assert triple.subject_node_id == "order_line", (
        f"triple.subject_node_id must be remapped to 'order_line'; got {triple.subject_node_id!r}. "
        "spec: plan §3 — triple refs rewritten through id_remap"
    )
    assert triple.edge_id == "has_edition", (
        f"triple.edge_id must be remapped to 'has_edition'; got {triple.edge_id!r}. "
        "spec: plan §3 — triple refs rewritten through edge_id_remap"
    )
    assert triple.object_node_id == "order_line", (
        f"triple.object_node_id must be remapped to 'order_line'; got {triple.object_node_id!r}. "
        "spec: plan §3 — triple refs rewritten through id_remap"
    )


def test_model_validator_absent_id_left_as_none() -> None:
    """Spec: BACKEND.md §Ontology Generation Service §Inference Pipeline — model_validator.
    A node submitted without an 'id' key (absent, not null) must have id=None after validate.
    The service derives the final id via make_snake_id(name) later; absent id is a valid
    LLM omission and must not be overwritten with a placeholder.
    spec: plan §3 — 'Absent id fields stay None — service.py derives them via make_snake_id later'
    """
    result = OntogenLLMOutput.model_validate({
        "nodes": [
            {
                # 'id' key intentionally absent
                "name": "Order Line",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            }
        ],
        "edges": [],
        "triples": [],
    })
    assert result.nodes[0].id is None, (
        f"node with absent 'id' key must have id=None after model_validate; "
        f"got {result.nodes[0].id!r}. "
        "spec: plan §3 — absent id stays None for service.py to derive"
    )


def test_model_validator_passes_through_unmodified_when_already_normalized() -> None:
    """Spec: BACKEND.md §Ontology Generation Service §Inference Pipeline — model_validator.
    Proves two distinct properties:
      (a) The validator ran and normalised the non-snake_case input 'Order Line' → 'order_line'.
      (b) Idempotency: an already-snake_case id 'line_item' is unchanged.
      (c) Triple refs referencing the pre-normalisation id are rewritten; already-snake_case
          triple ref is kept verbatim.
    spec: plan §3 — server-side normalisation at schema boundary; to_snake is idempotent
    """
    result = OntogenLLMOutput.model_validate({
        "nodes": [
            {
                "id": "Order Line",   # needs normalisation → 'order_line'
                "name": "Order Line",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
            {
                "id": "line_item",    # already snake_case → must be unchanged (idempotency)
                "name": "Line Item",
                "confidence_score": 0.8,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            },
        ],
        "edges": [
            {
                "id": "has_line",
                "label": "has line",
                "confidence_score": 0.75,
            }
        ],
        "triples": [
            {
                "subject_node_id": "Order Line",  # pre-normalisation ref → must be rewritten
                "edge_id": "has_line",
                "object_node_id": "line_item",    # already snake_case → must stay
                "confidence_score": 0.7,
            }
        ],
    })
    # (a) validator ran and normalised the non-snake_case id
    assert result.nodes[0].id == "order_line", (
        f"'Order Line' must normalise to 'order_line'; got {result.nodes[0].id!r}. "
        "spec: plan §3 — server-side slug normalisation"
    )
    # (b) already-snake_case id is unchanged (idempotency)
    assert result.nodes[1].id == "line_item", (
        f"Already-snake_case 'line_item' must be unchanged; got {result.nodes[1].id!r}. "
        "spec: plan §3 — to_snake is idempotent on valid snake_case slugs"
    )
    triple = result.triples[0]
    # (c) triple subject ref was pre-normalisation id → must be rewritten
    assert triple.subject_node_id == "order_line", (
        f"triple.subject_node_id must be rewritten from 'Order Line' to 'order_line'; "
        f"got {triple.subject_node_id!r}. spec: plan §3 — triple refs rewritten through id_remap"
    )
    # (c) triple object ref was already snake_case → stays
    assert triple.object_node_id == "line_item", (
        f"triple.object_node_id 'line_item' must be unchanged; "
        f"got {triple.object_node_id!r}. spec: plan §3 — id_remap defaults to identity"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group N: producer_rebuttal — adversarial-debate affordance survives validation
# ─────────────────────────────────────────────────────────────────────────────


def test_producer_rebuttal_defaults_to_none_when_absent() -> None:
    """Spec: BACKEND_LLM.md §Producer revision — producer_rebuttal is an optional field.
    A candidate that does not carry the field validates with producer_rebuttal=None
    on every item kind (the common turn-0 case, where no rebuttal exists yet).
    """
    out = OntogenLLMOutput.model_validate(_valid_payload())
    assert out.nodes[0].producer_rebuttal is None
    assert out.edges[0].producer_rebuttal is None
    assert out.triples[0].producer_rebuttal is None


def test_producer_rebuttal_survives_model_validation() -> None:
    """Spec: BACKEND_LLM.md §Producer revision — 'Keep the item as-is and attach a
    producer_rebuttal field ... with a one-sentence rationale.'

    The rationale must be captured by the schema (not silently dropped by Pydantic)
    on each of node/edge/triple, so the explicit adversarial affordance is preserved.
    """
    payload = {
        "nodes": [
            {
                "name": "Order",
                "id": "order",
                "confidence_score": 0.9,
                "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
                "producer_rebuttal": "Confidence is grounded in the order_total column profile.",
            }
        ],
        "edges": [
            {
                "label": "has item",
                "id": "has_item",
                "confidence_score": 0.8,
                "producer_rebuttal": "Edge label is the business term used by stewards.",
            }
        ],
        "triples": [
            {
                "subject_node_id": "order",
                "edge_id": "has_item",
                "object_node_id": "order",
                "confidence_score": 0.7,
                "producer_rebuttal": "Self-reference models nested orders; not a duplicate.",
            }
        ],
    }
    out = OntogenLLMOutput.model_validate(payload)
    assert out.nodes[0].producer_rebuttal == (
        "Confidence is grounded in the order_total column profile."
    )
    assert out.edges[0].producer_rebuttal == (
        "Edge label is the business term used by stewards."
    )
    assert out.triples[0].producer_rebuttal == (
        "Self-reference models nested orders; not a duplicate."
    )


def test_oversized_producer_rebuttal_rejected_by_pydantic() -> None:
    """Spec: BACKEND_LLM.md §Producer revision — the rebuttal is a one-sentence rationale.
    A rebuttal beyond the max_length bound must raise pydantic.ValidationError so an
    untrusted LLM cannot smuggle an oversized payload through the field.
    """
    with pytest.raises(pydantic.ValidationError):
        OntogenLLMNode(
            name="order",
            id="order",
            confidence_score=0.9,
            dataset_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,PROD)"],
            producer_rebuttal="x" * 501,  # exceeds max_length=500
        )
