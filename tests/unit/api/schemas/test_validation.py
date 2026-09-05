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
    ValidationAttribute,
    ValidationParameter,
    ValidationResultRow,
    ValidationVariable,
)
from src.shared.metric_conf import MAX_TIME_WINDOW_SEC


def _var(name: str, description: str = "") -> dict[str, str]:
    """Build a {name, description} variable object."""
    return {"name": name, "description": description}


def _param(name: str, value: str = "", description: str = "") -> dict[str, str]:
    """Build a {name, value, description} parameter object."""
    return {"name": name, "value": value, "description": description}


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


# ── attribute (data-arrival cadence) ─────────────────────────────────────────


class TestValidationAttribute:
    """spec: VALIDATION.md §Rule Configuration — the `attribute` field table.

    `cadence_unit` defaults to `86400`, MUST be `> 0` and `<= 315,360,000`;
    `cadence_offset` defaults to `0`, MUST be `>= 0`, and
    `cadence_offset * cadence_unit` MUST be `<= 315,360,000`.
    """

    def test_omitting_the_object_entirely_yields_both_defaults(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "Omitting it on `PUT` stores the
        # all-defaults object; it is never absent from a stored conf or from a response."
        attribute = ValidationAttribute()
        assert attribute.cadence_unit == 86400
        assert attribute.cadence_offset == 0

    def test_supplying_only_the_offset_defaults_the_unit(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — each field carries its own default,
        # so a partial object is completed field-by-field rather than rejected.
        attribute = ValidationAttribute(cadence_offset=7)
        assert attribute.cadence_offset == 7
        assert attribute.cadence_unit == 86400

    def test_supplying_only_the_unit_defaults_the_offset(self) -> None:
        attribute = ValidationAttribute(cadence_unit=3600)
        assert attribute.cadence_unit == 3600
        assert attribute.cadence_offset == 0

    def test_the_d8_example_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "Daily D-1 data is `unit = 86400,
        # offset = 0`; daily D-8 data is `unit = 86400, offset = 7`."
        attribute = ValidationAttribute(cadence_unit=86400, cadence_offset=7)
        assert (attribute.cadence_unit, attribute.cadence_offset) == (86400, 7)

    def test_cadence_unit_of_one_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — `cadence_unit` MUST be `> 0`, so the
        # interval is closed at 1.
        assert ValidationAttribute(cadence_unit=1).cadence_unit == 1

    @pytest.mark.parametrize("value", [0, -1])
    def test_cadence_unit_of_zero_or_below_is_rejected(self, value: int) -> None:
        # spec: VALIDATION.md §Rule Configuration — `cadence_unit` MUST be `> 0`. A
        # zero-second cadence would make every window collapse to its upper bound.
        with pytest.raises(ValidationError):
            ValidationAttribute(cadence_unit=value)

    def test_cadence_unit_at_the_ten_year_ceiling_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "MUST be … `<= 315,360,000` (ten
        # years — the same ceiling as `metric_conf.time_window_sec`)". The endpoint
        # itself is admissible.
        attribute = ValidationAttribute(cadence_unit=MAX_TIME_WINDOW_SEC)
        assert attribute.cadence_unit == MAX_TIME_WINDOW_SEC

    def test_cadence_unit_past_the_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ValidationAttribute(cadence_unit=MAX_TIME_WINDOW_SEC + 1)

    def test_cadence_offset_of_zero_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — `cadence_offset` MUST be `>= 0`, and
        # 0 is its default: D-1 data lags by no whole period.
        assert ValidationAttribute(cadence_offset=0).cadence_offset == 0

    def test_negative_cadence_offset_is_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — `cadence_offset` MUST be `>= 0`. A
        # negative lag would shift the window into the future.
        with pytest.raises(ValidationError):
            ValidationAttribute(cadence_offset=-1)

    def test_the_product_at_the_ceiling_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — the product bound is `<= 315,360,000`,
        # so the endpoint itself passes. Pairs with the test below, which is one second
        # past it: together they fix the bound rather than merely proving one exists.
        attribute = ValidationAttribute(cadence_unit=MAX_TIME_WINDOW_SEC, cadence_offset=1)
        assert attribute.cadence_unit * attribute.cadence_offset == MAX_TIME_WINDOW_SEC

    def test_a_product_past_the_ceiling_is_rejected_though_each_field_is_in_range(
        self,
    ) -> None:
        """The case no per-field bound can express: both factors are individually legal.

        `cadence_unit = 200_000_000` is inside `[1, 315_360_000]` and `cadence_offset = 2`
        is `>= 0`, yet their product is 400,000,000 — past the ceiling. Only the
        model-level check catches it, and it is the check that keeps the measurer's
        `timedelta(seconds=offset * unit)` from overflowing.

        spec: VALIDATION.md §Rule Configuration — "`cadence_offset * cadence_unit` MUST be
        `<= 315,360,000` — the same product bound the `validation-score` window arithmetic
        applies to `time_window_sec`, so an accepted `attribute` can never make a governed
        window's arithmetic overflow."
        """
        with pytest.raises(ValidationError, match=r"cadence_offset \* cadence_unit"):
            ValidationAttribute(cadence_unit=200_000_000, cadence_offset=2)

    def test_the_offset_carries_no_ceiling_of_its_own_up_to_the_product_bound(
        self,
    ) -> None:
        """The product bound's other endpoint: a huge `cadence_offset` with `unit = 1`.

        `cadence_offset` has no per-field ceiling — only the product is bounded — so
        315,360,000 periods of one second is admissible while 315,360,001 (below) is not.
        Pairs with the rejection below to fix the bound from the offset side, the way
        `test_the_product_at_the_ceiling_is_accepted` fixes it from the unit side.

        spec: VALIDATION.md §Rule Configuration — "`cadence_offset` … MUST be `>= 0`, and
        `cadence_offset * cadence_unit` MUST be `<= 315,360,000`".
        """
        attribute = ValidationAttribute(cadence_unit=1, cadence_offset=MAX_TIME_WINDOW_SEC)
        assert attribute.cadence_unit * attribute.cadence_offset == MAX_TIME_WINDOW_SEC

    def test_the_product_one_second_past_the_ceiling_is_rejected(self) -> None:
        """315,360,001 is the first rejected product — and it is the *product* check
        that rejects it.

        Both factors are individually legal (`cadence_unit = 1` is inside `[1,
        315_360_000]`, `cadence_offset` carries no ceiling of its own), so neither field
        bound can fire and the model-level `_check_window_shift` is the only thing left
        that can. The error is asserted to name the product rather than a field, which is
        what distinguishes this case from the per-field-ceiling test above.

        spec: VALIDATION.md §Rule Configuration — the product bound is inclusive at
        315,360,000, so 315,360,001 is the first rejected product.
        """
        with pytest.raises(ValidationError) as excinfo:
            ValidationAttribute(cadence_unit=1, cadence_offset=MAX_TIME_WINDOW_SEC + 1)

        (error,) = excinfo.value.errors()
        assert "cadence_offset * cadence_unit must not exceed" in error["msg"], (
            "the rejection must be the model-level product bound, not a per-field "
            f"ceiling; got {error['msg']!r}"
        )
        assert error["loc"] == (), (
            "a model validator's error carries no field location; a `cadence_unit` / "
            f"`cadence_offset` loc would mean a per-field bound fired instead; got "
            f"{error['loc']!r}"
        )

    def test_the_rejection_message_does_not_echo_the_rejected_product(self) -> None:
        # The rejected value is caller-controlled and arbitrarily long; the bound is what
        # the caller needs to be told. Keeps a 422 body's size out of the requester's
        # control, the same way the dataset_filter messages do.
        with pytest.raises(ValidationError) as excinfo:
            ValidationAttribute(cadence_unit=MAX_TIME_WINDOW_SEC, cadence_offset=1_000_000)
        assert "315360000000000" not in str(excinfo.value), (
            "the message must state the bound, not the rejected product"
        )

    @pytest.mark.parametrize("field", ["cadence_unit", "cadence_offset"])
    @pytest.mark.parametrize("value", [True, False])
    def test_a_json_boolean_is_not_an_admissible_integer(
        self, field: str, value: bool
    ) -> None:
        """`true` must not be admitted as a one-second cadence.

        `bool` subclasses `int`, so Pydantic's lax mode would otherwise coerce it —
        silently turning `{"cadence_unit": true}` into a one-second arrival cadence and
        every governed window into a one-second one.

        spec: VALIDATION.md §Rule Configuration — `cadence_unit` / `cadence_offset` are
        `int`; API.md §Metric states the same refusal for the sibling window integer:
        "out of range, non-integer, or boolean returns `422 INVALID_PARAMETER`".
        """
        with pytest.raises(ValidationError):
            ValidationAttribute(**{field: value})

    def test_an_unknown_key_is_not_stored(self) -> None:
        """`attribute` is a closed, typed object — not an open bag.

        spec: VALIDATION.md §Rule Configuration — "`attribute` is a **closed, typed
        object** — unknown keys are not stored."
        """
        attribute = ValidationAttribute.model_validate(
            {"cadence_unit": 3600, "cadence_offset": 1, "cadence_timezone": "UTC"}
        )
        assert attribute.model_dump() == {"cadence_unit": 3600, "cadence_offset": 1}


class TestPutValidationConfRequestAttribute:
    def test_omitting_attribute_stores_the_all_defaults_object(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "Omitting it on `PUT` stores the
        # all-defaults object". The field is non-optional on the response, so the request
        # model must materialise it rather than leaving it None.
        req = PutValidationConfRequest(description="d", variables=[_var("row_cnt")])
        assert req.attribute.cadence_unit == 86400
        assert req.attribute.cadence_offset == 0

    def test_a_partial_attribute_is_completed_with_per_field_defaults(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "Supplying `attribute` on `PUT` or
        # `PATCH` writes the **complete** per-field-defaulted object".
        req = PutValidationConfRequest.model_validate(
            {
                "description": "d",
                "variables": [_var("row_cnt")],
                "attribute": {"cadence_offset": 7},
            }
        )
        assert req.attribute.model_dump() == {"cadence_unit": 86400, "cadence_offset": 7}

    def test_an_out_of_range_attribute_rejects_the_whole_body(self) -> None:
        with pytest.raises(ValidationError):
            PutValidationConfRequest.model_validate(
                {
                    "description": "d",
                    "variables": [_var("row_cnt")],
                    "attribute": {"cadence_unit": 0},
                }
            )


class TestPatchValidationConfRequestAttribute:
    def test_omitting_attribute_leaves_it_unset(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — on PATCH, "Omitted, the stored value
        # is unchanged", which the schema expresses as None (nothing to write).
        req = PatchValidationConfRequest(description="d")
        assert req.attribute is None
        assert "attribute" not in req.model_dump(exclude_unset=True)

    def test_a_partial_attribute_defaults_the_unnamed_field_not_the_stored_one(
        self,
    ) -> None:
        """A PATCH naming one cadence field materialises the other at its **default**.

        This is the schema half of the wholesale-replacement rule: the model must produce
        a complete object with `cadence_unit = 86400` here, so a service writing
        `body.attribute.model_dump()` cannot accidentally deep-merge against whatever was
        stored. The service half is
        `tests/unit/backend/validation/test_service_config.py`.

        spec: VALIDATION.md §Rule Configuration — "A `PATCH` carrying
        `{"attribute": {"cadence_offset": 7}}` therefore also resets `cadence_unit` to
        `86400`."
        """
        req = PatchValidationConfRequest.model_validate({"attribute": {"cadence_offset": 7}})
        assert req.attribute is not None
        assert req.attribute.model_dump() == {"cadence_unit": 86400, "cadence_offset": 7}

    def test_an_out_of_range_attribute_rejects_the_patch(self) -> None:
        with pytest.raises(ValidationError):
            PatchValidationConfRequest.model_validate({"attribute": {"cadence_offset": -1}})


# ── parameter (opaque pipeline hyperparameters) ──────────────────────────────


class TestValidationParameterElement:
    """spec: VALIDATION.md §Rule Configuration — each `parameter` element follows "the
    same rules in its own separate namespace" as a `variables` element."""

    def test_name_value_and_description_accepted(self) -> None:
        parameter = ValidationParameter(
            name="z_threshold", value="2.5", description="Std-dev cutoff"
        )
        assert parameter.name == "z_threshold"
        assert parameter.value == "2.5"
        assert parameter.description == "Std-dev cutoff"

    def test_value_is_required_and_must_be_a_json_string(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — value is a required string field.
        with pytest.raises(ValidationError):
            ValidationParameter(name="z_threshold", description="cutoff")
        for non_string in (2.5, 3, True, None, {"threshold": 2.5}, ["2.5"]):
            with pytest.raises(ValidationError):
                ValidationParameter.model_validate(
                    {
                        "name": "z_threshold",
                        "value": non_string,
                        "description": "cutoff",
                    }
                )

    def test_empty_value_is_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — value is required but empty allowed.
        parameter = ValidationParameter(name="z_threshold", value="", description="")
        assert parameter.value == ""

    def test_value_at_200_chars_is_accepted_and_201_is_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — parameter value is ≤ 200 chars.
        assert (
            ValidationParameter(name="z_threshold", value="x" * 200, description="").value
            == "x" * 200
        )
        with pytest.raises(ValidationError, match=r"value must not exceed 200 characters"):
            ValidationParameter(name="z_threshold", value="x" * 201, description="")

    @pytest.mark.parametrize(
        "control_code",
        [*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F],
    )
    def test_value_rejects_every_forbidden_ascii_control_character(
        self, control_code: int
    ) -> None:
        # spec: VALIDATION.md §Rule Configuration — value excludes ASCII controls except
        # tab and newline.
        with pytest.raises(ValidationError, match=r"value contains control characters"):
            ValidationParameter(
                name="z_threshold",
                value=f"before{chr(control_code)}after",
                description="",
            )

    @pytest.mark.parametrize("allowed", ["left\tright", "line1\nline2"])
    def test_value_accepts_tab_and_newline(self, allowed: str) -> None:
        # Backstop: the two explicit carve-outs must survive verbatim.
        parameter = ValidationParameter(
            name="z_threshold", value=allowed, description=""
        )
        assert parameter.value == allowed

    def test_value_is_preserved_verbatim(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — value is stored/returned verbatim.
        supplied = '  {"threshold": 2.50}\t\n '
        parameter = ValidationParameter(
            name="z_threshold", value=supplied, description=""
        )
        assert parameter.value == supplied

    def test_empty_description_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "Required key, but the **empty string
        # is allowed**."
        assert ValidationParameter(name="z_threshold", value="", description="").description == ""

    def test_description_at_200_chars_accepted(self) -> None:
        parameter = ValidationParameter(name="z_threshold", value="", description="x" * 200)
        assert len(parameter.description) == 200

    def test_description_at_201_chars_rejected_naming_parameter(self) -> None:
        """The message says "parameter", not "variable".

        The two lists share their per-item rules but not their identity: a caller told
        their *variable* description is too long while editing `parameter` is being sent
        to the wrong field.

        The spec fixes the rule but not the wording — VALIDATION.md §Rule Configuration
        says only that `parameter` elements follow "the same rules in its own separate
        namespace". Which namespace the rejection *names* is therefore not a spec line;
        it is pinned here because the shared validator makes mislabelling the silent
        default, and this test is what keeps the two labels from collapsing back into one.
        """
        with pytest.raises(ValidationError) as excinfo:
            ValidationParameter(name="z_threshold", value="", description="x" * 201)
        message = str(excinfo.value)
        assert "parameter description must not exceed 200 characters" in message, (
            f"the message must name the parameter namespace; got {message}"
        )
        assert "variable description" not in message

    def test_description_with_control_byte_rejected_naming_parameter(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ValidationParameter(name="z_threshold", value="", description="bad\x01desc")
        message = str(excinfo.value)
        assert "parameter description contains control characters" in message, (
            f"the message must name the parameter namespace; got {message}"
        )
        assert "variable description" not in message

    @pytest.mark.parametrize("whitespace", ["\t", "\n"])
    def test_the_two_carve_out_control_characters_are_accepted(
        self, whitespace: str
    ) -> None:
        # Backstop for the control-character rejection above: \t (0x09) and \n (0x0a) are
        # the carve-out set, so the check is not simply refusing all whitespace.
        parameter = ValidationParameter(
            name="z_threshold", value="", description=f"a{whitespace}b"
        )
        assert whitespace in parameter.description

    @pytest.mark.parametrize("bad_name", ["1abc", "ZThreshold", "_z", "z-threshold", "café"])
    def test_the_name_regex_is_the_variable_one(self, bad_name: str) -> None:
        # spec: VALIDATION.md §Rule Configuration — name "MUST match
        # `\\A[a-z][a-z0-9_]{0,99}\\Z`", the same production `variables` uses.
        with pytest.raises(ValidationError):
            ValidationParameter(name=bad_name, value="", description="")

    def test_a_100_char_name_is_accepted_and_101_is_not(self) -> None:
        assert ValidationParameter(name="z" * 100, value="", description="").name == "z" * 100
        with pytest.raises(ValidationError):
            ValidationParameter(name="z" * 101, value="", description="")


class TestPutValidationConfRequestParameter:
    """spec: VALIDATION.md §Rule Configuration — the `parameter` lifecycle bullet list."""

    def test_omitting_parameter_leaves_it_absent(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "**`PUT`** is a full replace, like
        # every other field on it: omitting `parameter` stores it as absent".
        req = PutValidationConfRequest(description="d", variables=[_var("row_cnt")])
        assert req.parameter is None

    def test_a_non_empty_parameter_list_is_accepted(self) -> None:
        req = PutValidationConfRequest.model_validate(
            {
                "description": "d",
                "variables": [_var("row_cnt")],
                "parameter": [_param("z_threshold", "2.5", "Std-dev cutoff")],
            }
        )
        assert req.parameter is not None
        assert [p.name for p in req.parameter] == ["z_threshold"]

    def test_an_explicit_empty_list_is_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "When the key is present it MUST carry
        # 1–200 entries — an explicit `[]` is rejected exactly as an empty `variables` is."
        with pytest.raises(ValidationError, match=r"at least 1"):
            PutValidationConfRequest.model_validate(
                {"description": "d", "variables": [_var("row_cnt")], "parameter": []}
            )

    def test_the_cardinality_message_names_the_parameter_field(self) -> None:
        # The cardinality checks are about the *field*, so they name "parameter" rather
        # than "variables" — a caller told `variables` is empty while editing `parameter`
        # is being sent to the wrong field.
        with pytest.raises(ValidationError) as excinfo:
            PutValidationConfRequest.model_validate(
                {"description": "d", "variables": [_var("row_cnt")], "parameter": []}
            )
        message = str(excinfo.value)
        assert "parameter must have at least 1 entry" in message, (
            f"the message must name the parameter field; got {message}"
        )
        assert "variables must have at least 1 entry" not in message

    def test_200_parameters_are_accepted_and_201_are_not(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "MUST carry 1–200 entries".
        base = {"description": "d", "variables": [_var("row_cnt")]}
        req = PutValidationConfRequest.model_validate(
            {**base, "parameter": [_param(f"p{i:03}") for i in range(200)]}
        )
        assert req.parameter is not None and len(req.parameter) == 200
        with pytest.raises(ValidationError, match=r"parameter must not exceed 200 entries"):
            PutValidationConfRequest.model_validate(
                {**base, "parameter": [_param(f"p{i:03}") for i in range(201)]}
            )

    def test_duplicate_parameter_names_are_rejected(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — a name "MUST be unique across the
        # list", the list here being `parameter`.
        with pytest.raises(ValidationError, match=r"parameter names must be unique"):
            PutValidationConfRequest.model_validate(
                {
                    "description": "d",
                    "variables": [_var("row_cnt")],
                    "parameter": [_param("z_threshold"), _param("z_threshold")],
                }
            )

    def test_a_name_may_appear_in_both_variables_and_parameter(self) -> None:
        """Uniqueness is per list, so the two namespaces are genuinely separate.

        This is the assertion that distinguishes "two lists validated by one shared
        helper" from "one merged namespace": a cross-list uniqueness check would reject
        this body, and nothing else in the file would notice.

        spec: VALIDATION.md §Rule Configuration — "each `parameter` element, under the
        same rules in its own separate namespace (a name may appear in both lists;
        uniqueness is per list)".
        """
        req = PutValidationConfRequest.model_validate(
            {
                "description": "d",
                "variables": [_var("row_cnt", "Daily row count")],
                "parameter": [_param("row_cnt", "500", "Expected row count")],
            }
        )
        assert req.variables[0].name == "row_cnt"
        assert req.parameter is not None
        assert req.parameter[0].name == "row_cnt"
        assert req.parameter[0].description == "Expected row count"


class TestPatchValidationConfRequestParameter:
    def test_omitting_parameter_leaves_the_key_unset(self) -> None:
        """Omission and `null` must be distinguishable at the schema layer.

        Both surface as `parameter is None`, so `exclude_unset` is the only thing that
        tells "leave the stored value alone" from "clear it" — and the service reads
        exactly that. Asserting the value alone would pass for both spellings.

        spec: VALIDATION.md §Rule Configuration — "Omitting `parameter` leaves the stored
        value unchanged. `"parameter": null` clears it to absent".
        """
        req = PatchValidationConfRequest(description="d")
        assert req.parameter is None
        assert "parameter" not in req.model_dump(exclude_unset=True)

    def test_an_explicit_null_keeps_the_key_present(self) -> None:
        req = PatchValidationConfRequest.model_validate({"parameter": None})
        assert req.parameter is None
        assert "parameter" in req.model_dump(exclude_unset=True), (
            "an explicit null is the one spelling of 'clear', so the key has to survive "
            "exclude_unset for the service to see it"
        )

    def test_a_non_empty_list_replaces_wholesale(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "A non-empty list (1–200 entries,
        # validated exactly as `variables` is) replaces the stored value wholesale."
        req = PatchValidationConfRequest.model_validate(
            {"parameter": [_param("z_threshold", "2.5", "cutoff"), _param("window_days", "7")]}
        )
        assert req.parameter is not None
        assert [p.name for p in req.parameter] == ["z_threshold", "window_days"]

    def test_an_empty_list_is_rejected_on_patch_too(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — "`"parameter": []` is **rejected**
        # (`422`), the same as an empty `variables`, so there is no second spelling of
        # 'clear'."
        with pytest.raises(ValidationError, match=r"parameter must have at least 1 entry"):
            PatchValidationConfRequest.model_validate({"parameter": []})

    def test_duplicate_names_are_rejected_on_patch_too(self) -> None:
        with pytest.raises(ValidationError, match=r"parameter names must be unique"):
            PatchValidationConfRequest.model_validate(
                {"parameter": [_param("z_threshold"), _param("z_threshold")]}
            )


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


class TestPostValidationResultRequestScoreNote:
    """spec: VALIDATION.md §Validation Result — the `score_note` field table row."""

    def test_score_note_at_200_chars_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — score_note ≤ 200 chars
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
            score_note="x" * 200,
        )
        assert req.score_note == "x" * 200

    def test_score_note_at_201_chars_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — score_note ≤ 200 chars
        with pytest.raises(ValidationError, match=r"200|exceed|max"):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=0.8,
                variables={"row_cnt": 48.0},
                score_note="x" * 201,
            )

    def test_score_note_with_control_byte_rejected(self) -> None:
        # spec: VALIDATION.md §Validation Result — score_note disallows ASCII control
        # chars except \t (0x09) and \n (0x0a). 0x01 (SOH) is rejected.
        with pytest.raises(ValidationError):
            PostValidationResultRequest(
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=0.8,
                variables={"row_cnt": 48.0},
                score_note="bad\x01note",
            )

    def test_score_note_with_tab_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — \t (0x09) is in the carve-out set.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
            score_note="col1\tcol2",
        )
        assert "\t" in req.score_note

    def test_score_note_with_newline_accepted(self) -> None:
        # spec: VALIDATION.md §Validation Result — \n (0x0a) is in the carve-out set.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
            score_note="line1\nline2",
        )
        assert "\n" in req.score_note

    def test_empty_string_score_note_normalizes_to_none(self) -> None:
        # spec: VALIDATION.md §Validation Result — "Omitted or empty stores as absent."
        # Empty string is a valid input but normalizes to None, matching every other
        # optional free-text field's "omitted or empty" contract.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
            score_note="",
        )
        assert req.score_note is None

    def test_omitted_score_note_defaults_to_none(self) -> None:
        # spec: VALIDATION.md §Validation Result — score_note is optional.
        req = PostValidationResultRequest(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
        )
        assert req.score_note is None


class TestValidationResultRow:
    """spec: API.md §GET .../attr/validation/result — row shape includes score_note."""

    def test_non_null_score_note_round_trips(self) -> None:
        row = ValidationResultRow.model_validate(
            {
                "data_time": datetime(2026, 5, 1, tzinfo=UTC),
                "score": 0.8,
                "variables": {"row_cnt": 48.0},
                "score_note": "breached 1/5: var_03",
            }
        )
        assert row.score_note == "breached 1/5: var_03"

    def test_null_score_note_round_trips(self) -> None:
        row = ValidationResultRow.model_validate(
            {
                "data_time": datetime(2026, 5, 1, tzinfo=UTC),
                "score": 1.0,
                "variables": {"row_cnt": 50.0},
                "score_note": None,
            }
        )
        assert row.score_note is None
