"""Unit tests for src/api/schemas/ontogen.py — UC3 conf request validation.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - dataset_filter list dimensions capped at 1,000 entries
  - dataset_filter.origin accepted for all four-dimension shape (unified with UC5)
  - dataset_filter.origin='' rejected at schema layer
  - dataset_filter.dataset_urns malformed URN raises INVALID_DATASET_URN at schema layer (UC3)

Spec traceability:
  spec/API.md §Ontology Generation — dataset_filter unified four-dimension shape
  spec/API.md §Ontology Generation (Payload caps) — dataset_filter list dimensions capped at 1,000
  spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.ontogen import (
    OntogenConfPatchRequest,
    OntogenConfPutRequest,
)
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


# ── dataset_filter origin and unified four-dimension shape (new behavior) ──────


class TestOntogenDatasetFilterOrigin:
    def test_put_with_origin_only_accepted(self) -> None:
        """PUT with dataset_filter={"origin": "DEV"} is accepted.

        Spec: spec/API.md §Ontology Generation — dataset_filter unified four-dimension shape;
              origin is an optional AND-filter forwarded to DataHub.
        """
        req = OntogenConfPutRequest(is_enabled=False, dataset_filter={"origin": "DEV"})
        assert req.dataset_filter == {"origin": "DEV"}

    def test_put_with_origin_and_tags_accepted(self) -> None:
        """PUT with dataset_filter={"origin": "DEV", "tags": [...]} is accepted.

        Spec: spec/API.md §Ontology Generation — origin may be combined with other dimensions.
        """
        req = OntogenConfPutRequest(
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

        Spec: spec/API.md §Ontology Generation — dataset_filter four-dimension
        shape applies to PATCH.
        """
        req = OntogenConfPatchRequest(dataset_filter={"origin": "DEV"})
        assert req.dataset_filter == {"origin": "DEV"}

    def test_patch_with_origin_and_tags_accepted(self) -> None:
        """PATCH with dataset_filter={"origin": "DEV", "tags": [...]} is accepted.

        Spec: spec/API.md §Ontology Generation — origin may be combined with other
        dimensions in PATCH.
        """
        req = OntogenConfPatchRequest(
            dataset_filter={
                "origin": "DEV",
                "tags": ["urn:li:tag:area:fulfillment"],
            }
        )
        assert req.dataset_filter["origin"] == "DEV"

    def test_put_with_empty_origin_raises(self) -> None:
        """PUT with dataset_filter={"origin": ""} raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body, dataset_filter) —
              empty-or-whitespace origin rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            OntogenConfPutRequest(is_enabled=False, dataset_filter={"origin": ""})

    def test_patch_with_empty_origin_raises(self) -> None:
        """PATCH with dataset_filter={"origin": ""} raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body, dataset_filter) —
              empty-or-whitespace origin rejected at PUT/PATCH with 422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            OntogenConfPatchRequest(dataset_filter={"origin": ""})

    def test_put_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """PUT with malformed dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        This is validated at the schema layer for UC3 (unified with UC5 behavior).
        """
        with pytest.raises(InvalidDatasetUrnError):
            OntogenConfPutRequest(
                is_enabled=False,
                dataset_filter={"dataset_urns": ["not-a-valid-urn"]},
            )

    def test_patch_with_malformed_dataset_urn_raises_invalid_dataset_urn_error(self) -> None:
        """PATCH with malformed dataset_urns raises InvalidDatasetUrnError.

        Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
        Schema-layer enforcement applies to PATCH as well.
        """
        with pytest.raises(InvalidDatasetUrnError):
            OntogenConfPatchRequest(
                dataset_filter={"dataset_urns": ["not-a-valid-urn"]},
            )
