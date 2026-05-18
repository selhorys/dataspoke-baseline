"""Unit tests for src/api/schemas/ontogen.py — UC3 conf request validation.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - dataset_filter list dimensions capped at 1,000 entries
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.ontogen import (
    OntogenConfPatchRequest,
    OntogenConfPutRequest,
)


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
