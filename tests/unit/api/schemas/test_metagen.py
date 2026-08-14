"""Unit tests for src/api/schemas/metagen.py — UC4 conf-collection schema surface.

Spec: spec/API.md §Metadata Generation (/spoke/metagen)
      spec/feature/BACKEND_SCHEMA.md §metagen_config / §metagen_candidates

Constraints tested:
  - conf create requires unique `name` (min_length 1); result_limit ∈ [1, 20]
  - dataset_filter is a SQL WHERE-clause string on the shared grammar (POST/PUT/PATCH);
    ≤ 8,000 chars and ≤ 1,000 string literals
  - a malformed filter raises INVALID_DATASET_FILTER; a malformed `dataset_urn` literal
    raises INVALID_DATASET_URN — both at the schema layer (UC4)
  - schedule_tier ∈ {hourly, daily, weekly, null}
  - reason max_length=2000 (MetagenReviewRequest); verdict ∈ {approve, reject}
  - MetagenBoundaryPutRequest.allowed ∈ {dataset.description, column.description}
  - MetagenConfListResponse / MetagenItemListResponse / MetagenUncoveredResponse envelopes
    use 'total_count' (not 'total')
  - MetagenCandidate carries conf_id / conf_name
  - MetagenUncoveredRow.reason ∈ {no_conf_match, boundary_blocked}
  - MetagenRunResponse carries conf_id + status: Literal["success","failure"]
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.metagen import (
    MetagenBoundaryPatchRequest,
    MetagenBoundaryPutRequest,
    MetagenCandidate,
    MetagenConfCreateRequest,
    MetagenConfListResponse,
    MetagenConfPatchRequest,
    MetagenConfPutRequest,
    MetagenConfResponse,
    MetagenItemListResponse,
    MetagenItemSummary,
    MetagenReviewRequest,
    MetagenRunRequest,
    MetagenRunResponse,
    MetagenUncoveredResponse,
    MetagenUncoveredRow,
)
from src.shared.dataset_filter import DatasetFilterSyntaxError
from src.shared.exceptions import InvalidDatasetUrnError

#: spec/API.md §`dataset_filter` grammar — Caps.
_FILTER_LITERAL_CAP = 1000
_FILTER_CHAR_CAP = 8000


def _filter_with_literals(count: int) -> str:
    """A syntactically valid filter carrying exactly *count* string literals."""
    return "origin IN (" + ", ".join(f"'v{i}'" for i in range(count)) + ")"
_REASON_MAX_LEN = 2000

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


# ── MetagenConfCreateRequest ──────────────────────────────────────────────────


class TestMetagenConfCreateRequest:
    def test_valid_minimal_request_requires_name(self) -> None:
        """A create request with only `name` is valid and applies defaults.

        Spec: API.md §Metadata Generation — POST /metagen/conf body
        {name, is_enabled, schedule_tier, dataset_filter, result_limit, overwrite_pending}.
        """
        req = MetagenConfCreateRequest(name="catalog-docs")
        assert req.name == "catalog-docs"
        assert req.schedule_tier is None
        # result_limit/overwrite_pending defaults are spec'd in BACKEND_SCHEMA.md
        # §metagen_config (result_limit default 3; overwrite_pending default true).
        assert req.result_limit == 3
        assert req.overwrite_pending is True
        # is_enabled default is an impl-chosen safe default (create a conf disabled,
        # opt into scheduling explicitly) — not pinned by spec.
        assert req.is_enabled is False

    def test_name_missing_raises(self) -> None:
        """A create request without `name` raises ValidationError.

        Spec: API.md §Metadata Generation — `name` is required and unique.
        """
        with pytest.raises(ValidationError):
            MetagenConfCreateRequest()  # type: ignore[call-arg]

    def test_name_empty_raises(self) -> None:
        """A create request with an empty `name` raises ValidationError.

        Spec: feature/BACKEND.md §Metadata Generation Service — `name` is the unique
        conf name (min_length 1).
        """
        with pytest.raises(ValidationError):
            MetagenConfCreateRequest(name="")

    def test_result_limit_minimum_valid(self) -> None:
        """result_limit=1 is the minimum valid value.

        Spec: feature/BACKEND_SCHEMA.md — metagen_config.result_limit ∈ [1, 20].
        """
        assert MetagenConfCreateRequest(name="c", result_limit=1).result_limit == 1

    def test_result_limit_maximum_valid(self) -> None:
        """result_limit=20 is the maximum valid value.

        Spec: feature/BACKEND_SCHEMA.md — metagen_config.result_limit ∈ [1, 20].
        """
        assert MetagenConfCreateRequest(name="c", result_limit=20).result_limit == 20

    def test_result_limit_below_minimum_raises(self) -> None:
        """result_limit=0 raises ValidationError (below minimum of 1).

        Spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenConfCreateRequest(name="c", result_limit=0)

    def test_result_limit_above_maximum_raises(self) -> None:
        """result_limit=21 raises ValidationError (above maximum of 20).

        Spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenConfCreateRequest(name="c", result_limit=21)

    def test_dataset_filter_at_the_literal_cap_is_valid(self) -> None:
        """A filter carrying exactly 1,000 string literals is accepted.

        Spec: API.md §`dataset_filter` grammar — Caps: "≤ 1,000 string literals"; the
        per-feature Payload-caps lists "restate these; the values do not vary by feature".
        """
        at_cap = _filter_with_literals(_FILTER_LITERAL_CAP)
        req = MetagenConfCreateRequest(name="c", dataset_filter=at_cap)
        assert req.dataset_filter == at_cap

    def test_dataset_filter_over_the_literal_cap_raises(self) -> None:
        """Spec: API.md §`dataset_filter` grammar — Caps; §Error Catalogue —
        INVALID_DATASET_FILTER on `POST /spoke/metagen/conf`."""
        with pytest.raises(DatasetFilterSyntaxError):
            MetagenConfCreateRequest(
                name="c", dataset_filter=_filter_with_literals(_FILTER_LITERAL_CAP + 1)
            )

    def test_dataset_filter_over_the_character_cap_raises(self) -> None:
        """Spec: API.md §`dataset_filter` grammar — Caps: "filter text ≤ 8,000
        characters"."""
        prefix = "origin = '"
        over_cap = prefix + "x" * (_FILTER_CHAR_CAP - len(prefix)) + "'"
        with pytest.raises(DatasetFilterSyntaxError):
            MetagenConfCreateRequest(name="c", dataset_filter=over_cap)

    def test_dataset_filter_malformed_raises(self) -> None:
        """Spec: API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "names an
        unknown column"."""
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            MetagenConfCreateRequest(name="c", dataset_filter="owner = 'alice'")
        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"

    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            assert MetagenConfCreateRequest(name="c", schedule_tier=tier).schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: API.md §Metadata Generation — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenConfCreateRequest(name="c", schedule_tier="minutely")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "dataset_filter",
        [
            "",
            "origin = 'DEV'",
            "origin IN ('DEV', 'PROD')",
            "'urn:li:tag:area:catalog' IN tag_urns",
            # `bool_col '=' bool` — a bare, unquoted TRUE/FALSE against `is_primary`
            "is_primary = true",
            "origin = 'DEV' AND 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns",
        ],
    )
    def test_grammar_forms_accepted(self, dataset_filter: str) -> None:
        """UC4's conf filter is the same grammar as UC3's and UC5's.

        One form per production, `bool_col '=' bool` included: a route that accepted
        only the scalar and array predicates would scope UC4 by tag and origin but not
        to one row per logical asset.

        Spec: API.md §`dataset_filter` grammar — the productions
        `predicate := scalar_col '=' string | scalar_col IN '(' … ')' |
        string IN array_col | bool_col '=' bool`, with `bool_col := is_primary` and
        `bool := TRUE | FALSE` ("bare word, never quoted"); "UC3's
        `ontogen/attr/conf.dataset_filter` and UC4's per-conf
        `metagen/conf.dataset_filter` use this same grammar and validation."
        """
        req = MetagenConfCreateRequest(name="c", dataset_filter=dataset_filter)
        assert req.dataset_filter == dataset_filter

    def test_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN, validated on
        `POST /spoke/metagen/conf`."""
        with pytest.raises(InvalidDatasetUrnError):
            MetagenConfCreateRequest(name="c", dataset_filter="dataset_urn = 'not-a-urn'")


# ── MetagenConfPutRequest ─────────────────────────────────────────────────────


class TestMetagenConfPutRequest:
    def test_requires_name_and_is_enabled(self) -> None:
        """PUT request requires `name` and `is_enabled` (full replacement).

        Spec: API.md §Metadata Generation — PUT /metagen/conf/{conf_id} replaces a conf.
        """
        req = MetagenConfPutRequest(name="catalog-docs", is_enabled=True)
        assert req.name == "catalog-docs"
        assert req.is_enabled is True

    def test_result_limit_below_minimum_raises(self) -> None:
        """result_limit=0 raises ValidationError.

        Spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenConfPutRequest(name="c", is_enabled=True, result_limit=0)

    def test_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN, validated on
        `PUT /spoke/metagen/conf/{conf_id}`."""
        with pytest.raises(InvalidDatasetUrnError):
            MetagenConfPutRequest(
                name="c", is_enabled=False, dataset_filter="dataset_urn = 'not-a-urn'"
            )

    def test_malformed_filter_raises(self) -> None:
        """Spec: API.md §Error Catalogue — INVALID_DATASET_FILTER on PUT."""
        with pytest.raises(DatasetFilterSyntaxError):
            MetagenConfPutRequest(name="c", is_enabled=False, dataset_filter="origin = ")

    def test_a_well_formed_filter_is_accepted(self) -> None:
        """Backstop for the two rejections above."""
        req = MetagenConfPutRequest(
            name="c", is_enabled=False, dataset_filter="origin = 'DEV'"
        )
        assert req.dataset_filter == "origin = 'DEV'"


# ── MetagenConfPatchRequest ───────────────────────────────────────────────────


class TestMetagenConfPatchRequest:
    def test_empty_patch_is_valid(self) -> None:
        """An empty PATCH body is valid (no fields to update).

        Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial update.
        """
        req = MetagenConfPatchRequest()
        assert req.name is None
        assert req.is_enabled is None
        assert req.result_limit is None

    def test_result_limit_below_minimum_raises(self) -> None:
        """result_limit=0 in PATCH raises ValidationError.

        Spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
        """
        with pytest.raises(ValidationError):
            MetagenConfPatchRequest(result_limit=0)

    def test_dataset_filter_over_the_literal_cap_raises(self) -> None:
        """Spec: API.md §`dataset_filter` grammar — Caps, enforced on
        `PATCH /spoke/metagen/conf/{conf_id}` as on every filter write path."""
        with pytest.raises(DatasetFilterSyntaxError):
            MetagenConfPatchRequest(
                dataset_filter=_filter_with_literals(_FILTER_LITERAL_CAP + 1)
            )

    def test_malformed_filter_raises(self) -> None:
        """Spec: API.md §Error Catalogue — INVALID_DATASET_FILTER on PATCH."""
        with pytest.raises(DatasetFilterSyntaxError):
            MetagenConfPatchRequest(dataset_filter="owner = 'alice'")

    def test_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN on PATCH."""
        with pytest.raises(InvalidDatasetUrnError):
            MetagenConfPatchRequest(dataset_filter="dataset_urn = 'not-a-urn'")

    def test_a_well_formed_filter_patch_is_accepted(self) -> None:
        """Backstop for the three rejections above."""
        req = MetagenConfPatchRequest(dataset_filter="'urn:li:tag:pii' IN tag_urns")
        assert req.dataset_filter == "'urn:li:tag:pii' IN tag_urns"


# ── MetagenConfResponse / MetagenConfListResponse ─────────────────────────────


class TestMetagenConfResponse:
    def test_response_has_id_and_name_and_timestamps(self) -> None:
        """MetagenConfResponse carries the collection identity fields id + name + created_at.

        Spec: feature/BACKEND_SCHEMA.md §metagen_config — id (UUID PK), name (UNIQUE),
        created_at; the conf is a collection row, not a singleton.
        """
        fields = MetagenConfResponse.model_fields
        for field in (
            "id",
            "name",
            "is_enabled",
            "schedule_tier",
            "dataset_filter",
            "result_limit",
            "overwrite_pending",
            "created_at",
            "updated_at",
        ):
            assert field in fields, (
                f"MetagenConfResponse must have field '{field}'. "
                "spec: feature/BACKEND_SCHEMA.md §metagen_config"
            )

    def test_list_envelope_uses_total_count_and_confs_key(self) -> None:
        """MetagenConfListResponse uses 'total_count' and a resource-named 'confs' key.

        Spec: API.md §Standard Response Envelope — pagination uses total_count;
        list payload keyed by resource name.
        """
        resp = MetagenConfListResponse(total_count=2, offset=0, limit=20)
        assert resp.total_count == 2
        assert resp.confs == []


# ── MetagenUncoveredResponse ──────────────────────────────────────────────────


class TestMetagenUncoveredResponse:
    def test_row_reason_accepts_no_conf_match(self) -> None:
        """Uncovered row reason accepts 'no_conf_match'.

        Spec: API.md §Metadata Generation — uncovered reason ∈ {no_conf_match, boundary_blocked}.
        """
        row = MetagenUncoveredRow(dataset_urn=_VALID_URN, reason="no_conf_match")
        assert row.reason == "no_conf_match"

    def test_row_reason_accepts_boundary_blocked(self) -> None:
        """Uncovered row reason accepts 'boundary_blocked'.

        Spec: API.md §Metadata Generation — uncovered reason ∈ {no_conf_match, boundary_blocked}.
        """
        row = MetagenUncoveredRow(dataset_urn=_VALID_URN, reason="boundary_blocked")
        assert row.reason == "boundary_blocked"

    def test_row_reason_rejects_invalid(self) -> None:
        """Uncovered row reason rejects an unknown value.

        Spec: API.md §Metadata Generation — reason is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenUncoveredRow(dataset_urn=_VALID_URN, reason="something_else")  # type: ignore[arg-type]

    def test_list_envelope_uses_total_count_and_datasets_key(self) -> None:
        """MetagenUncoveredResponse uses 'total_count' and a 'datasets' key.

        Spec: API.md §Standard Response Envelope — pagination uses total_count.
        """
        resp = MetagenUncoveredResponse(total_count=0, offset=0, limit=100)
        assert resp.total_count == 0
        assert resp.datasets == []


# ── MetagenBoundaryPutRequest ─────────────────────────────────────────────────


class TestMetagenBoundaryPutRequest:
    def test_valid_with_both_kinds(self) -> None:
        """PUT boundary with both dataset.description and column.description is valid.

        Spec: API.md §Metadata Generation — boundary allowed ∈
        {dataset.description, column.description}.
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
        """PUT boundary with invalid kind raises ValidationError.

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


# ── MetagenItemSummary / MetagenItemListResponse ──────────────────────────────


class TestMetagenItemSummary:
    def test_has_composite_id(self) -> None:
        """MetagenItemSummary carries composite_id field.

        Spec: API.md §Metadata Generation — item detail path uses
        composite_id = {dataset_urn}::{item_id}.
        """
        assert "composite_id" in MetagenItemSummary.model_fields


class TestMetagenItemListResponse:
    def test_envelope_uses_total_count_field(self) -> None:
        """MetagenItemListResponse carries 'total_count', not a bare 'total'.

        Spec: API.md §Standard Response Envelope — pagination uses total_count.
        """
        resp = MetagenItemListResponse(total_count=42)
        assert resp.total_count == 42
        assert "total" not in MetagenItemListResponse.model_fields

    def test_items_defaults_to_empty_list(self) -> None:
        """MetagenItemListResponse.items defaults to empty list.

        Spec: API.md §Standard Response Envelope — list responses start empty.
        """
        assert MetagenItemListResponse(total_count=0).items == []


# ── MetagenCandidate (conf_id / conf_name) ────────────────────────────────────


class TestMetagenCandidate:
    def test_candidate_carries_conf_id_and_conf_name(self) -> None:
        """MetagenCandidate exposes conf_id and conf_name.

        Spec: API.md §Metadata Generation — each candidate row exposes conf_id/conf_name.
        Spec: feature/BACKEND_SCHEMA.md §metagen_candidates — conf_id FK (nullable).
        """
        for field in ("conf_id", "conf_name"):
            assert field in MetagenCandidate.model_fields, (
                f"MetagenCandidate must expose '{field}'. "
                "spec: API.md §Metadata Generation — candidate carries conf_id/conf_name"
            )

    def test_conf_id_and_conf_name_nullable(self) -> None:
        """conf_id / conf_name may be null (orphaned approved candidate after conf delete).

        Spec: feature/BACKEND_SCHEMA.md §metagen_candidates — ON DELETE SET NULL for conf_id
        on already-emitted approved candidates.
        """
        cand = MetagenCandidate(
            candidate_id="cand-1",
            conf_id=None,
            conf_name=None,
            item_id="dataset.description",
            dataset_urn=_VALID_URN,
            run_id="run-1",
            value="v",
            confidence_score=0.9,
            status="approved",
            evidence={},
            created_at=datetime.now(tz=UTC),
            reviewed_at=None,
            reviewer_id=None,
        )
        assert cand.conf_id is None
        assert cand.conf_name is None


# ── MetagenRunRequest / MetagenRunResponse ────────────────────────────────────


class TestMetagenRunRequest:
    def test_defaults(self) -> None:
        """MetagenRunRequest defaults: dataset_urns=None.

        Spec: API.md §Metadata Generation — POST conf/{conf_id}/method/run optional body
        {"dataset_urns": [...]}; dry_run travels as a query parameter, not a body field.
        """
        assert MetagenRunRequest().dataset_urns is None

    def test_dataset_urns_list(self) -> None:
        """MetagenRunRequest accepts a list of dataset_urns.

        Spec: API.md §Metadata Generation — optional dataset_urns scopes the run.
        """
        assert MetagenRunRequest(dataset_urns=[_VALID_URN]).dataset_urns == [_VALID_URN]

    def test_no_singular_dataset_urn_field(self) -> None:
        """MetagenRunRequest does not expose a singular 'dataset_urn' field.

        Spec: API.md §Metadata Generation — run body uses dataset_urns (plural).
        """
        assert "dataset_urn" not in MetagenRunRequest.model_fields
        assert "dataset_urns" in MetagenRunRequest.model_fields


class TestMetagenRunResponse:
    def _kwargs(self, **over):
        base = dict(
            run_id="run-1",
            conf_id="conf-1",
            status="success",
            dry_run=False,
            unresolved_urns=[],
            counts={"items_considered": 3},
            producer_iterations=None,
            debate_outcome=None,
        )
        base.update(over)
        return base

    def test_carries_conf_id(self) -> None:
        """MetagenRunResponse carries the conf_id the run was scoped to.

        Spec: API.md §Metadata Generation — run is per-conf
        (POST /metagen/conf/{conf_id}/method/run).
        Spec: feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail carries conf_id.
        """
        resp = MetagenRunResponse(**self._kwargs())
        assert resp.conf_id == "conf-1"

    def test_status_accepts_success_and_failure(self) -> None:
        """MetagenRunResponse status accepts 'success' and 'failure'.

        The response status is the synchronous analogue of the two terminal run
        events: success ↔ METAGEN.RUN_COMPLETE, failure ↔ METAGEN.RUN_FAILED.
        Spec: feature/BACKEND.md §Event Catalogue — METAGEN emits
        RUN_COMPLETE / RUN_FAILED at run end.
        """
        assert MetagenRunResponse(**self._kwargs(status="success")).status == "success"
        assert MetagenRunResponse(**self._kwargs(status="failure")).status == "failure"

    def test_status_rejects_skipped(self) -> None:
        """MetagenRunResponse rejects status='skipped' (enum-closure).

        The status field is a closed Literal of the two terminal-event outcomes
        (success / failure); 'skipped' is not a member.
        Spec: feature/BACKEND.md §Event Catalogue — only RUN_COMPLETE / RUN_FAILED
        are emitted at run end.
        """
        with pytest.raises(ValidationError):
            MetagenRunResponse(**self._kwargs(status="skipped"))


# ── MetagenReviewRequest ──────────────────────────────────────────────────────


class TestMetagenReviewRequest:
    def test_verdict_approve_valid(self) -> None:
        """verdict='approve' is valid.

        Spec: API.md §Metadata Generation — verdict ∈ {approve, reject}.
        """
        assert MetagenReviewRequest(verdict="approve").verdict == "approve"

    def test_verdict_reject_valid(self) -> None:
        """verdict='reject' is valid.

        Spec: API.md §Metadata Generation — verdict ∈ {approve, reject}.
        """
        assert MetagenReviewRequest(verdict="reject").verdict == "reject"

    def test_verdict_invalid_raises(self) -> None:
        """verdict='maybe' raises ValidationError.

        Spec: API.md §Metadata Generation — verdict is a Literal type.
        """
        with pytest.raises(ValidationError):
            MetagenReviewRequest(verdict="maybe")  # type: ignore[arg-type]

    def test_reason_at_max_length_valid(self) -> None:
        """reason at exactly max_length=2000 is valid.

        Spec: API.md §Payload caps — review reason ≤ 2,000 chars.
        """
        req = MetagenReviewRequest(verdict="approve", reason="x" * _REASON_MAX_LEN)
        assert len(req.reason) == _REASON_MAX_LEN

    def test_reason_exceeds_max_length_raises(self) -> None:
        """reason with 2001 chars raises ValidationError.

        Spec: API.md §Payload caps — review reason ≤ 2,000 chars.
        """
        with pytest.raises(ValidationError):
            MetagenReviewRequest(verdict="approve", reason="x" * (_REASON_MAX_LEN + 1))

    def test_reason_defaults_to_empty_string(self) -> None:
        """reason defaults to empty string when omitted.

        Spec: API.md §Metadata Generation — reason is optional.
        """
        assert MetagenReviewRequest(verdict="approve").reason == ""
