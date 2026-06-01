"""Unit tests for src/api/schemas/ingestion.py — per-source request/response shapes.

Spec: API.md §Ingestion (/spoke/ingestion)
Spec: BACKEND_SCHEMA.md §ingestion_source — schedule is cron string; schedule_tier is
      internal (never on wire); mode ∈ {DATAHUB_MANAGED, ACTIVE_CUSTOM_MANAGED, PASSIVE}.
Spec: SECRET_RESOLUTION.md §API schema
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.ingestion import (
    CreateIngestionSourceRequest,
    IngestionRunResponse,
    IngestionSourceListResponse,
    IngestionSourceResponse,
    PatchIngestionSourceRequest,
    ReplaceIngestionSourceRequest,
    SecretRefInfo,
    SecretRefListResponse,
)
from src.shared.models.ingestion import Mode


# ── CreateIngestionSourceRequest ──────────────────────────────────────────────


class TestCreateIngestionSourceRequest:
    """Spec: API.md §Ingestion — POST /spoke/ingestion/sources body shape.
    The body carries {mode, name, schedule, recipe} — no schedule_tier on wire.
    """

    _VALID_BODY = {
        "mode": "ACTIVE_CUSTOM_MANAGED",
        "name": "imazon catalog postgres",
        "schedule": "0 0 * * *",
        "recipe": {
            "source": {
                "type": "postgres",
                "config": {
                    "host_port": "example-pg:5432",
                    "database": "example_db",
                    "username": "spoke_reader",
                    "schema_pattern": {"allow": ["^catalog$"]},
                },
            }
        },
    }

    def test_valid_active_custom_managed_body_accepted(self) -> None:
        # spec: API.md §Ingestion — POST body with mode=ACTIVE_CUSTOM_MANAGED.
        req = CreateIngestionSourceRequest.model_validate(self._VALID_BODY)
        assert req.mode == Mode.ACTIVE_CUSTOM_MANAGED
        assert req.name == "imazon catalog postgres"
        assert req.schedule == "0 0 * * *"

    def test_passive_mode_accepted(self) -> None:
        # spec: API.md §Ingestion — PASSIVE sources can be created via the API.
        body = {
            "mode": "PASSIVE",
            "name": "external kafka pipeline",
            "schedule": None,
            "recipe": {
                "source": {
                    "type": "kafka",
                    "config": {"schema_pattern": {"allow": [".*"]}},
                }
            },
        }
        req = CreateIngestionSourceRequest.model_validate(body)
        assert req.mode == Mode.PASSIVE
        assert req.schedule is None

    def test_null_schedule_is_valid(self) -> None:
        # spec: BACKEND_SCHEMA.md §ingestion_source — schedule=null means manual-only.
        body = dict(self._VALID_BODY)
        body["schedule"] = None
        req = CreateIngestionSourceRequest.model_validate(body)
        assert req.schedule is None

    def test_recipe_must_be_dict(self) -> None:
        # spec: API.md §Ingestion — recipe must be a dict.
        body = dict(self._VALID_BODY)
        body["recipe"] = "source: postgres"
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest.model_validate(body)

    def test_empty_name_rejected(self) -> None:
        # spec: API.md §Ingestion — name min_length=1.
        body = dict(self._VALID_BODY)
        body["name"] = ""
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest.model_validate(body)

    def test_invalid_mode_rejected(self) -> None:
        # spec: BACKEND_SCHEMA.md §ingestion_source — mode must be one of three values.
        body = dict(self._VALID_BODY)
        body["mode"] = "active-custom"  # old mode string — must be rejected
        with pytest.raises(ValidationError):
            CreateIngestionSourceRequest.model_validate(body)


# ── PatchIngestionSourceRequest ───────────────────────────────────────────────


class TestPatchIngestionSourceRequest:
    """Spec: API.md §Ingestion — PATCH body accepts partial update of name/schedule/recipe.
    mode is not patchable.
    """

    def test_patch_only_name_is_valid(self) -> None:
        req = PatchIngestionSourceRequest(name="new name")
        assert req.name == "new name"
        assert req.schedule is None
        assert req.recipe is None

    def test_patch_only_schedule_is_valid(self) -> None:
        req = PatchIngestionSourceRequest(schedule="0 * * * *")
        assert req.schedule == "0 * * * *"

    def test_patch_all_fields_is_valid(self) -> None:
        recipe = {"source": {"type": "postgres", "config": {}}}
        req = PatchIngestionSourceRequest(name="n", schedule="0 0 * * 0", recipe=recipe)
        assert req.name == "n"
        assert req.schedule == "0 0 * * 0"
        assert req.recipe == recipe

    def test_empty_patch_body_is_valid(self) -> None:
        # All fields optional in PATCH.
        req = PatchIngestionSourceRequest()
        assert req.name is None
        assert req.schedule is None
        assert req.recipe is None


# ── ReplaceIngestionSourceRequest ─────────────────────────────────────────────


class TestReplaceIngestionSourceRequest:
    """Spec: API.md §Ingestion — PUT body is a full replacement."""

    def test_valid_put_body_accepted(self) -> None:
        req = ReplaceIngestionSourceRequest(
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="replace source",
            schedule="0 0 * * *",
            recipe={"source": {"type": "postgres", "config": {}}},
        )
        assert req.mode == Mode.ACTIVE_CUSTOM_MANAGED
        assert req.schedule == "0 0 * * *"


# ── IngestionSourceResponse ───────────────────────────────────────────────────


class TestIngestionSourceResponse:
    """Response shape has no schedule_tier (internal) — only schedule (cron string).

    Spec: BACKEND_SCHEMA.md §ingestion_source — 'schedule_tier is internal and
          never appears in the API.'
    Spec: API.md §Ingestion — response carries {id, mode, name, schedule, recipe,
          platform, status, datahub_source_urn, created_at, updated_at}.
    """

    def test_response_has_no_schedule_tier_field(self) -> None:
        # spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier internal only.
        now = datetime.now(tz=UTC)
        resp = IngestionSourceResponse(
            id="some-uuid",
            mode=Mode.ACTIVE_CUSTOM_MANAGED,
            name="test source",
            schedule="0 0 * * *",
            recipe={"source": {"type": "postgres", "config": {}}},
            platform="postgres",
            status="OK",
            datahub_source_urn=None,
            created_at=now,
            updated_at=now,
        )
        data = resp.model_dump()
        assert "schedule_tier" not in data, (
            "IngestionSourceResponse must not expose schedule_tier (internal column). "
            "spec: BACKEND_SCHEMA.md §ingestion_source."
        )
        assert "is_enabled" not in data, (
            "IngestionSourceResponse must not expose is_enabled (removed field). "
        )
        assert "schedule" in data
        assert data["schedule"] == "0 0 * * *"

    def test_response_has_required_fields(self) -> None:
        now = datetime.now(tz=UTC)
        resp = IngestionSourceResponse(
            id="some-uuid",
            mode=Mode.PASSIVE,
            name="passive source",
            schedule=None,
            recipe={"source": {"type": "kafka", "config": {}}},
            platform="kafka",
            status="OK",
            datahub_source_urn=None,
            created_at=now,
            updated_at=now,
        )
        assert resp.id == "some-uuid"
        assert resp.mode == Mode.PASSIVE
        assert resp.platform == "kafka"
        assert resp.datahub_source_urn is None


# ── SecretRefListResponse ─────────────────────────────────────────────────────


class TestSecretRefListResponse:
    """Spec: SECRET_RESOLUTION.md §API schema — {secrets: [{ref, secret_name, key}]}.
    Values are never included.
    """

    def test_shape_has_secrets_list(self) -> None:
        # spec: SECRET_RESOLUTION.md §Reference discovery (list flow).
        resp = SecretRefListResponse(
            secrets=[
                SecretRefInfo(
                    ref="team-pg__password",
                    secret_name="dataspoke-source-cred-team-pg",
                    key="password",
                )
            ]
        )
        assert len(resp.secrets) == 1
        assert resp.secrets[0].ref == "team-pg__password"
        assert resp.secrets[0].secret_name == "dataspoke-source-cred-team-pg"
        assert resp.secrets[0].key == "password"
        data = resp.secrets[0].model_dump()
        assert "value" not in data

    def test_empty_secrets_list_is_valid(self) -> None:
        resp = SecretRefListResponse(secrets=[])
        assert resp.secrets == []


# ── IngestionSourceListResponse ───────────────────────────────────────────────


class TestIngestionSourceListResponse:
    """Spec: API.md §Ingestion — paginated list response carries 'sources' key."""

    def test_default_empty_sources(self) -> None:
        resp = IngestionSourceListResponse(total_count=0)
        assert resp.sources == []
        assert resp.offset == 0
        assert resp.limit == 20
        assert resp.total_count == 0


# ── IngestionRunResponse ──────────────────────────────────────────────────────


class TestIngestionRunResponse:
    """Spec: API.md §Ingestion — POST .../method/run returns {run_id, status, detail}."""

    def test_run_response_fields(self) -> None:
        resp = IngestionRunResponse(run_id="r1", status="success")
        assert resp.run_id == "r1"
        assert resp.status == "success"
        assert resp.detail == {}
