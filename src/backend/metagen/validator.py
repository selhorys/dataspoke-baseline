"""Semantic validator for the Metagen LLM output.

Spec: spec/feature/BACKEND_LLM.md §Metagen Validator
"""

import re
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from src.backend.metagen.debate_models import MetagenLLMOutput

_ITEM_ID_RE = re.compile(r"^dataset\.description$|^column\.[^.]+\.description$")
_VALUE_MAX_BYTES = 16 * 1024  # 16 KiB


class ValidationError(BaseModel):
    path: str
    code: str
    message: str


def validate_metagen_output(
    payload: dict[str, Any],
    in_scope_urns: frozenset[str],
    boundary_allowed: dict[str, list[str]],
    schema_field_paths: dict[str, set[str]],
    approved_item_ids: frozenset[tuple[str, str]],
) -> list[ValidationError]:
    """Validate *payload* against all metagen semantic rules.

    Parameters
    ----------
    payload:
        Raw dict from the LLM Producer turn.
    in_scope_urns:
        Datasets that are in scope for this run (boundary-intersected).
    boundary_allowed:
        Maps dataset_urn → list of allowed kind strings.
    schema_field_paths:
        Maps dataset_urn → set of valid column field paths from schemaMetadata.
    approved_item_ids:
        Set of (dataset_urn, item_id) pairs that already have an approved candidate.

    Returns an empty list when all rules pass.  On a SCHEMA error the function
    returns immediately — semantic rules cannot be evaluated on a malformed shape.
    """
    try:
        output = MetagenLLMOutput.model_validate(payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        return [ValidationError(path="", code="SCHEMA", message=first["msg"])]

    errors: list[ValidationError] = []

    seen: dict[tuple[str, str], int] = {}

    for i, cand in enumerate(output.candidates):
        prefix = f"candidates[{i}]"

        # OUT_OF_SCOPE_URN
        if cand.dataset_urn not in in_scope_urns:
            errors.append(
                ValidationError(
                    path=f"{prefix}.dataset_urn",
                    code="OUT_OF_SCOPE_URN",
                    message=f"dataset_urn {cand.dataset_urn!r} not in run's in-scope set",
                )
            )
            continue

        # INVALID_ITEM_ID
        if not _ITEM_ID_RE.match(cand.item_id):
            errors.append(
                ValidationError(
                    path=f"{prefix}.item_id",
                    code="INVALID_ITEM_ID",
                    message=(
                        f"item_id {cand.item_id!r} must match "
                        f"^dataset\\.description$ or ^column\\.[^.]+\\.description$"
                    ),
                )
            )
            continue

        # Derive kind from item_id
        if cand.item_id == "dataset.description":
            kind = "dataset.description"
            field_path: str | None = None
        else:
            kind = "column.description"
            # item_id is column.<field_path>.description
            field_path = cand.item_id[len("column.") : -len(".description")]

        # UNKNOWN_FIELD_PATH — only for column items
        if field_path is not None:
            known_paths = schema_field_paths.get(cand.dataset_urn, set())
            if known_paths and field_path not in known_paths:
                errors.append(
                    ValidationError(
                        path=f"{prefix}.item_id",
                        code="UNKNOWN_FIELD_PATH",
                        message=(
                            f"field_path {field_path!r} not found in dataset schemaMetadata "
                            f"for {cand.dataset_urn!r}"
                        ),
                    )
                )

        # KIND_NOT_ALLOWED
        allowed = boundary_allowed.get(cand.dataset_urn, [])
        if kind not in allowed:
            errors.append(
                ValidationError(
                    path=f"{prefix}.item_id",
                    code="KIND_NOT_ALLOWED",
                    message=(
                        f"kind {kind!r} not in boundary.allowed {allowed!r} "
                        f"for {cand.dataset_urn!r}"
                    ),
                )
            )

        # EMPTY_VALUE / VALUE_TOO_LARGE
        if not cand.value or not cand.value.strip():
            errors.append(
                ValidationError(
                    path=f"{prefix}.value",
                    code="EMPTY_VALUE",
                    message="value must be non-empty Markdown",
                )
            )
        elif len(cand.value.encode("utf-8")) > _VALUE_MAX_BYTES:
            errors.append(
                ValidationError(
                    path=f"{prefix}.value",
                    code="VALUE_TOO_LARGE",
                    message=f"value exceeds 16 KiB limit",
                )
            )

        # CONF_OUT_OF_RANGE (belt-and-suspenders over Pydantic Field)
        if not (0.0 <= cand.confidence_score <= 1.0):
            errors.append(
                ValidationError(
                    path=f"{prefix}.confidence_score",
                    code="CONF_OUT_OF_RANGE",
                    message=f"confidence_score {cand.confidence_score} out of [0.0, 1.0]",
                )
            )

        # DUP_ITEM
        key = (cand.dataset_urn, cand.item_id)
        if key in seen:
            errors.append(
                ValidationError(
                    path=f"{prefix}",
                    code="DUP_ITEM",
                    message=(
                        f"duplicate (dataset_urn, item_id) {key} "
                        f"(first at index {seen[key]})"
                    ),
                )
            )
        else:
            seen[key] = i

        # ITEM_ALREADY_APPROVED
        if key in approved_item_ids:
            errors.append(
                ValidationError(
                    path=f"{prefix}",
                    code="ITEM_ALREADY_APPROVED",
                    message=(
                        f"item ({cand.dataset_urn!r}, {cand.item_id!r}) already has "
                        f"an approved candidate — generation skips approved items"
                    ),
                )
            )

    return errors


def build_metagen_validate_tool(
    in_scope_urns: frozenset[str],
    boundary_allowed: dict[str, list[str]],
    schema_field_paths: dict[str, set[str]],
    approved_item_ids: frozenset[tuple[str, str]],
) -> StructuredTool:
    """Return a LangChain StructuredTool wrapping the metagen validator."""

    async def _metagen_validate(payload: dict[str, Any]) -> dict[str, Any]:
        errs = validate_metagen_output(
            payload, in_scope_urns, boundary_allowed, schema_field_paths, approved_item_ids
        )
        return {"ok": not errs, "errors": [e.model_dump() for e in errs]}

    return StructuredTool.from_function(
        coroutine=_metagen_validate,
        name="metagen_validate",
        description=(
            "Validate the proposed metadata-generation candidate list against semantic rules "
            "(in-scope dataset URNs, valid item_id format, field_path existence, "
            "kind allowed by boundary, value size, no duplicates, no already-approved items). "
            "Returns {ok: true} on success or "
            "{ok: false, errors: [{path, code, message}]} on failure."
        ),
    )
