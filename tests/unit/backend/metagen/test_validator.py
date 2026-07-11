"""Unit tests for src/backend/metagen/validator.py — validate_metagen_output.

Spec: spec/feature/BACKEND_LLM.md §Metagen Validator

Each test exercises one rule from the spec table (lines 93–101):
  SCHEMA           — payload not matching MetagenLLMOutput
  OUT_OF_SCOPE_URN — dataset_urn not in run's in-scope set
  INVALID_ITEM_ID  — item_id does not match the expected pattern
  UNKNOWN_FIELD_PATH — column field_path not in schemaMetadata
  KIND_NOT_ALLOWED — element kind not in boundary.allowed
  EMPTY_VALUE      — value is empty or whitespace-only
  VALUE_TOO_LARGE  — value exceeds 16 KiB
  CONF_OUT_OF_RANGE — confidence_score not in [0.0, 1.0]
  DUP_ITEM         — duplicate (dataset_urn, item_id) pair within one turn
  ITEM_ALREADY_APPROVED — item with an approved candidate proposed again

Happy path: valid payload returns empty error list.
"""


from src.backend.metagen.validator import validate_metagen_output

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_URN2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"  # noqa: E501 URN literal

_IN_SCOPE = frozenset([_URN])
_BOUNDARY_ALLOWED = {_URN: ["dataset.description", "column.description"]}
_SCHEMA_FIELD_PATHS = {_URN: {"title", "isbn", "author"}}
_APPROVED_ITEM_IDS: frozenset[tuple[str, str]] = frozenset()


def _valid_payload(**overrides) -> dict:
    base = {
        "candidates": [
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "A comprehensive book catalog.",
                "confidence_score": 0.9,
            }
        ]
    }
    base.update(overrides)
    return base


# ── Happy path ────────────────────────────────────────────────────────────────


def test_validate_happy_path_returns_no_errors() -> None:
    """A fully valid payload returns an empty error list.

    Spec: BACKEND_LLM.md §Metagen Validator — all rules pass on a well-formed payload.
    """
    errors = validate_metagen_output(
        _valid_payload(),
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )

    assert errors == [], (
        "A valid payload must produce zero validation errors. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )


def test_validate_column_item_id_happy_path() -> None:
    """column.<field_path>.description with known field_path passes all rules.

    Spec: BACKEND_LLM.md §Metagen Validator — INVALID_ITEM_ID / UNKNOWN_FIELD_PATH rules.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "column.isbn.description",
            "value": "The ISBN-13 identifier for this title.",
            "confidence_score": 0.85,
        }]
    }
    errors = validate_metagen_output(
        payload,
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    assert errors == [], (
        "Valid column item_id 'column.isbn.description' must pass all validators. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )


# ── SCHEMA ────────────────────────────────────────────────────────────────────


def test_validate_schema_error_on_missing_candidates_key() -> None:
    """Payload missing 'candidates' key triggers SCHEMA error.

    Spec: BACKEND_LLM.md §Metagen Validator — SCHEMA: Pydantic shape of MetagenLLMOutput.
    """
    errors = validate_metagen_output(
        {"wrong_key": []},
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    assert len(errors) == 1
    assert errors[0].code == "SCHEMA", (
        "Missing 'candidates' key must produce SCHEMA error. "
        "spec: BACKEND_LLM.md §Metagen Validator — SCHEMA rule"
    )


def test_validate_schema_error_stops_further_evaluation() -> None:
    """On a SCHEMA error, no semantic rules are evaluated (single error returned).

    Spec: BACKEND_LLM.md §Metagen Validator — on SCHEMA error return immediately.
    """
    errors = validate_metagen_output(
        {},
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    assert len(errors) == 1
    assert errors[0].code == "SCHEMA"


def test_validate_schema_error_on_missing_required_candidate_field() -> None:
    """Candidate missing 'value' field triggers SCHEMA error.

    Spec: BACKEND_LLM.md §Metagen Validator — SCHEMA rule checks MetagenLLMOutput shape.
    """
    errors = validate_metagen_output(
        {
            "candidates": [
                {"dataset_urn": _URN, "item_id": "dataset.description", "confidence_score": 0.9}
            ]
        },
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    assert any(e.code == "SCHEMA" for e in errors), (
        "Missing 'value' field in candidate must produce SCHEMA error. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )


# ── OUT_OF_SCOPE_URN ──────────────────────────────────────────────────────────


def test_validate_out_of_scope_urn() -> None:
    """Candidate with dataset_urn not in in_scope_urns triggers OUT_OF_SCOPE_URN.

    Spec: BACKEND_LLM.md §Metagen Validator — OUT_OF_SCOPE_URN: dataset_urn ∈ run's in-scope set.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN2,  # not in _IN_SCOPE
            "item_id": "dataset.description",
            "value": "Out of scope.",
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload,
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    assert any(e.code == "OUT_OF_SCOPE_URN" for e in errors), (
        "dataset_urn not in in_scope_urns must produce OUT_OF_SCOPE_URN. "
        "spec: BACKEND_LLM.md §Metagen Validator — OUT_OF_SCOPE_URN rule"
    )


def test_validate_out_of_scope_urn_skips_further_semantic_rules() -> None:
    """OUT_OF_SCOPE_URN stops further semantic validation for that candidate.

    The candidate has an empty value and an out-of-scope URN. Only OUT_OF_SCOPE_URN
    must appear; EMPTY_VALUE and KIND_NOT_ALLOWED must be absent because the `continue`
    after OUT_OF_SCOPE_URN skips subsequent per-candidate checks.

    Spec: BACKEND_LLM.md §Metagen Validator — OUT_OF_SCOPE_URN triggers continue.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN2,           # not in _IN_SCOPE → OUT_OF_SCOPE_URN
            "item_id": "dataset.description",
            "value": "",                     # would trigger EMPTY_VALUE if evaluated
            "confidence_score": 0.9,         # valid per Pydantic so model_validate succeeds
        }]
    }
    errors = validate_metagen_output(
        payload,
        _IN_SCOPE,
        _BOUNDARY_ALLOWED,
        _SCHEMA_FIELD_PATHS,
        _APPROVED_ITEM_IDS,
    )
    # Must have OUT_OF_SCOPE_URN but NOT EMPTY_VALUE or KIND_NOT_ALLOWED (skipped after scope check)
    codes = [e.code for e in errors]
    assert "OUT_OF_SCOPE_URN" in codes
    assert "EMPTY_VALUE" not in codes
    assert "KIND_NOT_ALLOWED" not in codes


# ── INVALID_ITEM_ID ───────────────────────────────────────────────────────────


def test_validate_invalid_item_id_random_string() -> None:
    """item_id with arbitrary string triggers INVALID_ITEM_ID.

    Spec: BACKEND_LLM.md §Metagen Validator — INVALID_ITEM_ID: item_id pattern check.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "completely.wrong.format",
            "value": "Some value.",
            "confidence_score": 0.8,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "INVALID_ITEM_ID" for e in errors), (
        "Arbitrary item_id must produce INVALID_ITEM_ID. "
        "spec: BACKEND_LLM.md §Metagen Validator — item_id pattern"
    )


def test_validate_invalid_item_id_column_with_dots_in_field_path() -> None:
    """item_id 'column.a.b.description' (two dots in field path) triggers INVALID_ITEM_ID.

    Spec: BACKEND_LLM.md §Metagen Validator — item_id must match ^column\\.[^.]+\\.description$.
    Pattern forbids dots inside the field_path segment.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "column.nested.path.description",
            "value": "A column.",
            "confidence_score": 0.8,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "INVALID_ITEM_ID" for e in errors), (
        "'column.nested.path.description' must fail pattern check (dots in field_path). "
        "spec: BACKEND_LLM.md §Metagen Validator — ^column\\.[^.]+\\.description$"
    )


# ── UNKNOWN_FIELD_PATH ────────────────────────────────────────────────────────


def test_validate_unknown_field_path() -> None:
    """Candidate with column field_path not in schemaMetadata triggers UNKNOWN_FIELD_PATH.

    Spec: BACKEND_LLM.md §Metagen Validator — UNKNOWN_FIELD_PATH: field_path resolves.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "column.nonexistent_col.description",
            "value": "A column.",
            "confidence_score": 0.8,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "UNKNOWN_FIELD_PATH" for e in errors), (
        "Unknown column field_path must produce UNKNOWN_FIELD_PATH. "
        "spec: BACKEND_LLM.md §Metagen Validator — UNKNOWN_FIELD_PATH rule"
    )


def test_validate_unknown_field_path_skipped_when_schema_empty() -> None:
    """UNKNOWN_FIELD_PATH is not raised when schema_field_paths is empty (schema not fetched).

    Spec: BACKEND_LLM.md §Metagen Validator — UNKNOWN_FIELD_PATH only checked when known_paths
    non-empty.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "column.any_col.description",
            "value": "A column.",
            "confidence_score": 0.8,
        }]
    }
    empty_schema = {_URN: set()}
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, empty_schema, _APPROVED_ITEM_IDS
    )
    assert not any(e.code == "UNKNOWN_FIELD_PATH" for e in errors), (
        "UNKNOWN_FIELD_PATH must not fire when schema_field_paths is empty (schema unavailable). "
        "spec: BACKEND_LLM.md §Metagen Validator — only check when known_paths non-empty"
    )


# ── KIND_NOT_ALLOWED ──────────────────────────────────────────────────────────


def test_validate_kind_not_allowed() -> None:
    """Candidate with kind not in boundary.allowed triggers KIND_NOT_ALLOWED.

    Spec: BACKEND_LLM.md §Metagen Validator — KIND_NOT_ALLOWED: kind ∈ boundary.allowed.
    """
    # Only allow column.description, not dataset.description
    restricted = {_URN: ["column.description"]}
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "A dataset.",
            "confidence_score": 0.8,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, restricted, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "KIND_NOT_ALLOWED" for e in errors), (
        "dataset.description when only column.description is allowed must produce "
        "KIND_NOT_ALLOWED. "
        "spec: BACKEND_LLM.md §Metagen Validator — KIND_NOT_ALLOWED rule"
    )


def test_validate_kind_allowed_passes() -> None:
    """Candidate kind matching boundary.allowed does not trigger KIND_NOT_ALLOWED.

    Spec: BACKEND_LLM.md §Metagen Validator — kind within boundary is valid.
    """
    only_dataset = {_URN: ["dataset.description"]}
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "The catalog of Imazon books.",
            "confidence_score": 0.85,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, only_dataset, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert not any(e.code == "KIND_NOT_ALLOWED" for e in errors)


# ── EMPTY_VALUE ───────────────────────────────────────────────────────────────


def test_validate_empty_value_empty_string() -> None:
    """Candidate with value='' triggers EMPTY_VALUE.

    Spec: BACKEND_LLM.md §Metagen Validator — EMPTY_VALUE: value is non-empty Markdown.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "",
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "EMPTY_VALUE" for e in errors), (
        "Empty value must produce EMPTY_VALUE. "
        "spec: BACKEND_LLM.md §Metagen Validator — EMPTY_VALUE rule"
    )


def test_validate_empty_value_whitespace_only() -> None:
    """Candidate with value='   ' (whitespace-only) triggers EMPTY_VALUE.

    Spec: BACKEND_LLM.md §Metagen Validator — value is non-empty (non-blank) Markdown.
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "   \n  ",
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "EMPTY_VALUE" for e in errors), (
        "Whitespace-only value must produce EMPTY_VALUE. "
        "spec: BACKEND_LLM.md §Metagen Validator — non-empty Markdown"
    )


# ── VALUE_TOO_LARGE ───────────────────────────────────────────────────────────


def test_validate_value_too_large_exceeds_16kib() -> None:
    """Candidate with value > 16 KiB triggers VALUE_TOO_LARGE.

    Spec: BACKEND_LLM.md §Metagen Validator — VALUE_TOO_LARGE: value ≤ 16 KiB.
    """
    big_value = "x" * (16 * 1024 + 1)  # 1 byte over the 16 KiB limit
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": big_value,
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "VALUE_TOO_LARGE" for e in errors), (
        "Value exceeding 16 KiB must produce VALUE_TOO_LARGE. "
        "spec: BACKEND_LLM.md §Metagen Validator — VALUE_TOO_LARGE rule"
    )


def test_validate_value_at_16kib_boundary_passes() -> None:
    """Candidate with value exactly at 16 KiB passes VALUE_TOO_LARGE check.

    Spec: BACKEND_LLM.md §Metagen Validator — 16 KiB limit is exclusive (> 16 KiB fails).
    """
    exact_value = "y" * (16 * 1024)
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": exact_value,
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert not any(e.code == "VALUE_TOO_LARGE" for e in errors), (
        "Value exactly at 16 KiB must NOT produce VALUE_TOO_LARGE. "
        "spec: BACKEND_LLM.md §Metagen Validator — ≤ 16 KiB is valid"
    )


# ── CONF_OUT_OF_RANGE ─────────────────────────────────────────────────────────


def test_validate_confidence_below_zero() -> None:
    """confidence_score < 0.0 triggers CONF_OUT_OF_RANGE.

    Spec: BACKEND_LLM.md §Metagen Validator — CONF_OUT_OF_RANGE: confidence_score ∈ [0.0, 1.0].
    """
    # Pydantic Field(ge=0.0, le=1.0) in MetagenLLMCandidate will likely raise SCHEMA;
    # the validator has a belt-and-suspenders check too. Accept either code.
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "A description.",
            "confidence_score": -0.1,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code in ("CONF_OUT_OF_RANGE", "SCHEMA") for e in errors), (
        "confidence_score < 0.0 must produce CONF_OUT_OF_RANGE or SCHEMA. "
        "spec: BACKEND_LLM.md §Metagen Validator — CONF_OUT_OF_RANGE rule"
    )


def test_validate_confidence_above_one() -> None:
    """confidence_score > 1.0 triggers CONF_OUT_OF_RANGE or SCHEMA.

    Spec: BACKEND_LLM.md §Metagen Validator — CONF_OUT_OF_RANGE: confidence_score ∈ [0.0, 1.0].
    """
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "A description.",
            "confidence_score": 1.5,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code in ("CONF_OUT_OF_RANGE", "SCHEMA") for e in errors), (
        "confidence_score > 1.0 must produce CONF_OUT_OF_RANGE or SCHEMA. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )


# ── DUP_ITEM ──────────────────────────────────────────────────────────────────


def test_validate_dup_item_same_urn_and_item_id() -> None:
    """Two candidates with the same (dataset_urn, item_id) trigger DUP_ITEM.

    Spec: BACKEND_LLM.md §Metagen Validator — DUP_ITEM: no duplicate (dataset_urn, item_id).
    """
    payload = {
        "candidates": [
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "First description.",
                "confidence_score": 0.9,
            },
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "Second description.",
                "confidence_score": 0.8,
            },
        ]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    assert any(e.code == "DUP_ITEM" for e in errors), (
        "Duplicate (dataset_urn, item_id) must produce DUP_ITEM. "
        "spec: BACKEND_LLM.md §Metagen Validator — DUP_ITEM rule"
    )


def test_validate_dup_item_only_on_second_occurrence() -> None:
    """DUP_ITEM is reported on the second (duplicate) occurrence, not the first.

    Spec: BACKEND_LLM.md §Metagen Validator — DUP_ITEM includes the first occurrence index.
    """
    payload = {
        "candidates": [
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "First.",
                "confidence_score": 0.9,
            },
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "Second.",
                "confidence_score": 0.85,
            },
        ]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    dup_errors = [e for e in errors if e.code == "DUP_ITEM"]
    # path should reference candidates[1], not candidates[0]
    assert any("candidates[1]" in e.path for e in dup_errors), (
        "DUP_ITEM must reference the second occurrence. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )


# ── ITEM_ALREADY_APPROVED ─────────────────────────────────────────────────────


def test_validate_item_already_approved() -> None:
    """Candidate for an item with existing approved candidate triggers ITEM_ALREADY_APPROVED.

    Spec: BACKEND_LLM.md §Metagen Validator — ITEM_ALREADY_APPROVED: generation skips approved.
    """
    approved = frozenset({(_URN, "dataset.description")})
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "Trying to overwrite approved.",
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, approved
    )
    assert any(e.code == "ITEM_ALREADY_APPROVED" for e in errors), (
        "Proposing a candidate for an already-approved item must produce ITEM_ALREADY_APPROVED. "
        "spec: BACKEND_LLM.md §Metagen Validator — ITEM_ALREADY_APPROVED rule"
    )


def test_validate_item_not_in_approved_passes() -> None:
    """Candidate for an unapproved item does not trigger ITEM_ALREADY_APPROVED.

    Spec: BACKEND_LLM.md §Metagen Validator — ITEM_ALREADY_APPROVED only fires for approved items.
    """
    approved: frozenset[tuple[str, str]] = frozenset()  # no approved items
    payload = {
        "candidates": [{
            "dataset_urn": _URN,
            "item_id": "dataset.description",
            "value": "A fresh description.",
            "confidence_score": 0.9,
        }]
    }
    errors = validate_metagen_output(
        payload, _IN_SCOPE, _BOUNDARY_ALLOWED, _SCHEMA_FIELD_PATHS, approved
    )
    assert not any(e.code == "ITEM_ALREADY_APPROVED" for e in errors)


# ── Multiple errors in one payload ────────────────────────────────────────────


def test_validate_multiple_candidates_errors_reported_independently() -> None:
    """Each candidate accumulates its own errors independently.

    Spec: BACKEND_LLM.md §Metagen Validator — semantic rules evaluated per-candidate.
    """
    payload = {
        "candidates": [
            # Candidate 0: valid
            {
                "dataset_urn": _URN,
                "item_id": "dataset.description",
                "value": "Good description.",
                "confidence_score": 0.9,
            },
            # Candidate 1: KIND_NOT_ALLOWED (column not in restricted boundary)
            {
                "dataset_urn": _URN,
                "item_id": "column.isbn.description",
                "value": "A column description.",
                "confidence_score": 0.7,
            },
        ]
    }
    only_dataset = {_URN: ["dataset.description"]}  # column.description not allowed
    errors = validate_metagen_output(
        payload, _IN_SCOPE, only_dataset, _SCHEMA_FIELD_PATHS, _APPROVED_ITEM_IDS
    )
    # First candidate is valid, second triggers KIND_NOT_ALLOWED
    assert any(e.code == "KIND_NOT_ALLOWED" and "candidates[1]" in e.path for e in errors), (
        "KIND_NOT_ALLOWED must be associated with candidates[1] path. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )
    assert not any("candidates[0]" in e.path for e in errors), (
        "Valid candidate at index 0 must produce no errors. "
        "spec: BACKEND_LLM.md §Metagen Validator"
    )
