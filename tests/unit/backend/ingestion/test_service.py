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

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from datahub.metadata.schema_classes import (  # type: ignore
    DataProcessInstanceOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRunEventClass,
    DataProcessRunStatusClass,
    DataProcessTypeClass,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.ingestion.extractors import IngestionResult
from src.backend.ingestion.service import (
    IngestionRunResult,
    IngestionService,
    run_report_detail,
)
from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    EntityNotFoundError,
    PreconditionFailedError,
)
from src.shared.redaction import REDACTED
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
    async def test_emitted_beats_matched(self, service: IngestionService, db: AsyncMock) -> None:
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
    async def test_most_recent_last_seen_at_breaks_a_tie_between_two_regular_sources(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """At equal derivation rank and neither source a wrapper, the newer mapping wins.

        The third and last term of the rank, and the only fixture shape that reaches it:
        both sources are **regular** (``parent_source_id is None``), so the wrapper term is
        equal, and both claim the dataset at ``pipeline_name``, so the derivation term is
        equal too. A parent-versus-its-wrapper fixture cannot substitute — the wrapper term
        decides there before ``last_seen_at`` is consulted.

        The older mapping is first in the result list, so a lookup that dropped the
        ``last_seen_at`` term would keep that order and return the older source; a lookup
        that inverted it would return the older source as well.

        Spec: feature/BACKEND.md §Metrics Service §Time windows — "derivation rank emitted
        > pipeline_name > matched; at equal rank a regular parent beats its CLI wrapper;
        remaining ties go to the most recent last_seen_at."
        """
        now = datetime.now(tz=UTC)

        mapping_older = MagicMock()
        mapping_older.derivation = "pipeline_name"
        mapping_older.last_seen_at = now - timedelta(hours=3)
        mapping_older.dataset_urn = _DATASET_URN

        mapping_newer = MagicMock()
        mapping_newer.derivation = "pipeline_name"
        mapping_newer.last_seen_at = now
        mapping_newer.dataset_urn = _DATASET_URN

        source_older = _make_source_row(name="older-source", schedule_tier="daily")
        source_older.parent_source_id = None
        source_newer = _make_source_row(name="newer-source", schedule_tier="hourly")
        source_newer.parent_source_id = None

        result_mock = MagicMock()
        # Older first: a rank that ignores last_seen_at leaves this order untouched.
        result_mock.all.return_value = [
            (mapping_older, source_older),
            (mapping_newer, source_newer),
        ]
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)

        assert winner is not None, (
            "Backstop: a mapped dataset must resolve to an owner, or the name comparison "
            "below compares against None."
        )
        assert winner.name == "newer-source", (
            f"the most recently seen mapping must win the tie; got {winner.name!r}. "
            "Spec: feature/BACKEND.md §Metrics Service §Time windows — 'remaining ties go "
            "to the most recent last_seen_at'."
        )
        assert winner.schedule_tier == "hourly", (
            "the returned record must be the newer source's row, whose tier is what the "
            f"freshness measurer derives its window from; got {winner.schedule_tier!r}."
        )

    @pytest.mark.asyncio
    async def test_no_mapping_returns_none(self, service: IngestionService, db: AsyncMock) -> None:
        """No source maps the dataset → reverse_lookup returns None."""
        result_mock = MagicMock()
        result_mock.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        winner = await service.reverse_lookup(_DATASET_URN)
        assert winner is None


# ── list_active_sources_for_tier ──────────────────────────────────────────────


class TestListActiveSourcesForTier:
    """Spec: BACKEND.md §DAG Catalogue — returns ACTIVE_CUSTOM_MANAGED sources
    for the given schedule tier.
    """

    @pytest.mark.asyncio
    async def test_returns_active_custom_managed_sources_for_tier(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """list_active_sources_for_tier returns sources matching mode+tier.

        Spec: BACKEND.md §DAG Catalogue — 'Used by the ingestion-active-{hourly,daily,weekly}
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
    """Spec: BACKEND_SCHEMA.md §ingestion_source_dataset — dataset-mapping queries."""

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

        assert count == 1, f"Expected 1 event for DataHub status {datahub_status!r}; got {count}."
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
    @pytest.mark.parametrize("datahub_status", ["FAILURE", "TIMEOUT", "ABORTED", "ROLLBACK_FAILED"])
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

        assert count == 1, f"Expected 1 event for DataHub status {datahub_status!r}; got {count}."
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


# ── The run-history poll is best-effort ──────────────────────────────────────


class TestMirrorExecutionRequestsPollIsBestEffort:
    """A per-source run-history poll that fails skips that source and nothing else.

    This is the one best-effort operation whose fallback is "skip the affected source
    for this hourly tick" — the source contributes no events and the sweep carries on.
    Two consequences are separable, and both are asserted below, because an
    implementation can satisfy either alone:

    - The failure is *contained*: it does not escape the mirror call, so no other source
      loses its events and the sweep still completes.
    - The failure is *not promoted to a fault signal*: it must not flip the
      ``datahub-api`` health row to ``error``, because that row answers a different
      question — whether the sweep's GMS enumeration completed.

    spec: feature/BACKEND.md §Best-Effort Operations — the table row 'DataHub run-history
        poll | IngestionService (sync sweep) | Skip the affected source for this hourly
        tick; retry next tick'.
    spec: feature/BACKEND.md §Best-Effort Operations — 'Failures of the operations listed
        below are logged at WARNING with ``exc_info=True``.'
    spec: feature/BACKEND.md §Sync + mapping sweep — '``ok`` asserts only that the sweep's
        GMS enumeration completed, not that every GMS call inside it succeeded. Per-source
        run-history polls are best-effort ... and a skipped source does not flip the row.'
    """

    @pytest.mark.asyncio
    async def test_a_failed_poll_skips_the_source_and_logs_at_warning(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The poll's failure is swallowed: zero events, no raise, one WARNING carrying it.

        The failure is injected at ``list_execution_requests`` because that is the GMS
        call the spec calls out — a transport fault or an expired PAT on the run-history
        query for one source.

        spec: feature/BACKEND.md §Best-Effort Operations — 'Skip the affected source for
            this hourly tick; retry next tick', logged 'at WARNING with ``exc_info=True``'.
        """
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:poll-fails"
        poll_failure = DataHubUnavailableError("GMS refused the run-history query")

        datahub.list_execution_requests = AsyncMock(side_effect=poll_failure)
        caplog.set_level(logging.DEBUG)

        # No pytest.raises: the point is that nothing escapes. A propagating failure
        # fails the test as an error, which is the correct verdict.
        mirrored = await service._mirror_execution_requests(source_id, dh_urn)

        assert mirrored == 0, (
            f"a source whose run-history poll failed contributes no events this tick; got "
            f"{mirrored}. spec: feature/BACKEND.md §Best-Effort Operations — 'Skip the "
            "affected source for this hourly tick'."
        )
        assert db.add.call_args_list == [], (
            f"a skipped source must write no event rows at all; got "
            f"{db.add.call_args_list!r}. spec: feature/BACKEND.md §Best-Effort Operations."
        )

        # The skip is invisible in the summary — the count simply does not rise — so the
        # log record is the only evidence that a source was dropped this tick.
        carrying = [
            r for r in caplog.records if r.exc_info is not None and r.exc_info[1] is poll_failure
        ]
        assert carrying, (
            f"the swallowed poll failure must reach the log with exc_info, or a source "
            f"silently stops contributing events; captured "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]!r}. "
            "spec: feature/BACKEND.md §Best-Effort Operations — logged 'with "
            "``exc_info=True``'."
        )
        # Non-empty by the assertion above, so this cannot pass vacuously.
        assert {r.levelname for r in carrying} == {"WARNING"}, (
            f"a best-effort poll failure is one of the operations logged at WARNING; got "
            f"{sorted({r.levelname for r in carrying})!r}. spec: feature/BACKEND.md "
            "§Best-Effort Operations — 'Failures of the operations listed below are logged "
            "at WARNING with ``exc_info=True``'; the ERROR level is reserved for a "
            "reporter's own peripheral_health write (§Health reporting)."
        )

    @pytest.mark.asyncio
    async def test_a_failed_poll_does_not_flip_the_datahub_api_row_to_error(
        self,
        datahub: AsyncMock,
        db: AsyncMock,
    ) -> None:
        """A skipped source leaves ``datahub-api`` reading ``ok``.

        The health row answers whether the sweep's GMS enumeration completed, not whether
        every GMS call inside it succeeded. Reporting ``error`` for a single source's
        run-history poll would make the row fire on a condition the operator cannot act
        on, and would mask the fault it exists to surface.

        The sweep body is stood in for here rather than driven whole: only step 4 is under
        test, so the stand-in performs exactly step 4's contribution — await the *real*
        ``_mirror_execution_requests`` and fold its return into ``events_mirrored``. What
        is genuinely exercised is ``sync()``'s outer ``try/except``, which is what decides
        the reported status. Step 4 wraps the mirror call in no ``try`` of its own, so the
        mirror's ``except`` is the sole containment: deleting it makes the failure escape
        the stand-in exactly as it would escape the real sweep. This test then errors on
        the escape rather than reading ``error`` from the row — the row reading ``error``
        alongside a suppressed re-raise is a combination the sibling tests already forbid.
        The assertion below is therefore a boundary statement, not the regression's catcher.

        spec: feature/BACKEND.md §Sync + mapping sweep — 'Per-source run-history polls are
            best-effort ... and a skipped source does not flip the row.'
        spec: feature/BACKEND.md §Health reporting — '``datahub-api`` | ... | ``ok`` on a
            completed sweep; ``error`` on any failure that escapes it'.
        """
        service = IngestionService(datahub=datahub, db=db)
        source_id = str(uuid.uuid4())
        dh_urn = "urn:li:dataHubIngestionSource:poll-fails"
        datahub.list_execution_requests = AsyncMock(
            side_effect=DataHubUnavailableError("GMS refused the run-history query")
        )

        async def _sweep_step_four_only() -> dict[str, int]:
            mirrored = await service._mirror_execution_requests(
                source_id=source_id, datahub_source_urn=dh_urn
            )
            return {"events_mirrored": mirrored}

        service._run_sweep = _sweep_step_four_only  # type: ignore[method-assign]

        reported: list[tuple[str, str, str | None]] = []

        async def _record(_db, name, status, error=None):  # type: ignore[no-untyped-def]
            reported.append((name, status, error))
            return None

        with patch("src.backend.admin.peripheral_health.report_peripheral_health", _record):
            summary = await service.sync()

        assert summary == {"events_mirrored": 0}, (
            f"the sweep completes and the skipped source contributes nothing; got "
            f"{summary!r}. spec: feature/BACKEND.md §Best-Effort Operations — 'Skip the "
            "affected source for this hourly tick; retry next tick'."
        )
        # Backstop: the health report really was attempted. Without it the status
        # assertion below passes on a sweep that reported nothing at all.
        assert len(reported) == 1, (
            f"a completed sweep writes the 'datahub-api' row exactly once; got "
            f"{reported!r}. spec: feature/BACKEND.md §Health reporting."
        )
        assert reported[0][:2] == ("datahub-api", "ok"), (
            f"a best-effort run-history poll failure must not flip 'datahub-api' to "
            f"'error' — the row reports whether the sweep's GMS enumeration completed, "
            f"which it did; got {reported[0][:2]!r} with message {reported[0][2]!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — 'a skipped source does not "
            "flip the row'."
        )


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
        a for a in _emitted_aspects(datahub) if isinstance(a, DataProcessInstancePropertiesClass)
    ]
    assert len(props) == 1, (
        f"Expected exactly one DataProcessInstanceProperties emission; got {len(props)}."
    )
    return props[0]


def _dpi_outputs(datahub: AsyncMock) -> list[DataProcessInstanceOutputClass]:
    return [a for a in _emitted_aspects(datahub) if isinstance(a, DataProcessInstanceOutputClass)]


def _patched_run(
    service: IngestionService,
    *,
    emitted_urns: list[str],
    discovered_urns: list[str] | None = None,
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

    ``discovered_urns`` defaults to ``emitted_urns`` (a clean real run where every
    discovered table emitted ok); pass it explicitly to model a partial emit.
    Spec: API.md §method/run — emitted_urns ⊆ discovered_urns.
    """
    return patch.multiple(
        "src.backend.ingestion.service",
        resolve_recipe_secrets=MagicMock(side_effect=lambda r: r),
        run_extractor=AsyncMock(
            return_value=IngestionResult(
                discovered_urns=emitted_urns if discovered_urns is None else discovered_urns,
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
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
        second_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
        emitted = [_DATASET_URN, second_urn]

        with _patched_run(service, emitted_urns=emitted):
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        output_calls = [
            c
            for c in datahub.emit_aspect.call_args_list
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
            id(c.kwargs.get("system_metadata")) for c in datahub.emit_aspect.call_args_list
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        aspects = _emitted_aspects(datahub)
        output_idx = next(
            i for i, a in enumerate(aspects) if isinstance(a, DataProcessInstanceOutputClass)
        )
        # The terminal RunEvent is the COMPLETE one (STARTED precedes the crawl).
        complete_idx = next(
            i
            for i, a in enumerate(aspects)
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

        with _patched_run(service, emitted_urns=[_DATASET_URN]):
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
            a
            for a in _emitted_aspects(datahub)
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

        with _patched_run(service, emitted_urns=[]):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert _dpi_outputs(datahub) == [], (
            "Zero-entity run must emit no DataProcessInstanceOutput aspect. "
            "Spec: DATAHUB_INTEGRATION.md §DPI emission contract aspect #2b — "
            "requires non-empty emitted URNs."
        )


# ── run_report_detail helper ──────────────────────────────────────────────────


_SECOND_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"


class TestRunReportDetail:
    """Spec: spec/API.md §POST /sources/{id}/method/run — the run-response detail and the
    INGESTION event detail carry the flat discovered/emitted keys.

    run_report_detail builds exactly the four flat keys
    ``discovered_urns`` / ``discovered_urns_count`` / ``emitted_urns`` /
    ``emitted_urns_count`` — counts equal the list lengths and emitted ⊆ discovered.
    """

    def test_returns_exactly_the_four_keys(self) -> None:
        """run_report_detail returns only the four discovered/emitted keys.

        Spec: API.md §method/run — detail carries discovered_urns / discovered_urns_count /
        emitted_urns / emitted_urns_count (dry_run / errors / warnings / run_id / platform
        are added by the callers, not by this helper).
        """
        result = IngestionRunResult(
            run_id="r1",
            status="success",
            dry_run=False,
            discovered_urns=[_DATASET_URN, _SECOND_URN],
            emitted_urns=[_DATASET_URN, _SECOND_URN],
            errors=[],
            warnings=[],
        )
        detail = run_report_detail(result)
        assert set(detail.keys()) == {
            "discovered_urns",
            "discovered_urns_count",
            "emitted_urns",
            "emitted_urns_count",
        }, (
            f"run_report_detail must return exactly the four flat keys; got "
            f"{sorted(detail.keys())}. spec: API.md §method/run."
        )

    def test_counts_match_list_lengths(self) -> None:
        """*_count fields equal the lengths of their lists.

        Spec: API.md §method/run — discovered_urns_count / emitted_urns_count.
        """
        result = IngestionRunResult(
            run_id="r2",
            status="success",
            dry_run=False,
            discovered_urns=[_DATASET_URN, _SECOND_URN],
            emitted_urns=[_DATASET_URN],
            errors=[],
            warnings=[],
        )
        detail = run_report_detail(result)
        assert detail["discovered_urns"] == [_DATASET_URN, _SECOND_URN]
        assert detail["discovered_urns_count"] == 2
        assert detail["emitted_urns"] == [_DATASET_URN]
        assert detail["emitted_urns_count"] == 1

    def test_dry_run_discovers_without_emitting(self) -> None:
        """A dry-run result discovers URNs while emitting none.

        Spec: API.md §method/run — discovered_urns present on a dry-run; emitted_urns
        empty with count 0.
        """
        result = IngestionRunResult(
            run_id="r3",
            status="success",
            dry_run=True,
            discovered_urns=[_DATASET_URN, _SECOND_URN],
            emitted_urns=[],
            errors=[],
            warnings=[],
        )
        detail = run_report_detail(result)
        assert detail["discovered_urns_count"] == 2, (
            "dry-run still discovers. spec: API.md §method/run."
        )
        assert detail["emitted_urns"] == []
        assert detail["emitted_urns_count"] == 0


# ── Zero-emit real run = failure + event detail keys ───────────────────────────


class TestRunZeroEmitFailureAndEventDetail:
    """Spec: spec/API.md §method/run + spec/feature/BACKEND.md §Event Catalogue INGESTION row.

    A real run that emits zero datasets is treated as a failure, and the recorded
    INGESTION event detail carries the flat discovered/emitted report keys.
    """

    @pytest.mark.asyncio
    async def test_real_run_emitting_zero_is_failure(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A non-dry real run with emitted_urns=[] yields status='error'.

        The service's ``not emitted_urns`` check classifies a zero-emit real run as a
        failure even when the extractor reported no errors.
        Spec: feature/BACKEND.md §Active-custom run pipeline — zero-entity non-dry-run is
        treated as failure.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        with _patched_run(service, emitted_urns=[], discovered_urns=[_DATASET_URN]):
            result = await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert result.status == "error", (
            f"A real run that emitted zero datasets must be a failure; got "
            f"status={result.status!r}. spec: feature/BACKEND.md §Active-custom run pipeline."
        )

    @pytest.mark.asyncio
    async def test_run_event_detail_carries_four_report_keys(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The recorded INGESTION event detail carries the four flat discovered/emitted keys.

        Spec: feature/BACKEND.md §Event Catalogue INGESTION row — ACTIVE_CUSTOM_MANAGED run
        detail keys include discovered_urns / discovered_urns_count / emitted_urns /
        emitted_urns_count (plus run_id, platform, dry_run, errors, warnings).
        Spec: API.md §method/run — emitted_urns ⊆ discovered_urns.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        # STRICT superset: emit only A, but discover A and B, so the subset check is
        # discriminating (A ⊊ {A, B}) rather than the vacuous X ⊆ X.
        emitted = [_DATASET_URN]
        discovered = [_DATASET_URN, _SECOND_URN]
        recorded: dict[str, object] = {}

        async def _capture(source_id, event_type, status, detail):  # type: ignore[no-untyped-def]
            recorded["detail"] = detail
            recorded["event_type"] = event_type

        with (
            _patched_run(service, emitted_urns=emitted, discovered_urns=discovered),
            patch.object(service, "_record_source_event", side_effect=_capture),
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        detail = recorded["detail"]
        assert isinstance(detail, dict)
        for key in (
            "discovered_urns",
            "discovered_urns_count",
            "emitted_urns",
            "emitted_urns_count",
        ):
            assert key in detail, (
                f"INGESTION event detail must carry {key!r}. "
                "spec: feature/BACKEND.md §Event Catalogue INGESTION row."
            )
        assert detail["emitted_urns"] == emitted
        assert detail["emitted_urns_count"] == 1
        assert detail["discovered_urns_count"] == 2
        # emitted ⊊ discovered (proper subset): a swapped-field or leak bug would fail
        # both the count check above and this strict-subset check. spec: API.md §method/run.
        emitted_set = set(detail["emitted_urns"])
        discovered_set = set(detail["discovered_urns"])
        assert emitted_set < discovered_set, (
            f"emitted_urns must be a PROPER subset of discovered_urns; "
            f"emitted={detail['emitted_urns']!r} discovered={detail['discovered_urns']!r}. "
            "spec: API.md §method/run."
        )

    @pytest.mark.asyncio
    async def test_partial_emit_real_run_succeeds_with_discriminating_detail(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A real run that discovers 2 and emits 1 succeeds with discriminating detail.

        ``discovered − emitted > 0`` signals per-table emit failures, but the run still
        succeeds because emitted_urns is non-empty (and the extractor reported no errors).
        The recorded INGESTION event detail carries discovered_urns_count=2,
        emitted_urns_count=1, and a proper-subset emitted ⊊ discovered — a discriminating
        partition rather than equal sets.
        Spec: feature/BACKEND.md §Active-custom run pipeline — discovered − emitted > 0 marks
        per-table emit failures while a non-empty emit still succeeds.
        Spec: API.md §method/run — run detail keys; emitted_urns ⊆ discovered_urns.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)

        emitted = [_DATASET_URN]
        discovered = [_DATASET_URN, _SECOND_URN]
        recorded: dict[str, object] = {}

        async def _capture(source_id, event_type, status, detail):  # type: ignore[no-untyped-def]
            recorded["detail"] = detail

        with (
            _patched_run(service, emitted_urns=emitted, discovered_urns=discovered),
            patch.object(service, "_record_source_event", side_effect=_capture),
        ):
            result = await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert result.status == "success", (
            f"A real run with a non-empty emit must succeed even on a partial emit; got "
            f"status={result.status!r}. spec: feature/BACKEND.md §Active-custom run pipeline."
        )

        detail = recorded["detail"]
        assert isinstance(detail, dict)
        assert detail["discovered_urns_count"] == 2, (
            "partial real run discovers 2. spec: API.md §method/run."
        )
        assert detail["emitted_urns_count"] == 1, (
            "partial real run emits 1. spec: API.md §method/run."
        )
        # discovered − emitted > 0 = per-table emit failures, run still succeeds.
        # spec: feature/BACKEND.md §Active-custom run pipeline.
        assert detail["discovered_urns_count"] - detail["emitted_urns_count"] == 1
        assert set(detail["emitted_urns"]) < set(detail["discovered_urns"]), (
            f"emitted_urns must be a PROPER subset of discovered_urns; "
            f"emitted={detail['emitted_urns']!r} discovered={detail['discovered_urns']!r}. "
            "spec: API.md §method/run."
        )


# ── _observe_passive_operations: per-dataset observation identity ─────────────


class TestObservePassiveOperations:
    """Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — 'For PASSIVE sources,
    observe Operation timeseries on mapped datasets.'

    Spec invariant under test (per-dataset observation identity): every dataset mapped to
    a PASSIVE source that has a fresh DataHub Operation yields its OWN passive_observation
    event — a distinct row keyed on its dataset_urn. Two datasets under the same PASSIVE
    source whose Operations share an identical millisecond lastUpdatedTimestamp must each
    produce an event; they must NOT collide into one. The event detail carries
    {dataset_urn, operation_type, source='passive_observation'} and occurred_at is derived
    from the Operation's lastUpdatedTimestamp.

    The success/failure status-string vocabulary is anchored in USE_CASE_en.md §UC1
    API Mapping (BACKEND.md §Sync step 4 maps DataHub status → event type only).
    """

    _ORDERS_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
    )
    _SHIPPING_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)"
    )

    @staticmethod
    def _make_ds_row(source_id: str, dataset_urn: str) -> MagicMock:
        """An IngestionSourceDataset row mapped to the PASSIVE source."""
        row = MagicMock()
        row.source_id = uuid.UUID(source_id)
        row.dataset_urn = dataset_urn
        return row

    @staticmethod
    def _make_op(ts_ms: int | None, op_type: str | None = "INSERT") -> MagicMock:
        """A DataHub Operation timeseries record exposing operationType +
        lastUpdatedTimestamp (the only attributes the sweep reads)."""
        op = MagicMock()
        op.operationType = op_type
        op.lastUpdatedTimestamp = ts_ms
        return op

    @staticmethod
    def _wire_fake_session(
        db: AsyncMock,
        datahub: AsyncMock,
        dataset_rows: list[object],
        ops_by_urn: dict[str, list[object]],
    ) -> list[object]:
        """Wire a fake AsyncSession + DataHub stub for _observe_passive_operations and
        return the list of Event objects passed to db.add().

        In-memory dedup simulation: the fake session dedups by exactly the bind values the
        running code puts in its WHERE clause — so the test adapts to whatever dedup key the
        impl uses (pre-fix: no dataset_urn → collision; fixed: dataset_urn → no collision)
        without pinning to the service's current bytes. State (seen_keys/added_events) lives
        in this closure, so it persists across repeated _observe_passive_operations calls —
        modelling a flushed/committed row visible to a later sweep's dedup read.
        """
        from sqlalchemy.dialects import postgresql

        seen_keys: set[tuple[str, ...]] = set()
        added_events: list[object] = []
        pending: dict[str, tuple[str, ...] | None] = {"key": None}

        def _where_key(stmt: object) -> tuple[str, ...]:
            compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
            return tuple(sorted(str(v) for v in compiled.params.values()))

        async def _fake_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            sql = str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
            if "ingestion_source_dataset" in sql:
                result = MagicMock()
                result.scalars.return_value.all.return_value = dataset_rows
                return result
            # Dedup query on the events table: match iff this WHERE key was already inserted.
            key = _where_key(stmt)
            pending["key"] = key
            result = MagicMock()
            result.scalar_one_or_none.return_value = object() if key in seen_keys else None
            return result

        def _fake_add(obj: object) -> None:
            added_events.append(obj)
            if pending["key"] is not None:
                seen_keys.add(pending["key"])
                pending["key"] = None

        datahub.get_timeseries = AsyncMock(
            side_effect=lambda urn, *args, **kwargs: ops_by_urn.get(urn, [])
        )
        db.execute = AsyncMock(side_effect=_fake_execute)
        db.add = MagicMock(side_effect=_fake_add)
        return added_events

    @staticmethod
    def _passive(added_events: list[object]) -> list[object]:
        """Filter to the passive_observation events among db.add() calls."""
        return [
            e
            for e in added_events
            if getattr(e, "detail", {}).get("source") == "passive_observation"
        ]

    @pytest.mark.asyncio
    async def test_two_datasets_sharing_occurred_at_yield_two_events(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """Two PASSIVE-mapped datasets with Operations at the SAME millisecond timestamp
        produce TWO distinct passive_observation events — one per dataset_urn.

        Spec: spec/feature/BACKEND.md §Sync step 4 — per-dataset Operation observation.
        Asserts the spec invariant (each mapped dataset with a fresh Operation gets its own
        observation event), not the dedup query's internals. The shared occurred_at is the
        regression trigger: a dedup key that omits dataset_urn would drop the second event,
        leaving 1 — so this asserts 2 to fail on that collision and pass on the fix.
        """
        from src.shared.events import INGESTION_COMPLETE

        source_id = str(uuid.uuid4())
        dataset_rows = [
            self._make_ds_row(source_id, self._ORDERS_URN),
            self._make_ds_row(source_id, self._SHIPPING_URN),
        ]
        # Both Operations carry the IDENTICAL millisecond lastUpdatedTimestamp — the
        # collision condition. operationType=INSERT is an ingestion-class op.
        shared_ts_ms = 1_700_000_000_000
        ops_by_urn = {
            self._ORDERS_URN: [self._make_op(shared_ts_ms)],
            self._SHIPPING_URN: [self._make_op(shared_ts_ms)],
        }
        added_events = self._wire_fake_session(db, datahub, dataset_rows, ops_by_urn)

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 2, (
            "Two datasets mapped to one PASSIVE source, each with a fresh Operation at the "
            f"same millisecond, must yield two passive_observation events; got {inserted}. "
            "A dedup key omitting dataset_urn collapses them into one. "
            "Spec: spec/feature/BACKEND.md §Sync step 4."
        )

        passive = self._passive(added_events)
        observed_urns = {e.detail["dataset_urn"] for e in passive}
        assert observed_urns == {self._ORDERS_URN, self._SHIPPING_URN}, (
            "Each mapped dataset must get its own passive_observation event keyed on its "
            f"dataset_urn; observed {observed_urns!r}. Spec: spec/feature/BACKEND.md §Sync step 4."
        )
        for e in passive:
            assert e.event_type == INGESTION_COMPLETE
            assert e.status == "success"
            assert e.detail["operation_type"] == "INSERT"
            # occurred_at derived from the Operation's lastUpdatedTimestamp, not now().
            assert e.occurred_at == datetime.fromtimestamp(shared_ts_ms / 1000, tz=UTC)

    @pytest.mark.asyncio
    async def test_non_ingestion_operation_types_yield_no_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """Operations whose operationType is not an ingestion-class type (e.g. 'DROP', or
        absent/None) produce NO passive_observation event and NO commit.

        Spec: spec/feature/BACKEND.md §Sync step 4 — only ingestion-class Operations
        (INSERT/UPDATE/CREATE/ALTER) are observed as ingestion-completion signals; a DROP or
        a typeless Operation is not an ingestion outcome.
        """
        source_id = str(uuid.uuid4())
        dataset_rows = [self._make_ds_row(source_id, self._ORDERS_URN)]
        ops_by_urn = {
            self._ORDERS_URN: [
                self._make_op(1_700_000_000_000, op_type="DROP"),
                self._make_op(1_700_000_000_500, op_type=None),
            ]
        }
        added_events = self._wire_fake_session(db, datahub, dataset_rows, ops_by_urn)

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 0, (
            f"Non-ingestion Operations (DROP / typeless) must mint no events; got {inserted}. "
            "Spec: spec/feature/BACKEND.md §Sync step 4."
        )
        assert self._passive(added_events) == []
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_timeseries_yields_no_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """A mapped dataset whose Operation timeseries is empty produces NO event, NO commit.

        Spec: spec/feature/BACKEND.md §Sync step 4 — a dataset with no observed Operation has
        nothing to mirror.
        """
        source_id = str(uuid.uuid4())
        dataset_rows = [self._make_ds_row(source_id, self._ORDERS_URN)]
        ops_by_urn: dict[str, list[object]] = {self._ORDERS_URN: []}
        added_events = self._wire_fake_session(db, datahub, dataset_rows, ops_by_urn)

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 0, (
            f"An empty Operation timeseries must mint no events; got {inserted}. "
            "Spec: spec/feature/BACKEND.md §Sync step 4."
        )
        assert self._passive(added_events) == []
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_operation_across_two_sweeps_yields_one_event(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
    ) -> None:
        """The SAME Operation surfacing on two consecutive sweeps yields exactly ONE event
        for that (source, dataset_urn, occurred_at) — no per-sweep growth.

        This is the positive-dedup complement of the collision test: the (source, dataset
        URN, occurred_at) triple is the observation identity, so a repeat sweep over an
        unchanged Operation must not append a duplicate. Sweep 2's dedup read sees sweep 1's
        persisted row (modelled by the shared in-memory store).

        Spec: spec/feature/BACKEND.md §Sync step 4 — one passive_observation event per
        (source, mapped dataset URN, Operation timestamp).
        """
        source_id = str(uuid.uuid4())
        ts_ms = 1_700_000_000_000
        dataset_rows = [self._make_ds_row(source_id, self._ORDERS_URN)]
        ops_by_urn = {self._ORDERS_URN: [self._make_op(ts_ms)]}
        added_events = self._wire_fake_session(db, datahub, dataset_rows, ops_by_urn)

        # Sweep 1: the Operation is observed for the first time → one event.
        inserted_1 = await service._observe_passive_operations(source_id)
        assert inserted_1 == 1, (
            f"First sweep over a fresh Operation must mint exactly one event; got {inserted_1}."
        )

        # Sweep 2: the SAME Operation (same source/URN/timestamp) → dedup finds the first
        # sweep's row → no new event.
        inserted_2 = await service._observe_passive_operations(source_id)
        assert inserted_2 == 0, (
            f"Re-observing the same Operation must mint no further event (idempotent); "
            f"got {inserted_2}. Spec: spec/feature/BACKEND.md §Sync step 4 — one event per "
            "(source, dataset URN, occurred_at)."
        )

        passive = self._passive(added_events)
        assert len(passive) == 1, (
            f"Exactly one passive_observation event must exist for the URN across both sweeps; "
            f"got {len(passive)}. No per-sweep growth."
        )
        assert passive[0].detail["dataset_urn"] == self._ORDERS_URN
        assert passive[0].occurred_at == datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


# ── sync(): the datahub-api health side effect ────────────────────────────────


class TestSyncReportsApiHealth:
    """``sync()`` reports the ``datahub-api`` peripheral health as a side effect.

    The persisted row and its two-session independence are covered against real
    PostgreSQL in ``tests/integration/spot/test_datahub_api_health.py``. What this class
    covers is the part a unit test can prove better: which status is reported for which
    outcome, that the failure is re-raised, that the reported *message* carries neither a
    credential nor a stack trace, and **which database the report is aimed at** — a
    property the integration tier cannot discriminate, because there the caller's engine
    and the module-level one address the same cluster.

    spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "``ok`` on
        completion, ``error`` carrying the message on failure — which is then re-raised";
        "The ``error`` branch catches broadly — any failure that escapes the sweep, not
        only ``DataHubUnavailableError``".
    spec: feature/BACKEND.md §Health reporting — "no credentials, no stack traces, and a
        length bound".
    """

    class _PassThroughSanitize:
        """A DataHub client whose ``sanitize`` holds no matching credential.

        Real behaviour for a client whose PAT does not appear in the message: the text
        comes back unchanged. Used as the default so the message assertions below read the
        text the reporter would actually store, rather than a bare ``MagicMock`` repr.
        """

        def sanitize(self, message: str) -> str:
            return message

    @classmethod
    def _service(
        cls,
        sweep_raises: BaseException | None = None,
        datahub: object | None = None,
        db: object | None = None,
    ):
        service = IngestionService(
            datahub=datahub if datahub is not None else cls._PassThroughSanitize(),  # type: ignore[arg-type]
            db=db if db is not None else AsyncMock(spec=AsyncSession),  # type: ignore[arg-type]
        )

        async def _run_sweep() -> dict[str, int]:
            if sweep_raises is not None:
                raise sweep_raises
            return {"sources_synced": 1}

        service._run_sweep = _run_sweep  # type: ignore[method-assign]
        return service

    @pytest.mark.asyncio
    async def test_completed_sweep_reports_ok_with_no_message(self) -> None:
        """A sweep that completes reports ``ok`` and returns its summary unchanged.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "``ok`` on
        completion".
        """
        service = self._service()
        reports: list[tuple[str, str | None]] = []

        async def _report(status: str, error: str | None = None) -> None:
            reports.append((status, error))

        service._report_api_health = _report  # type: ignore[method-assign]

        summary = await service.sync()

        assert summary == {"sources_synced": 1}, "the sweep's summary must pass through"
        assert reports == [("ok", None)], (
            f"a completed sweep must report exactly ('ok', None); got {reports}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "exc"),
        [
            ("DataSpoke transport error", DataHubUnavailableError("GMS unreachable")),
            # A rotated PAT takes DataHubClient's 401/403 fail-fast path and escapes as a
            # raw SDK exception, never as DataHubUnavailableError. A narrow catch here
            # would leave api_health reading 'ok' through a dead credential.
            ("raw 401 from a revoked PAT", RuntimeError("401 Client Error: Unauthorized")),
            ("a database fault, not a GMS one", ValueError("current transaction is aborted")),
        ],
    )
    async def test_any_escaping_failure_reports_error_and_is_re_raised(
        self, label: str, exc: BaseException
    ) -> None:
        """Every exception escaping the sweep reports ``error`` and then propagates.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "The
        ``error`` branch catches broadly — any failure that escapes the sweep, not only
        ``DataHubUnavailableError``"; "The accepted trade-off: a non-GMS failure escaping
        the sweep (a database error, say) also flips the row."
        """
        service = self._service(sweep_raises=exc)
        reports: list[tuple[str, str | None]] = []

        async def _report(status: str, error: str | None = None) -> None:
            reports.append((status, error))

        service._report_api_health = _report  # type: ignore[method-assign]

        with pytest.raises(type(exc)) as raised:
            await service.sync()

        assert raised.value is exc, (
            f"{label}: the original exception must be re-raised unchanged so the activity "
            "endpoint answers as it would have. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
        assert len(reports) == 1 and reports[0][0] == "error", (
            f"{label}: the failure must be reported as 'error'; got {reports}."
        )
        assert reports[0][1] and str(exc) in reports[0][1], (
            f"{label}: the report must carry the failure's own message; got {reports[0][1]!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "
            "'``error`` carrying the message on failure'."
        )
        # Deliberately not asserted here: how the message is *formatted*, or whether the
        # exception class appears in it. The spec requires only that the report carry "the
        # message"; a reporter that rendered it as "GMS unreachable (DataHubUnavailableError)"
        # would satisfy the contract just as well. The one place the layout is pinned is
        # ``test_describe_failure_reports_no_stack_trace``, which says why.

    def test_describe_failure_routes_through_the_clients_own_sanitizer(self) -> None:
        """``_describe_failure`` calls ``DataHubClient.sanitize`` when the client has it.

        Only the client holds the live PAT, so only it can scrub by exact value — and the
        401/403 fail-fast path re-raises the SDK's own exception, which therefore never
        crossed the client's boundary scrub on the way here.

        spec: feature/BACKEND.md §Health reporting — "A reporter that holds the live
        credential (the event consumer, ``DataHubClient``) additionally scrubs it by exact
        value before calling".
        """
        seen: list[str] = []

        class _DataHubWithSanitize:
            def sanitize(self, message: str) -> str:
                seen.append(message)
                # The marker is imported from the production module rather than spelled
                # out: no spec names a marker string, so the property under test is that
                # the sanitizer's *result* is what gets reported, not which literal it
                # substitutes.
                return message.replace("pat-abc123def456ghi", REDACTED)

        service = self._service(datahub=_DataHubWithSanitize())

        described = service._describe_failure(
            RuntimeError("401 Unauthorized while presenting pat-abc123def456ghi")
        )

        assert seen, (
            "the client's sanitize must be called — this is the only path that scrubs the "
            "live PAT by exact value. "
            "spec: feature/BACKEND.md §Health reporting."
        )
        assert "pat-abc123def456ghi" not in described, (
            f"the sanitizer's result must be what is reported; got {described!r}"
        )
        assert REDACTED in described and "401 Unauthorized" in described

    def test_describe_failure_survives_a_client_without_a_sanitizer(self) -> None:
        """A DataHub client carrying no ``sanitize`` still yields a usable message.

        Health reporting must never be the thing that raises; the pattern layer at
        ``report_peripheral_health`` still applies to whatever comes out. What is asserted
        is the spec'd property — the exception's own message survives — not the layout it
        is rendered in, which no spec fixes.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "``error``
        carrying the message on failure".
        """
        service = self._service(datahub=object())
        exc = ValueError("boom")

        described = service._describe_failure(exc)

        assert str(exc) in described, (
            f"the failure's own message must survive a client with no sanitizer; got {described!r}."
        )
        for marker in ("Traceback", 'File "'):
            assert marker not in described, (
                f"a stack-trace marker ({marker!r}) must not appear; got {described!r}. "
                "spec: feature/BACKEND.md §Health reporting — 'no stack traces'."
            )

    def test_describe_failure_reports_no_stack_trace(self) -> None:
        """The reported message carries the exception, not its traceback.

        A traceback would put file paths and source lines into a column an Admin reads
        back over HTTP, and the sanitizer would not help: it collapses a traceback onto
        one line rather than dropping frames.

        **This is the one place the message layout is pinned by exact equality, and that is
        deliberate.** "No stack traces" is an absence claim, and the marker list below can
        only rule out the shapes it happens to enumerate — a frame rendered without the word
        ``Traceback`` would slip past it. Equality is the strongest available way to say
        "the output is the exception and nothing else": any extra content at all, framed
        however, fails. The cost is that a spec-conformant reformat of ``_describe_failure``
        fails here too; that is accepted at exactly one site, and the other
        ``_describe_failure`` tests assert containment instead so a reformat is a one-line
        update rather than a suite-wide one.

        spec: feature/BACKEND.md §Health reporting — a persisted message carries "no
        credentials, no stack traces, and a length bound".
        """
        service = self._service()
        try:
            raise RuntimeError("GMS said no")
        except RuntimeError as exc:
            # Backstop: the exception really has a traceback to leak, so the absence
            # assertions below have a subject.
            assert exc.__traceback__ is not None
            described = service._describe_failure(exc)

        for marker in ("Traceback", 'File "', "line ", __file__):
            assert marker not in described, (
                f"a stack-trace marker ({marker!r}) must not reach the health row; got "
                f"{described!r}. spec: feature/BACKEND.md §Health reporting."
            )
        assert described == "RuntimeError: GMS said no", (
            f"the output must be the exception and nothing else — no frame, no path, no "
            f"trailing context; got {described!r}. If ``_describe_failure`` is reformatted "
            f"deliberately, update this expectation: it is the layout pin, and the sibling "
            f"tests assert containment precisely so that this is the only site to touch."
        )

    # ── which database the report is written to ───────────────────────────────
    #
    # The spec fixes *that* the report is committed independently of the sweep's
    # transaction (feature/BACKEND.md §Sync + mapping sweep §Health side effect) and
    # *that* the sweep is the writer of the ``datahub-api`` row (§Health reporting).
    # Independence alone is satisfiable by a session on some other database entirely —
    # in which case no row the sweep's caller can read is ever written, and the
    # swallowing ``except`` below makes that indistinguishable from success. The two
    # tests here hold both halves at once: a session of its own, on the database the
    # caller handed the service.
    #
    # Both drive the real ``_report_api_health`` and stub only ``report_peripheral_health``
    # — the seam is the row writer, not the session plumbing under test. Engines are never
    # connected to: SQLAlchemy defers connection until a statement runs, and no statement
    # does.

    @staticmethod
    def _unconnected_engine(host: str):
        """An ``AsyncEngine`` that is never connected to — only its identity is used."""
        return create_async_engine(f"postgresql+asyncpg://u:p@{host}:5432/d")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "sweep_raises", "expected_status"),
        [
            ("completed sweep", None, "ok"),
            # The failing leg matters most: this is the report that has to outlive the
            # re-raise, so it is the one whose loss is silent.
            ("failing sweep", DataHubUnavailableError("GMS unreachable"), "error"),
        ],
    )
    async def test_the_report_goes_to_the_callers_database_on_a_session_of_its_own(
        self, label: str, sweep_raises: BaseException | None, expected_status: str
    ) -> None:
        """The report is written on a **new** session bound to the **injected** session's engine.

        Two properties, asserted together because either alone is satisfiable by a
        reporter that does the wrong thing:

        - *A session of its own* — the ``error`` report is written while an exception is
          unwinding and must not ride the sweep's transaction.
        - *The caller's engine* — a session on a module-level factory is bound at import
          time to the app-runtime connection settings, which a caller that injected a
          session built elsewhere (a host-side sweep through a forwarded port) does not
          share. The write would then land in a different database than every other
          statement of the same call — or nowhere — and the swallowing ``except`` in the
          reporter makes that outcome look exactly like success.

        A distinct fallback engine is installed as ``SessionLocal`` for the duration, so
        "the caller's engine was used" is a discriminating reading rather than the only
        engine in the process.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — 'The
            ``error`` report is committed independently of the sweep's transaction.
            ... Which database that independent write lands on is governed by §Shared
            Services (PostgreSQL row).'
        spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — such a write 'opens a
            session from a factory built on the **bind of the injected session**, so it
            reaches the database the caller is actually using'; aimed at the module-level
            factory instead it 'would otherwise be aimed at a different address than every
            other statement in the same call, with no diagnostic distinguishing that from
            success'.
        spec: feature/BACKEND.md §Health reporting — '``datahub-api`` | Metadata API (GMS
            REST / GraphQL) | the hourly sync + mapping sweep': the sweep is the writer of
            that row, so the row must land where the sweep's caller reads it.
        """
        callers_engine = self._unconnected_engine("callers-db")
        fallback_engine = self._unconnected_engine("module-level-db")
        injected = async_sessionmaker(callers_engine, class_=AsyncSession, expire_on_commit=False)()
        seen: list[tuple[object, str, str, str | None]] = []

        async def _record(db, name, status, error=None):  # type: ignore[no-untyped-def]
            seen.append((db, name, status, error))
            return None

        service = self._service(sweep_raises=sweep_raises, db=injected)
        try:
            with (
                patch(
                    "src.backend.admin.peripheral_health.report_peripheral_health",
                    _record,
                ),
                patch(
                    "src.shared.db.session.SessionLocal",
                    async_sessionmaker(
                        fallback_engine, class_=AsyncSession, expire_on_commit=False
                    ),
                ),
            ):
                if sweep_raises is None:
                    await service.sync()
                else:
                    with pytest.raises(type(sweep_raises)):
                        await service.sync()
        finally:
            await injected.close()
            await callers_engine.dispose()
            await fallback_engine.dispose()

        # Backstop: the reporter really reached the row writer. Without this the three
        # assertions below would pass vacuously on a reporter that wrote nothing at all
        # — which is precisely the defect they exist to catch, since the reporter's
        # ``except`` swallows the failure that would otherwise announce it.
        assert len(seen) == 1, (
            f"{label}: the sweep must call the health-row writer exactly once; got "
            f"{len(seen)} call(s). spec: feature/BACKEND.md §Health reporting."
        )
        reported_db, name, status, _error = seen[0]
        assert (name, status) == ("datahub-api", expected_status), (
            f"{label}: the sweep writes the 'datahub-api' row with status "
            f"{expected_status!r}; got {(name, status)!r}. "
            "spec: feature/BACKEND.md §Health reporting."
        )
        assert reported_db is not injected, (
            f"{label}: the report must be written on a session distinct from the sweep's, "
            "so it survives the sweep's transaction unwinding. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
        assert reported_db.bind is callers_engine, (
            f"{label}: the report must be written against the engine the caller's session "
            f"is bound to ({callers_engine!r}), so it reaches the database the sweep is "
            f"actually operating on; it went to {reported_db.bind!r}. "
            "spec: feature/BACKEND.md §Health reporting — the sweep is the writer of the "
            "'datahub-api' row."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "db"),
        [
            # An AsyncMock(spec=AsyncSession) exposes no `bind`: it is not in
            # dir(AsyncSession). This is the shape every other unit test in this class
            # injects, so the fallback is what keeps them off a nonsense factory.
            ("a session mock carrying no bind at all", AsyncMock(spec=AsyncSession)),
            ("a session whose bind is None", MagicMock(spec_set=["bind"], bind=None)),
            # A *sync* Engine, or anything else that is not an AsyncEngine, cannot build
            # an async factory: degrade rather than raise out of the reporter.
            (
                "a session bound to something that is not an AsyncEngine",
                MagicMock(spec_set=["bind"], bind=object()),
            ),
        ],
    )
    async def test_a_session_with_no_usable_engine_falls_back_without_raising(
        self, label: str, db: object
    ) -> None:
        """An injected session that offers no usable engine degrades to the module-level factory.

        The fallback is the only address available in that case, and reporting must never
        be the thing that breaks the sweep: the sweep's own summary still returns.

        spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — 'A session with no
            usable bind falls back to the module-level factory, the only address available
            in that case.'
        spec: feature/BACKEND.md §Health reporting — '``datahub-api`` | … | the hourly
            sync + mapping sweep': the sweep is the writer of that row, so there is no
            shape of injected session for which it stops trying to write it.
        spec: feature/BACKEND.md §Sync + mapping sweep — the health report 'is a side
            effect of the sweep, not a step of the pipeline above', so it never changes
            the sweep's outcome.
        """
        fallback_engine = self._unconnected_engine("module-level-db")
        seen: list[tuple[object, str, str, str | None]] = []

        async def _record(db_, name, status, error=None):  # type: ignore[no-untyped-def]
            seen.append((db_, name, status, error))
            return None

        service = self._service(db=db)
        try:
            with (
                patch(
                    "src.backend.admin.peripheral_health.report_peripheral_health",
                    _record,
                ),
                patch(
                    "src.shared.db.session.SessionLocal",
                    async_sessionmaker(
                        fallback_engine, class_=AsyncSession, expire_on_commit=False
                    ),
                ),
            ):
                summary = await service.sync()
        finally:
            await fallback_engine.dispose()

        assert summary == {"sources_synced": 1}, (
            f"{label}: the sweep's summary must pass through untouched — health reporting "
            f"is a side effect, never a step of the pipeline; got {summary!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep."
        )
        assert len(seen) == 1, (
            f"{label}: the sweep must still attempt the 'datahub-api' report through the "
            f"module-level factory; the row writer was called {len(seen)} time(s). "
            "spec: feature/BACKEND.md §Health reporting."
        )
        reported_db, name, status, _error = seen[0]
        assert (name, status) == ("datahub-api", "ok"), (
            f"{label}: the sweep writes the 'datahub-api' row with status 'ok'; got "
            f"{(name, status)!r}. spec: feature/BACKEND.md §Health reporting."
        )
        assert reported_db.bind is fallback_engine, (
            f"{label}: with no usable engine on the injected session the report must go "
            f"through the module-level factory; it went to {reported_db.bind!r}."
        )

    @pytest.mark.asyncio
    async def test_reading_the_injected_sessions_bind_cannot_break_the_sweep(self) -> None:
        """A session whose ``bind`` raises on access is swallowed like any other report failure.

        This is what makes the placement of the engine derivation observable. The
        derivation reads ``self._db.bind`` and builds the factory *inside*
        ``_report_api_health``'s ``try``; hoisted above it, the read below escapes the
        reporter. On the success leg that turns a completed sweep into a failed one, and
        on the failing leg it substitutes itself for the sweep's own exception — the
        re-raise the spec requires would never run.

        The shape is deliberately synthetic: no production caller can produce it, because
        every injected session comes from an ``AsyncEngine``-bound sessionmaker. It exists
        so the try-scope is pinned by something rather than by nothing.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — the report
            'is a side effect of the sweep, not a step of the pipeline above', so no shape
            of injected session may propagate out of the reporter.
        """

        class _BindRaises:
            """Stands in for a session whose ``bind`` is unreadable (e.g. detached)."""

            @property
            def bind(self) -> object:
                raise RuntimeError("session is detached")

        # Stubbed so that if the implementation does reach the writer under this shape,
        # it reaches a harmless one rather than a real session.
        async def _record(_db, _name, _status, error=None):  # type: ignore[no-untyped-def]
            return None

        service = self._service(db=_BindRaises())
        with patch("src.backend.admin.peripheral_health.report_peripheral_health", _record):
            summary = await service.sync()

        # The sweep's outcome is the whole assertion. Whether the row is written under
        # this shape is deliberately left open: an implementation that caught the
        # unreadable bind and still reported through the module-level factory would also
        # satisfy the spec — §Health reporting names the sweep as the row's writer — so
        # pinning ``seen`` here would fail a conformant alternative rather than a
        # regression. The hoist mutation this test exists for is caught above, where the
        # escaping RuntimeError fails ``await service.sync()`` outright.
        assert summary == {"sources_synced": 1}, (
            f"a bind that raises must not change the sweep's outcome; got {summary!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "sweep_raises"),
        [
            ("completed sweep", None),
            # The failing leg is the load-bearing one: the reporter is invoked from
            # inside ``except Exception as exc:``, so a reporter that raised would
            # *replace* the sweep's exception and the spec'd re-raise would never run.
            ("failing sweep", DataHubUnavailableError("GMS unreachable")),
        ],
    )
    async def test_a_health_row_write_that_raises_never_changes_the_sweeps_outcome(
        self, label: str, sweep_raises: BaseException | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A row writer that raises is swallowed: the sweep's own outcome is unchanged.

        This pins the ``except Exception`` in ``_report_api_health``, which is not
        decoration. ``sync()`` calls the reporter from inside its own
        ``except Exception as exc:`` clause, immediately before ``raise``. A reporter that
        let a DB fault escape would therefore substitute its own exception for the sweep's
        and the spec'd re-raise — the thing that makes the activity endpoint answer with a
        *retryable* failure — would never execute. On the success leg the same escape
        would turn a completed sweep into a failed one.

        The failure is injected at the row writer (``report_peripheral_health``) because
        that is where a real one lands: an unreachable database, or a schema predating the
        ``datahub-api`` name in the ``ck_peripheral_health_name`` constraint.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — '``ok`` on
            completion, ``error`` carrying the message on failure — which is then
            re-raised, so the activity endpoint still answers with a retryable failure.'
        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — 'This is a
            side effect of the sweep, not a step of the pipeline above.'
        """
        callers_engine = self._unconnected_engine("callers-db")
        injected = async_sessionmaker(callers_engine, class_=AsyncSession, expire_on_commit=False)()
        attempts: list[str] = []
        reporter_failure = RuntimeError("pg down")

        async def _boom(_db, _name, status, error=None):  # type: ignore[no-untyped-def]
            attempts.append(status)
            raise reporter_failure

        service = self._service(sweep_raises=sweep_raises, db=injected)
        # Root, not the reporter's module: the identity assertion below already
        # discriminates against records from other loggers, and pinning the module path
        # here would silently stop guarding the demotion case if the reporter ever moves.
        caplog.set_level(logging.DEBUG)
        try:
            with patch("src.backend.admin.peripheral_health.report_peripheral_health", _boom):
                if sweep_raises is None:
                    summary = await service.sync()
                    assert summary == {"sources_synced": 1}, (
                        f"{label}: a report that raises must not turn a completed sweep "
                        f"into a failed one; got {summary!r}. "
                        "spec: feature/BACKEND.md §Sync + mapping sweep §Health side "
                        "effect — the report is a side effect, not a step of the pipeline."
                    )
                else:
                    with pytest.raises(Exception) as raised:  # noqa: B017 — identity asserted below
                        await service.sync()
                    assert raised.value is sweep_raises, (
                        f"{label}: the sweep's own exception must be what propagates, so "
                        f"the activity endpoint answers with a retryable failure; got "
                        f"{raised.value!r}. A failing reporter must not substitute its "
                        "own. spec: feature/BACKEND.md §Sync + mapping sweep §Health side "
                        "effect."
                    )
        finally:
            await injected.close()
            await callers_engine.dispose()

        # Backstop: the writer really was reached and really did raise. Without it both
        # legs above pass on a reporter that never called the writer at all — the exact
        # shape the swallowing ``except`` makes invisible.
        assert attempts == ["error" if sweep_raises is not None else "ok"], (
            f"{label}: the row writer must have been called once with the outcome's "
            f"status and raised from there; got {attempts!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )

        # A swallowed report is invisible by construction: the row keeps whatever it held,
        # which reads identically to "no reporter deployed". The log record is the only
        # evidence that a reporter ran and failed, so the swallow is only safe while it
        # stays observable — a silent ``except: pass`` would pass every assertion above.
        # The level is part of that contract, not a stylistic choice: the operations that
        # log at WARNING each leave an in-band fallback behind, and a lost health write
        # leaves nothing, so it is the one best-effort failure that reports at ERROR.
        #
        # spec: feature/BACKEND.md §Health reporting — a reporter's own write failure is
        #     swallowed "and logged at ``ERROR`` with ``exc_info=True``"; "The log record
        #     is then the only evidence that a deployed reporter is running and failing."
        # spec: feature/BACKEND.md §Best-Effort Operations — the WARNING level covers
        #     "the operations listed below"; "a reporter's failure to write its own
        #     ``peripheral_health`` row falls outside this set and is logged at ``ERROR``".
        swallowed = [r for r in caplog.records if r.exc_info is not None]
        assert swallowed, (
            f"{label}: the swallowed report failure must reach the log with exc_info, or a "
            f"reporter that is running and failing leaves no evidence at all; captured "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]!r}."
        )
        assert any(r.exc_info[1] is reporter_failure for r in swallowed), (  # type: ignore[index]
            f"{label}: the logged cause must be the reporter's own failure, not some other "
            "exception that happened to be in flight."
        )
        # Non-empty by the assertion above, so this cannot pass vacuously.
        reported_levels = {
            r.levelname
            for r in swallowed
            if r.exc_info[1] is reporter_failure  # type: ignore[index]
        }
        assert reported_levels == {"ERROR"}, (
            f"{label}: the reporter's own write failure must be logged at ERROR, not at the "
            f"WARNING level the best-effort operations use; got {sorted(reported_levels)!r}. "
            "A WARNING is filtered out of the default operational log exactly where the row "
            "it would have explained is stuck reading `unknown`. "
            "spec: feature/BACKEND.md §Health reporting."
        )
