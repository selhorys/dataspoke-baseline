"""Unit tests for src/api/schemas/metrics.py — schedule_tier validation.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - Pydantic rejects unknown values with ValidationError → 422 at the route boundary
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.metrics import (
    PatchMetricConfigRequest,
    UpsertMetricConfigRequest,
)


_VALID_BODY = {
    "title": "Ingestion freshness coverage",
    "description": "Pct of datasets with a recent successful ingestion run",
    "theme": "freshness",
    "measurement_query": {"aggregation": "pct_fresh"},
    "is_enabled": True,
}


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
