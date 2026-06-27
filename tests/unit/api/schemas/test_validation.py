"""Pydantic constraint tests for the passive result-store validation schemas.

Conf `variables` is an array of {name, description} objects; result POSTs are
keyed by variable name (descriptions live only on the conf).

spec: VALIDATION.md §Rule Configuration, §Validation Result
spec: API.md §Data Resource (validation rows)
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.validation import (
    PatchValidationConfRequest,
    PostValidationResultRequest,
    PutValidationConfRequest,
    ValidationVariable,
)


def _var(name: str, description: str = "") -> dict[str, str]:
    """Build a {name, description} variable object."""
    return {"name": name, "description": description}


# ── ValidationVariable element ────────────────────────────────────────────────


class TestValidationVariableElement:
    def test_name_and_description_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — variable = {name, description}
        v = ValidationVariable(name="row_cnt", description="Daily row count")
        assert v.name == "row_cnt"
        assert v.description == "Daily row count"

    def test_empty_description_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description required key,
        # empty string allowed.
        v = ValidationVariable(name="row_cnt", description="")
        assert v.description == ""

    def test_description_at_200_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — per-variable description ≤ 200 chars
        v = ValidationVariable(name="row_cnt", description="x" * 200)
        assert len(v.description) == 200

    def test_description_at_201_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — per-variable description ≤ 200 chars
        with pytest.raises(ValidationError, match=r"200|exceed|max"):
            ValidationVariable(name="row_cnt", description="x" * 201)

    def test_description_with_control_byte_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description disallows ASCII control
        # chars except \t (0x09) and \n (0x0a). 0x01 (SOH) is rejected.
        with pytest.raises(ValidationError):
            ValidationVariable(name="row_cnt", description="bad\x01desc")

    def test_description_with_tab_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — \t (0x09) is in the carve-out set.
        v = ValidationVariable(name="row_cnt", description="col1\tcol2")
        assert "\t" in v.description

    def test_description_with_newline_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — \n (0x0a) is in the carve-out set.
        v = ValidationVariable(name="row_cnt", description="line1\nline2")
        assert "\n" in v.description

    def test_name_must_match_regex_starting_digit_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — name matches [a-z][a-z0-9_]{0,99}
        with pytest.raises(ValidationError):
            ValidationVariable(name="1abc", description="")

    def test_name_uppercase_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — only lowercase [a-z][a-z0-9_]
        with pytest.raises(ValidationError):
            ValidationVariable(name="RowCnt", description="")

    def test_name_missing_description_key_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description is a required key.
        with pytest.raises(ValidationError):
            ValidationVariable(name="row_cnt")  # type: ignore[call-arg]


# ── PUT /attr/validation/conf ─────────────────────────────────────────────────


class TestPutValidationConfRequestDescription:
    def test_description_at_2000_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — rule description ≤ 2,000 chars
        req = PutValidationConfRequest(
            description="x" * 2000,
            variables=[_var("row_cnt")],
        )
        assert len(req.description) == 2000

    def test_description_at_2001_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — rule description ≤ 2,000 chars
        with pytest.raises(ValidationError, match=r"2[,]?000|exceed|max"):
            PutValidationConfRequest(
                description="x" * 2001,
                variables=[_var("row_cnt")],
            )

    def test_description_with_escape_code_control_byte_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — rule description disallows ASCII
        # control bytes except \t (0x09) and \n (0x0a). ESC (0x1b) is rejected.
        with pytest.raises(ValidationError):
            PutValidationConfRequest(
                description="text \x1b[31m red \x1b[0m text",
                variables=[_var("row_cnt")],
            )

    def test_description_with_tab_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — \t (0x09) is permitted.
        req = PutValidationConfRequest(
            description="col1\tcol2\tcol3",
            variables=[_var("row_cnt")],
        )
        assert "\t" in req.description

    def test_description_with_newline_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — \n (0x0a) is permitted.
        req = PutValidationConfRequest(
            description="line1\nline2",
            variables=[_var("row_cnt")],
        )
        assert "\n" in req.description


class TestPutValidationConfRequestVariables:
    def test_single_variable_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — ≥ 1 entry
        req = PutValidationConfRequest(
            description="check",
            variables=[_var("row_cnt", "Daily row count")],
        )
        assert req.variables[0].name == "row_cnt"
        assert req.variables[0].description == "Daily row count"

    def test_200_variables_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200
        vars_200 = [_var(f"v{i:03}") for i in range(200)]
        req = PutValidationConfRequest(description="check", variables=vars_200)
        assert len(req.variables) == 200

    def test_empty_variables_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — MUST be ≥ 1 entry
        with pytest.raises(ValidationError, match=r"at least 1|minimum|too_short"):
            PutValidationConfRequest(description="check", variables=[])

    def test_201_variables_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200
        vars_201 = [_var(f"v{i:03}") for i in range(201)]
        with pytest.raises(ValidationError, match=r"200|exceed|too_many"):
            PutValidationConfRequest(description="check", variables=vars_201)

    def test_valid_variable_names_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — [a-z][a-z0-9_]{0,99}
        req = PutValidationConfRequest(
            description="check",
            variables=[_var("a"), _var("abc123"), _var("col_1_mean")],
        )
        assert [v.name for v in req.variables] == ["a", "abc123", "col_1_mean"]

    def test_variable_starting_with_digit_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — name must start with [a-z]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=[_var("1abc")])

    def test_variable_with_uppercase_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — only lowercase [a-z][a-z0-9_]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=[_var("RowCnt")])

    def test_variable_starting_with_underscore_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — name must start with [a-z]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=[_var("_x")])

    def test_variable_name_101_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — [a-z][a-z0-9_]{0,99} → max 100 chars
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=[_var("a" * 101)])

    def test_variable_name_100_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — exactly 100 chars is the max
        req = PutValidationConfRequest(description="check", variables=[_var("a" * 100)])
        assert len(req.variables[0].name) == 100

    def test_duplicate_variable_names_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — names MUST be unique
        with pytest.raises(ValidationError, match=r"unique|duplicate"):
            PutValidationConfRequest(
                description="check",
                variables=[_var("row_cnt"), _var("null_rate"), _var("row_cnt")],
            )

    def test_per_variable_description_over_200_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — per-variable description ≤ 200 chars
        with pytest.raises(ValidationError, match=r"200|exceed|max"):
            PutValidationConfRequest(
                description="check",
                variables=[_var("row_cnt", "x" * 201)],
            )

    def test_per_variable_empty_description_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — empty per-variable description allowed
        req = PutValidationConfRequest(
            description="check",
            variables=[_var("row_cnt", "")],
        )
        assert req.variables[0].description == ""

    def test_bare_string_variable_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — each variable is a {name, description}
        # object, not a bare string. A plain string must fail validation.
        with pytest.raises(ValidationError):
            PutValidationConfRequest(
                description="check",
                variables=["row_cnt"],  # type: ignore[list-item]
            )


# ── PATCH /attr/validation/conf ───────────────────────────────────────────────


class TestPatchValidationConfRequest:
    def test_empty_body_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — PATCH accepts partial body
        req = PatchValidationConfRequest()
        assert req.description is None
        assert req.variables is None

    def test_description_only_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description alone is valid partial update
        req = PatchValidationConfRequest(description="Updated check")
        assert req.description == "Updated check"
        assert req.variables is None

    def test_variables_only_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — variables alone is valid partial update
        req = PatchValidationConfRequest(
            variables=[_var("row_cnt"), _var("null_rate")]
        )
        assert req.variables is not None
        assert [v.name for v in req.variables] == ["row_cnt", "null_rate"]
        assert req.description is None

    def test_variables_empty_list_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — empty list is rejected even on PATCH
        with pytest.raises(ValidationError, match=r"at least 1|minimum|too_short"):
            PatchValidationConfRequest(variables=[])

    def test_description_and_variables_both_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — both fields accepted together
        req = PatchValidationConfRequest(
            description="Updated daily check",
            variables=[_var("row_cnt"), _var("col1_mean", "Mean of col1")],
        )
        assert req.description == "Updated daily check"
        assert req.variables is not None
        assert req.variables[1].description == "Mean of col1"

    def test_variables_max_200_enforced(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200 entries.
        vars_201 = [_var(f"v{i:03}") for i in range(201)]
        with pytest.raises(ValidationError, match=r"200|exceed|too_many"):
            PatchValidationConfRequest(variables=vars_201)

    def test_variables_exactly_200_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200 (boundary: 200 valid)
        vars_200 = [_var(f"v{i:03}") for i in range(200)]
        req = PatchValidationConfRequest(variables=vars_200)
        assert req.variables is not None
        assert len(req.variables) == 200

    def test_variables_unicode_name_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — variable names MUST match
        # \A[a-z][a-z0-9_]{0,99}\Z. Non-ASCII characters fall outside the class.
        with pytest.raises(ValidationError):
            PatchValidationConfRequest(variables=[_var("café"), _var("naïve")])

    def test_variables_duplicate_names_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — names MUST be unique (PATCH too)
        with pytest.raises(ValidationError, match=r"unique|duplicate"):
            PatchValidationConfRequest(variables=[_var("row_cnt"), _var("row_cnt")])


# ── POST /attr/validation/result ─────────────────────────────────────────────


class TestPostValidationResultRequestScore:
    def test_score_0_0_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — 0.0 ≤ score ≤ 1.0 (service enforces range)
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.0,
            variables={"row_cnt": 0.0},
        )
        assert req.score == 0.0

    def test_score_1_0_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — 0.0 ≤ score ≤ 1.0 (service enforces range)
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )
        assert req.score == 1.0

    def test_score_out_of_range_accepted_at_schema_layer(self) -> None:
        # impl note: score range is NOT enforced at the schema layer — the service is the
        # single source of INVALID_SCORE (handles range AND NaN/Inf). Schema accepts any
        # float so the service can return a structured 422 with error_code.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.5,
            variables={"row_cnt": 50.0},
        )
        assert req.score == 1.5  # schema passes it through; service rejects it

    def test_score_nan_accepted_at_schema_layer(self) -> None:
        # impl note: NaN is accepted at the schema layer; the service rejects it via
        # math.isfinite and returns INVALID_SCORE.
        import math

        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=float("nan"),
            variables={"row_cnt": 50.0},
        )
        assert math.isnan(req.score)  # schema passes it through; service rejects it

    def test_score_positive_infinity_accepted_at_schema_layer(self) -> None:
        # impl note: Inf is accepted at the schema layer (same rationale as NaN).
        import math

        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=float("+inf"),
            variables={"row_cnt": 50.0},
        )
        assert math.isinf(req.score)  # schema passes it through; service rejects it


class TestPostValidationResultRequestVariables:
    def test_variable_value_nan_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — each value in variables must be finite
        with pytest.raises(ValidationError):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"row_cnt": float("nan")},
            )

    def test_variable_value_inf_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — each value in variables must be finite
        with pytest.raises(ValidationError):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"row_cnt": float("inf")},
            )

    def test_variable_key_must_match_regex(self) -> None:
        # spec: VALIDATION.md §Validation Result — result keys (variable names) match
        # [a-z][a-z0-9_]{0,99}. Result variables stay a {name: float} map.
        with pytest.raises(ValidationError):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"BadKey": 1.0},
            )

    def test_empty_variables_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — a result may report partial coverage
        # (including none); results impose no ≥1 floor (only conf declares ≥1 variables).
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={},
        )
        assert req.variables == {}

    def test_200_variable_entries_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — ≤ 200 entries
        vars_200 = {f"v{i:03}": float(i) for i in range(200)}
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables=vars_200,
        )
        assert len(req.variables) == 200

    def test_201_variable_entries_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — cap 200
        vars_201 = {f"v{i:03}": float(i) for i in range(201)}
        with pytest.raises(ValidationError, match=r"200|exceed|too_many"):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables=vars_201,
            )


class TestPostValidationResultRequestDataTime:
    def test_rfc3339_utc_string_parses(self) -> None:
        # spec: VALIDATION.md §Validation Result — data_time parses RFC 3339
        req = PostValidationResultRequest.model_validate(
            {
                "data_time": "2026-05-08T00:00:00Z",
                "score": 1.0,
                "variables": {"row_cnt": 50.0},
            }
        )
        assert req.data_time.tzinfo is not None

    def test_bad_string_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — bad data_time → 422 INVALID_PARAMETER
        with pytest.raises(ValidationError):
            PostValidationResultRequest.model_validate(
                {
                    "data_time": "not-a-date",
                    "score": 1.0,
                    "variables": {"row_cnt": 50.0},
                }
            )

    def test_naive_datetime_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — data_time must be timezone-aware (RFC 3339)
        with pytest.raises(ValidationError):
            PostValidationResultRequest.model_validate(
                {
                    "data_time": "2026-05-08T00:00:00",  # no tz info
                    "score": 1.0,
                    "variables": {"row_cnt": 50.0},
                }
            )
