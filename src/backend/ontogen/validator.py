"""Semantic validator for the Ontogen LLM output.

Spec: spec/feature/BACKEND.md §Ontogen validator rules
"""

import re
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from src.backend.ontogen.models import OntogenLLMOutput

_SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_PATH_INDEX_RE = re.compile(r"^(nodes|edges|triples)\[(\d+)\]")


class ValidationError(BaseModel):
    path: str
    code: str
    message: str


def validate_ontogen_output(
    payload: dict[str, Any],
    in_scope_urns: frozenset[str],
) -> list[ValidationError]:
    """Validate *payload* against all 8 ontogen semantic rules.

    Returns an empty list when all rules pass.  On a schema violation the
    function returns immediately with a single SCHEMA error — semantic rules
    cannot be evaluated on a malformed payload shape.
    """
    try:
        output = OntogenLLMOutput.model_validate(payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        return [
            ValidationError(
                path="",
                code="SCHEMA",
                message=first["msg"],
            )
        ]

    errors: list[ValidationError] = []

    # ── SLUG_FORMAT / DOUBLE_UNDERSCORE ──────────────────────────────────────
    for i, node in enumerate(output.nodes):
        if node.id is not None:
            _check_slug(f"nodes[{i}].id", node.id, errors)
    for i, edge in enumerate(output.edges):
        if edge.id is not None:
            _check_slug(f"edges[{i}].id", edge.id, errors)

    # ── DUP_ID ───────────────────────────────────────────────────────────────
    # Skip None ids: the service always re-slugs id from name, so id=None is
    # a legitimate LLM omission and must not be treated as a duplicate.
    seen_node_ids: dict[str, int] = {}
    for i, node in enumerate(output.nodes):
        if node.id is None:
            continue
        if node.id in seen_node_ids:
            errors.append(
                ValidationError(
                    path=f"nodes[{i}].id",
                    code="DUP_ID",
                    message=(
                        f"duplicate node id {node.id!r} "
                        f"(first at index {seen_node_ids[node.id]})"
                    ),
                )
            )
        else:
            seen_node_ids[node.id] = i

    seen_edge_ids: dict[str, int] = {}
    for i, edge in enumerate(output.edges):
        if edge.id is None:
            continue
        if edge.id in seen_edge_ids:
            errors.append(
                ValidationError(
                    path=f"edges[{i}].id",
                    code="DUP_ID",
                    message=(
                        f"duplicate edge id {edge.id!r} "
                        f"(first at index {seen_edge_ids[edge.id]})"
                    ),
                )
            )
        else:
            seen_edge_ids[edge.id] = i

    # Build lookup sets for reference checks
    node_id_set: set[str | None] = {n.id for n in output.nodes}
    edge_id_set: set[str | None] = {e.id for e in output.edges}

    # ── UNKNOWN_NODE_REF / UNKNOWN_EDGE_REF ──────────────────────────────────
    for i, triple in enumerate(output.triples):
        if triple.subject_node_id not in node_id_set:
            errors.append(
                ValidationError(
                    path=f"triples[{i}].subject_node_id",
                    code="UNKNOWN_NODE_REF",
                    message=f"subject_node_id {triple.subject_node_id!r} not in nodes",
                )
            )
        if triple.object_node_id not in node_id_set:
            errors.append(
                ValidationError(
                    path=f"triples[{i}].object_node_id",
                    code="UNKNOWN_NODE_REF",
                    message=f"object_node_id {triple.object_node_id!r} not in nodes",
                )
            )
        if triple.edge_id not in edge_id_set:
            errors.append(
                ValidationError(
                    path=f"triples[{i}].edge_id",
                    code="UNKNOWN_EDGE_REF",
                    message=f"edge_id {triple.edge_id!r} not in edges",
                )
            )

    # ── CONF_OUT_OF_RANGE (belt-and-suspenders over Pydantic Field) ───────────
    for i, node in enumerate(output.nodes):
        if not (0.0 <= node.confidence_score <= 1.0):
            errors.append(
                ValidationError(
                    path=f"nodes[{i}].confidence_score",
                    code="CONF_OUT_OF_RANGE",
                    message=f"confidence_score {node.confidence_score} out of [0.0, 1.0]",
                )
            )
    for i, edge in enumerate(output.edges):
        if not (0.0 <= edge.confidence_score <= 1.0):
            errors.append(
                ValidationError(
                    path=f"edges[{i}].confidence_score",
                    code="CONF_OUT_OF_RANGE",
                    message=f"confidence_score {edge.confidence_score} out of [0.0, 1.0]",
                )
            )
    for i, triple in enumerate(output.triples):
        if not (0.0 <= triple.confidence_score <= 1.0):
            errors.append(
                ValidationError(
                    path=f"triples[{i}].confidence_score",
                    code="CONF_OUT_OF_RANGE",
                    message=f"confidence_score {triple.confidence_score} out of [0.0, 1.0]",
                )
            )

    # ── MISSING_DATASET_URNS / OUT_OF_SCOPE_URN ──────────────────────────────
    for i, node in enumerate(output.nodes):
        if not node.dataset_urns:
            errors.append(
                ValidationError(
                    path=f"nodes[{i}].dataset_urns",
                    code="MISSING_DATASET_URNS",
                    message="dataset_urns must be non-empty",
                )
            )
        else:
            for j, urn in enumerate(node.dataset_urns):
                if urn not in in_scope_urns:
                    errors.append(
                        ValidationError(
                            path=f"nodes[{i}].dataset_urns[{j}]",
                            code="OUT_OF_SCOPE_URN",
                            message=f"URN {urn!r} not in evidence set",
                        )
                    )

    # ── DUP_TRIPLE ────────────────────────────────────────────────────────────
    seen_triples: dict[tuple[str, str, str], int] = {}
    for i, triple in enumerate(output.triples):
        key = (triple.subject_node_id, triple.edge_id, triple.object_node_id)
        if key in seen_triples:
            errors.append(
                ValidationError(
                    path=f"triples[{i}]",
                    code="DUP_TRIPLE",
                    message=f"duplicate triple {key} (first at index {seen_triples[key]})",
                )
            )
        else:
            seen_triples[key] = i

    return errors


def build_ontogen_validate_tool(in_scope_urns: frozenset[str]) -> StructuredTool:
    """Return a LangChain StructuredTool wrapping the ontogen validator.

    The tool closes over *in_scope_urns* so the model can call it without
    the caller having to pass the URN set as a tool argument.
    """

    async def _ontogen_validate(payload: dict[str, Any]) -> dict[str, Any]:
        errs = validate_ontogen_output(payload, in_scope_urns)
        return {"ok": not errs, "errors": [e.model_dump() for e in errs]}

    return StructuredTool.from_function(
        coroutine=_ontogen_validate,
        name="ontogen_validate",
        description=(
            "Validate the proposed ontology payload against semantic rules "
            "(slug format, ID-reference integrity, in-scope dataset URN provenance, "
            "no duplicates). "
            "Returns {ok: true} on success or "
            "{ok: false, errors: [{path, code, message}]} on failure."
        ),
    )


def partition_clean_rows(
    payload: OntogenLLMOutput,
    errors: list[ValidationError],
) -> tuple[OntogenLLMOutput, int]:
    """Drop rows that triggered validation errors and cascade to dependent triples.

    Returns *(cleaned_payload, total_dropped_count)*.  The drop count is a
    flat total: bad nodes + bad edges + bad or cascaded triples.

    When *errors* contains a SCHEMA error the payload shape is untrustworthy;
    an empty output is returned immediately.
    """
    if any(e.code == "SCHEMA" for e in errors):
        total_dropped = len(payload.nodes) + len(payload.edges) + len(payload.triples)
        return OntogenLLMOutput(nodes=[], edges=[], triples=[]), total_dropped

    bad_node_indices: set[int] = set()
    bad_edge_indices: set[int] = set()
    bad_triple_indices: set[int] = set()

    for error in errors:
        m = _PATH_INDEX_RE.match(error.path)
        if m is None:
            continue
        collection, idx_str = m.group(1), m.group(2)
        idx = int(idx_str)
        if collection == "nodes":
            bad_node_indices.add(idx)
        elif collection == "edges":
            bad_edge_indices.add(idx)
        elif collection == "triples":
            bad_triple_indices.add(idx)

    bad_node_ids: set[str | None] = {
        payload.nodes[i].id for i in bad_node_indices if i < len(payload.nodes)
    }
    bad_edge_ids: set[str | None] = {
        payload.edges[i].id for i in bad_edge_indices if i < len(payload.edges)
    }

    # Cascade: triples whose subject/object node or edge was dropped
    for i, triple in enumerate(payload.triples):
        if i in bad_triple_indices:
            continue
        if (
            triple.subject_node_id in bad_node_ids
            or triple.object_node_id in bad_node_ids
            or triple.edge_id in bad_edge_ids
        ):
            bad_triple_indices.add(i)

    clean_nodes = [n for i, n in enumerate(payload.nodes) if i not in bad_node_indices]
    clean_edges = [e for i, e in enumerate(payload.edges) if i not in bad_edge_indices]
    clean_triples = [t for i, t in enumerate(payload.triples) if i not in bad_triple_indices]

    total_dropped = (
        len(bad_node_indices) + len(bad_edge_indices) + len(bad_triple_indices)
    )

    return (
        OntogenLLMOutput(nodes=clean_nodes, edges=clean_edges, triples=clean_triples),
        total_dropped,
    )


def _check_slug(path: str, value: str, errors: list[ValidationError]) -> None:
    if "__" in value:
        errors.append(
            ValidationError(
                path=path,
                code="DOUBLE_UNDERSCORE",
                message=f"{path} contains double-underscore: {value!r}",
            )
        )
    elif not _SLUG_RE.match(value):
        errors.append(
            ValidationError(
                path=path,
                code="SLUG_FORMAT",
                message=f"{path} does not match ^[a-z0-9_]{{1,64}}$ (a-z 0-9 _): {value!r}",
            )
        )
