"""Unit tests for IngestionService — per-source CRUD, run, and queries.

Covers:
- DATAHUB_MANAGED create/update/delete → 409 INGESTION_SOURCE_READONLY
- ACTIVE_CUSTOM_MANAGED run on PASSIVE source → 409 INGESTION_RUN_NOT_APPLICABLE
- cron → tier validation: unknown cron → INVALID_PARAMETER error code
- reverse_lookup precedence: emitted > pipeline_name > matched
- list_active_sources_for_tier: mode + tier filter
- list_datasets_for_source: propagates EntityNotFoundError on unknown source
- get_source: raises EntityNotFoundError for non-existent ID
- _mirror_execution_requests: DataHub status → INGESTION_COMPLETE / INGESTION_FAIL /
  no-event mapping per spec/feature/BACKEND.md §Sync step 4 (Run events).

Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source, §ingestion_source_dataset
Spec: spec/USE_CASE_en.md §UC1
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from datahub.metadata.schema_classes import (  # type: ignore
    DataProcessInstanceOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRunEventClass,
    DataProcessRunStatusClass,
    DataProcessTypeClass,
)

from src.backend.ingestion.extractors import IngestionResult
from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError
from tests.unit.backend.conftest import mock_db_refresh, mock_scalar_query
from tests.unit.backend.ingestion.conftest import (
    _DATASET_URN,
    _RECIPE_NO_SECRET,
    _make_source_row,
)

# ── DATAHUB_MANAGED read-only guard ───────────────────────────────────────────


class TestDatahubManagedReadOnly:
    """Spec: BACKEND.md §Ingestion Service — 'DATAHUB_MANAGED rows are read-only
    in DataSpoke (DataHub is SSOT) — create/update/delete return 409
    INGESTION_SOURCE_READONLY'.
    """

    @pytest.mark.asyncio
    async def test_create_datahub_managed_raises_conflict(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
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
    async def test_replace_datahub_managed_raises_conflict(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
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
    async def test_patch_datahub_managed_raises_conflict(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """PATCH on a DATAHUB_MANAGED source raises ConflictError(INGESTION_SOURCE_READONLY).

        Spec: BACKEND.md §Ingestion Service §Editability.
        """
        row = _make_source_row(mode="DATAHUB_MANAGED")
        mock_scalar_query(db, row)

        with pytest.raises(ConflictError) as exc_info:
            await service.patch_source(str(row.id), {"name": "new-name"})
        assert exc_info.value.error_code == "INGESTION_SOURCE_READONLY"

    @pytest.mark.asyncio
    async def test_delete_datahub_managed_raises_conflict(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
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
    """Spec: BACKEND_SCHEMA.md §ingestion_source_dataset — authority levels (emitted/
    pipeline_name = high, matched = medium). When multiple sources map the same dataset,
    the highest-authority derivation wins; the emitted > pipeline_name relative order is
    implementation-defined (these tests assert only the spec-grounded high > medium boundary).
    Ties within the same priority are broken by most-recent last_seen_at.
    """

    @pytest.mark.asyncio
    async def test_emitted_beats_matched(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Source with derivation='emitted' wins over derivation='matched'.

        Spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted is authority 'high',
        matched is authority 'medium'; the higher-authority derivation wins.
        """
        source_a_id = uuid.uuid4()
        source_b_id = uuid.uuid4()

        mapping_emitted = MagicMock()
        mapping_emitted.derivation = "emitted"
        mapping_emitted.last_seen_at = datetime.now(tz=UTC)
        mapping_emitted.dataset_urn = _DATASET_URN

        mapping_matched = MagicMock()
        mapping_matched.derivation = "matched"
        mapping_matched.last_seen_at = datetime.now(tz=UTC)
        mapping_matched.dataset_urn = _DATASET_URN

        source_a = MagicMock()
        source_a.id = source_a_id
        source_a.mode = "ACTIVE_CUSTOM_MANAGED"
        source_a.name = "source-a"
        source_a.platform = "postgres"
        source_a.recipe = _RECIPE_NO_SECRET
        source_a.schedule = "0 0 * * *"
        source_a.schedule_tier = "daily"
        source_a.datahub_source_urn = None
        source_a.parent_source_id = None
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
        source_b.parent_source_id = None
        source_b.status = "OK"
        source_b.created_at = datetime.now(tz=UTC)
        source_b.updated_at = datetime.now(tz=UTC)

        result_mock = MagicMock()
        # source_b has derivation='emitted', source_a has derivation='matched' → source_b wins
        result_mock.all.return_value = [
            (mapping_matched, source_a),
            (mapping_emitted, source_b),
        ]
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is not None
        assert winner.name == "source-b", (
            f"Expected 'source-b' (emitted derivation) to win; got '{winner.name}'. "
            "Spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted (high) > matched (medium)."
        )

    @pytest.mark.asyncio
    async def test_pipeline_name_beats_matched(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """Source with derivation='pipeline_name' wins over derivation='matched'.

        Spec: BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name is authority
        'high', matched is authority 'medium'; the higher-authority derivation wins.
        """
        mapping_pipeline = MagicMock()
        mapping_pipeline.derivation = "pipeline_name"
        mapping_pipeline.last_seen_at = datetime.now(tz=UTC)

        mapping_matched = MagicMock()
        mapping_matched.derivation = "matched"
        mapping_matched.last_seen_at = datetime.now(tz=UTC)

        source_pipeline = MagicMock()
        source_pipeline.id = uuid.uuid4()
        source_pipeline.name = "pipeline-source"
        source_pipeline.mode = "DATAHUB_MANAGED"
        source_pipeline.platform = "postgres"
        source_pipeline.recipe = _RECIPE_NO_SECRET
        source_pipeline.schedule = None
        source_pipeline.schedule_tier = None
        source_pipeline.datahub_source_urn = "urn:li:dataHubIngestionSource:abc"
        source_pipeline.parent_source_id = None
        source_pipeline.status = "OK"
        source_pipeline.created_at = datetime.now(tz=UTC)
        source_pipeline.updated_at = datetime.now(tz=UTC)

        source_matched = MagicMock()
        source_matched.id = uuid.uuid4()
        source_matched.name = "matched-source"
        source_matched.mode = "PASSIVE"
        source_matched.platform = "postgres"
        source_matched.recipe = _RECIPE_NO_SECRET
        source_matched.schedule = None
        source_matched.schedule_tier = None
        source_matched.datahub_source_urn = None
        source_matched.parent_source_id = None
        source_matched.status = "OK"
        source_matched.created_at = datetime.now(tz=UTC)
        source_matched.updated_at = datetime.now(tz=UTC)

        result_mock = MagicMock()
        result_mock.all.return_value = [
            (mapping_matched, source_matched),
            (mapping_pipeline, source_pipeline),
        ]
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is not None
        assert winner.name == "pipeline-source", (
            f"Expected 'pipeline-source' (pipeline_name derivation) to win; "
            f"got '{winner.name}'. "
            "Spec: BACKEND_SCHEMA.md §ingestion_source_dataset"
            " — pipeline_name (high) > matched (medium)."
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


# ── _mirror_execution_requests: DataHub status mapping ───────────────────────


class TestMirrorExecutionRequestsStatusMapping:
    """Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — DataHub execution-request
    statuses map to INGESTION_COMPLETE, INGESTION_FAIL, or no event, keyed by the stable
    execution-request URN.

    Invariants per the spec status→event table (only executions that reached a real
    ingestion outcome are mirrored):
      SUCCESS / SUCCEEDED                            → INGESTION.COMPLETE, status 'success'
      FAILURE / TIMEOUT / ABORTED / ROLLBACK_FAILED  → INGESTION.FAIL,     status 'failure'
      RUNNING / ROLLING_BACK / UP_FOR_RETRY / *no result*
                                                     → no event (in-progress / pending)
      CANCELLED / DUPLICATE / ROLLED_BACK            → no event (not an ingestion outcome)

    Identity / dedup is the execution-request URN: the service looks up an existing event
    for the source via ``detail->>'execution_request_urn'`` (a ``select(...).first()``
    result) and writes at most one row per URN, so repeated syncs are idempotent.

    ``occurred_at`` = ``startTimeMs`` when present (>0), else ``requestedAt`` — never ``now()``.
    """

    # Canonical request times used by the helpers (epoch ms).
    _START_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    _REQUESTED_MS = 1_699_999_000_000  # earlier than _START_MS

    def _make_exec_request(
        self,
        status: str,
        *,
        start_ms: int | None = None,
        requested_ms: int | None = None,
        urn: str | None = None,
    ) -> dict:
        """Build a list_execution_requests dict mirroring the client's shape."""
        return {
            "urn": urn or f"urn:li:dataHubExecutionRequest:test-{status}",
            "status": status,
            "startTimeMs": self._START_MS if start_ms is None else start_ms,
            "durationMs": 1000,
            "requestedAt": self._REQUESTED_MS if requested_ms is None else requested_ms,
        }

    def _dup_check(self, db: AsyncMock, existing: bool = False) -> None:
        """Stub the dedup query: ``db.execute(...).first()`` returns a row (existing)
        or None (no duplicate), matching the impl's ``select(Event.id)...first()``.
        """
        dup_result = MagicMock()
        dup_result.first.return_value = (uuid.uuid4(),) if existing else None
        db.execute = AsyncMock(return_value=dup_result)

    # ── COMPLETE outcomes ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("datahub_status", ["SUCCESS", "SUCCEEDED"])
    async def test_complete_statuses_write_complete_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        datahub_status: str,
    ) -> None:
        """DataHub SUCCESS / SUCCEEDED → Event(event_type=INGESTION.COMPLETE, status='success').

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) status table —
        'SUCCESS, SUCCEEDED (cross-version) → INGESTION.COMPLETE'. entity_type =
        'ingestion_source', entity_id = source_id, and detail carries the
        execution_request_urn identity key.
        """
        from src.shared.events import INGESTION_COMPLETE

        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"
        req = self._make_exec_request(datahub_status)

        datahub.list_execution_requests = AsyncMock(return_value=[req])
        self._dup_check(db, existing=False)

        count = await service._mirror_execution_requests(source_id, dh_urn)

        assert count == 1, (
            f"Expected 1 event for DataHub status {datahub_status!r}; got {count}."
        )
        db.add.assert_called_once()
        added_event = db.add.call_args[0][0]
        assert added_event.event_type == INGESTION_COMPLETE, (
            f"Expected event_type={INGESTION_COMPLETE!r} for {datahub_status!r}; "
            f"got {added_event.event_type!r}."
        )
        assert added_event.status == "success"
        assert added_event.entity_type == "ingestion_source", (
            f"Expected entity_type='ingestion_source'; got {added_event.entity_type!r}. "
            "Spec: BACKEND.md §Sync step 4 — source-level run events."
        )
        assert added_event.entity_id == source_id
        # Identity key per spec §Event Catalogue: detail carries execution_request_urn.
        assert added_event.detail["execution_request_urn"] == req["urn"], (
            "detail must carry the execution-request URN as the identity key. "
            "Spec: BACKEND.md §Sync step 4 / §Event Catalogue."
        )

    # ── FAIL outcomes ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "datahub_status", ["FAILURE", "TIMEOUT", "ABORTED", "ROLLBACK_FAILED"]
    )
    async def test_fail_statuses_write_fail_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        datahub_status: str,
    ) -> None:
        """DataHub FAILURE / TIMEOUT / ABORTED / ROLLBACK_FAILED → INGESTION.FAIL,
        status='failure'.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) status table —
        'FAILURE, TIMEOUT, ABORTED, ROLLBACK_FAILED → INGESTION.FAIL'. Note ABORTED
        maps to FAIL while CANCELLED (a user abort, not an ingestion outcome) does not.
        """
        from src.shared.events import INGESTION_FAIL

        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"

        datahub.list_execution_requests = AsyncMock(
            return_value=[self._make_exec_request(datahub_status)]
        )
        self._dup_check(db, existing=False)

        count = await service._mirror_execution_requests(source_id, dh_urn)

        assert count == 1, (
            f"Expected 1 event for DataHub status {datahub_status!r}; got {count}."
        )
        db.add.assert_called_once()
        added_event = db.add.call_args[0][0]
        assert added_event.event_type == INGESTION_FAIL, (
            f"Expected event_type={INGESTION_FAIL!r} for {datahub_status!r}; "
            f"got {added_event.event_type!r}."
        )
        assert added_event.status == "failure"

    # ── Skipped (in-progress / non-outcome) statuses ──────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "datahub_status",
        [
            "RUNNING",
            "ROLLING_BACK",
            "UP_FOR_RETRY",
            "CANCELLED",
            "DUPLICATE",
            "ROLLED_BACK",
            "",  # no-status / no-result → also skipped
        ],
    )
    async def test_skipped_statuses_write_no_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        datahub_status: str,
    ) -> None:
        """In-progress (RUNNING / ROLLING_BACK / UP_FOR_RETRY) and non-outcome
        (CANCELLED / DUPLICATE / ROLLED_BACK / no-status) → no Event row written.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) status table — these rows
        are 'not mirrored (in-progress / pending)' or 'not mirrored (not an ingestion
        outcome)'. In particular CANCELLED (formerly coerced to FAIL) now produces no event.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"

        datahub.list_execution_requests = AsyncMock(
            return_value=[self._make_exec_request(datahub_status)]
        )
        self._dup_check(db, existing=False)

        count = await service._mirror_execution_requests(source_id, dh_urn)

        assert count == 0, (
            f"Expected 0 events for DataHub status {datahub_status!r} "
            f"(in-progress / non-outcome); got {count}."
        )
        db.add.assert_not_called()

    # ── occurred_at derivation ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_occurred_at_uses_start_time_when_present(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """occurred_at = startTimeMs (when >0), never now().

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — 'occurred_at =
        startTimeMs when present (> 0), else requestedAt — never now()'.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"

        datahub.list_execution_requests = AsyncMock(
            return_value=[self._make_exec_request("SUCCESS")]
        )
        self._dup_check(db, existing=False)

        before = datetime.now(tz=UTC)
        await service._mirror_execution_requests(source_id, dh_urn)

        added_event = db.add.call_args[0][0]
        expected = datetime.fromtimestamp(self._START_MS / 1000, tz=UTC)
        assert added_event.occurred_at == expected, (
            f"Expected occurred_at derived from startTimeMs ({expected.isoformat()}); "
            f"got {added_event.occurred_at!r}."
        )
        # Guard: must NOT be a fresh now() — the bug this redesign fixes.
        assert abs((added_event.occurred_at - before).total_seconds()) > 60, (
            "occurred_at must derive from startTimeMs, not now()."
        )

    @pytest.mark.asyncio
    async def test_occurred_at_falls_back_to_requested_at(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """occurred_at falls back to requestedAt when startTimeMs is absent/0.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — 'startTimeMs is the
        optional execution start (absent/0 before the executor runs); requestedAt is the
        always-present fallback'.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"

        # SUCCESS but the executor's start time never landed (0) → use requestedAt.
        datahub.list_execution_requests = AsyncMock(
            return_value=[self._make_exec_request("SUCCESS", start_ms=0)]
        )
        self._dup_check(db, existing=False)

        before = datetime.now(tz=UTC)
        await service._mirror_execution_requests(source_id, dh_urn)

        added_event = db.add.call_args[0][0]
        expected = datetime.fromtimestamp(self._REQUESTED_MS / 1000, tz=UTC)
        assert added_event.occurred_at == expected, (
            f"Expected occurred_at derived from requestedAt ({expected.isoformat()}); "
            f"got {added_event.occurred_at!r}."
        )
        assert abs((added_event.occurred_at - before).total_seconds()) > 60, (
            "occurred_at must derive from requestedAt, not now()."
        )

    # ── Idempotency (URN-keyed upsert) ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_same_urn_mirrored_twice_yields_one_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """The SAME execution-request URN across two sync passes → exactly ONE event.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — 'One DataSpoke event per
        execution request, upserted by its URN ... repeated syncs ... are idempotent (no
        per-sync event growth)'. Pass 1: dedup finds nothing → insert. Pass 2: dedup finds
        the existing row → skip.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"
        req = self._make_exec_request("SUCCESS", urn="urn:li:dataHubExecutionRequest:run-1")
        datahub.list_execution_requests = AsyncMock(return_value=[req])

        # Pass 1: no existing event → one insert.
        self._dup_check(db, existing=False)
        count1 = await service._mirror_execution_requests(source_id, dh_urn)
        assert count1 == 1, f"First pass should insert exactly one event; got {count1}."
        assert db.add.call_count == 1

        # Pass 2: dedup query now finds the existing row → no new insert.
        db.add.reset_mock()
        self._dup_check(db, existing=True)
        count2 = await service._mirror_execution_requests(source_id, dh_urn)
        assert count2 == 0, (
            f"Second pass with the same URN must insert nothing (idempotent); got {count2}."
        )
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_zero_start_no_growth_across_syncs(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """CANCELLED with startTimeMs=0 → zero events on every sync, no per-sync growth.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — CANCELLED is 'not mirrored
        (not an ingestion outcome)'. A cancelled-before-start run (startTimeMs=0) must never
        mint an event, and syncing it repeatedly must not grow the event set.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"
        datahub.list_execution_requests = AsyncMock(
            return_value=[
                self._make_exec_request(
                    "CANCELLED",
                    start_ms=0,
                    urn="urn:li:dataHubExecutionRequest:cancelled-1",
                )
            ]
        )
        self._dup_check(db, existing=False)

        for sync_pass in range(3):
            db.add.reset_mock()
            count = await service._mirror_execution_requests(source_id, dh_urn)
            assert count == 0, (
                f"Sync pass {sync_pass}: CANCELLED (startTimeMs=0) must produce zero "
                f"events; got {count}."
            )
            db.add.assert_not_called()

    # ── Mixed batch ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mixed_statuses_terminal_and_skipped_combined(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """Mixed batch: SUCCESS→COMPLETE, RUNNING→skip, FAILURE→FAIL, CANCELLED→skip.

        Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — the status→event mapping
        is applied per-request across the batch; only terminal outcomes are mirrored.
        """
        from src.shared.events import INGESTION_COMPLETE, INGESTION_FAIL

        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:abc"

        datahub.list_execution_requests = AsyncMock(
            return_value=[
                self._make_exec_request("SUCCESS", urn="urn:li:dataHubExecutionRequest:a"),
                self._make_exec_request("RUNNING", urn="urn:li:dataHubExecutionRequest:b"),
                self._make_exec_request("FAILURE", urn="urn:li:dataHubExecutionRequest:c"),
                self._make_exec_request("CANCELLED", urn="urn:li:dataHubExecutionRequest:d"),
            ]
        )
        # No existing events for any URN.
        self._dup_check(db, existing=False)

        count = await service._mirror_execution_requests(source_id, dh_urn)

        # Only SUCCESS and FAILURE produce events; RUNNING and CANCELLED are skipped.
        assert count == 2
        assert db.add.call_count == 2
        added_types = {call[0][0].event_type for call in db.add.call_args_list}
        assert added_types == {INGESTION_COMPLETE, INGESTION_FAIL}


# ── DPI emission contract (run pipeline) ──────────────────────────────────────


def _emitted_aspects(datahub: AsyncMock) -> list[object]:
    """Return the ordered list of aspect objects passed to datahub.emit_aspect.

    The mocked DataHub client records every emission as
    ``emit_aspect(entity_urn, aspect, system_metadata=...)``. The aspect is the
    second positional argument; this returns them in emission order.
    """
    return [call.args[1] for call in datahub.emit_aspect.call_args_list]


def _dpi_properties(datahub: AsyncMock) -> DataProcessInstancePropertiesClass:
    props = [
        a for a in _emitted_aspects(datahub)
        if isinstance(a, DataProcessInstancePropertiesClass)
    ]
    assert len(props) == 1, (
        f"Expected exactly one DataProcessInstanceProperties emission; got {len(props)}."
    )
    return props[0]


def _dpi_outputs(datahub: AsyncMock) -> list[DataProcessInstanceOutputClass]:
    return [
        a for a in _emitted_aspects(datahub)
        if isinstance(a, DataProcessInstanceOutputClass)
    ]


def _patched_run(
    service: IngestionService,
    *,
    entities_ingested: int,
    emitted_urns: list[str],
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    dry_run: bool = False,
    manual: bool = False,
):
    """Drive _run_inner with the extractor and secret resolution stubbed.

    Returns an async context manager wrapping the patches; the caller awaits
    ``service._run_inner(...)`` inside the `with` block. The extractor is forced
    to return a fixed IngestionResult so the DPI emission branches are exercised
    deterministically without a real crawl.
    """
    return patch.multiple(
        "src.backend.ingestion.service",
        resolve_recipe_secrets=MagicMock(side_effect=lambda r: r),
        run_extractor=AsyncMock(
            return_value=IngestionResult(
                entities_ingested=entities_ingested,
                emitted_urns=emitted_urns,
                errors=errors or [],
                warnings=warnings or [],
            )
        ),
    )


class TestDpiEmissionContract:
    """Spec: spec/DATAHUB_INTEGRATION.md §DPI emission contract.

    The mocked DataHub client records emissions on emit_aspect.call_args_list.
    Assertions derive from the contract's required enum values and aspect set,
    not from incidental code constants.
    """

    @pytest.mark.asyncio
    async def test_manual_run_emits_batch_ad_hoc(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """manual=True → DataProcessInstanceProperties.type == BATCH_AD_HOC.

        Spec: §DPI emission contract aspect #1 — 'type = BATCH_AD_HOC for manual
        sources/{id}/method/run'.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=2, emitted_urns=[_DATASET_URN]
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        props = _dpi_properties(datahub)
        assert props.type == DataProcessTypeClass.BATCH_AD_HOC, (
            f"Manual run must emit DPI type=BATCH_AD_HOC; got {props.type!r}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #1."
        )

    @pytest.mark.asyncio
    async def test_scheduled_run_emits_batch_scheduled(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """manual=False (default tier-DAG path) → DPI type == BATCH_SCHEDULED.

        Spec: §DPI emission contract aspect #1 — 'type = BATCH_SCHEDULED for tier runs'.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=2, emitted_urns=[_DATASET_URN]
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=False)

        props = _dpi_properties(datahub)
        assert props.type == DataProcessTypeClass.BATCH_SCHEDULED, (
            f"Scheduled run must emit DPI type=BATCH_SCHEDULED; got {props.type!r}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #1."
        )

    @pytest.mark.asyncio
    async def test_run_default_manual_flag_is_scheduled(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """run() without an explicit manual flag defaults to BATCH_SCHEDULED.

        The tier-DAG internal activity uses the manual=False default; only the
        public sources/{id}/method/run route opts into manual=True.
        Spec: §DPI emission contract aspect #1.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=2, emitted_urns=[_DATASET_URN]
        ):
            # No cache configured on the bare `service` fixture, so run() runs
            # _run_inner directly with its manual default.
            await service.run(str(row.id), dry_run=False)

        props = _dpi_properties(datahub)
        assert props.type == DataProcessTypeClass.BATCH_SCHEDULED, (
            f"run() default must emit DPI type=BATCH_SCHEDULED; got {props.type!r}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #1."
        )

    @pytest.mark.asyncio
    async def test_successful_run_emits_single_output_with_emitted_urns(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """Non-dry-run success with ≥1 emitted URN emits exactly one
        DataProcessInstanceOutput on the DPI URN with outputs == emitted_urns.

        Spec: §DPI emission contract aspect #2b — 'outputs = [<dataset_urn>] …
        what makes the DPI surface in dataset(urn).runs'.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)
        second_urn = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "example_db.catalog.editions,DEV)"
        )
        emitted = [_DATASET_URN, second_urn]

        with _patched_run(service, entities_ingested=2, emitted_urns=emitted):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        outputs = _dpi_outputs(datahub)
        assert len(outputs) == 1, (
            f"Successful non-dry-run with emitted URNs must emit exactly one "
            f"DataProcessInstanceOutput; got {len(outputs)}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b."
        )
        assert outputs[0].outputs == emitted, (
            f"DataProcessInstanceOutput.outputs must equal emitted_urns {emitted}; "
            f"got {outputs[0].outputs}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b."
        )

    @pytest.mark.asyncio
    async def test_output_emitted_on_dpi_urn_with_run_systemmetadata(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The Output aspect is emitted on the DPI URN and carries the run's
        systemMetadata (the same sysmeta reused across the run).

        Spec: §DPI emission contract — the Output aspect is a DPI aspect; and
        §systemMetadata requirement — every emit within a run carries the run's
        non-default systemMetadata, reused across aspects.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=1, emitted_urns=[_DATASET_URN]
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        output_calls = [
            c for c in datahub.emit_aspect.call_args_list
            if isinstance(c.args[1], DataProcessInstanceOutputClass)
        ]
        assert len(output_calls) == 1
        output_call = output_calls[0]
        # Emitted on the DPI URN, not a dataset URN.
        dpi_urn = output_call.args[0]
        assert dpi_urn.startswith("urn:li:dataProcessInstance:"), (
            f"Output aspect must be emitted on the DPI URN; got {dpi_urn!r}. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b "
            "(DPI aspect)."
        )
        # Carries the run's systemMetadata (non-default runId), reused across the run.
        sysmeta = output_call.kwargs.get("system_metadata")
        assert sysmeta is not None, (
            "Output aspect emission must carry the run's systemMetadata. "
            "Spec: DATAHUB_INTEGRATION.md §systemMetadata requirement."
        )
        assert sysmeta.runId and sysmeta.runId != "no-run-id-provided", (
            f"Output aspect systemMetadata.runId must be non-default; got "
            f"{sysmeta.runId!r}. Spec: DATAHUB_INTEGRATION.md §systemMetadata requirement."
        )
        # Same sysmeta object reused across the run's emissions.
        all_sysmetas = {
            id(c.kwargs.get("system_metadata"))
            for c in datahub.emit_aspect.call_args_list
        }
        assert len(all_sysmetas) == 1, (
            "All emissions in a run must reuse one SystemMetadataClass instance. "
            "Spec: DATAHUB_INTEGRATION.md §Conventions adopted by DataSpoke."
        )

    @pytest.mark.asyncio
    async def test_output_emitted_before_terminal_complete_event(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The Output aspect is recorded BEFORE the terminal COMPLETE RunEvent.

        Spec: §DPI emission contract aspect #2b — the Output aspect is emitted
        'after the crawl completes and before the terminal RunEvent'.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=1, emitted_urns=[_DATASET_URN]
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        aspects = _emitted_aspects(datahub)
        output_idx = next(
            i for i, a in enumerate(aspects)
            if isinstance(a, DataProcessInstanceOutputClass)
        )
        # The terminal RunEvent is the COMPLETE one (STARTED precedes the crawl).
        complete_idx = next(
            i for i, a in enumerate(aspects)
            if isinstance(a, DataProcessInstanceRunEventClass)
            and a.status == DataProcessRunStatusClass.COMPLETE
        )
        assert output_idx < complete_idx, (
            f"Output aspect (idx {output_idx}) must precede the terminal COMPLETE "
            f"RunEvent (idx {complete_idx}). "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b — "
            "emitted after the crawl and before the terminal RunEvent."
        )

    @pytest.mark.asyncio
    async def test_dry_run_emits_no_output(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A dry-run emits NO DataProcessInstanceOutput aspect.

        Spec: §DPI emission contract aspect #2b — Output is emitted only on a
        non-dry-run; dry-run skips aspect emission.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=2, emitted_urns=[_DATASET_URN]
        ):
            await service._run_inner(str(row.id), dry_run=True, manual=True)

        assert _dpi_outputs(datahub) == [], (
            "Dry-run must emit no DataProcessInstanceOutput aspect. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b."
        )

    @pytest.mark.asyncio
    async def test_failed_run_emits_no_output(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A failed run (extractor reports errors) emits NO Output aspect.

        Spec: §DPI emission contract aspect #2b — Output is conditioned on
        status=='success'; a failed run still emits the terminal RunEvent but no
        Output.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service,
            entities_ingested=1,
            emitted_urns=[_DATASET_URN],
            errors=["extractor crawl failed"],
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert _dpi_outputs(datahub) == [], (
            "Failed run must emit no DataProcessInstanceOutput aspect. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b."
        )
        # A failed run still emits the terminal COMPLETE RunEvent (failure semantics).
        complete_events = [
            a for a in _emitted_aspects(datahub)
            if isinstance(a, DataProcessInstanceRunEventClass)
            and a.status == DataProcessRunStatusClass.COMPLETE
        ]
        assert len(complete_events) == 1, (
            "A failed run must still emit the terminal COMPLETE RunEvent. "
            "Spec: DATAHUB_INTEGRATION.md §Failure semantics."
        )

    @pytest.mark.asyncio
    async def test_zero_entity_run_emits_no_output(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A non-dry-run that ingests zero entities emits NO Output aspect.

        A zero-entity non-dry-run is treated as failure by the service, so the
        success+non-empty-URN condition for Output emission is never met.
        Spec: §DPI emission contract aspect #2b — Output requires success AND
        non-empty emitted URNs.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(
            service, entities_ingested=0, emitted_urns=[]
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert _dpi_outputs(datahub) == [], (
            "Zero-entity run must emit no DataProcessInstanceOutput aspect. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b — "
            "requires non-empty emitted URNs."
        )
