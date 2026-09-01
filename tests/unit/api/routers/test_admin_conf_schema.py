"""Unit tests for RuntimeConfPatchRequest schema bound validation.

Concerns covered:

1. Out-of-bounds int values raise ValidationError (Pydantic rejects them before
   the service layer is reached).
2. Out-of-bounds float raises ValidationError.
3. At-boundary values (minimum and maximum) are accepted.
4. Partial payloads (None / omitted fields) are accepted — no field is required.

Spec traceability:
- spec/API.md §Admin (/admin) — PATCH numeric fields are bound-validated (out-of-range → 422);
  exact bound values live in impl (src/api/schemas/admin.py RuntimeConfPatchRequest) — field bounds:
    ontogen_debate_max_turns: ge=2, le=10
    ontogen_llm_max_iterations: ge=1, le=20
    ontogen_debate_rag_k: ge=0, le=20
    metagen_debate_max_turns: ge=2, le=10
    metagen_llm_max_iterations: ge=1, le=20
    metagen_debate_rag_k: ge=0, le=20
    metagen_confidence_threshold: ge=0.0, le=1.0
    metagen_ontology_rag_{node,edge,triple}_k: ge=0, le=20
- spec/API.md §Admin (/admin) — out-of-range values rejected (422); PATCH is partial
  (accepts None / partial). RuntimeConfPatchRequest raises pydantic ValidationError.
- src/api/schemas/admin.py RuntimeConfPatchRequest
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.admin import RuntimeConfPatchRequest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _expect_valid(**kwargs) -> RuntimeConfPatchRequest:
    """Assert that a RuntimeConfPatchRequest with the given kwargs is valid."""
    return RuntimeConfPatchRequest(**kwargs)


def _expect_invalid(**kwargs) -> None:
    """Assert that a RuntimeConfPatchRequest with the given kwargs raises ValidationError."""
    with pytest.raises(ValidationError):
        RuntimeConfPatchRequest(**kwargs)


# ── 1. Out-of-bounds int fields ───────────────────────────────────────────────


class TestOntogenDebateMaxTurnsBounds:
    """ontogen_debate_max_turns: ge=2, le=10.

    Spec: API.md §Admin (/admin) — out-of-range rejected (422); bounds in
    src/api/schemas/admin.py (ontogen_debate_max_turns=1, =11 rejected).
    """

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(ontogen_debate_max_turns=1)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(ontogen_debate_max_turns=11)

    def test_at_minimum_accepted(self) -> None:
        req = _expect_valid(ontogen_debate_max_turns=2)
        assert req.ontogen_debate_max_turns == 2

    def test_at_maximum_accepted(self) -> None:
        req = _expect_valid(ontogen_debate_max_turns=10)
        assert req.ontogen_debate_max_turns == 10

    def test_mid_range_accepted(self) -> None:
        req = _expect_valid(ontogen_debate_max_turns=6)
        assert req.ontogen_debate_max_turns == 6


class TestMetagenDebateMaxTurnsBounds:
    """metagen_debate_max_turns: ge=2, le=10.

    Spec: API.md §Admin (/admin) — out-of-range rejected (422); same impl bounds as
    ontogen_debate_max_turns (src/api/schemas/admin.py).
    """

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_debate_max_turns=1)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_debate_max_turns=11)

    def test_at_minimum_accepted(self) -> None:
        req = _expect_valid(metagen_debate_max_turns=2)
        assert req.metagen_debate_max_turns == 2

    def test_at_maximum_accepted(self) -> None:
        req = _expect_valid(metagen_debate_max_turns=10)
        assert req.metagen_debate_max_turns == 10


class TestOntogenLlmMaxIterationsBounds:
    """ontogen_llm_max_iterations: ge=1, le=20."""

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(ontogen_llm_max_iterations=0)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(ontogen_llm_max_iterations=21)

    def test_at_minimum_accepted(self) -> None:
        req = _expect_valid(ontogen_llm_max_iterations=1)
        assert req.ontogen_llm_max_iterations == 1

    def test_at_maximum_accepted(self) -> None:
        req = _expect_valid(ontogen_llm_max_iterations=20)
        assert req.ontogen_llm_max_iterations == 20


class TestMetagenLlmMaxIterationsBounds:
    """metagen_llm_max_iterations: ge=1, le=20."""

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_llm_max_iterations=0)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_llm_max_iterations=21)

    def test_at_minimum_accepted(self) -> None:
        req = _expect_valid(metagen_llm_max_iterations=1)
        assert req.metagen_llm_max_iterations == 1

    def test_at_maximum_accepted(self) -> None:
        req = _expect_valid(metagen_llm_max_iterations=20)
        assert req.metagen_llm_max_iterations == 20


class TestOntogenDebateRagKBounds:
    """ontogen_debate_rag_k: ge=0, le=20."""

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(ontogen_debate_rag_k=-1)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(ontogen_debate_rag_k=21)

    def test_at_minimum_zero_accepted(self) -> None:
        req = _expect_valid(ontogen_debate_rag_k=0)
        assert req.ontogen_debate_rag_k == 0

    def test_at_maximum_accepted(self) -> None:
        req = _expect_valid(ontogen_debate_rag_k=20)
        assert req.ontogen_debate_rag_k == 20


class TestMetagenDebateRagKBounds:
    """metagen_debate_rag_k: ge=0, le=20."""

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_debate_rag_k=-1)

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_debate_rag_k=21)

    def test_at_minimum_zero_accepted(self) -> None:
        req = _expect_valid(metagen_debate_rag_k=0)
        assert req.metagen_debate_rag_k == 0


class TestMetagenOntologyRagKBounds:
    """metagen_ontology_rag_{node,edge,triple}_k: ge=0, le=20."""

    def test_node_k_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_node_k=-1)

    def test_node_k_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_node_k=21)

    def test_edge_k_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_edge_k=-1)

    def test_edge_k_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_edge_k=21)

    def test_triple_k_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_triple_k=-1)

    def test_triple_k_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_ontology_rag_triple_k=21)

    def test_node_k_at_minimum_zero_accepted(self) -> None:
        req = _expect_valid(metagen_ontology_rag_node_k=0)
        assert req.metagen_ontology_rag_node_k == 0

    def test_node_k_at_maximum_accepted(self) -> None:
        req = _expect_valid(metagen_ontology_rag_node_k=20)
        assert req.metagen_ontology_rag_node_k == 20


# ── 2. Out-of-bounds float field ──────────────────────────────────────────────


class TestMetagenConfidenceThresholdBounds:
    """metagen_confidence_threshold: ge=0.0, le=1.0.

    Spec: API.md §Admin (/admin) — out-of-range rejected (422); bounds in
    src/api/schemas/admin.py (metagen_confidence_threshold=1.5 rejected).
    """

    def test_above_maximum_rejected(self) -> None:
        _expect_invalid(metagen_confidence_threshold=1.5)

    def test_below_minimum_rejected(self) -> None:
        _expect_invalid(metagen_confidence_threshold=-0.1)

    def test_at_zero_accepted(self) -> None:
        req = _expect_valid(metagen_confidence_threshold=0.0)
        assert req.metagen_confidence_threshold == 0.0

    def test_at_one_accepted(self) -> None:
        req = _expect_valid(metagen_confidence_threshold=1.0)
        assert req.metagen_confidence_threshold == 1.0

    def test_mid_range_accepted(self) -> None:
        req = _expect_valid(metagen_confidence_threshold=0.7)
        assert req.metagen_confidence_threshold == 0.7


# ── 3. Partial payloads (None / omitted) ──────────────────────────────────────


def test_empty_patch_request_is_valid() -> None:
    """An empty RuntimeConfPatchRequest (all fields omitted) is valid.

    Spec: API.md §Admin (/admin) — PATCH is partial (accepts None / partial).
    Every field is optional; callers supply only the fields to update.
    """
    req = RuntimeConfPatchRequest()
    # Every declared field defaults to None (excluded from the patch set). The dump
    # is asserted wholesale rather than field by field so a newly added field with a
    # non-None default cannot slip in unnoticed.
    assert req.model_dump() == dict.fromkeys(RuntimeConfPatchRequest.model_fields), (
        "every RuntimeConfPatchRequest field must default to None so an empty PATCH "
        "changes nothing. Spec: API.md §Admin (/admin) — PATCH is partial."
    )


def test_single_field_patch_is_valid() -> None:
    """A single-field RuntimeConfPatchRequest is valid; unset fields are None.

    Spec: API.md §Admin (/admin) — PATCH is partial; partial updates are the primary use case.
    """
    req = RuntimeConfPatchRequest(llm_model="gpt-4o-mini")
    assert req.llm_model == "gpt-4o-mini"
    assert req.llm_provider is None
    assert req.ontogen_debate_max_turns is None


def test_none_for_optional_string_fields_is_valid() -> None:
    """reviewer_model fields accept None explicitly (nullable string).

    Spec: API.md §Admin (/admin) — runtime config carries the ontogen debate knobs;
    ontogen_debate_reviewer_model: str|None (field shape in src/api/schemas/admin.py).
    """
    req = RuntimeConfPatchRequest(
        ontogen_debate_reviewer_model=None,
        metagen_debate_reviewer_model=None,
    )
    assert req.ontogen_debate_reviewer_model is None
    assert req.metagen_debate_reviewer_model is None


def test_exclude_unset_excludes_fields_not_provided() -> None:
    """model_dump(exclude_unset=True) returns only explicitly supplied fields.

    The router uses exclude_unset=True (without exclude_none) so that an explicit
    llm_api_key="" is preserved.  Only fields not supplied at all are absent.

    Spec: src/api/routers/admin.py — all_updates = body.model_dump(exclude_unset=True).
    """
    req = RuntimeConfPatchRequest(llm_model="my-model", ontogen_debate_max_turns=6)
    updates = req.model_dump(exclude_unset=True)
    assert "llm_model" in updates
    assert "ontogen_debate_max_turns" in updates
    # Fields not supplied must not appear.
    assert "llm_provider" not in updates
    assert "metagen_confidence_threshold" not in updates


# ── 5. llm_api_key field — free string, empty string allowed ─────────────────


def test_llm_api_key_none_is_valid() -> None:
    """llm_api_key=None (or omitted) is valid — means 'leave unchanged'.

    spec: BACKEND_LLM.md §LLM API key — omitting the field leaves the key unchanged.
    """
    req = RuntimeConfPatchRequest()
    assert req.llm_api_key is None


def test_llm_api_key_empty_string_is_valid() -> None:
    """llm_api_key="" is valid — explicit empty string means 'clear the key'.

    spec: BACKEND_LLM.md §LLM API key — explicit "" clears the key.
    """
    req = RuntimeConfPatchRequest(llm_api_key="")
    assert req.llm_api_key == ""


def test_llm_api_key_value_is_valid() -> None:
    """llm_api_key accepts a typical-length key value.

    The field has an upper bound of max_length=8192 (enforced by Pydantic; oversized
    values return 422 — covered by test_patch_conf_llm_api_key_over_8192_chars_returns_422
    in test_admin_conf_routes.py). A typical short key is well within that bound.

    spec: src/api/schemas/admin.py RuntimeConfPatchRequest — llm_api_key max_length=8192.
    """
    req = RuntimeConfPatchRequest(llm_api_key="sk-test-abc123")
    assert req.llm_api_key == "sk-test-abc123"


def test_llm_api_key_present_in_exclude_unset_dump() -> None:
    """An explicit llm_api_key="" is preserved by model_dump(exclude_unset=True).

    The router relies on this so that set_llm_api_key("") is called on clear.
    exclude_none=True would silently drop it — the router must NOT use that flag.

    spec: BACKEND_LLM.md §LLM API key — explicit "" must reach set_llm_api_key.
    """
    req = RuntimeConfPatchRequest(llm_api_key="")
    dump = req.model_dump(exclude_unset=True)
    assert "llm_api_key" in dump, (
        "llm_api_key='' must appear in exclude_unset dump so the router can detect a clear op."
    )
    assert dump["llm_api_key"] == ""


# ── 6. Unknown / misspelled keys are rejected (extra="forbid") ────────────────


class TestUnknownFieldRejected:
    """An unrecognised key in the PATCH body raises ValidationError — a misspelled
    field name is a loud 422, not a silent no-op.

    Spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
      "Write-request bodies (POST, PUT, PATCH) reject unknown fields with 422
      INVALID_PARAMETER rather than silently ignoring them".
    Spec: spec/API.md §/admin/conf — "A PATCH body carrying an unrecognised field
      is rejected 422 INVALID_PARAMETER rather than silently ignored, so a
      misspelled toggle or knob name fails loudly instead of leaving the config
      unchanged".
    impl: src/api/schemas/admin.py RuntimeConfPatchRequest —
      model_config = ConfigDict(extra="forbid").
    """

    def test_arbitrary_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(bogus_key="x")

    def test_issue_164_misspelled_stub_toggle_rejected(self) -> None:
        # `stub_llm_client` is the real field; the trailing "s" is issue #164's typo.
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(stub_llm_clients=False)

    def test_plural_corp_group_key_rejected(self) -> None:
        # `auth_datahub_corp_group` (singular) is the real field.
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(auth_datahub_corp_groups="dataspoke-users")

    def test_valid_known_partial_body_still_constructs(self) -> None:
        req = RuntimeConfPatchRequest(stub_llm_client=False)
        assert req.model_dump(exclude_unset=True) == {"stub_llm_client": False}


# ── 7. auth_datahub_corp_group — bounded, URN-safe token ─────────────────────


class TestAuthDatahubCorpGroupBounds:
    """auth_datahub_corp_group: max_length=128 + pattern CORP_GROUP_NAME_PATTERN.

    Spec: spec/feature/AUTH.md §Marker corpGroup — "Length-capped, URN-safe
      charset (exact bounds in impl); interpolated into the group URN and
      displayName".
    Spec: spec/API.md §/admin/conf — auth_datahub_corp_group is "a bounded,
      URN-safe token, default dataspoke-users"; "string fields are length- and
      shape-bound".
    impl: src/api/schemas/admin.py RuntimeConfPatchRequest / src/backend/datahub/users.py
      CORP_GROUP_NAME_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$.
    """

    def test_shipped_default_accepted(self) -> None:
        req = RuntimeConfPatchRequest(auth_datahub_corp_group="dataspoke-users")
        assert req.auth_datahub_corp_group == "dataspoke-users"

    def test_name_with_space_and_paren_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(auth_datahub_corp_group="bad name)")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(auth_datahub_corp_group="")

    def test_comma_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(auth_datahub_corp_group="a,b")

    def test_over_128_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(auth_datahub_corp_group="a" * 129)

    def test_exactly_128_chars_accepted(self) -> None:
        # A pattern-legal 128-char value: leading [A-Za-z0-9] + 127 tail chars.
        # This exercises the field's own max_length=128 cap (a 129×"a" value is
        # already barred by the pattern's {0,127} tail, so it does not).
        # spec: spec/feature/AUTH.md §Marker corpGroup — "Length-capped ...
        # exact bounds in impl".
        # impl: src/api/schemas/admin.py RuntimeConfPatchRequest — max_length=128;
        # src/backend/datahub/users.py CORP_GROUP_NAME_PATTERN.
        value = "a" + "b" * 127
        req = RuntimeConfPatchRequest(auth_datahub_corp_group=value)
        assert req.auth_datahub_corp_group == value


# ── 8. String knob length caps (max_length=128) ─────────────────────────────


_LENGTH_CAPPED_STRING_FIELDS = [
    "llm_provider",
    "llm_model",
    "ontogen_debate_reviewer_model",
    "metagen_debate_reviewer_model",
]


class TestStringKnobLengthCaps:
    """llm_provider / llm_model / {ontogen,metagen}_debate_reviewer_model: max_length=128.

    Spec: spec/API.md §/admin/conf — "string fields are length- and shape-bound".
    impl: src/api/schemas/admin.py RuntimeConfPatchRequest — max_length=128 on
      each of these fields.
    """

    @pytest.mark.parametrize("field", _LENGTH_CAPPED_STRING_FIELDS)
    def test_over_128_chars_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfPatchRequest(**{field: "x" * 129})

    @pytest.mark.parametrize("field", _LENGTH_CAPPED_STRING_FIELDS)
    def test_exactly_128_chars_accepted(self, field: str) -> None:
        value = "x" * 128
        req = RuntimeConfPatchRequest(**{field: value})
        assert getattr(req, field) == value
