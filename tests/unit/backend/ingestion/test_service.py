"""Unit tests for IngestionService — per-source CRUD, run, and queries.

Covers:
- DATAHUB_MANAGED create/update/delete → 409 INGESTION_SOURCE_READONLY
- ACTIVE_CUSTOM_MANAGED run on PASSIVE source → 409 INGESTION_RUN_NOT_APPLICABLE
- cron → tier validation: unknown cron → INVALID_PARAMETER error code
- reverse_lookup precedence: emitted > pipeline_name > matcher
- list_active_sources_for_tier: mode + tier filter
- list_datasets_for_source: propagates EntityNotFoundError on unknown source
- get_source: raises EntityNotFoundError for non-existent ID

Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source, §ingestion_source_dataset
Spec: spec/USE_CASE_en.md §UC1
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError
from tests.unit.backend.conftest import mock_db_refresh, mock_scalar_query
from tests.unit.backend.ingestion.conftest import (
    _DATASET_URN,
    _RECIPE_NO_SECRET,
    _RECIPE_POSTGRES,
    _SOURCE_ID,
    _make_source_row,
)


# ── DATAHUB_MANAGED read-only guard ───────────────────────────────────────────


class TestDatahubManagedReadOnly:
    """Spec: BACKEND.md §Ingestion Service — 'DATAHUB_MANAGED rows are read-only
    in DataSpoke (DataHub is SSOT) — create/update/delete return 409
    INGESTION_SOURCE_READONLY'.
    """

    @pytest.mark.asyncio
    async def test_create_datahub_managed_raises_conflict(self, service: IngestionService, db: AsyncMock) -> None:
        """POST with mode=DATAHUB_MANAGED raises ConflictError(INGESTION_SOURCE_READONLY).

        Spec: BACKEND.md §Ingestion Service §Editability.
        """
        with pytest.raises(ConflictError) as exc_info:
            await service.create_source(
                mode="DATAHUB_MANAGED",
                name="hub-managed-source",
                schedule=None,
                recipe=_RECIPE_NO_SECRET,
            )
        assert exc_info.value.error_code == "INGESTION_SOURCE_READONLY"

    @pytest.mark.asyncio
    async def test_replace_datahub_managed_raises_conflict(self, service: IngestionService, db: AsyncMock) -> None:
        """PUT on a DATAHUB_MANAGED source raises ConflictError(INGESTION_SOURCE_READONLY).

        Spec: BACKEND.md §Ingestion Service §Editability.
        """
        row = _make_source_row(mode="DATAHUB_MANAGED")
        mock_scalar_query(db, row)

        with pytest.raises(ConflictError) as exc_info:
            await service.replace_source(
                source_id=str(row.id),
                mode="DATAHUB_MANAGED",
                name="hub-managed",
                schedule=None,
                recipe=_RECIPE_NO_SECRET,
            )
        assert exc_info.value.error_code == "INGESTION_SOURCE_READONLY"

    @pytest.mark.asyncio
    async def test_patch_datahub_managed_raises_conflict(self, service: IngestionService, db: AsyncMock) -> None:
        """PATCH on a DATAHUB_MANAGED source raises ConflictError(INGESTION_SOURCE_READONLY).

        Spec: BACKEND.md §Ingestion Service §Editability.
        """
        row = _make_source_row(mode="DATAHUB_MANAGED")
        mock_scalar_query(db, row)

        with pytest.raises(ConflictError) as exc_info:
            await service.patch_source(str(row.id), {"name": "new-name"})
        assert exc_info.value.error_code == "INGESTION_SOURCE_READONLY"

    @pytest.mark.asyncio
    async def test_delete_datahub_managed_raises_conflict(self, service: IngestionService, db: AsyncMock) -> None:
        """DELETE on a DATAHUB_MANAGED source raises ConflictError(INGESTION_SOURCE_READONLY).

        Spec: BACKEND.md §Ingestion Service §Editability.
        """
        row = _make_source_row(mode="DATAHUB_MANAGED")
        mock_scalar_query(db, row)

        with pytest.raises(ConflictError) as exc_info:
            await service.delete_source(str(row.id))
        assert exc_info.value.error_code == "INGESTION_SOURCE_READONLY"


# ── Run not-applicable guard ──────────────────────────────────────────────────


class TestRunNotApplicable:
    """Spec: BACKEND.md §Active-custom run pipeline — 'reject if mode !=
    ACTIVE_CUSTOM_MANAGED (409 INGESTION_RUN_NOT_APPLICABLE)'.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["PASSIVE", "DATAHUB_MANAGED"])
    async def test_run_on_non_active_custom_managed_raises(
        self,
        service: IngestionService,
        db: AsyncMock,
        mode: str,
    ) -> None:
        """run() on PASSIVE or DATAHUB_MANAGED source raises INGESTION_RUN_NOT_APPLICABLE.

        Spec: BACKEND.md §Active-custom run pipeline step 1.
        """
        row = _make_source_row(mode=mode)
        mock_scalar_query(db, row)

        with pytest.raises(ConflictError) as exc_info:
            await service._run_inner(str(row.id), dry_run=False)
        assert exc_info.value.error_code == "INGESTION_RUN_NOT_APPLICABLE"


# ── Schedule validation (cron → tier) ────────────────────────────────────────


class TestScheduleValidation:
    """Spec: BACKEND.md §Ingestion Service §Schedule — 'on upsert the service validates
    it maps to one of the three tiers ... cron→tier validation error code = INVALID_PARAMETER'.
    """

    @pytest.mark.asyncio
    async def test_unknown_cron_on_create_raises_invalid_parameter(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Creating ACTIVE_CUSTOM_MANAGED with unknown cron raises INVALID_PARAMETER.

        Spec: BACKEND.md §Ingestion Service §Schedule.
        """
        with (
            patch(
                "src.backend.ingestion.service._verify_recipe_secret_refs",
                return_value=None,
            ),
        ):
            with pytest.raises(PreconditionFailedError) as exc_info:
                await service.create_source(
                    mode="ACTIVE_CUSTOM_MANAGED",
                    name="test source",
                    schedule="*/15 * * * *",  # 15-minute interval — not a valid tier
                    recipe=_RECIPE_NO_SECRET,
                )
        assert exc_info.value.error_code == "INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_valid_cron_on_create_accepted(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Creating ACTIVE_CUSTOM_MANAGED with '0 0 * * *' (daily) is accepted.

        Spec: BACKEND.md §Ingestion Service §Schedule.
        """
        mock_scalar_query(db, None)
        mock_db_refresh(db)

        with patch(
            "src.backend.ingestion.service._verify_recipe_secret_refs",
            return_value=None,
        ):
            record = await service.create_source(
                mode="ACTIVE_CUSTOM_MANAGED",
                name="test source",
                schedule="0 0 * * *",
                recipe=_RECIPE_NO_SECRET,
            )
        assert record.schedule_tier == "daily"
        assert record.schedule == "0 0 * * *"

    @pytest.mark.asyncio
    async def test_null_schedule_on_active_custom_managed_is_manual_only(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """schedule=None on ACTIVE_CUSTOM_MANAGED → schedule_tier=None (manual-only).

        Spec: BACKEND_SCHEMA.md §ingestion_source — 'schedule: NULL means manual-only'.
        """
        mock_scalar_query(db, None)
        mock_db_refresh(db)

        with patch(
            "src.backend.ingestion.service._verify_recipe_secret_refs",
            return_value=None,
        ):
            record = await service.create_source(
                mode="ACTIVE_CUSTOM_MANAGED",
                name="manual source",
                schedule=None,
                recipe=_RECIPE_NO_SECRET,
            )
        assert record.schedule is None
        assert record.schedule_tier is None

    @pytest.mark.asyncio
    async def test_passive_source_allows_null_schedule(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """PASSIVE source with schedule=None is valid (PASSIVE has no tier constraint).

        Spec: BACKEND_SCHEMA.md §ingestion_source — 'Null for PASSIVE'.
        """
        mock_scalar_query(db, None)
        mock_db_refresh(db)

        with patch(
            "src.backend.ingestion.service._verify_recipe_secret_refs",
            return_value=None,
        ):
            record = await service.create_source(
                mode="PASSIVE",
                name="passive source",
                schedule=None,
                recipe=_RECIPE_NO_SECRET,
            )
        assert record.mode == "PASSIVE"
        assert record.schedule_tier is None


# ── get_source ────────────────────────────────────────────────────────────────


class TestGetSource:
    """Spec: BACKEND.md §Ingestion Service — EntityNotFoundError on unknown source."""

    @pytest.mark.asyncio
    async def test_get_source_not_found_raises(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """get_source with unknown UUID raises EntityNotFoundError.

        Spec: BACKEND.md §Ingestion Service — 'EntityNotFoundError if not found'.
        """
        mock_scalar_query(db, None)
        with pytest.raises(EntityNotFoundError):
            await service.get_source(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_get_source_invalid_uuid_raises(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """get_source with non-UUID string raises EntityNotFoundError (not ValueError).

        Spec: BACKEND.md §Ingestion Service.
        """
        with pytest.raises(EntityNotFoundError):
            await service.get_source("not-a-uuid")

    @pytest.mark.asyncio
    async def test_get_source_found_returns_record(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """get_source returns IngestionSourceRecord for a valid row."""
        row = _make_source_row()
        mock_scalar_query(db, row)

        record = await service.get_source(str(row.id))
        assert record.mode == "ACTIVE_CUSTOM_MANAGED"
        assert record.platform == "postgres"
        assert record.schedule_tier == "daily"


# ── reverse_lookup precedence ─────────────────────────────────────────────────


class TestReverseLookupPrecedence:
    """Spec: BACKEND.md §Ingestion Service §reverse_lookup — 'emitted > pipeline_name > matcher'.

    When multiple sources map the same dataset, the highest-priority origin wins.
    Ties within the same priority are broken by most-recent last_seen_at.
    """

    @pytest.mark.asyncio
    async def test_emitted_beats_matcher(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Source with origin='emitted' wins over origin='matcher'.

        Spec: BACKEND.md §reverse_lookup — 'emitted > pipeline_name > matcher'.
        """
        source_a_id = uuid.uuid4()
        source_b_id = uuid.uuid4()

        mapping_emitted = MagicMock()
        mapping_emitted.origin = "emitted"
        mapping_emitted.last_seen_at = datetime.now(tz=UTC)
        mapping_emitted.dataset_urn = _DATASET_URN

        mapping_matcher = MagicMock()
        mapping_matcher.origin = "matcher"
        mapping_matcher.last_seen_at = datetime.now(tz=UTC)
        mapping_matcher.dataset_urn = _DATASET_URN

        source_a = MagicMock()
        source_a.id = source_a_id
        source_a.mode = "ACTIVE_CUSTOM_MANAGED"
        source_a.name = "source-a"
        source_a.platform = "postgres"
        source_a.recipe = _RECIPE_NO_SECRET
        source_a.schedule = "0 0 * * *"
        source_a.schedule_tier = "daily"
        source_a.datahub_source_urn = None
        source_a.status = "OK"
        source_a.created_at = datetime.now(tz=UTC)
        source_a.updated_at = datetime.now(tz=UTC)

        source_b = MagicMock()
        source_b.id = source_b_id
        source_b.mode = "PASSIVE"
        source_b.name = "source-b"
        source_b.platform = "postgres"
        source_b.recipe = _RECIPE_NO_SECRET
        source_b.schedule = None
        source_b.schedule_tier = None
        source_b.datahub_source_urn = None
        source_b.status = "OK"
        source_b.created_at = datetime.now(tz=UTC)
        source_b.updated_at = datetime.now(tz=UTC)

        result_mock = MagicMock()
        # source_b has 'emitted', source_a has 'matcher' → source_b wins
        result_mock.all.return_value = [
            (mapping_matcher, source_a),
            (mapping_emitted, source_b),
        ]
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is not None
        assert winner.name == "source-b", (
            f"Expected 'source-b' (emitted origin) to win; got '{winner.name}'. "
            "Spec: BACKEND.md §reverse_lookup — emitted > matcher."
        )

    @pytest.mark.asyncio
    async def test_pipeline_name_beats_matcher(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Source with origin='pipeline_name' wins over origin='matcher'.

        Spec: BACKEND.md §reverse_lookup — 'emitted > pipeline_name > matcher'.
        """
        mapping_pipeline = MagicMock()
        mapping_pipeline.origin = "pipeline_name"
        mapping_pipeline.last_seen_at = datetime.now(tz=UTC)

        mapping_matcher = MagicMock()
        mapping_matcher.origin = "matcher"
        mapping_matcher.last_seen_at = datetime.now(tz=UTC)

        source_pipeline = MagicMock()
        source_pipeline.id = uuid.uuid4()
        source_pipeline.name = "pipeline-source"
        source_pipeline.mode = "DATAHUB_MANAGED"
        source_pipeline.platform = "postgres"
        source_pipeline.recipe = _RECIPE_NO_SECRET
        source_pipeline.schedule = None
        source_pipeline.schedule_tier = None
        source_pipeline.datahub_source_urn = "urn:li:dataHubIngestionSource:abc"
        source_pipeline.status = "OK"
        source_pipeline.created_at = datetime.now(tz=UTC)
        source_pipeline.updated_at = datetime.now(tz=UTC)

        source_matcher = MagicMock()
        source_matcher.id = uuid.uuid4()
        source_matcher.name = "matcher-source"
        source_matcher.mode = "PASSIVE"
        source_matcher.platform = "postgres"
        source_matcher.recipe = _RECIPE_NO_SECRET
        source_matcher.schedule = None
        source_matcher.schedule_tier = None
        source_matcher.datahub_source_urn = None
        source_matcher.status = "OK"
        source_matcher.created_at = datetime.now(tz=UTC)
        source_matcher.updated_at = datetime.now(tz=UTC)

        result_mock = MagicMock()
        result_mock.all.return_value = [
            (mapping_matcher, source_matcher),
            (mapping_pipeline, source_pipeline),
        ]
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is not None
        assert winner.name == "pipeline-source", (
            f"Expected 'pipeline-source' (pipeline_name origin) to win; got '{winner.name}'. "
            "Spec: BACKEND.md §reverse_lookup — pipeline_name > matcher."
        )

    @pytest.mark.asyncio
    async def test_no_mapping_returns_none(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """No source maps the dataset → reverse_lookup returns None."""
        result_mock = MagicMock()
        result_mock.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is None


# ── list_active_sources_for_tier ──────────────────────────────────────────────


class TestListActiveSourcesForTier:
    """Spec: BACKEND.md §Tier DAG support — returns ACTIVE_CUSTOM_MANAGED sources
    for the given schedule tier.
    """

    @pytest.mark.asyncio
    async def test_returns_active_custom_managed_sources_for_tier(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """list_active_sources_for_tier returns sources matching mode+tier.

        Spec: BACKEND.md §Tier DAG support — 'Used by the ingestion-active-{hourly,daily,weekly}
        Airflow DAGs.'
        """
        rows = [_make_source_row(mode="ACTIVE_CUSTOM_MANAGED", schedule_tier="daily")]
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = rows
        db.execute = AsyncMock(return_value=result_mock)

        sources = await service.list_active_sources_for_tier("daily")
        assert len(sources) == 1
        assert sources[0].mode == "ACTIVE_CUSTOM_MANAGED"
        assert sources[0].schedule_tier == "daily"

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matching_sources(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Empty result set when no ACTIVE_CUSTOM_MANAGED source is in the given tier."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        sources = await service.list_active_sources_for_tier("weekly")
        assert sources == []


# ── list_datasets_for_source ──────────────────────────────────────────────────


class TestListDatasetsForSource:
    """Spec: BACKEND.md §Dataset mapping queries."""

    @pytest.mark.asyncio
    async def test_unknown_source_raises_entity_not_found(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """list_datasets_for_source raises EntityNotFoundError for unknown source.

        Spec: BACKEND.md §Ingestion Service — 'raises if not found'.
        """
        mock_scalar_query(db, None)
        with pytest.raises(EntityNotFoundError):
            await service.list_datasets_for_source(str(uuid.uuid4()))
