"""Unit tests for src/api/schemas/metrics.py — schedule_tier + dataset_filter caps.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - measurement_query.dataset_filter.{tags,glossary_terms,dataset_urns}
    ≤ 1,000 per dimension (Upsert and Patch)
  - Pydantic rejects unknown / over-cap values with ValidationError → 422
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.metrics import (
    PatchMetricConfigRequest,
    UpsertMetricConfigRequest,
)


_DATASET_FILTER_LIST_CAP = 1000

_VALID_BODY = {
    "title": "Ingestion freshness coverage",
    "description": "Pct of datasets with a recent successful ingestion run",
    "theme": "freshness",
    "measurement_query": {"aggregation": "pct_fresh"},
    "is_enabled": True,
}


def _too_many(dimension: str) -> list[str]:
    if dimension == "dataset_urns":
        return [f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
                for i in range(_DATASET_FILTER_LIST_CAP + 1)]
    prefix = "urn:li:tag:t" if dimension == "tags" else "urn:li:glossaryTerm:t"
    return [f"{prefix}{i}" for i in range(_DATASET_FILTER_LIST_CAP + 1)]


class TestUpsertMetricConfigRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = UpsertMetricConfigRequest(**_VALID_BODY, schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: API.md §Governance — metric schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            UpsertMetricConfigRequest(**_VALID_BODY, schedule_tier="minutely")  # type: ignore[arg-type]

    @pytest.mark.parametrize("dimension", ["dataset_urns", "tags", "glossary_terms"])
    def test_dataset_filter_dimension_exceeds_cap_raises(self, dimension: str) -> None:
        """measurement_query.dataset_filter.{dimension} > 1000 raises ValidationError.

        Spec: API.md §UC5 Governance Payload caps —
        measurement_query.dataset_filter.{tags,glossary_terms,dataset_urns}
        ≤ 1,000 per dimension.
        """
        body = {**_VALID_BODY, "measurement_query": {
            "aggregation": "pct_fresh",
            "dataset_filter": {dimension: _too_many(dimension)},
        }}
        with pytest.raises(ValidationError):
            UpsertMetricConfigRequest(**body)


class TestPatchMetricConfigRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = PatchMetricConfigRequest(schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='yearly' raises ValidationError.

        Spec: API.md §Governance — metric schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            PatchMetricConfigRequest(schedule_tier="yearly")  # type: ignore[arg-type]

    @pytest.mark.parametrize("dimension", ["dataset_urns", "tags", "glossary_terms"])
    def test_dataset_filter_dimension_exceeds_cap_raises(self, dimension: str) -> None:
        """PATCH with measurement_query.dataset_filter.{dimension} > 1000 raises.

        Spec: API.md §UC5 Governance Payload caps —
        measurement_query.dataset_filter.{tags,glossary_terms,dataset_urns}
        ≤ 1,000 per dimension.
        """
        mq = {"aggregation": "pct_fresh", "dataset_filter": {dimension: _too_many(dimension)}}
        with pytest.raises(ValidationError):
            PatchMetricConfigRequest(measurement_query=mq)
