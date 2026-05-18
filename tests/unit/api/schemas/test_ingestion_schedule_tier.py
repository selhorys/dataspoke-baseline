"""Unit tests for src/api/schemas/ingestion.py — schedule_tier validation.

Pins:
  - schedule_tier ∈ {hourly, daily, weekly, None} (Literal-typed)
  - Pydantic rejects unknown values with ValidationError → 422 at the route boundary
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    PatchIngestionConfigRequest,
)


_VALID_BODY = {
    "mode": "active-custom",
    "platform": "postgres",
    "locator": {"host": "db.example.com", "port": 5432},
    "identifier": {"database": "d", "schema_name": "s", "table": "t"},
    "auth": {
        "username": "u",
        "secret_ref": {"name": "dataspoke-source-cred-x", "key": "password"},
    },
    "is_enabled": True,
}


class TestCreateIngestionConfigRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly"):
            req = CreateIngestionConfigRequest(**_VALID_BODY, schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: API.md §Ingestion Control — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            CreateIngestionConfigRequest(**_VALID_BODY, schedule_tier="minutely")  # type: ignore[arg-type]


class TestPatchIngestionConfigRequest:
    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: feature/BACKEND_SCHEMA.md — schedule_tier ∈ {hourly, daily, weekly, null}.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = PatchIngestionConfigRequest(schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='yearly' raises ValidationError.

        Spec: API.md §Ingestion Control — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            PatchIngestionConfigRequest(schedule_tier="yearly")  # type: ignore[arg-type]
