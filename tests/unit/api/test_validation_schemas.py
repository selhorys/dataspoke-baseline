"""Pydantic constraint tests for the passive result-store validation schemas.

spec: VALIDATION.md §Rule Configuration, §Validation Result
spec: API.md §Data Resource (validation rows) lines 305–326
"""

from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from src.api.schemas.validation import (
    PatchValidationConfRequest,
    PostValidationResultRequest,
    PutValidationConfRequest,
)


# ── PUT /attr/validation/conf ─────────────────────────────────────────────────


class TestPutValidationConfRequestDescription:
    def test_description_at_2000_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description ≤ 2,000 chars
        req = PutValidationConfRequest(
            description="x" * 2000,
            variables=["row_cnt"],
        )
        assert len(req.description) == 2000

    def test_description_at_2001_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description ≤ 2,000 chars
        with pytest.raises(ValidationError, match=r"2[,]?000|exceed|max"):
            PutValidationConfRequest(
                description="x" * 2001,
                variables=["row_cnt"],
            )

    def test_description_with_escape_code_control_byte_rejected(self) -> None:
        # defense-in-depth: schema rejects ASCII control bytes (security-reviewer S-F2).
        # VALIDATION.md §Rule Configuration does not mandate this; the impl adds it as
        # defense-in-depth to prevent ESC-injection into DataHub UI strings.
        with pytest.raises(ValidationError):
            PutValidationConfRequest(
                description="text \x1b[31m red \x1b[0m text",
                variables=["row_cnt"],
            )

    def test_description_with_tab_accepted(self) -> None:
        # defense-in-depth allows tab (0x09): horizontal tabs are useful in multi-column
        # descriptions and are not security-relevant. The control-byte regex deliberately
        # excludes \t (security-reviewer S-F2 carve-out).
        req = PutValidationConfRequest(
            description="col1\tcol2\tcol3",
            variables=["row_cnt"],
        )
        assert "\t" in req.description

    def test_description_with_newline_accepted(self) -> None:
        # defense-in-depth allows newline (0x0a): newlines are normal in multi-line
        # descriptions and are excluded from the control-byte rejection regex
        # (security-reviewer S-F2 carve-out).
        req = PutValidationConfRequest(
            description="line1\nline2",
            variables=["row_cnt"],
        )
        assert "\n" in req.description


class TestPutValidationConfRequestVariables:
    def test_single_variable_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — ≥ 1 entry
        req = PutValidationConfRequest(
            description="check",
            variables=["row_cnt"],
        )
        assert req.variables == ["row_cnt"]

    def test_200_variables_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200
        vars_200 = [f"v{i:03}" for i in range(200)]
        req = PutValidationConfRequest(description="check", variables=vars_200)
        assert len(req.variables) == 200

    def test_empty_variables_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — MUST be ≥ 1 entry
        with pytest.raises(ValidationError, match=r"at least 1|minimum|too_short"):
            PutValidationConfRequest(description="check", variables=[])

    def test_201_variables_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — hard cap 200
        vars_201 = [f"v{i:03}" for i in range(201)]
        with pytest.raises(ValidationError, match=r"200|exceed|too_many"):
            PutValidationConfRequest(description="check", variables=vars_201)

    def test_valid_variable_name_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — [a-z][a-z0-9_]{0,99}
        req = PutValidationConfRequest(
            description="check",
            variables=["a", "abc123", "col_1_mean"],
        )
        assert "col_1_mean" in req.variables

    def test_variable_starting_with_digit_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — must start with [a-z]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=["1abc"])

    def test_variable_with_uppercase_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — only lowercase [a-z][a-z0-9_]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=["RowCnt"])

    def test_variable_starting_with_underscore_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — must start with [a-z]
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=["_x"])

    def test_variable_name_101_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — [a-z][a-z0-9_]{0,99} → max 100 chars total
        with pytest.raises(ValidationError):
            PutValidationConfRequest(description="check", variables=["a" * 101])

    def test_variable_name_100_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — exactly 100 chars is the max
        req = PutValidationConfRequest(description="check", variables=["a" * 100])
        assert len(req.variables[0]) == 100

    def test_duplicate_variable_names_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — names MUST be unique
        with pytest.raises(ValidationError, match=r"unique|duplicate"):
            PutValidationConfRequest(
                description="check",
                variables=["row_cnt", "null_rate", "row_cnt"],
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
        req = PatchValidationConfRequest(variables=["row_cnt", "null_rate"])
        assert req.variables == ["row_cnt", "null_rate"]
        assert req.description is None

    def test_variables_empty_list_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — empty list is rejected even on PATCH
        with pytest.raises(ValidationError, match=r"at least 1|minimum|too_short"):
            PatchValidationConfRequest(variables=[])

    def test_description_and_variables_both_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — both fields accepted together
        req = PatchValidationConfRequest(
            description="Updated daily check",
            variables=["row_cnt", "col1_mean"],
        )
        assert req.description == "Updated daily check"
        assert req.variables == ["row_cnt", "col1_mean"]


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
        # float so the service can return a structured 422 with error_code rather than a
        # raw Pydantic 422. Range enforcement tests live in test_validation_routes.py (F14).
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.5,
            variables={"row_cnt": 50.0},
        )
        assert req.score == 1.5  # schema passes it through; service rejects it

    def test_score_nan_accepted_at_schema_layer(self) -> None:
        # impl note: NaN is accepted at the schema layer (score is plain float with
        # no allow_inf_nan=False constraint). The service rejects it via math.isfinite
        # and returns INVALID_SCORE. Service-layer tests live in test_validation_routes.py.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=float("nan"),
            variables={"row_cnt": 50.0},
        )
        import math
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
        # spec: VALIDATION.md §Rule Configuration — keys match [a-z][a-z0-9_]{0,99}
        with pytest.raises(ValidationError):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"BadKey": 1.0},
            )

    def test_at_least_one_variable_entry_required(self) -> None:
        # spec: VALIDATION.md §Validation Result — variables must have ≥ 1 entry
        with pytest.raises(ValidationError, match=r"at least 1|minimum|too_short"):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={},
            )

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
        # spec: VALIDATION.md §Validation Result — bad data_time → 400 INVALID_PARAMETER
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
