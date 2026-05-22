"""Unit tests for RuntimeConfPatchRequest schema bound validation.

Concerns covered:

1. Out-of-bounds int values raise ValidationError (Pydantic rejects them before
   the service layer is reached).
2. Out-of-bounds float raises ValidationError.
3. At-boundary values (minimum and maximum) are accepted.
4. Partial payloads (None / omitted fields) are accepted — schema must not
   require all 15 fields.
5. Exact boundary: validation_score_n_intervals=0 is rejected (ge=1); =1 is
   accepted.

Spec traceability:
- task brief §What's under test — field bounds:
    ontogen_debate_max_turns: ge=2, le=10
    ontogen_llm_max_iterations: ge=1, le=20
    ontogen_debate_rag_k: ge=0, le=20
    metagen_debate_max_turns: ge=2, le=10
    metagen_llm_max_iterations: ge=1, le=20
    metagen_debate_rag_k: ge=0, le=20
    metagen_confidence_threshold: ge=0.0, le=1.0
    metagen_ontology_rag_{node,edge,triple}_k: ge=0, le=20
    validation_score_n_intervals: ge=1
- task brief §Unit — 'Schema bound validation: RuntimeConfPatchRequest rejects
  out-of-bounds values ... — pydantic ValidationError. Accepts None / partial.'
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

    Spec: task brief §Unit — 'ontogen_debate_max_turns=1, =11 rejected'.
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

    Spec: task brief §Unit — same bounds as ontogen_debate_max_turns.
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

    Spec: task brief §Unit — 'metagen_confidence_threshold=1.5 rejected'.
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


# ── 3. validation_score_n_intervals boundary ──────────────────────────────────


class TestValidationScoreNIntervalsBounds:
    """validation_score_n_intervals: ge=1 (no upper bound).

    Spec: task brief §Unit — 'validation_score_n_intervals=0 rejected'.
    """

    def test_zero_rejected(self) -> None:
        _expect_invalid(validation_score_n_intervals=0)

    def test_negative_rejected(self) -> None:
        _expect_invalid(validation_score_n_intervals=-1)

    def test_at_minimum_one_accepted(self) -> None:
        req = _expect_valid(validation_score_n_intervals=1)
        assert req.validation_score_n_intervals == 1

    def test_large_value_accepted(self) -> None:
        req = _expect_valid(validation_score_n_intervals=100)
        assert req.validation_score_n_intervals == 100


# ── 4. Partial payloads (None / omitted) ──────────────────────────────────────


def test_empty_patch_request_is_valid() -> None:
    """An empty RuntimeConfPatchRequest (all fields omitted) is valid.

    Spec: task brief §Unit — 'Accepts None / partial.'
    All 16 fields are optional; callers supply only the fields to update.
    """
    req = RuntimeConfPatchRequest()
    # All fields default to None (excluded from patch set).
    assert req.llm_provider is None
    assert req.llm_model is None
    assert req.llm_api_key is None
    assert req.ontogen_llm_max_iterations is None
    assert req.ontogen_debate_max_turns is None
    assert req.ontogen_debate_rag_k is None
    assert req.ontogen_debate_reviewer_model is None
    assert req.metagen_llm_max_iterations is None
    assert req.metagen_debate_max_turns is None
    assert req.metagen_debate_rag_k is None
    assert req.metagen_debate_reviewer_model is None
    assert req.metagen_confidence_threshold is None
    assert req.metagen_ontology_rag_node_k is None
    assert req.metagen_ontology_rag_edge_k is None
    assert req.metagen_ontology_rag_triple_k is None
    assert req.validation_score_n_intervals is None


def test_single_field_patch_is_valid() -> None:
    """A single-field RuntimeConfPatchRequest is valid; unset fields are None.

    Spec: task brief §Unit — partial updates are the primary use case.
    """
    req = RuntimeConfPatchRequest(llm_model="gpt-4o-mini")
    assert req.llm_model == "gpt-4o-mini"
    assert req.llm_provider is None
    assert req.ontogen_debate_max_turns is None


def test_none_for_optional_string_fields_is_valid() -> None:
    """reviewer_model fields accept None explicitly (nullable string).

    Spec: task brief §What's under test — 'ontogen_debate_reviewer_model: str|None'.
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
