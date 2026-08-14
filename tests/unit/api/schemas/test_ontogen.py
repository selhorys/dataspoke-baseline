"""Unit tests for src/api/schemas/ontogen.py — UC3 conf request validation.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - dataset_filter is a SQL WHERE-clause string validated against the shared grammar
  - a malformed filter raises INVALID_DATASET_FILTER; a malformed `dataset_urn`
    literal raises INVALID_DATASET_URN — both at the schema layer, on PUT and PATCH

Spec traceability:
  spec/API.md §`dataset_filter` grammar — "UC3's `ontogen/attr/conf.dataset_filter` and
    UC4's per-conf `metagen/conf.dataset_filter` use this same grammar and validation"
  spec/API.md §Error Catalogue — INVALID_DATASET_FILTER / INVALID_DATASET_URN, both 422,
    "Validated wherever a `dataset_filter` is written: `PUT`/`PATCH /spoke/ontogen/attr/conf`"
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.ontogen import (
    OntogenConfPatchRequest,
    OntogenConfPutRequest,
)
from src.shared.dataset_filter import DatasetFilterSyntaxError
from src.shared.exceptions import InvalidDatasetUrnError


class TestOntogenConfPutRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: spec/feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = OntogenConfPutRequest(is_enabled=False, schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: API.md §Ontology Generation — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            OntogenConfPutRequest(is_enabled=False, schedule_tier="minutely")  # type: ignore[arg-type]


class TestOntogenConfPatchRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: spec/feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = OntogenConfPatchRequest(schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='yearly' raises ValidationError.

        Spec: API.md §Ontology Generation — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            OntogenConfPatchRequest(schedule_tier="yearly")  # type: ignore[arg-type]


# ── dataset_filter: the shared SQL grammar ────────────────────────────────────


class TestOntogenDatasetFilter:
    """UC3's conf filter is the same grammar and the same validation as UC4 and UC5.

    The accepted list enumerates one form per production, `bool_col '=' bool` included:
    a route that accepted only the scalar and array predicates would scope UC3 by tag
    and origin but not to one row per logical asset.

    Spec: spec/API.md §`dataset_filter` grammar — the productions
    `predicate := scalar_col '=' string | scalar_col IN '(' … ')' | string IN array_col
    | bool_col '=' bool`, with `bool_col := is_primary` and `bool := TRUE | FALSE`
    ("bare word, never quoted"); "UC3's `ontogen/attr/conf.dataset_filter` and UC4's
    per-conf `metagen/conf.dataset_filter` use this same grammar and validation."
    """

    _ACCEPTED = [
        "",
        "origin = 'DEV'",
        "origin IN ('DEV', 'PROD')",
        "platform_urn = 'urn:li:dataPlatform:postgres'",
        "'urn:li:tag:area:fulfillment' IN tag_urns",
        # `bool_col '=' bool` — a bare, unquoted TRUE/FALSE against `is_primary`
        "is_primary = true",
        "origin = 'DEV' AND 'urn:li:tag:area:fulfillment' IN tag_urns",
    ]

    @pytest.mark.parametrize("dataset_filter", _ACCEPTED)
    def test_put_accepts_the_grammar(self, dataset_filter: str) -> None:
        req = OntogenConfPutRequest(is_enabled=False, dataset_filter=dataset_filter)
        assert req.dataset_filter == dataset_filter

    @pytest.mark.parametrize("dataset_filter", _ACCEPTED)
    def test_patch_accepts_the_grammar(self, dataset_filter: str) -> None:
        req = OntogenConfPatchRequest(dataset_filter=dataset_filter)
        assert req.dataset_filter == dataset_filter

    def test_put_with_a_malformed_filter_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, validated on
        `PUT /spoke/ontogen/attr/conf`."""
        with pytest.raises(DatasetFilterSyntaxError) as excinfo:
            OntogenConfPutRequest(is_enabled=False, dataset_filter="owner = 'alice'")
        assert excinfo.value.error_code == "INVALID_DATASET_FILTER"

    def test_patch_with_a_malformed_filter_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — the same code on
        `PATCH /spoke/ontogen/attr/conf`."""
        with pytest.raises(DatasetFilterSyntaxError):
            OntogenConfPatchRequest(dataset_filter="origin = ")

    def test_put_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for a malformed
        `dataset_urn` literal inside a filter."""
        with pytest.raises(InvalidDatasetUrnError):
            OntogenConfPutRequest(
                is_enabled=False,
                dataset_filter="dataset_urn = 'not-a-valid-urn'",
            )

    def test_patch_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """Spec: spec/API.md §Error Catalogue — the same on PATCH."""
        with pytest.raises(InvalidDatasetUrnError):
            OntogenConfPatchRequest(dataset_filter="dataset_urn = 'not-a-valid-urn'")

    def test_over_the_literal_cap_raises(self) -> None:
        """Spec: spec/API.md §`dataset_filter` grammar — Caps: "≤ 1,000 string
        literals"; the caps "do not vary by feature"."""
        over_cap = "origin IN (" + ", ".join(f"'v{i}'" for i in range(1001)) + ")"
        with pytest.raises(DatasetFilterSyntaxError):
            OntogenConfPutRequest(is_enabled=False, dataset_filter=over_cap)
