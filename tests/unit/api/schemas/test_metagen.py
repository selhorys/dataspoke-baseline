"""Unit tests for src/api/schemas/metagen.py — UC4 global-conf schema surface.

Spec: spec/API.md §Metadata Generation
      spec/feature/BACKEND_SCHEMA.md — metagen tables constraints

Constraints tested:
  - result_limit ∈ [1, 20] (MetagenGlobalConfPutRequest and PatchRequest)
  - dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1000 per dimension (PUT and PATCH)
  - dataset_filter.origin accepted; origin='' rejected at schema layer (unified four-dim shape)
  - dataset_filter.dataset_urns malformed URN raises INVALID_DATASET_URN at schema layer (UC4)
  - reason max_length=2000 (MetagenReviewRequest)
  - verdict ∈ {approve, reject} (MetagenReviewRequest)
  - MetagenBoundaryPutRequest.allowed ∈ {dataset.description, column.description}
  - MetagenItemListResponse envelope shape uses 'total_count' (not 'total')
  - MetagenRunResponse carries status: Literal["success","failure"]

Spec traceability:
  spec/API.md §UC4 Metadata Generation — dataset_filter unified four-dimension shape
  spec/API.md §Payload caps — dataset_filter list dimensions capped at 1,000
  spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.metagen import (
    MetagenBoundaryPatchRequest,
    MetagenBoundaryPutRequest,
    MetagenGlobalConfPatchRequest,
    MetagenGlobalConfPutRequest,
    MetagenGlobalConfResponse,
    MetagenItemListResponse,
    MetagenItemSummary,
    MetagenReviewRequest,
    MetagenRunRequest,
    MetagenRunResponse,
)
from src.shared.exceptions import InvalidDatasetUrnError

_DATASET_FILTER_LIST_CAP = 1000
_REASON_MAX_LEN = 2000

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── MetagenGlobalConfPutRequest ───────────────────────────────────────────────


class TestMetagenGlobalConfPutRequest:
    def test_valid_minimal_request(self) -> None:
        """Minimal PUT request with is_enabled only is valid.

        Spec: API.md §Metadata Generation — PUT /metagen/attr/conf accepts is_enabled.
        """
        req = MetagenGlobalConfPutRequest(is_enabled=False)
        assert req.is_enabled is False
        assert req.result_limit == 3  # default
        assert req.overwrite_pending is True  # default

    def test_result_limit_minimum_valid(self) -> None:
        """result_limit=1 is the minimum valid value.

        Spec: spec/feature/BACKEND_SCHEMA.md — metagen_config.result_limit ∈ [1, 20].
        """
        req = MetagenGlobalConfPutRequest(is_enabled=False, result_limit=1)
        assert req.result_limit == 1

    def test_result_limit_maximum_valid(self) -> None:
        """result_limit=20 is the maximum valid value.

        Spec: spec/feature/BACKEND_SCHEMA.md — metagen_config.result_limit ∈ [1, 20].
        """
        req = MetagenGlobalConfPutRequest(is_enabled=False, result_limit=20)
        assert req.result_limit == 20

    def test_result_limit_below_minimum_raises(self) -> None:
        """result_limit=0 raises ValidationError (below minimum of 1).

        Spec: spec/feature/BACKEND_SCHEMA.md — CHECK result_limit BETWEEN 1 AND 20.
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(is_enabled=False, result_limit=0)

    def test_result_limit_above_maximum_raises(self) -> None:
        """result_limit=21 raises ValidationError (above maximum of 20).

        Spec: spec/feature/BACKEND_SCHEMA.md — CHECK result_limit BETWEEN 1 AND 20.
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(is_enabled=False, result_limit=21)

    def test_dataset_filter_urns_at_cap_valid(self) -> None:
        """dataset_filter.dataset_urns with exactly 1000 entries is valid.

        Spec: API.md §Payload caps — dataset_filter.dataset_urns ≤ 1,000 entries.
        """
        urns = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)" for i in range(_DATASET_FILTER_LIST_CAP)]
        req = MetagenGlobalConfPutRequest(
            is_enabled=False,
            dataset_filter={"dataset_urns": urns},
        )
        assert len(req.dataset_filter["dataset_urns"]) == _DATASET_FILTER_LIST_CAP

    def test_dataset_filter_urns_exceeds_cap_raises(self) -> None:
        """dataset_filter.dataset_urns with 1001 entries raises ValidationError.

        Spec: API.md §Payload caps — dataset_filter.dataset_urns ≤ 1,000 entries.
        """
        too_many = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)" for i in range(_DATASET_FILTER_LIST_CAP + 1)]
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(
                is_enabled=False,
                dataset_filter={"dataset_urns": too_many},
            )

    @pytest.mark.parametrize("dimension", ["tags", "glossary_terms"])
    def test_dataset_filter_non_urn_dimensions_exceed_cap_raises(self, dimension: str) -> None:
        """dataset_filter.{tags,glossary_terms} with 1001 entries raises ValidationError.

        Spec: API.md §Payload caps —
        dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1,000 per dimension.
        """
        too_many = [f"urn:li:tag:t{i}" if dimension == "tags" else f"urn:li:glossaryTerm:t{i}"
                    for i in range(_DATASET_FILTER_LIST_CAP + 1)]
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(
                is_enabled=False,
                dataset_filter={dimension: too_many},
            )

    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: spec/feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = MetagenGlobalConfPutRequest(is_enabled=False, schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: API.md §Metadata Generation — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(is_enabled=False, schedule_tier="minutely")  # type: ignore[arg-type]


# ── MetagenGlobalConfPatchRequest ─────────────────────────────────────────────


class TestMetagenGlobalConfPatchRequest:
    def test_empty_patch_is_valid(self) -> None:
        """An empty PATCH body is valid (no fields to update).

        Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial update.
        """
        req = MetagenGlobalConfPatchRequest()
        assert req.is_enabled is None
        assert req.result_limit is None

    def test_result_limit_below_minimum_raises(self) -> None:
        """result_limit=0 in PATCH raises ValidationError.

        Spec: spec/feature/BACKEND_SCHEMA.md — result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPatchRequest(result_limit=0)

    def test_result_limit_above_maximum_raises(self) -> None:
        """result_limit=21 in PATCH raises ValidationError.

        Spec: spec/feature/BACKEND_SCHEMA.md — result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPatchRequest(result_limit=21)

    def test_dataset_filter_urns_exceeds_cap_raises(self) -> None:
        """PATCH with dataset_filter.dataset_urns > 1000 raises ValidationError.

        Spec: API.md §Payload caps — dataset_filter.dataset_urns ≤ 1,000 entries.
        """
        too_many = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)" for i in range(_DATASET_FILTER_LIST_CAP + 1)]
        with pytest.raises(ValidationError):
            MetagenGlobalConfPatchRequest(dataset_filter={"dataset_urns": too_many})

    @pytest.mark.parametrize("dimension", ["tags", "glossary_terms"])
    def test_dataset_filter_non_urn_dimensions_exceed_cap_raises(self, dimension: str) -> None:
        """PATCH with dataset_filter.{tags,glossary_terms} > 1000 raises ValidationError.

        Spec: API.md §Payload caps —
        dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1,000 per dimension.
        """
        too_many = [f"urn:li:tag:t{i}" if dimension == "tags" else f"urn:li:glossaryTerm:t{i}"
                    for i in range(_DATASET_FILTER_LIST_CAP + 1)]
        with pytest.raises(ValidationError):
            MetagenGlobalConfPatchRequest(dataset_filter={dimension: too_many})


# ── MetagenBoundaryPutRequest ──────────────────────────────────────────────────


class TestMetagenBoundaryPutRequest:
    def test_valid_with_both_kinds(self) -> None:
        """PUT boundary with both dataset.description and column.description is valid.

        Spec: API.md §Metadata Generation — boundary allowed ∈ {dataset.description, column.description}.
        """
        req = MetagenBoundaryPutRequest(
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
        )
        assert len(req.allowed) == 2

    def test_valid_with_empty_allowed(self) -> None:
        """PUT boundary with allowed=[] is valid (no generation for this dataset).

        Spec: API.md §Metadata Generation — empty allowed list is a valid opt-in state.
        """
        req = MetagenBoundaryPutRequest(is_enabled=True, allowed=[])
        assert req.allowed == []

    def test_invalid_allowed_kind_raises(self) -> None:
        """PUT boundary with invalid kind 'cross_data.md' raises ValidationError.

        Spec: API.md §Metadata Generation — allowed values are a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenBoundaryPutRequest(is_enabled=True, allowed=["cross_data.md"])  # type: ignore[list-item]


class TestMetagenBoundaryPatchRequest:
    def test_empty_patch_valid(self) -> None:
        """Empty PATCH is valid for boundary.

        Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial.
        """
        req = MetagenBoundaryPatchRequest()
        assert req.is_enabled is None
        assert req.allowed is None


# ── MetagenItemListResponse ────────────────────────────────────────────────────


class TestMetagenItemListResponse:
    def test_envelope_uses_total_count_field(self) -> None:
        """MetagenItemListResponse envelope carries 'total_count', not 'total'.

        Spec: API.md §Standard Response Envelope — pagination uses total_count.
        """
        resp = MetagenItemListResponse(total_count=42)
        assert resp.total_count == 42, (
            "MetagenItemListResponse must use 'total_count'. "
            "spec: API.md §Standard Response Envelope"
        )
        assert not hasattr(resp, "total") or not hasattr(MetagenItemListResponse, "total"), (
            "MetagenItemListResponse must not expose a bare 'total' field. "
            "spec: API.md §Standard Response Envelope"
        )

    def test_items_defaults_to_empty_list(self) -> None:
        """MetagenItemListResponse.items defaults to empty list.

        Spec: API.md §Standard Response Envelope — list responses start empty.
        """
        resp = MetagenItemListResponse(total_count=0)
        assert resp.items == []

    def test_envelope_has_offset_and_limit(self) -> None:
        """MetagenItemListResponse inherits offset/limit from PaginatedResponse.

        Spec: API.md §Standard Response Envelope — pagination fields offset, limit.
        """
        resp = MetagenItemListResponse(total_count=5, offset=10, limit=20)
        assert resp.offset == 10
        assert resp.limit == 20
        assert resp.total_count == 5


# ── MetagenRunRequest ──────────────────────────────────────────────────────────


class TestMetagenRunRequest:
    def test_defaults(self) -> None:
        """MetagenRunRequest defaults: dataset_urns=None, dry_run=False.

        Spec: API.md §Metadata Generation — POST method/run optional fields.
        """
        req = MetagenRunRequest()
        assert req.dataset_urns is None
        assert req.dry_run is False

    def test_dataset_urns_list(self) -> None:
        """MetagenRunRequest accepts a list of dataset_urns.

        Spec: API.md §Metadata Generation — optional dataset_urns scopes the run.
        """
        req = MetagenRunRequest(dataset_urns=[_VALID_URN])
        assert req.dataset_urns == [_VALID_URN]

    def test_dry_run_true(self) -> None:
        """MetagenRunRequest accepts dry_run=True.

        Spec: API.md §Metadata Generation — dry_run flag.
        """
        req = MetagenRunRequest(dry_run=True)
        assert req.dry_run is True


# ── MetagenRunResponse ─────────────────────────────────────────────────────────


class TestMetagenRunResponse:
    def test_status_accepts_success(self) -> None:
        """MetagenRunResponse accepts status='success'.

        Spec: feature/BACKEND.md §Metadata Generation Service — public run
        outcomes are 'success' or 'failure'; tier short-circuit ('skipped')
        is owned by /internal/activities/metagen/run, not the public route.
        """
        from datetime import UTC, datetime
        resp = MetagenRunResponse(
            run_id="run-1",
            status="success",
            dry_run=False,
            unresolved_urns=[],
            counts={"items_considered": 3},
            producer_iterations=None,
            debate_outcome=None,
        )
        assert resp.status == "success"

    def test_status_rejects_skipped(self) -> None:
        """MetagenRunResponse rejects status='skipped' — the tier-mismatch
        short-circuit is the activity's responsibility and is not surfaced
        on the public run schema.

        Spec: feature/BACKEND.md §DAG Catalogue tier-DAG selection — only the
        internal activity returns the {status: 'skipped', reason: ...} shape.
        """
        with pytest.raises(ValidationError):
            MetagenRunResponse(
                run_id="run-2",
                status="skipped",
                dry_run=False,
                unresolved_urns=[],
                counts={},
                producer_iterations=None,
                debate_outcome=None,
            )

    def test_status_accepts_failure(self) -> None:
        """MetagenRunResponse accepts status='failure'.

        Spec: API.md §Metadata Generation — 'failure' on error.
        """
        resp = MetagenRunResponse(
            run_id="run-3",
            status="failure",
            dry_run=False,
            unresolved_urns=[],
            counts={},
            producer_iterations=None,
            debate_outcome=None,
        )
        assert resp.status == "failure"

    def test_status_rejects_invalid(self) -> None:
        """MetagenRunResponse rejects unknown status value.

        Spec: API.md §Metadata Generation — status is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenRunResponse(
                run_id="run-4",
                status="running",  # type: ignore[arg-type]
                dry_run=False,
                unresolved_urns=[],
                counts={},
                producer_iterations=None,
                debate_outcome=None,
            )


# ── MetagenReviewRequest ───────────────────────────────────────────────────────


class TestMetagenReviewRequest:
    def test_verdict_approve_valid(self) -> None:
        """verdict='approve' is valid.

        Spec: API.md §Metadata Generation — verdict ∈ {approve, reject}.
        """
        req = MetagenReviewRequest(verdict="approve")
        assert req.verdict == "approve"

    def test_verdict_reject_valid(self) -> None:
        """verdict='reject' is valid.

        Spec: API.md §Metadata Generation — verdict ∈ {approve, reject}.
        """
        req = MetagenReviewRequest(verdict="reject")
        assert req.verdict == "reject"

    def test_verdict_invalid_raises(self) -> None:
        """verdict='maybe' raises ValidationError.

        Spec: API.md §Metadata Generation — verdict is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenReviewRequest(verdict="maybe")  # type: ignore[arg-type]

    def test_reason_at_max_length_valid(self) -> None:
        """reason at exactly max_length=2000 characters is valid.

        Spec: API.md §Metadata Generation — reason max_length 2000.
        """
        req = MetagenReviewRequest(verdict="approve", reason="x" * _REASON_MAX_LEN)
        assert len(req.reason) == _REASON_MAX_LEN

    def test_reason_exceeds_max_length_raises(self) -> None:
        """reason with 2001 characters raises ValidationError.

        Spec: API.md §Metadata Generation — reason max_length 2000.
        """
        with pytest.raises(ValidationError):
            MetagenReviewRequest(verdict="approve", reason="x" * (_REASON_MAX_LEN + 1))

    def test_reason_defaults_to_empty_string(self) -> None:
        """reason defaults to empty string when omitted.

        Spec: API.md §Metadata Generation — reason is optional.
        """
        req = MetagenReviewRequest(verdict="approve")
        assert req.reason == ""


# ── Schema field existence checks ─────────────────────────────────────────────


def test_global_conf_response_has_required_fields() -> None:
    """MetagenGlobalConfResponse has all spec-mandated fields.

    Spec: API.md §Metadata Generation — GET /metagen/attr/conf response shape.
    """
    fields = MetagenGlobalConfResponse.model_fields
    for field in ("is_enabled", "schedule_tier", "dataset_filter", "result_limit", "overwrite_pending", "updated_at"):
        assert field in fields, (
            f"MetagenGlobalConfResponse must have field '{field}'. "
            "spec: API.md §Metadata Generation"
        )


def test_metagen_item_summary_has_composite_id() -> None:
    """MetagenItemSummary carries composite_id field.

    Spec: API.md §Metadata Generation — item detail path uses composite_id = {dataset_urn}::{item_id}.
    """
    assert "composite_id" in MetagenItemSummary.model_fields, (
        "MetagenItemSummary must expose composite_id. "
        "spec: API.md §Metadata Generation — composite_id for item detail lookup"
    )


def test_run_request_does_not_require_dataset_urn_singular() -> None:
    """MetagenRunRequest does not require a singular 'dataset_urn' field.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — global singleton run
    accepts optional dataset_urns (plural list), not a single required dataset_urn.
    """
    assert "dataset_urn" not in MetagenRunRequest.model_fields, (
        "MetagenRunRequest must not have singular 'dataset_urn' (use dataset_urns list). "
        "spec: BACKEND.md §Metadata Generation Service — global run"
    )
    assert "dataset_urns" in MetagenRunRequest.model_fields


# ── dataset_filter origin and unified four-dimension shape (new behavior) ──────


class TestMetagenDatasetFilterOrigin:
    def test_put_with_origin_only_accepted(self) -> None:
        """PUT with dataset_filter={"origin": "DEV"} is accepted.

        Spec: spec/API.md §UC4 — dataset_filter unified four-dimension shape;
              origin is an optional AND-filter forwarded to DataHub.
        """
        req = MetagenGlobalConfPutRequest(is_enabled=False, dataset_filter={"origin": "DEV"})
        assert req.dataset_filter == {"origin": "DEV"}

    def test_put_with_origin_and_tags_accepted(self) -> None:
        """PUT with dataset_filter={"origin": "DEV", "tags": [...]} is accepted.

        Spec: spec/API.md §UC4 — origin may be combined with other dimensions.
        """
        req = MetagenGlobalConfPutRequest(
            is_enabled=False,
            dataset_filter={
                "origin": "DEV",
                "tags": ["urn:li:tag:area:fulfillment"],
            },
        )
        assert req.dataset_filter["origin"] == "DEV"
        assert req.dataset_filter["tags"] == ["urn:li:tag:area:fulfillment"]

    def test_patch_with_origin_only_accepted(self) -> None:
        """PATCH with dataset_filter={"origin": "DEV"} is accepted.

        Spec: spec/API.md §UC4 — dataset_filter unified four-dimension shape applies to PATCH.
        """
        req = MetagenGlobalConfPatchRequest(dataset_filter={"origin": "DEV"})
        assert req.dataset_filter == {"origin": "DEV"}

    def test_patch_with_origin_and_tags_accepted(self) -> None:
        """PATCH with dataset_filter={"origin": "DEV", "tags": [...]} is accepted.

        Spec: spec/API.md §UC4 — origin may be combined with other dimensions in PATCH.
        """
        req = MetagenGlobalConfPatchRequest(
            dataset_filter={
                "origin": "DEV",
                "tags": ["urn:li:tag:area:fulfillment"],
            }
        )
        assert req.dataset_filter["origin"] == "DEV"

    def test_put_with_empty_origin_raises(self) -> None:
        """PUT with dataset_filter={"origin": ""} raises ValidationError.

        Spec: spec/API.md §UC5 Definition body §dataset_filter — empty-or-whitespace origin
              rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPutRequest(is_enabled=False, dataset_filter={"origin": ""})

    def test_patch_with_empty_origin_raises(self) -> None:
        """PATCH with dataset_filter={"origin": ""} raises ValidationError.

        Spec: spec/API.md §UC5 Definition body §dataset_filter — empty-or-whitespace origin
              rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            MetagenGlobalConfPatchRequest(dataset_filter={"origin": ""})

    def test_put_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """PUT with malformed dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        This is validated at the schema layer for UC4 (unified with UC5 behavior).
        """
        with pytest.raises(InvalidDatasetUrnError):
            MetagenGlobalConfPutRequest(
                is_enabled=False,
                dataset_filter={"dataset_urns": ["not-a-valid-urn"]},
            )

    def test_patch_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """PATCH with malformed dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        Schema-layer enforcement applies to PATCH as well.
        """
        with pytest.raises(InvalidDatasetUrnError):
            MetagenGlobalConfPatchRequest(
                dataset_filter={"dataset_urns": ["not-a-valid-urn"]},
            )
