"""Unit tests for src/api/schemas/ingestion.py — per-source request/response shapes.

Covers:
- CreateIngestionSourceRequest: required fields, mode enum, schedule field
- ReplaceIngestionSourceRequest: same shape as create
- PatchIngestionSourceRequest: all fields optional
- IngestionSourceResponse: no schedule_tier, no schedule_cron; has recipe, schedule
- SecretRefListResponse: shape with no values
- IngestionReverseLookupResponse: source_id/mode/name/latest_run nullable

Spec: spec/API.md §Ingestion (/spoke/ingestion)
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source — 'schedule_tier internal, never exposed'
Spec: spec/feature/BACKEND.md §Ingestion Service §API body shape
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from src.api.schemas.ingestion import (
    CreateIngestionSourceRequest,
    IngestionReverseLookupResponse,
    IngestionSourceResponse,
    PatchIngestionSourceRequest,
    ReplaceIngestionSourceRequest,
    RunIngestionSourceRequest,
    SecretRefInfo,
    SecretRefListResponse,
)
from src.shared.models.ingestion import Mode


# ── CreateIngestionSourceRequest ──────────────────────────────────────────────


class TestCreateIngestionSourceRequest:
    """Spec: API.md §Ingestion — POST /spoke/ingestion/sources body."""

    def test_valid_active_custom_managed_accepted(self) -> None:
        """Full valid body for ACTIVE_CUSTOM_MANAGED is accepted.

        Spec: BACKEND.md §Ingestion Service §API body shape — '{mode, name, schedule, recipe}'.
        """
        req = CreateIngestionSourceRequest(
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="imazon catalog pg",
            schedule="0 0 * * *",
            recipe={
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": "pg:5432",
                        "password": "${team_pg__password}",
                    },
                }
            },
        )
        assert req.mode == Mode.ACTIVE_CUSTOM_MANAGED
        assert req.name == "imazon catalog pg"
        assert req.schedule == "0 0 * * *"

    def test_valid_passive_accepted(self) -> None:
        """PASSIVE mode with schedule=None is accepted.

        Spec: BACKEND_SCHEMA.md §ingestion_source — 'Null for PASSIVE'.
        """
        req = CreateIngestionSourceRequest(
            mode=Mode.PASSIVE,
            name="external passive source",
            schedule=None,
            recipe={
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                }
            },
        )
        assert req.mode == Mode.PASSIVE
        assert req.schedule is None

    def test_empty_name_rejected(self) -> None:
        """Empty name (min_length=1) raises ValidationError.

        Spec: API.md — name min_length=1.
        """
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest(
                mode=Mode.ACTIVE_CUSTOM_MANAGED,
                name="",
                schedule=None,
                recipe={"source": {"type": "postgres", "config": {}}},
            )

    def test_mode_is_required(self) -> None:
        """Missing mode raises ValidationError."""
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest(
                name="test",
                schedule=None,
                recipe={"source": {"type": "postgres", "config": {}}},
            )

    def test_recipe_is_required(self) -> None:
        """Missing recipe raises ValidationError."""
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest(
                mode=Mode.ACTIVE_CUSTOM_MANAGED,
                name="test",
                schedule=None,
            )

    def test_schedule_defaults_to_none(self) -> None:
        """schedule has default=None — omitting it is valid.

        Spec: BACKEND.md §Ingestion Service §Schedule — 'schedule: null means manual-only'.
        """
        req = CreateIngestionSourceRequest(
            mode=Mode.PASSIVE,
            name="test",
            recipe={"source": {"type": "kafka", "config": {}}},
        )
        assert req.schedule is None

    def test_no_schedule_tier_field(self) -> None:
        """CreateIngestionSourceRequest must NOT have a schedule_tier field.

        Spec: BACKEND_SCHEMA.md §ingestion_source — 'schedule_tier is internal and
        never appears in the API'.
        """
        req = CreateIngestionSourceRequest(
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="source",
            schedule="0 0 * * *",
            recipe={"source": {"type": "postgres", "config": {}}},
        )
        assert not hasattr(req, "schedule_tier"), (
            "schedule_tier must not be a field on the request schema. "
            "Spec: BACKEND_SCHEMA.md — schedule_tier is internal, never in API."
        )

    def test_no_is_enabled_field(self) -> None:
        """CreateIngestionSourceRequest must NOT have an is_enabled field.

        Spec: BACKEND.md §API body shape — only {mode, name, schedule, recipe}.
        """
        req = CreateIngestionSourceRequest(
            mode=Mode.PASSIVE,
            name="source",
            recipe={"source": {"type": "kafka", "config": {}}},
        )
        assert not hasattr(req, "is_enabled"), (
            "is_enabled must not be a field on the per-source request schema. "
            "Spec: BACKEND.md §API body shape."
        )


# ── ReplaceIngestionSourceRequest ─────────────────────────────────────────────


class TestReplaceIngestionSourceRequest:
    """Spec: API.md §Ingestion — PUT /spoke/ingestion/sources/{id} body."""

    def test_valid_replace_accepted(self) -> None:
        req = ReplaceIngestionSourceRequest(
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="replacement source",
            schedule="@daily",
            recipe={"source": {"type": "postgres", "config": {}}},
        )
        assert req.mode == Mode.ACTIVE_CUSTOM_MANAGED

    def test_no_schedule_tier_field(self) -> None:
        """ReplaceIngestionSourceRequest must NOT have schedule_tier.

        Spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier internal.
        """
        req = ReplaceIngestionSourceRequest(
            mode=Mode.PASSIVE,
            name="source",
            recipe={"source": {"type": "postgres", "config": {}}},
        )
        assert not hasattr(req, "schedule_tier")


# ── PatchIngestionSourceRequest ───────────────────────────────────────────────


class TestPatchIngestionSourceRequest:
    """Spec: API.md §Ingestion — PATCH /spoke/ingestion/sources/{id} body.
    All fields optional; mode is NOT patchable.
    """

    def test_empty_patch_is_valid(self) -> None:
        """Empty patch body is valid — all fields optional."""
        req = PatchIngestionSourceRequest()
        assert req.name is None
        assert req.schedule is None
        assert req.recipe is None

    def test_name_only_patch(self) -> None:
        """Patching only name is valid."""
        req = PatchIngestionSourceRequest(name="new name")
        assert req.name == "new name"

    def test_no_mode_field_in_patch(self) -> None:
        """PatchIngestionSourceRequest must NOT have a mode field.

        Spec: BACKEND.md §Ingestion Service §patch_source — 'mode is not patchable'.
        """
        req = PatchIngestionSourceRequest(name="test")
        assert not hasattr(req, "mode"), (
            "mode must not be a field on PatchIngestionSourceRequest. "
            "Spec: BACKEND.md §patch_source — mode is not patchable."
        )

    def test_no_schedule_tier_in_patch(self) -> None:
        """PatchIngestionSourceRequest must NOT have schedule_tier.

        Spec: BACKEND_SCHEMA.md — schedule_tier is internal.
        """
        req = PatchIngestionSourceRequest()
        assert not hasattr(req, "schedule_tier")


# ── IngestionSourceResponse ───────────────────────────────────────────────────


class TestIngestionSourceResponse:
    """Spec: BACKEND.md §API body shape — response fields.

    'No DataSpoke-isms on the wire: schedule is the cron string (not schedule_cron/
    schedule_tier), and the frontend renders/edits this JSON as YAML.'
    """

    def _make_response(self, **kwargs: Any) -> IngestionSourceResponse:
        now = datetime.now(tz=UTC)
        defaults: dict[str, Any] = dict(
            id="abc-123",
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="imazon catalog pg",
            schedule="0 0 * * *",
            recipe={"source": {"type": "postgres", "config": {}}},
            platform="postgres",
            status="OK",
            created_at=now,
            updated_at=now,
        )
        defaults.update(kwargs)
        return IngestionSourceResponse(**defaults)

    def test_has_schedule_not_schedule_tier(self) -> None:
        """Response carries 'schedule' (the cron string), not 'schedule_tier'.

        Spec: BACKEND.md §API body shape — 'schedule is the cron string (not
        schedule_cron/schedule_tier)'.
        Spec: BACKEND_SCHEMA.md — 'schedule_tier is internal and never appears in the API'.
        """
        resp = self._make_response()
        assert resp.schedule == "0 0 * * *"
        assert not hasattr(resp, "schedule_tier"), (
            "schedule_tier must not appear in the API response. "
            "Spec: BACKEND_SCHEMA.md — schedule_tier internal."
        )

    def test_has_no_is_enabled(self) -> None:
        """Response must NOT carry is_enabled.

        Spec: BACKEND.md §API body shape.
        """
        resp = self._make_response()
        assert not hasattr(resp, "is_enabled")

    def test_has_recipe_intact(self) -> None:
        """Response carries recipe verbatim (${name__key} refs as-is).

        Spec: BACKEND.md §API body shape — '${name__key} secret references … the
        response returns those references verbatim (never plaintext)'.
        """
        recipe = {"source": {"type": "postgres", "config": {"password": "${team_pg__password}"}}}
        resp = self._make_response(recipe=recipe)
        assert resp.recipe["source"]["config"]["password"] == "${team_pg__password}"

    def test_datahub_source_urn_optional(self) -> None:
        """datahub_source_urn is None for non-DATAHUB_MANAGED sources."""
        resp = self._make_response(datahub_source_urn=None)
        assert resp.datahub_source_urn is None


# ── RunIngestionSourceRequest ─────────────────────────────────────────────────


class TestRunIngestionSourceRequest:
    def test_defaults_to_non_dry_run(self) -> None:
        """dry_run defaults to False — a real run by default.

        Spec: API.md §Ingestion — POST .../method/run body.
        """
        req = RunIngestionSourceRequest()
        assert req.dry_run is False

    def test_dry_run_true_accepted(self) -> None:
        req = RunIngestionSourceRequest(dry_run=True)
        assert req.dry_run is True


# ── SecretRefListResponse ─────────────────────────────────────────────────────


class TestSecretRefListResponse:
    """Spec: SECRET_RESOLUTION.md §API schema — 'Values are never returned'.

    Spec: API.md §Ingestion — GET /spoke/ingestion/secrets.
    """

    def test_response_shape(self) -> None:
        """SecretRefListResponse carries a list of SecretRefInfo — no values."""
        resp = SecretRefListResponse(
            secrets=[
                SecretRefInfo(
                    ref="team_pg__password",
                    secret_name="dataspoke-source-cred-team-pg",
                    key="password",
                )
            ]
        )
        assert len(resp.secrets) == 1
        ref = resp.secrets[0]
        assert ref.ref == "team_pg__password"
        assert ref.secret_name == "dataspoke-source-cred-team-pg"
        assert ref.key == "password"

    def test_secret_ref_info_has_no_value_field(self) -> None:
        """SecretRefInfo does NOT have a 'value' field.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'Values are never returned'.
        """
        info = SecretRefInfo(
            ref="team_pg__password",
            secret_name="dataspoke-source-cred-team-pg",
            key="password",
        )
        assert not hasattr(info, "value"), (
            "SecretRefInfo must not expose a 'value' field. "
            "Spec: SECRET_RESOLUTION.md — values are never returned."
        )

    def test_empty_secrets_list(self) -> None:
        """Empty secrets list is valid."""
        resp = SecretRefListResponse(secrets=[])
        assert resp.secrets == []


# ── IngestionReverseLookupResponse ────────────────────────────────────────────


class TestIngestionReverseLookupResponse:
    """Spec: API.md §Ingestion — GET /spoke/common/data/{urn}/attr/ingestion."""

    def test_unmapped_dataset_all_nulls(self) -> None:
        """Unmapped dataset → source_id=None, mode=None, name=None, latest_run=None.

        Spec: API.md §Ingestion — 'Returns the owning source for a dataset, or null if unmapped'.
        """
        resp = IngestionReverseLookupResponse(
            dataset_urn="urn:li:dataset:test",
            source_id=None,
            mode=None,
            name=None,
            latest_run=None,
        )
        assert resp.source_id is None
        assert resp.mode is None
        assert resp.name is None
        assert resp.latest_run is None

    def test_mapped_dataset_carries_source_info(self) -> None:
        """Mapped dataset carries source_id, mode, and name."""
        resp = IngestionReverseLookupResponse(
            dataset_urn="urn:li:dataset:test",
            source_id="source-uuid-here",
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="imazon catalog pg",
            latest_run=None,
        )
        assert resp.source_id == "source-uuid-here"
        assert resp.mode == Mode.ACTIVE_CUSTOM_MANAGED

    def test_no_schedule_tier_in_response(self) -> None:
        """Reverse-lookup response does NOT expose schedule_tier.

        Spec: BACKEND_SCHEMA.md — schedule_tier internal, never in API.
        """
        resp = IngestionReverseLookupResponse(
            dataset_urn="urn:li:dataset:test",
        )
        assert not hasattr(resp, "schedule_tier")
