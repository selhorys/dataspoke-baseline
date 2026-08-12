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
from typing import Any
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
            "at WARNING with ``exc_info=True``'. ERROR belongs to the two carve-outs that "
            "section names — a reporter's own peripheral_health write and the "
            "api_tokens.last_used_at stamp — and this poll is neither; per §Health "
            "reporting the WARNING operations 'sit outside that surface: their failure "
            "degrades a single operation, not the operator's view of the system.'"
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


# ── Observed instants: the _observed_at bounds ────────────────────────────────


def _to_ms(moment: datetime) -> int:
    """Epoch milliseconds for *moment*, the shape DataHub stores every instant in."""
    return int(moment.timestamp() * 1000)


class TestObservedAtBounds:
    """``_observed_at`` converts a writer-supplied epoch-millisecond value, or rejects it.

    Every instant this function sees is writer-supplied on the MCP that wrote it —
    ``Operation.lastUpdatedTimestamp``, the ``systemMetadata`` scan behind
    ``Dataset.lastIngested``, and the execution request's ``startTimeMs`` /
    ``requestedAt`` — so as far as DataSpoke is concerned the value is arbitrary JSON
    and a malformed one must cost that one observation and nothing more.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "the observed millisecond
        timestamp must be a positive integer resolving to a representable instant no
        later than a small skew allowance past now. A value that is absent, zero,
        non-numeric, negative, out of range, or **future-dated** books nothing and is
        logged."
    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "Neither producer ever falls
        back to ``now()``, and neither clamps."
    spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "a null, non-numeric,
        non-positive, out-of-range, or beyond-a-small-future-skew value is **rejected,
        never clamped and never defaulted to ``now()``**. Clamping or defaulting
        fabricates an instant DataHub never reported."
    """

    # Every shape the spec enumerates as unusable, plus the two Python traps the
    # enumeration implies: ``bool`` is an ``int`` subclass so it passes a bare numeric
    # check, and a non-finite float passes an ``isinstance(..., float)`` one.
    _REJECTED: list[tuple[str, object]] = [
        ("absent", None),
        ("zero", 0),
        ("negative", -1),
        ("negative-epoch", -1_700_000_000_000),
        ("bool-true", True),
        ("bool-false", False),
        ("numeric-string", "1700000000000"),
        ("nan", float("nan")),
        ("positive-infinity", float("inf")),
        ("negative-infinity", float("-inf")),
        ("beyond-datetime-range", 10**18),
        ("mapping", {"lastUpdatedTimestamp": 1_700_000_000_000}),
    ]

    @pytest.mark.parametrize(
        ("label", "value"), _REJECTED, ids=[label for label, _ in _REJECTED]
    )
    def test_an_unusable_value_is_rejected_and_logged(
        self, label: str, value: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unusable millisecond value returns ``None`` — never an instant, never a raise.

        ``None`` is what the callers read as "nothing observable here", so it is the only
        answer that books nothing. The log record is the other half of the contract: a
        silently dropped observation is indistinguishable from a dataset DataHub cannot
        date.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — such a value "books
            nothing and is logged".
        """
        from src.backend.ingestion.service import _observed_at

        caplog.set_level(logging.WARNING)
        assert _observed_at(value) is None, (
            f"{label}: an unusable observed millisecond value ({value!r}) must be "
            "rejected, not converted. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4."
        )
        assert caplog.records, (
            f"{label}: the rejection must be logged, or a dropped observation is "
            "indistinguishable from an undatable dataset. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )

    def test_a_representable_past_instant_converts_to_that_exact_instant(self) -> None:
        """A positive past millisecond value converts to exactly the instant it names.

        The expected instant is written out independently of the conversion arithmetic,
        so an off-by-a-factor error (seconds read as milliseconds, or the reverse) fails
        here rather than passing against a restatement of the implementation.

        spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "``Dataset.lastIngested``
            (epoch ms)".
        """
        from src.backend.ingestion.service import _observed_at

        # 1_700_000_000_123 ms = 2023-11-14T22:13:20.123Z.
        assert _observed_at(1_700_000_000_123) == datetime(
            2023, 11, 14, 22, 13, 20, 123_000, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        "ms",
        [1, 1_000, 1_200_000_000_999, 1_500_000_000_000, 1_700_000_000_123],
        ids=["epoch+1ms", "epoch+1s", "2008", "2017", "2023"],
    )
    def test_the_integer_form_agrees_with_the_float_form_across_the_usable_range(
        self, ms: int
    ) -> None:
        """Integer-exact conversion agrees with ``fromtimestamp(ms / 1000)`` everywhere usable.

        This is what makes the change safe on an existing estate: rows already booked by
        the float form must dedup against rows the integer form computes, and they only
        do so if the two land on the same instant.

        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the observation identity
            tuple includes ``occurred_at``, so a shifted instant is a *new* identity and
            would re-book every historical observation.
        """
        from src.backend.ingestion.service import _observed_at

        assert _observed_at(ms) == datetime.fromtimestamp(ms / 1000, tz=UTC)

    def test_the_future_bound_is_the_declared_skew_allowance(self) -> None:
        """The upper bound sits at ``now + _OBSERVED_AT_MAX_SKEW``, on both sides of it.

        Both legs are derived from the declared constant rather than from a wall-clock
        literal, so widening or narrowing the allowance moves the test with it and only a
        *removed* bound fails. The backstop guards the degenerate reading: a zero (or
        negative) allowance would make the "inside" leg pass for the wrong reason.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "no later than a small
            skew allowance past now"; a "**future-dated**" value "books nothing".
        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "an unbounded upper end lets
            one future-dated value permanently poison every recency reading derived from
            it, since the newest evidence always wins and nothing later can displace it."
        """
        from src.backend.ingestion.service import _OBSERVED_AT_MAX_SKEW, _observed_at

        assert _OBSERVED_AT_MAX_SKEW > timedelta(0), (
            "backstop: the skew allowance must be a real, positive window, or the "
            "'inside the allowance' leg below proves nothing."
        )
        # Both offsets are proportions of the allowance rather than fixed durations, so
        # neither leg degenerates at any positive value of it: a fixed −30s offset would
        # make `inside` a *past* instant for any allowance under half a minute, and the
        # "accepted" leg would then be proving the past branch instead of the bound.
        now = datetime.now(tz=UTC)
        inside = now + _OBSERVED_AT_MAX_SKEW / 2
        beyond = now + _OBSERVED_AT_MAX_SKEW * 2

        assert _observed_at(_to_ms(inside)) is not None, (
            "an instant inside the declared skew allowance must be accepted — writer "
            "clocks drift and DataHub's need not agree with the API pod's. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert _observed_at(_to_ms(beyond)) is None, (
            "an instant past the declared skew allowance must be rejected outright. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — one future-dated "
            "value otherwise poisons every recency reading derived from it."
        )

    def test_a_far_future_instant_is_rejected_rather_than_clamped(self) -> None:
        """A representable but far-future value is rejected, not clamped back to now.

        ``10**14`` ms resolves to the year 5138 — perfectly representable, so the
        range check cannot be what rejects it. Rejecting it (rather than clamping to
        ``now``) is the discriminating half: a clamp would return an instant, and a
        clamped instant outranks every real one forever.

        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "rejected, never clamped
            and never defaulted to ``now()``".
        """
        from src.backend.ingestion.service import _observed_at

        # Backstop: the value must be representable, or this test would be re-proving the
        # out-of-range branch instead of the future branch. Asserting the resolved year
        # (rather than merely that the arithmetic did not raise) also fails if the value
        # ever stops being far-future.
        representable = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=10**14)
        assert representable.year > datetime.now(tz=UTC).year + 1000, (
            f"backstop: 10**14 ms must resolve to a representable far-future instant; got "
            f"{representable!r}."
        )
        assert _observed_at(10**14) is None


# ── Step 4 observation sub-passes: shared in-memory store ─────────────────────


class _ObservationStore:
    """A query-routing fake session over the two tables step 4's observers touch.

    Two queries reach the database from an observation sub-pass, and both are routed by
    the SQL they compile to, never by call position
    (spec: TESTING.md §Unit Testing §Mocking rules):

    1. ``SELECT dataset_urn FROM ingestion_source_dataset WHERE source_id = :id`` — the
       source's mapped datasets.
    2. ``SELECT detail->>'dataset_urn', occurred_at FROM events WHERE …`` — the batched
       dedup read (:meth:`IngestionService._existing_observation_keys`).

    Query 2 is **emulated, not reimplemented**. The rows it returns come from what
    ``db.add`` actually stored, filtered by the values the statement bound: the source
    id, the observed-instant range, and — *only when the statement binds one* — the
    producer. That last conditional is what makes the fake honest in both directions. A
    real database given a dedup query with no ``detail->>'source'`` term returns rows of
    every producer, and this fake does the same, so an implementation that drops the
    producer term from the identity tuple sees the other signal's row and skips its own
    insert. An implementation that keeps it sees only its own.

    The store persists across calls, so a second sweep's dedup read sees the first
    sweep's committed rows.
    """

    def __init__(self, mappings: dict[str, list[str]]) -> None:
        self.mappings = {str(k): list(v) for k, v in mappings.items()}
        self.events: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        # (compiled SQL, DBAPI-level bind count) of every dedup read, for the
        # parameter-bound pin in TestObservationDedupReadIsParameterBounded.
        self.dedup_reads: list[tuple[str, int]] = []

    # ── wiring ────────────────────────────────────────────────────────────────

    def wire(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(side_effect=self._execute)
        db.add = MagicMock(side_effect=self.events.append)
        db.commit = AsyncMock(side_effect=self._commit)
        db.rollback = AsyncMock(side_effect=self._rollback)

    async def _commit(self) -> None:
        self.commits += 1

    async def _rollback(self) -> None:
        self.rollbacks += 1

    # ── routing ───────────────────────────────────────────────────────────────

    async def _execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        from sqlalchemy.dialects import postgresql

        # render_postcompile expands an ``IN (...)`` list into one bind per element,
        # which is how the driver ultimately sends it. Compiling without it reports a
        # single "expanding" parameter for any list length, which would make the
        # per-instant and the constant form indistinguishable to the bind-count pin.
        compiled = stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
        )
        sql = str(compiled)
        binds = list(compiled.params.values())

        if "ingestion_source_dataset" in sql:
            return self._mapping_result(binds)
        if "events" in sql:
            self.dedup_reads.append((sql, len(compiled.params)))
            return self._dedup_result(binds)
        raise AssertionError(f"_ObservationStore: unrouted statement:\n{sql}")

    def _mapping_result(self, binds: list[Any]) -> MagicMock:
        bound_ids = {str(v) for v in binds}
        urns: list[str] = []
        for source_id, mapped in self.mappings.items():
            if source_id in bound_ids:
                urns.extend(mapped)
        result = MagicMock()
        result.scalars.return_value.all.return_value = urns
        return result

    def _dedup_result(self, binds: list[Any]) -> MagicMock:
        from src.backend.ingestion.service import _OBSERVATION_SOURCES

        bound_strings = {str(v) for v in binds}
        bound_instants = [v for v in binds if isinstance(v, datetime)]
        bound_producers = bound_strings & set(_OBSERVATION_SOURCES)

        rows: list[tuple[str | None, datetime]] = []
        for event in self.events:
            if event.entity_id not in bound_strings:
                continue
            if bound_instants and not (
                min(bound_instants) <= event.occurred_at <= max(bound_instants)
            ):
                continue
            # No producer term bound ⇒ a real database returns every producer's rows.
            if bound_producers and event.detail.get("source") not in bound_producers:
                continue
            rows.append((event.detail.get("dataset_urn"), event.occurred_at))

        result = MagicMock()
        result.all.return_value = rows
        return result

    # ── readbacks ─────────────────────────────────────────────────────────────

    def booked(self, producer: str) -> list[Any]:
        """Events stored for one ``detail.source`` producer, in insertion order."""
        return [e for e in self.events if e.detail.get("source") == producer]


def _make_operation(ts_ms: object, op_type: str | None = "INSERT") -> MagicMock:
    """A DataHub ``Operation`` timeseries record.

    Exposes only ``operationType`` and ``lastUpdatedTimestamp`` — the two attributes the
    sweep reads (spec: feature/BACKEND.md §Sync + mapping sweep step 4).
    """
    op = MagicMock()
    op.operationType = op_type
    op.lastUpdatedTimestamp = ts_ms
    return op


# ── The batched dedup read is bind-parameter bounded ──────────────────────────


class TestObservationDedupReadIsParameterBounded:
    """One dedup read costs the same number of bind parameters whatever the batch size.

    ``_existing_observation_keys`` is handed one instant per observation the sub-pass is
    about to book, and the first sweep of a fresh deployment books the whole historical
    backlog — one instant per mapped dataset. Binding the instants individually
    (``occurred_at IN (…)``) therefore scales the parameter count with the estate, and
    asyncpg hard-fails above 32767 bind parameters: a source with more mapped datasets
    than that takes the whole sub-pass down on the sweep that matters most. The parameter
    count must be a function of the *query*, not of the batch.

    No behavioural test can see this: both forms return the same keys, and every estate a
    test can afford to build is orders of magnitude below the ceiling. The bind count is
    the only observable that separates them, which is why it is pinned directly.

    NOTE — no spec anchor. This rule is stated in the approved plan (§Stage B2, "Do not
    use ``occurred_at IN (<instants>)`` … asyncpg hard-fails above 32767 … Use a range
    predicate … 8 bind parameters, constant") and in the docstring of
    ``_existing_observation_keys``, but not in ``spec/feature/BACKEND.md``. Flagged for the
    spec owner: it belongs in §Sync + mapping sweep step 4 beside the identity rule.
    """

    _ORDERS_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
    )

    @pytest.mark.asyncio
    async def test_a_hundredfold_larger_batch_binds_no_more_parameters(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """3 instants and 300 instants compile to the same bind-parameter count."""
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION

        base = datetime(2024, 6, 1, tzinfo=UTC)
        counts: dict[int, int] = {}
        statements: dict[int, str] = {}

        for batch in (3, 300):
            source_id = str(uuid.uuid4())
            instants = {base + timedelta(minutes=n) for n in range(batch)}
            store = _ObservationStore({source_id: [self._ORDERS_URN]})
            store.wire(db)

            keys = await service._existing_observation_keys(
                source_id, instants, _OBS_PASSIVE_OPERATION
            )

            # Backstop: the read really was issued for this batch. Without it the
            # comparison below could hold between two batches that never reached the
            # database at all.
            assert len(store.dedup_reads) == 1, (
                f"batch={batch}: the dedup read is one statement per source per signal; "
                f"got {len(store.dedup_reads)}."
            )
            assert keys == set(), (
                f"batch={batch}: nothing is booked yet, so no key comes back; got {keys!r}."
            )
            statements[batch], counts[batch] = store.dedup_reads[0]

        assert counts[3] == counts[300], (
            f"the dedup read must bind a constant number of parameters: a 3-instant batch "
            f"bound {counts[3]} and a 300-instant batch bound {counts[300]}, so the count "
            f"tracks the batch and the first sweep of a large estate walks into asyncpg's "
            f"32767-parameter ceiling. plan §Stage B2 — 'Do not use occurred_at IN "
            f"(<instants>) … Use a range predicate … 8 bind parameters, constant'. "
            f"statement (truncated): {statements[300][:400]}"
        )

    @pytest.mark.asyncio
    async def test_the_read_still_matches_the_instants_it_was_given(
        self, service: IngestionService, db: AsyncMock
    ) -> None:
        """A constant-parameter read is still exact: only the booked instant comes back.

        Paired with the count pin above so "bind fewer parameters" cannot be satisfied by
        binding fewer *predicates*: the range narrows the read, and the intersection with
        the requested instant set is what makes the result exact.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — identity is the "(source,
            ``detail.dataset_urn``, ``occurred_at``, ``detail.source``)" tuple.
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION
        from src.shared.db.models import Event
        from src.shared.events import INGESTION_COMPLETE

        source_id = str(uuid.uuid4())
        booked_at = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        inside_the_range_but_not_requested = booked_at + timedelta(seconds=30)
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        store.events.extend(
            Event(
                entity_type="ingestion_source",
                entity_id=source_id,
                event_type=INGESTION_COMPLETE,
                status="success",
                detail={"dataset_urn": self._ORDERS_URN, "source": _OBS_PASSIVE_OPERATION},
                occurred_at=moment,
            )
            for moment in (booked_at, inside_the_range_but_not_requested)
        )

        keys = await service._existing_observation_keys(
            source_id,
            {booked_at, booked_at + timedelta(minutes=1)},
            _OBS_PASSIVE_OPERATION,
        )

        assert keys == {(self._ORDERS_URN, booked_at)}, (
            f"only the requested instants may come back — a row inside the bound range "
            f"that was not asked about is not an identity match; got {keys!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the identity tuple."
        )


# ── _observe_passive_operations: per-dataset Operation observation ────────────


class TestObservePassiveOperations:
    """Sub-pass 4b: ingestion-like ``Operation`` aspects on a PASSIVE source's datasets.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the sub-pass table row
        "``Operation`` observation | ``Operation`` aspects (``operationType ∈ {INSERT,
        UPDATE, CREATE, ALTER}``) | ``PASSIVE`` | per dataset | ``COMPLETE`` only |
        ``passive_observation``".
    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — identity is the "(source,
        ``detail.dataset_urn``, ``occurred_at``, ``detail.source``)" tuple, "**appended**
        — booked once for that instant, never again".
    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "there is **no cap of one
        event per dataset per sweep** — every new qualifying instant since the last sweep
        is booked in that sweep, which is what makes two consecutive sweeps over an
        unchanged estate report zero."
    spec: feature/BACKEND.md §Event Catalogue §producers — ``passive_observation`` detail
        keys are "``source``, ``dataset_urn``, ``operation_type``".
    """

    _ORDERS_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
    )
    _SHIPPING_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)"
    )

    @pytest.mark.asyncio
    async def test_five_qualifying_operations_book_five_events_in_one_sweep(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """Five qualifying Operations on one dataset book FIVE events in a single sweep.

        The cap this replaces walked the window backwards one record per sweep, so the
        same estate reported a non-zero count on every tick and the oldest instant took
        five hours to surface. Both halves are asserted: five in the first sweep, and
        **zero** in a second sweep over the unchanged estate.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "every new qualifying
            instant since the last sweep is booked in that sweep, which is what makes two
            consecutive sweeps over an unchanged estate report zero."
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION

        source_id = str(uuid.uuid4())
        instants = [1_700_000_000_000 + n * 60_000 for n in range(5)]
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            return_value=[_make_operation(ms) for ms in instants]
        )

        first = await service._observe_passive_operations(source_id)
        second = await service._observe_passive_operations(source_id)

        assert first == 5, (
            f"five distinct qualifying Operation instants must book five events in one "
            f"sweep; got {first}. spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "
            "no cap of one event per dataset per sweep."
        )
        assert second == 0, (
            f"a second sweep over an unchanged estate must book nothing; got {second}. "
            "spec: feature/BACKEND.md §Sweep summary — 'A second consecutive sweep over "
            "an unchanged estate returns zero for all of those.'"
        )
        booked = store.booked(_OBS_PASSIVE_OPERATION)
        assert {e.occurred_at for e in booked} == {
            datetime.fromtimestamp(ms / 1000, tz=UTC) for ms in instants
        }, (
            "each booked event must carry its own observed instant, so the five are five "
            "distinct identities rather than one repeated. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — observed-ingestion "
            "identity."
        )

    @pytest.mark.asyncio
    async def test_two_operations_on_one_dataset_sharing_a_millisecond_book_one_event(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """Two Operations on ONE dataset at the same millisecond book exactly one event.

        Same source, same dataset, same instant is one identity, so the second is a
        duplicate. The dedup read cannot see it — the colliding row is one this same pass
        is about to insert — so only an in-pass guard can catch it, and without one the
        pair persists forever and every later sweep treats the two rows as one.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — identity is the "(source,
            ``detail.dataset_urn``, ``occurred_at``, ``detail.source``)" tuple.
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION

        source_id = str(uuid.uuid4())
        shared_ms = 1_700_000_000_000
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        # Two aspects, same instant — different operationTypes so they are genuinely two
        # records rather than one repeated object.
        datahub.get_timeseries = AsyncMock(
            return_value=[
                _make_operation(shared_ms, op_type="INSERT"),
                _make_operation(shared_ms, op_type="UPDATE"),
            ]
        )

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 1, (
            f"two Operations on one dataset sharing a millisecond are one identity and "
            f"must book exactly one event; got {inserted}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert len(store.booked(_OBS_PASSIVE_OPERATION)) == 1

    @pytest.mark.asyncio
    async def test_two_datasets_sharing_a_millisecond_each_book_their_own_event(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """Two datasets whose Operations share a millisecond each keep their own event.

        The complement of the collision test above: the dataset URN is a term of the
        identity tuple, so a shared instant across *different* datasets is two identities.
        A dedup key omitting the URN would collapse them into one.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — identity is the "(source,
            ``detail.dataset_urn``, ``occurred_at``, ``detail.source``)" tuple.
        spec: feature/BACKEND.md §Event Catalogue §producers — ``passive_observation``
            detail keys "``source``, ``dataset_urn``, ``operation_type``".
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION
        from src.shared.events import INGESTION_COMPLETE

        source_id = str(uuid.uuid4())
        shared_ms = 1_700_000_000_000
        store = _ObservationStore({source_id: [self._ORDERS_URN, self._SHIPPING_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            side_effect=lambda urn, *a, **kw: [_make_operation(shared_ms)]
        )

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 2, (
            f"two mapped datasets with Operations at the same millisecond must each book "
            f"their own event; got {inserted}. A dedup key omitting dataset_urn collapses "
            "them. spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        booked = store.booked(_OBS_PASSIVE_OPERATION)
        assert {e.detail["dataset_urn"] for e in booked} == {
            self._ORDERS_URN,
            self._SHIPPING_URN,
        }
        for event in booked:
            assert event.entity_type == "ingestion_source", (
                "an observation is booked on the source, never on the dataset. "
                "spec: feature/BACKEND.md §Sync + mapping sweep step 4 — 'It is not an "
                "event *on* the dataset.'"
            )
            assert event.entity_id == source_id
            assert event.event_type == INGESTION_COMPLETE, (
                "observation is success-only — an Operation is written when data changes "
                "and cannot express a failure. "
                "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
            )
            assert event.status == "success"
            assert set(event.detail) == {"source", "dataset_urn", "operation_type"}, (
                f"passive_observation detail keys must be exactly "
                f"{{source, dataset_urn, operation_type}}; got {sorted(event.detail)}. "
                "spec: feature/BACKEND.md §Event Catalogue §producers."
            )
            assert event.detail["source"] == _OBS_PASSIVE_OPERATION
            assert event.detail["operation_type"] == "INSERT"
            assert event.occurred_at == datetime.fromtimestamp(shared_ms / 1000, tz=UTC)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "op_type"),
        [("drop", "DROP"), ("typeless", None), ("custom", "CUSTOM")],
    )
    async def test_a_non_ingestion_operation_type_books_nothing(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        label: str,
        op_type: str | None,
    ) -> None:
        """An Operation outside the qualifying set books nothing and commits nothing.

        Both sides are seeded across this class: the qualifying INSERT/UPDATE types are
        booked by the tests above, and these types are excluded here, so an over-broad
        ``operationType`` filter fails one or the other.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the qualifying set is
            "``operationType ∈ {INSERT, UPDATE, CREATE, ALTER}``".
        """
        source_id = str(uuid.uuid4())
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            return_value=[_make_operation(1_700_000_000_000, op_type=op_type)]
        )

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 0, (
            f"{label}: operationType {op_type!r} is not an ingestion-class operation and "
            f"must book nothing; got {inserted}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert store.events == []
        assert store.commits == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "ts_ms"),
        [
            ("absent", None),
            ("zero", 0),
            ("negative", -1),
            ("non-numeric", "not-a-timestamp"),
            ("out-of-range", 10**18),
        ],
    )
    async def test_an_undatable_operation_books_nothing_and_raises_nothing(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        label: str,
        ts_ms: object,
    ) -> None:
        """An Operation whose timestamp is unusable books nothing and does not raise.

        The zero case is the one that used to matter most: a truthiness test let ``0``
        fall through to ``now()``, so the identity never matched on the next sweep and
        that dataset accrued one event per sweep forever. A qualifying ``operationType``
        is supplied throughout, so the timestamp is the only reason nothing is booked.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "A value that is absent,
            zero, non-numeric, negative, out of range, or **future-dated** books nothing".
        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "defaulting to ``now()``
            breaks the identity tuple, so the key never matches on the next sweep and
            that dataset accrues one event per sweep forever".
        """
        source_id = str(uuid.uuid4())
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            return_value=[_make_operation(ts_ms, op_type="INSERT")]
        )

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 0, (
            f"{label}: an Operation carrying {ts_ms!r} must book nothing; got {inserted}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert store.events == [], (
            f"{label}: nothing may be booked at a fabricated instant (now(), the epoch, "
            "or a clamp). spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync."
        )

    @pytest.mark.asyncio
    async def test_a_future_dated_operation_books_nothing_while_a_past_one_still_does(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A future-dated Operation books nothing; a past one in the same batch still books.

        Both sides are in one batch so the assertion cannot pass by the sub-pass failing
        wholesale: exactly one of the two instants survives, and it is the past one.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — a "**future-dated**" value
            "books nothing and is logged".
        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "an unbounded upper end lets
            one future-dated value permanently poison every recency reading derived from
            it".
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION, _OBSERVED_AT_MAX_SKEW

        source_id = str(uuid.uuid4())
        now = datetime.now(tz=UTC)
        past = now - timedelta(hours=1)
        future = now + _OBSERVED_AT_MAX_SKEW + timedelta(hours=1)
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            return_value=[
                _make_operation(_to_ms(future)),
                _make_operation(_to_ms(past)),
            ]
        )

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 1, (
            f"exactly the past Operation must be booked out of a future/past pair; got "
            f"{inserted}. spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        booked = store.booked(_OBS_PASSIVE_OPERATION)
        assert len(booked) == 1
        assert booked[0].occurred_at < now, (
            f"the surviving event must be the past one; got {booked[0].occurred_at!r}. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — future-dated instants "
            "are rejected, never clamped."
        )

    @pytest.mark.asyncio
    async def test_a_database_failure_degrades_this_source_and_rolls_the_session_back(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A DB failure inside the sub-pass books nothing, raises nothing, and rolls back.

        Degrading the *signal* rather than the sweep is the whole point of the wrapper: a
        failure that escaped here would reach ``sync()``'s ``except`` and flip the
        ``datahub-api`` health row to ``error`` for every source. The rollback is the
        other half — a failed statement aborts the whole PostgreSQL transaction, so
        without it every later statement in the sweep fails too and the containment holds
        in name only.

        spec: feature/BACKEND.md §Best-Effort Operations — "Every failure of that read
            skips the dataset."
        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — "``ok``
            asserts only that the sweep's source-definition enumeration completed, not
            that every GMS call inside it succeeded".
        """
        source_id = str(uuid.uuid4())
        store = _ObservationStore({source_id: [self._ORDERS_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(
            return_value=[_make_operation(1_700_000_000_000)]
        )
        # The dedup read fails; the mapping read still succeeds, so the sub-pass has
        # genuinely reached the point where it would otherwise insert.
        real_execute = db.execute.side_effect

        async def _fail_on_events(stmt: Any, *a: Any, **kw: Any) -> Any:
            from sqlalchemy.dialects import postgresql

            if "events" in str(stmt.compile(dialect=postgresql.dialect())):
                raise RuntimeError("connection reset by peer")
            return await real_execute(stmt, *a, **kw)

        db.execute = AsyncMock(side_effect=_fail_on_events)

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 0, (
            f"a database failure must degrade this source's signal to zero, not raise; "
            f"got {inserted}. spec: feature/BACKEND.md §Best-Effort Operations."
        )
        assert store.rollbacks == 1, (
            "the aborted transaction must be rolled back so the rest of the sweep can "
            "still issue statements. spec: feature/BACKEND.md §Best-Effort Operations."
        )

    @pytest.mark.asyncio
    async def test_a_malformed_remote_aspect_skips_that_dataset_only(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """An ``AttributeError`` from the per-dataset read skips that dataset, not the pass.

        The interface-violation exemption applies to the estate-wide ``lastIngested`` read
        and deliberately **not** here: this read deserialises a writer-supplied remote
        aspect through the SDK, which raises ``AttributeError`` on a malformed stored
        payload, so an error of that type is not evidence of a call-shape fault. The
        second dataset is the backstop — it proves the pass carried on rather than
        aborting silently.

        spec: feature/BACKEND.md §Best-Effort Operations — "The per-dataset ``Operation``
            read is **not** exempt … one corrupted aspect would abort the sweep for every
            source. Every failure of that read skips the dataset."
        """
        from src.backend.ingestion.service import _OBS_PASSIVE_OPERATION

        source_id = str(uuid.uuid4())
        store = _ObservationStore({source_id: [self._ORDERS_URN, self._SHIPPING_URN]})
        store.wire(db)

        async def _timeseries(urn: str, *a: Any, **kw: Any) -> list[Any]:
            if urn == self._ORDERS_URN:
                raise AttributeError("'NoneType' object has no attribute 'operationType'")
            return [_make_operation(1_700_000_000_000)]

        datahub.get_timeseries = AsyncMock(side_effect=_timeseries)

        inserted = await service._observe_passive_operations(source_id)

        assert inserted == 1, (
            f"the malformed dataset must be skipped and the healthy one still booked; got "
            f"{inserted}. spec: feature/BACKEND.md §Best-Effort Operations."
        )
        booked = store.booked(_OBS_PASSIVE_OPERATION)
        assert [e.detail["dataset_urn"] for e in booked] == [self._SHIPPING_URN]


# ── _observe_last_ingested: per-dataset Dataset.lastIngested observation ──────


class TestObserveLastIngested:
    """Sub-pass 4c: ``Dataset.lastIngested`` observation, for every mode.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the sub-pass table row
        "``lastIngested`` observation | ``Dataset.lastIngested``, read once for the whole
        estate | **all modes** | per dataset | ``COMPLETE`` only |
        ``last_ingested_observation``".
    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the four load-bearing
        properties: "It is not an event *on* the dataset"; "A dataset mapped to N sources
        books N events"; "CLI wrapper sources are skipped"; "A null ``lastIngested`` books
        nothing."
    spec: feature/BACKEND.md §Event Catalogue §producers — ``last_ingested_observation``
        detail keys are "``source``, ``dataset_urn``".
    """

    _TITLE_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    )
    _EDITIONS_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    )

    @staticmethod
    def _sweep_source(
        source_id: uuid.UUID,
        *,
        mode: str = "PASSIVE",
        parent_source_id: uuid.UUID | None = None,
    ):
        """One detached sweep snapshot, the shape step 4 drives off."""
        from src.backend.ingestion.service import _SweepSource

        return _SweepSource(
            id=source_id,
            name=f"unit-source-{source_id}",
            mode=mode,
            parent_source_id=parent_source_id,
            datahub_source_urn=None,
            recipe={},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode", ["PASSIVE", "DATAHUB_MANAGED", "ACTIVE_CUSTOM_MANAGED"]
    )
    async def test_every_mode_books_a_per_dataset_observation(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock, mode: str
    ) -> None:
        """Every mode books one ``INGESTION.COMPLETE`` per mapped, datable dataset.

        The mode parametrisation is the contract: this is the only per-dataset evidence
        the two managed modes have, and the only PASSIVE signal that does not depend on
        the estate's pipelines emitting ``Operation`` aspects.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "For every dataset mapped
            to a source, when DataHub reports a non-null ``Dataset.lastIngested``, the
            sweep books an ``INGESTION.COMPLETE`` carrying ``detail.dataset_urn``."
        spec: feature/BACKEND.md §Event Catalogue §producers — detail keys "``source``,
            ``dataset_urn``".
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED
        from src.shared.events import INGESTION_COMPLETE

        source_uid = uuid.uuid4()
        ms = 1_700_000_000_000
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(return_value={self._TITLE_URN: ms})

        observed = await service._observe_last_ingested(
            [self._sweep_source(source_uid, mode=mode)]
        )

        assert observed == 1, (
            f"mode={mode}: a mapped dataset with a non-null lastIngested must book one "
            f"observation; got {observed}. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4 — the sub-pass applies to **all modes**."
        )
        booked = store.booked(_OBS_LAST_INGESTED)
        assert len(booked) == 1
        event = booked[0]
        assert event.entity_type == "ingestion_source", (
            "the observation is booked on the source; the dataset link is "
            "detail.dataset_urn alone. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4 — 'It is not an event *on* the dataset.'"
        )
        assert event.entity_id == str(source_uid)
        assert event.event_type == INGESTION_COMPLETE, (
            "observation is success-only — lastIngested advances when aspects are written "
            "and cannot express a failure. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4."
        )
        assert event.status == "success"
        assert set(event.detail) == {"source", "dataset_urn"}, (
            f"last_ingested_observation detail keys must be exactly {{source, "
            f"dataset_urn}}; got {sorted(event.detail)}. "
            "spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert event.detail["source"] == _OBS_LAST_INGESTED
        assert event.detail["dataset_urn"] == self._TITLE_URN
        assert event.occurred_at == datetime.fromtimestamp(ms / 1000, tz=UTC)

    @pytest.mark.asyncio
    async def test_a_dataset_mapped_to_two_sources_books_one_event_per_source(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """One dataset mapped to two regular sources books two events, one per source.

        There is no owner arbitration at write time — the owning-source rule is a
        read-side resolution recomputed on every read — so an implementation that picked
        a winner here would silently drop the other source's evidence.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "A dataset mapped to N
            sources books N events. There is no owner arbitration at write time".
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED

        first = uuid.uuid4()
        second = uuid.uuid4()
        store = _ObservationStore(
            {str(first): [self._TITLE_URN], str(second): [self._TITLE_URN]}
        )
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )

        observed = await service._observe_last_ingested(
            [self._sweep_source(first), self._sweep_source(second)]
        )

        assert observed == 2, (
            f"a dataset mapped to two sources must book one event per source; got "
            f"{observed}. spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        assert {e.entity_id for e in store.booked(_OBS_LAST_INGESTED)} == {
            str(first),
            str(second),
        }

    @pytest.mark.asyncio
    async def test_a_cli_wrapper_books_nothing_while_its_parent_books(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A dataset mapped to a parent and its CLI wrapper books once, on the parent.

        Both sides are seeded — the wrapper carries the same mapping — so a pass that
        ignored ``parent_source_id`` would book two events and the per-source feed, which
        unions parent with wrappers, would show one fact twice on the parent.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "CLI wrapper sources are
            skipped: ``lastIngested`` is a property of the dataset with no wrapper
            affinity, the parent already covers the URN, and the per-source feed unions
            parent with wrappers — booking on both would show one fact twice on the
            parent."
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED

        parent = uuid.uuid4()
        wrapper = uuid.uuid4()
        store = _ObservationStore(
            {str(parent): [self._TITLE_URN], str(wrapper): [self._TITLE_URN]}
        )
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )

        observed = await service._observe_last_ingested(
            [
                self._sweep_source(parent, mode="DATAHUB_MANAGED"),
                self._sweep_source(
                    wrapper, mode="DATAHUB_MANAGED", parent_source_id=parent
                ),
            ]
        )

        assert observed == 1, (
            f"a parent and its CLI wrapper covering one dataset must book exactly one "
            f"event; got {observed}. spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        booked = store.booked(_OBS_LAST_INGESTED)
        assert [e.entity_id for e in booked] == [str(parent)], (
            f"the single event must be booked on the parent, not the wrapper; got "
            f"{[e.entity_id for e in booked]}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )

    @pytest.mark.asyncio
    async def test_a_dataset_datahub_cannot_date_books_nothing(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A mapped dataset absent from the estate reading books nothing.

        The client omits null and non-positive values rather than carrying them as
        ``None``, so absence is the guard. Both sides are mapped and only one is datable,
        so a pass that booked at ``now()`` for the undatable one would produce two.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "A null ``lastIngested``
            books nothing. It is null when every aspect on the dataset carries DataHub's
            ``"no-run-id-provided"`` sentinel — there is nothing observable to date."
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED

        source_uid = uuid.uuid4()
        store = _ObservationStore(
            {str(source_uid): [self._TITLE_URN, self._EDITIONS_URN]}
        )
        store.wire(db)
        # Only the title table is datable; editions carries the sentinel, so the client
        # omitted it from the reading entirely.
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )

        observed = await service._observe_last_ingested([self._sweep_source(source_uid)])

        assert observed == 1, (
            f"only the datable dataset may book an event; got {observed}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4 — a null lastIngested "
            "books nothing."
        )
        assert [e.detail["dataset_urn"] for e in store.booked(_OBS_LAST_INGESTED)] == [
            self._TITLE_URN
        ]

    @pytest.mark.asyncio
    async def test_an_unchanged_reading_books_nothing_on_the_next_sweep(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The same ``lastIngested`` on two consecutive sweeps books exactly one event.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "An unchanged observation
            books nothing on the next sweep. The guarantee is 'at least one event over the
            dataset's lifetime', not one per hour."
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED

        source_uid = uuid.uuid4()
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )
        sources = [self._sweep_source(source_uid)]

        first = await service._observe_last_ingested(sources)
        second = await service._observe_last_ingested(sources)

        assert (first, second) == (1, 0), (
            f"the first sweep books the observation and the second books nothing; got "
            f"{(first, second)}. spec: feature/BACKEND.md §Sweep summary — 'the reading "
            "collapses to zero on the next sweep over an unchanged estate.'"
        )
        assert len(store.booked(_OBS_LAST_INGESTED)) == 1

    @pytest.mark.asyncio
    async def test_an_advanced_reading_books_a_second_event(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """An advanced ``lastIngested`` is a new identity and books a second event.

        The backstop for the idempotence test above: without it, an implementation that
        booked nothing at all would satisfy "the second sweep books nothing".

        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "a dataset accrues one event
            per distinct observed instant over its life".
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED

        source_uid = uuid.uuid4()
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        sources = [self._sweep_source(source_uid)]

        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )
        first = await service._observe_last_ingested(sources)
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_600_000}
        )
        second = await service._observe_last_ingested(sources)

        assert (first, second) == (1, 1), (
            f"an advanced lastIngested is a new identity and books again; got "
            f"{(first, second)}. spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync."
        )
        assert {e.occurred_at for e in store.booked(_OBS_LAST_INGESTED)} == {
            datetime.fromtimestamp(1_700_000_000_000 / 1000, tz=UTC),
            datetime.fromtimestamp(1_700_000_600_000 / 1000, tz=UTC),
        }

    @pytest.mark.asyncio
    async def test_an_operation_and_a_last_ingested_instant_that_coincide_book_both(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The two observation signals coinciding to the millisecond book BOTH events.

        The producer is the fourth term of the identity tuple precisely for this case:
        without it the two signals share a key and whichever sub-pass runs second
        silently drops its event. The store's dedup emulation honours a producer term
        only when the statement binds one, so an implementation that dropped the term
        sees the ``Operation`` row and skips — which is the failure this asserts against.

        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "The producer term is not
            optional: the ``Operation`` and ``lastIngested`` observations otherwise share
            a key, so an instant both report to the same millisecond silently drops one."
        """
        from src.backend.ingestion.service import _OBS_LAST_INGESTED, _OBS_PASSIVE_OPERATION

        source_uid = uuid.uuid4()
        ms = 1_700_000_000_000
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_timeseries = AsyncMock(return_value=[_make_operation(ms)])
        datahub.get_last_ingested = AsyncMock(return_value={self._TITLE_URN: ms})

        passive = await service._observe_passive_operations(str(source_uid))
        last_ingested = await service._observe_last_ingested(
            [self._sweep_source(source_uid)]
        )

        assert (passive, last_ingested) == (1, 1), (
            f"both observation producers must book their own event at a coincident "
            f"instant; got passive={passive}, last_ingested={last_ingested}. "
            "spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the producer is the "
            "fourth term of the identity tuple."
        )
        assert len(store.booked(_OBS_PASSIVE_OPERATION)) == 1
        assert len(store.booked(_OBS_LAST_INGESTED)) == 1

    @pytest.mark.asyncio
    async def test_no_bookable_source_makes_no_gms_call(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """An estate of nothing but CLI wrappers reads nothing from GMS.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "The estate read is one
            call per sweep"; CLI wrappers are skipped, so a sweep with no bookable source
            has nothing to read for.
        """
        parent = uuid.uuid4()
        store = _ObservationStore({})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(return_value={self._TITLE_URN: 1})

        observed = await service._observe_last_ingested(
            [
                self._sweep_source(
                    uuid.uuid4(), mode="DATAHUB_MANAGED", parent_source_id=parent
                )
            ]
        )

        assert observed == 0
        datahub.get_last_ingested.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_transport_failure_degrades_the_signal_without_raising(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A GMS transport failure books nothing and lets the sweep continue.

        spec: feature/BACKEND.md §Best-Effort Operations — the "Estate-wide
            ``lastIngested`` read and its per-dataset observation inserts" row: "The
            sub-pass books nothing this tick and reports ``last_ingested_observed = 0``;
            the other two sub-passes, the rest of the sweep, and the ``datahub-api``
            health row are untouched".
        """
        source_uid = uuid.uuid4()
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(
            side_effect=DataHubUnavailableError("GMS unreachable")
        )

        observed = await service._observe_last_ingested([self._sweep_source(source_uid)])

        assert observed == 0, (
            f"a transport failure must degrade the signal to zero, not raise; got "
            f"{observed}. spec: feature/BACKEND.md §Best-Effort Operations."
        )
        assert store.events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "error"),
        [
            ("renamed method", AttributeError("'DataHubClient' has no 'get_last_ingested'")),
            ("changed signature", TypeError("get_last_ingested() takes 1 positional arg")),
        ],
    )
    async def test_an_interface_violation_is_re_raised_rather_than_degraded(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        caplog: pytest.LogCaptureFixture,
        label: str,
        error: BaseException,
    ) -> None:
        """``AttributeError`` / ``TypeError`` from the estate read escape the sub-pass.

        Swallowing them would report ``last_ingested_observed = 0`` forever —
        indistinguishable from an estate with nothing observable — and a duck-typed test
        double missing the method would pass green with the sub-pass never running. The
        ERROR log is asserted too: it is what tells an operator the fault is DataSpoke's
        own call shape rather than DataHub's.

        spec: feature/BACKEND.md §Best-Effort Operations — "**Interface violations are
            exempt from best-effort, on the estate-wide ``lastIngested`` read.** … Those
            are logged at ``ERROR`` and **re-raised**".
        """
        source_uid = uuid.uuid4()
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(side_effect=error)
        caplog.set_level(logging.ERROR)

        with pytest.raises(type(error)) as raised:
            await service._observe_last_ingested([self._sweep_source(source_uid)])

        assert raised.value is error, (
            f"{label}: the client's own exception must propagate unchanged. "
            "spec: feature/BACKEND.md §Best-Effort Operations."
        )
        assert store.events == []
        assert [r.levelname for r in caplog.records] == ["ERROR"], (
            f"{label}: an interface violation must be logged at ERROR, not at the WARNING "
            f"level the degraded paths use; got "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]!r}. "
            "spec: feature/BACKEND.md §Best-Effort Operations."
        )

    @pytest.mark.asyncio
    async def test_a_database_failure_degrades_one_source_and_rolls_back(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A DB failure books nothing for that source, raises nothing, and rolls back.

        The rollback matters beyond this sub-pass: a rollback expires every ORM instance
        in the identity map, which is why the sweep drives off detached snapshots rather
        than ORM rows — a later attribute read would emit a lazy refresh outside
        ``greenlet_spawn`` and turn the contained degradation back into a sweep-wide
        failure.

        spec: feature/BACKEND.md §Best-Effort Operations — the estate-read row: "the other
            two sub-passes, the rest of the sweep, and the ``datahub-api`` health row are
            untouched".
        """
        source_uid = uuid.uuid4()
        store = _ObservationStore({str(source_uid): [self._TITLE_URN]})
        store.wire(db)
        datahub.get_last_ingested = AsyncMock(
            return_value={self._TITLE_URN: 1_700_000_000_000}
        )
        db.execute = AsyncMock(side_effect=RuntimeError("connection reset by peer"))

        observed = await service._observe_last_ingested([self._sweep_source(source_uid)])

        assert observed == 0, (
            f"a database failure must degrade this source's signal to zero, not raise; "
            f"got {observed}. spec: feature/BACKEND.md §Best-Effort Operations."
        )
        assert store.rollbacks == 1, (
            "the aborted transaction must be rolled back so the rest of the sweep can "
            "still issue statements. spec: feature/BACKEND.md §Best-Effort Operations."
        )


# ── Step 4 wiring: each sub-pass's return reaches its own counter ──────────────


class _SweepSession:
    """A query-routing fake session for driving a whole ``_run_sweep`` over one source.

    Every sibling test in this file exercises a sub-pass *directly*, so none of them can
    see whether step 4 calls it at all. This fake exists to drive the real
    :meth:`IngestionService._run_sweep` end to end over a deliberately empty estate —
    DataHub reports no managed source and no dataset — so that the only thing left with a
    non-zero contribution is step 4, and the summary is a statement about step 4's wiring.

    Statements are routed by the table they compile against, never by call position
    (spec: TESTING.md §Unit Testing §Mocking rules). Three reads reach this fake:

    1. ``SELECT … FROM ingestion_source WHERE mode = 'DATAHUB_MANAGED'`` — step 1's
       stale-removal scan. Empty: DataHub reports no managed source, so nothing is stale.
    2. ``SELECT … FROM ingestion_source`` (unfiltered) — step 2's source load, which is
       what step 4 iterates. This one returns the estate.
    3. ``ingestion_source_dataset`` / ``dataset_registry`` — step 2's matched-row prefetch
       and step 2b's registry reconcile, both empty on an estate DataHub reports no
       dataset for.
    """

    def __init__(self, source_rows: list[Any]) -> None:
        self.source_rows = source_rows
        self.commits = 0
        self.added: list[Any] = []

    def wire(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(side_effect=self._execute)
        db.add = MagicMock(side_effect=self.added.append)
        db.delete = AsyncMock()
        db.commit = AsyncMock(side_effect=self._commit)
        db.rollback = AsyncMock()

    async def _commit(self) -> None:
        self.commits += 1

    async def _execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        from sqlalchemy.dialects import postgresql

        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        bound = {str(v) for v in compiled.params.values()}

        rows: list[Any] = []
        if "ingestion_source_dataset" in sql or "dataset_registry" in sql:
            rows = []
        elif "ingestion_source" in sql:
            # The stale-removal scan binds the mode; the step-2 source load does not.
            rows = [] if "DATAHUB_MANAGED" in bound else self.source_rows
        else:
            raise AssertionError(f"_SweepSession: unrouted statement:\n{sql}")

        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result


class TestStepFourFoldsEachSubPassIntoItsCounter:
    """Step 4 calls all three sub-passes and folds each return into the right counter.

    Asserting only that a counter *key* exists is satisfied by the zero-initialised
    summary with the sub-pass never called — the sweep would report
    ``last_ingested_observed = 0`` on an estate full of observable datasets, which reads
    exactly like an estate with nothing observable. Each sub-pass is therefore stood in
    with a distinct non-zero sentinel, so the summary can only carry it if step 4 both
    invoked that sub-pass and folded its return into the counter the spec assigns it.

    The estate is empty by construction (no managed source in DataHub, no dataset in the
    enumeration), so every other counter's contribution is zero and the sentinels are the
    only signal in the summary.

    spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary — "Step 4's three
        sub-passes split across two counters: ``events_mirrored`` covers the first two —
        the execution-request mirror and the ``Operation`` observation — while
        ``last_ingested_observed`` covers the third."
    """

    @pytest.mark.asyncio
    async def test_the_two_observation_sub_passes_land_in_their_own_counters(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A PASSIVE estate: 2 → ``events_mirrored``, 3 → ``last_ingested_observed``.

        The two sentinels are distinct so a cross-fold (either return landing in the other
        counter, or both summed into one) fails as loudly as a sub-pass never called.

        spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary — the split above,
            and "It stays a counter of its own rather than folding into
            ``events_mirrored``".
        """
        passive = _make_source_row(mode="PASSIVE", recipe={})
        passive.parent_source_id = None
        passive.datahub_source_urn = None
        _SweepSession([passive]).wire(db)
        datahub.list_ingestion_sources = AsyncMock(return_value=[])
        datahub.enumerate_datasets = AsyncMock(return_value=[])

        service._observe_passive_operations = AsyncMock(return_value=2)  # type: ignore[method-assign]
        service._observe_last_ingested = AsyncMock(return_value=3)  # type: ignore[method-assign]

        summary = await service._run_sweep()

        # Backstop: both stand-ins really ran, and the Operation sub-pass was told which
        # source to observe. Without this the counter assertions below would also pass on
        # a sweep that never reached step 4 at all. Argument *shape* is not pinned —
        # positional or keyword is the caller's business — only that the source reaches it.
        service._observe_passive_operations.assert_awaited_once()
        passive_call = service._observe_passive_operations.await_args
        assert str(passive.id) in {
            str(v) for v in (*passive_call.args, *passive_call.kwargs.values())
        }, (
            f"the Operation observation must be run for the PASSIVE source the sweep "
            f"loaded; got {passive_call!r}. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4 — the sub-pass covers PASSIVE sources' mapped datasets."
        )
        service._observe_last_ingested.assert_awaited_once()
        observed_sources = service._observe_last_ingested.await_args.args[0]
        assert [s.id for s in observed_sources] == [passive.id], (
            f"the lastIngested sub-pass must be handed the sweep's own source set — "
            f"handing it an empty list observes nothing while still returning a number; "
            f"got {observed_sources!r}. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4 — the sub-pass runs over every mapped dataset of every bookable source."
        )

        assert summary["events_mirrored"] == 2, (
            f"the Operation observation's return is step 4's contribution to "
            f"events_mirrored; got {summary['events_mirrored']} for a sub-pass that "
            f"returned 2. spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )
        assert summary["last_ingested_observed"] == 3, (
            f"the lastIngested observation's return is reported under its own counter; got "
            f"{summary['last_ingested_observed']} for a sub-pass that returned 3. A sweep "
            "that never calls it reports the zero-initialised key, which is "
            "indistinguishable from an estate with nothing observable. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )

    @pytest.mark.asyncio
    async def test_the_execution_request_mirror_lands_in_events_mirrored(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A DATAHUB_MANAGED estate: the mirror's 7 reaches ``events_mirrored``.

        spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary — "``events_mirrored``
            covers the first two — the execution-request mirror and the ``Operation``
            observation".
        """
        managed = _make_source_row(
            mode="DATAHUB_MANAGED",
            recipe={},
            datahub_source_urn="urn:li:dataHubIngestionSource:unit-managed",
        )
        managed.parent_source_id = None
        _SweepSession([managed]).wire(db)
        # Empty: the estate this sweep reconciles against holds no source and no dataset,
        # so step 1 removes nothing and steps 2–3 do nothing. The row above is a stored
        # row the sweep loads in step 2, which is what step 4 iterates.
        datahub.list_ingestion_sources = AsyncMock(return_value=[])
        datahub.enumerate_datasets = AsyncMock(return_value=[])

        service._mirror_execution_requests = AsyncMock(return_value=7)  # type: ignore[method-assign]
        service._observe_last_ingested = AsyncMock(return_value=0)  # type: ignore[method-assign]

        summary = await service._run_sweep()

        # Backstop, argument-shape agnostic: the mirror ran, for this source and against
        # the DataHub source URN the stored row carries.
        service._mirror_execution_requests.assert_awaited_once()
        mirror_call = service._mirror_execution_requests.await_args
        mirror_args = {str(v) for v in (*mirror_call.args, *mirror_call.kwargs.values())}
        assert {
            str(managed.id),
            "urn:li:dataHubIngestionSource:unit-managed",
        } <= mirror_args, (
            f"the mirror must be run for the DATAHUB_MANAGED source and its DataHub source "
            f"URN; got {mirror_call!r}. spec: feature/BACKEND.md §Sync + mapping sweep "
            "step 4 — the mirror runs for every DATAHUB_MANAGED row."
        )
        assert summary["events_mirrored"] == 7, (
            f"the mirror's return is step 4's first contribution to events_mirrored; got "
            f"{summary['events_mirrored']} for a sub-pass that returned 7. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )
        assert summary["last_ingested_observed"] == 0, (
            f"the mirror's events belong to events_mirrored alone; got "
            f"last_ingested_observed={summary['last_ingested_observed']}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )

    @pytest.mark.asyncio
    async def test_each_sub_pass_covers_exactly_the_modes_the_spec_scopes_it_to(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A three-mode estate in one sweep: each sub-pass sees exactly its own scope.

        The two tests above each drive a *single-mode* estate, so neither can see a mode
        gate at all: with only one mode present, "runs for this mode" and "runs for every
        source" are the same observation, in both directions. This estate carries one
        source of each mode at once, which is the only shape where the three scoping rules
        of step 4's sub-pass table are separable:

        - the mirror is ``DATAHUB_MANAGED``-only — a gate that let it run for the other two
          would poll ``listExecutionRequests`` for sources that have no DataHub source URN;
        - the ``Operation`` observation is ``PASSIVE``-only — dropping that gate books
          per-dataset ``Operation`` evidence for managed sources, which the run layer
          already covers;
        - the ``lastIngested`` observation is **all modes** — narrowing it to ``PASSIVE``
          (or excluding ``DATAHUB_MANAGED``) silently removes the only per-dataset evidence
          the two managed modes have, and `ingestion-freshness` tier 1 with it. That
          narrowing is invisible in a single-mode estate *and* in the api-wired suite,
          where each UC arc drives one mode.

        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — the sub-pass table's
            **Modes** column: "Execution-request mirror … ``DATAHUB_MANAGED``";
            "``Operation`` observation … ``PASSIVE``"; "``lastIngested`` observation …
            **all modes**".
        spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "``lastIngested``
            observation … is the only per-dataset evidence the two managed modes have."
        """
        managed = _make_source_row(
            mode="DATAHUB_MANAGED",
            recipe={},
            datahub_source_urn="urn:li:dataHubIngestionSource:unit-mixed-managed",
        )
        managed.parent_source_id = None
        passive = _make_source_row(mode="PASSIVE", recipe={})
        passive.parent_source_id = None
        passive.datahub_source_urn = None
        active = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED", recipe={})
        active.parent_source_id = None
        active.datahub_source_urn = None

        _SweepSession([managed, passive, active]).wire(db)
        datahub.list_ingestion_sources = AsyncMock(return_value=[])
        datahub.enumerate_datasets = AsyncMock(return_value=[])

        service._mirror_execution_requests = AsyncMock(return_value=5)  # type: ignore[method-assign]
        service._observe_passive_operations = AsyncMock(return_value=2)  # type: ignore[method-assign]
        service._observe_last_ingested = AsyncMock(return_value=3)  # type: ignore[method-assign]

        summary = await service._run_sweep()

        # 4a — the mirror, DATAHUB_MANAGED only.
        mirrored_ids = {
            str(v)
            for call in service._mirror_execution_requests.await_args_list
            for v in (*call.args, *call.kwargs.values())
        }
        assert service._mirror_execution_requests.await_count == 1, (
            f"the execution-request mirror runs for the DATAHUB_MANAGED source alone; got "
            f"{service._mirror_execution_requests.await_count} awaits over a three-mode "
            f"estate. spec: feature/BACKEND.md §Sync + mapping sweep step 4 — sub-pass "
            "table, Modes column."
        )
        assert str(managed.id) in mirrored_ids
        assert {str(passive.id), str(active.id)}.isdisjoint(mirrored_ids), (
            f"neither the PASSIVE nor the ACTIVE_CUSTOM_MANAGED source may reach the "
            f"mirror; got {mirrored_ids!r}."
        )

        # 4b — the Operation observation, PASSIVE only.
        observed_op_ids = {
            str(v)
            for call in service._observe_passive_operations.await_args_list
            for v in (*call.args, *call.kwargs.values())
        }
        assert service._observe_passive_operations.await_count == 1, (
            f"the Operation observation runs for the PASSIVE source alone; got "
            f"{service._observe_passive_operations.await_count} awaits over a three-mode "
            f"estate. spec: feature/BACKEND.md §Sync + mapping sweep step 4 — sub-pass "
            "table, Modes column."
        )
        assert str(passive.id) in observed_op_ids
        assert {str(managed.id), str(active.id)}.isdisjoint(observed_op_ids), (
            f"only the PASSIVE source's mapped datasets are observed through Operation "
            f"aspects; got {observed_op_ids!r}."
        )

        # 4c — the lastIngested observation, every mode.
        service._observe_last_ingested.assert_awaited_once()
        handed = service._observe_last_ingested.await_args.args[0]
        assert {s.id for s in handed} == {managed.id, passive.id, active.id}, (
            f"the lastIngested sub-pass must be handed every source the sweep loaded, "
            f"whatever its mode; got {sorted(str(s.id) for s in handed)} for an estate of "
            f"managed={managed.id}, passive={passive.id}, active={active.id}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4 — sub-pass table, "
            "Modes column: **all modes**."
        )
        assert {s.mode for s in handed} == {
            "DATAHUB_MANAGED",
            "PASSIVE",
            "ACTIVE_CUSTOM_MANAGED",
        }, (
            f"all three modes must reach the sub-pass, since the two managed modes have no "
            f"other per-dataset evidence; got {sorted({s.mode for s in handed})}."
        )

        assert summary["events_mirrored"] == 7, (
            f"events_mirrored carries the mirror's 5 plus the Operation observation's 2; "
            f"got {summary['events_mirrored']}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )
        assert summary["last_ingested_observed"] == 3, (
            f"the lastIngested observation reports under its own counter; got "
            f"{summary['last_ingested_observed']}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep — Sweep summary."
        )


# ── get_events_for_source: dataset_urn is keyword-only ────────────────────────


class TestGetEventsForSourceRejectsAPositionalDatasetUrn:
    """``dataset_urn`` cannot be reached positionally on ``get_events_for_source``.

    The callers' side of this — that every one of them passes the URN by keyword — is
    asserted in ``tests/unit/backend/dataset/test_service.py`` and in the router test.
    Neither can see the signature: they would keep passing a keyword argument to a
    parameter that had become positional, and the defect this rules out is on the *other*
    side. ``dataset_urn`` sits after ``order_by: Any``, so a positional caller's sort spec
    would land in it and become a silent ``detail->>'dataset_urn' = <sort spec>`` text
    comparison — matching nothing, emptying every dataset timeline of exactly the run-level
    rows the ``IS NULL`` disjunct exists to keep, and invisible to ``mypy`` through ``Any``.
    Positional calls into this service are not hypothetical: ``core.py`` already calls the
    sibling ``get_events`` that way.

    spec: feature/BACKEND.md §Querying Events — the per-dataset timeline resolves the
        covering source's feed "by reverse-lookup plus the ``detail.dataset_urn``
        predicate", which is the read this parameter drives.
    """

    def test_an_extra_positional_argument_raises_rather_than_binding_dataset_urn(
        self, service: IngestionService
    ) -> None:
        """A fully positional call is a ``TypeError``, not a silently narrowed feed."""
        import inspect

        sig = inspect.signature(service.get_events_for_source)
        positional = [
            p.name
            for p in sig.parameters.values()
            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ]
        assert "dataset_urn" not in positional, (
            f"dataset_urn must be keyword-only; it is currently positional at index "
            f"{positional.index('dataset_urn') if 'dataset_urn' in positional else None} "
            f"of {positional!r}, where a positional order_by would land in it."
        )

        # One positional argument per positional parameter — a well-formed call in its own
        # right. Derived from the signature rather than hard-coded so that adding a
        # positional parameter does not turn this into an arity test by accident.
        args: list[Any] = ["source-id"] + [None] * (len(positional) - 1)
        sig.bind(*args, dataset_urn=_DATASET_URN)  # backstop: raises if args are malformed

        with pytest.raises(TypeError) as raised:
            service.get_events_for_source(*args, _DATASET_URN)

        assert "positional" in str(raised.value), (
            f"the refusal must come from argument binding, not from something the body "
            f"did with a URN it should never have received; got {raised.value!r}."
        )


# ── Producer invariant: no run-level producer writes a scalar dataset_urn ──────


class TestRunLevelProducersWriteNoScalarDatasetUrn:
    """No run-level ``INGESTION.*`` producer writes a scalar ``detail.dataset_urn``.

    This is the invariant the per-dataset timeline's ``IS NULL`` disjunct rests on. If
    any run-level producer ever wrote that key, the disjunct would stop admitting its
    rows and every dataset's timeline would silently lose exactly the run outcomes and
    failures it exists to show — a regression with no other visible symptom.

    Both run-level producers are covered: the inline ``ACTIVE_CUSTOM_MANAGED`` record on
    all four of its paths (success, zero-emit failure, secret-resolution failure,
    extractor crash) and the ``datahub_sync`` execution-request mirror.

    spec: feature/BACKEND.md §Event Catalogue §producers — "**No run-level producer
        writes a scalar ``detail.dataset_urn``.** The mirror carries no dataset link at
        all, and the inline record carries dataset URN *lists* (``discovered_urns`` /
        ``emitted_urns``) under different keys. That is what lets the per-dataset timeline
        admit run-level rows through an ``IS NULL`` disjunct".
    spec: feature/BACKEND.md §Event Catalogue §producers — "**``detail.source`` is absent,
        not null, on the inline record.**"
    """

    @pytest.mark.asyncio
    async def test_the_mirror_writes_no_scalar_dataset_urn(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The ``datahub_sync`` mirror's detail carries no ``dataset_urn`` key.

        spec: feature/BACKEND.md §Event Catalogue §producers — ``datahub_sync`` detail keys
            are "``source``, ``execution_request_urn`` …, ``duration_ms``".
        """
        source_id = str(uuid.uuid4())
        datahub.list_execution_requests = AsyncMock(
            return_value=[
                {
                    "urn": "urn:li:dataHubExecutionRequest:run-1",
                    "status": "SUCCESS",
                    "startTimeMs": 1_700_000_000_000,
                    "durationMs": 1000,
                    "requestedAt": 1_699_999_000_000,
                }
            ]
        )
        dup_result = MagicMock()
        dup_result.first.return_value = None
        db.execute = AsyncMock(return_value=dup_result)

        inserted = await service._mirror_execution_requests(
            source_id, "urn:li:dataHubIngestionSource:abc"
        )

        assert inserted == 1, "backstop: the mirror must actually have booked an event."
        detail = db.add.call_args[0][0].detail
        assert "dataset_urn" not in detail, (
            f"the execution-request mirror must write no scalar dataset_urn; got "
            f"{sorted(detail)}. spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert detail["source"] == "datahub_sync"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "emitted", "discovered"),
        [
            ("success", [_DATASET_URN], [_DATASET_URN]),
            ("zero-emit failure", [], [_DATASET_URN]),
        ],
    )
    async def test_the_inline_run_record_writes_lists_not_a_scalar_dataset_urn(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        label: str,
        emitted: list[str],
        discovered: list[str],
    ) -> None:
        """The inline ACM record carries URN *lists*, never a scalar ``dataset_urn``.

        And it carries no ``source`` key at all, which is the other half of the same
        invariant: a consumer's producer filter must treat an absent key as run-level,
        because ``detail->>'source'`` on a missing key is SQL ``NULL``.

        spec: feature/BACKEND.md §Event Catalogue §producers — the inline run record's
            keys, and "``detail.source`` is absent, not null, on the inline record".
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)
        recorded: list[dict[str, Any]] = []

        async def _capture(source_id, event_type, status, detail):  # type: ignore[no-untyped-def]
            recorded.append(detail)

        with (
            _patched_run(service, emitted_urns=emitted, discovered_urns=discovered),
            patch.object(service, "_record_source_event", side_effect=_capture),
        ):
            await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert len(recorded) == 1, (
            f"{label}: backstop — the run must have recorded exactly one event; got "
            f"{len(recorded)}."
        )
        detail = recorded[0]
        assert "dataset_urn" not in detail, (
            f"{label}: the inline run record must carry URN lists, never a scalar "
            f"dataset_urn; got {sorted(detail)}. "
            "spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert "source" not in detail, (
            f"{label}: the inline run record carries no detail.source key; got "
            f"{sorted(detail)}. spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert "discovered_urns" in detail and "emitted_urns" in detail

    @pytest.mark.asyncio
    async def test_the_extractor_crash_failure_writes_no_scalar_dataset_urn(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The extractor-crash ``INGESTION.FAIL`` payload carries no scalar ``dataset_urn``.

        This payload is reachable from no other test in the suite, and it is the shape
        most likely to acquire a dataset link: a crash mid-crawl is exactly when an
        author reaches for "which dataset was it on".

        spec: feature/BACKEND.md §Event Catalogue §producers — the invariant holds across
            "run-level" producers, and a ``FAIL`` is one.
        """
        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)
        recorded: list[dict[str, Any]] = []

        async def _capture(source_id, event_type, status, detail):  # type: ignore[no-untyped-def]
            recorded.append(detail)

        crash = RuntimeError("connection to example-pg refused mid-crawl")
        with (
            patch.multiple(
                "src.backend.ingestion.service",
                resolve_recipe_secrets=MagicMock(side_effect=lambda r: r),
                run_extractor=AsyncMock(side_effect=crash),
            ),
            patch.object(service, "_record_source_event", side_effect=_capture),
        ):
            with pytest.raises(RuntimeError):
                await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert len(recorded) == 1, (
            f"backstop — the crash path must record exactly one FAIL event; got "
            f"{len(recorded)}."
        )
        detail = recorded[0]
        assert "dataset_urn" not in detail, (
            f"the extractor-crash FAIL must carry no scalar dataset_urn; got "
            f"{sorted(detail)}. spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert "source" not in detail
        assert detail["exception"] == str(crash), (
            "backstop: the captured payload must be the crash event, not some earlier one."
        )

    @pytest.mark.asyncio
    async def test_the_secret_resolution_failure_writes_no_scalar_dataset_urn(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """The secret-resolution ``INGESTION.FAIL`` payload carries no scalar ``dataset_urn``.

        spec: feature/BACKEND.md §Event Catalogue §producers — the invariant holds across
            run-level producers.
        """
        from src.shared.secrets.interface import SecretRefNotFound

        row = _make_source_row(mode="ACTIVE_CUSTOM_MANAGED")
        mock_scalar_query(db, row)
        recorded: list[dict[str, Any]] = []

        async def _capture(source_id, event_type, status, detail):  # type: ignore[no-untyped-def]
            recorded.append(detail)

        with (
            patch(
                "src.backend.ingestion.service.resolve_recipe_secrets",
                MagicMock(side_effect=SecretRefNotFound("dummy-data-pg__password")),
            ),
            patch.object(service, "_record_source_event", side_effect=_capture),
        ):
            result = await service._run_inner(str(row.id), dry_run=False, manual=True)

        assert result.status == "error", (
            "backstop: an unresolvable secret must fail the run, or no FAIL payload was "
            "produced to inspect."
        )
        assert len(recorded) == 1
        detail = recorded[0]
        assert "dataset_urn" not in detail, (
            f"the secret-resolution FAIL must carry no scalar dataset_urn; got "
            f"{sorted(detail)}. spec: feature/BACKEND.md §Event Catalogue §producers."
        )
        assert "source" not in detail


# ── The mirror's own occurred_at bounds ───────────────────────────────────────


class TestMirrorExecutionRequestUndatable:
    """An execution neither timestamp can date is not mirrored at all.

    spec: feature/BACKEND.md §Sync + mapping sweep step 4 — "Mirror: ``startTimeMs``,
        falling back to ``requestedAt`` … Both are remote, writer-supplied values and both
        pass the same bounds as an observed instant; an execution neither field can date
        is **not mirrored**."
    spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "an execution neither field can
        date is **not mirrored**, rather than booked at the epoch or at a future instant
        that would then outrank every later run."
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "start_ms", "requested_ms"),
        [
            ("both zero", 0, 0),
            ("both absent", None, None),
            ("both negative", -1, -5),
            ("both non-numeric", "soon", "soon"),
        ],
    )
    async def test_an_undatable_execution_is_not_mirrored(
        self,
        service: IngestionService,
        db: AsyncMock,
        datahub: AsyncMock,
        label: str,
        start_ms: object,
        requested_ms: object,
    ) -> None:
        """Neither field usable ⇒ no event, rather than an event at the epoch.

        The old form booked such a run at ``1970-01-01``, which is the worst possible
        answer for ``latest_run``: it is a real terminal outcome pinned so far in the past
        that it is invisible to every recency reading.
        """
        source_id = str(uuid.uuid4())
        datahub.list_execution_requests = AsyncMock(
            return_value=[
                {
                    "urn": "urn:li:dataHubExecutionRequest:undatable",
                    "status": "SUCCESS",
                    "startTimeMs": start_ms,
                    "durationMs": 1000,
                    "requestedAt": requested_ms,
                }
            ]
        )
        dup_result = MagicMock()
        dup_result.first.return_value = None
        db.execute = AsyncMock(return_value=dup_result)

        inserted = await service._mirror_execution_requests(
            source_id, "urn:li:dataHubIngestionSource:abc"
        )

        assert inserted == 0, (
            f"{label}: an execution neither timestamp can date must not be mirrored; got "
            f"{inserted}. spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_future_dated_start_falls_back_to_a_usable_requested_at(
        self, service: IngestionService, db: AsyncMock, datahub: AsyncMock
    ) -> None:
        """A future-dated ``startTimeMs`` is rejected and ``requestedAt`` supplies the instant.

        The discriminating case: an implementation that only checked ``> 0`` would take
        the future value and pin the source's ``latest_run`` at an instant nothing later
        can displace.

        spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — "both pass the same bounds as
            an observed instant (positive, representable, no further ahead than a small
            skew allowance)".
        """
        from src.backend.ingestion.service import _OBSERVED_AT_MAX_SKEW

        source_id = str(uuid.uuid4())
        requested = datetime.now(tz=UTC) - timedelta(hours=2)
        future_start = datetime.now(tz=UTC) + _OBSERVED_AT_MAX_SKEW + timedelta(days=365)
        datahub.list_execution_requests = AsyncMock(
            return_value=[
                {
                    "urn": "urn:li:dataHubExecutionRequest:future-start",
                    "status": "SUCCESS",
                    "startTimeMs": _to_ms(future_start),
                    "durationMs": 1000,
                    "requestedAt": _to_ms(requested),
                }
            ]
        )
        dup_result = MagicMock()
        dup_result.first.return_value = None
        db.execute = AsyncMock(return_value=dup_result)

        inserted = await service._mirror_execution_requests(
            source_id, "urn:li:dataHubIngestionSource:abc"
        )

        assert inserted == 1, (
            f"a usable requestedAt must still date the execution; got {inserted}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep step 4."
        )
        occurred_at = db.add.call_args[0][0].occurred_at
        assert occurred_at < datetime.now(tz=UTC), (
            f"the future-dated startTimeMs must be rejected in favour of requestedAt; got "
            f"{occurred_at!r}. spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync."
        )
        assert abs((occurred_at - requested).total_seconds()) < 0.001


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

        The containment this holds is the helper's, not the reporter's ``try``. Since
        ``independent_sessionmaker`` became total, an unreadable ``bind`` is caught inside
        the helper and answered with the module-level factory, so the read cannot escape
        the reporter wherever the derivation sits. This test therefore does **not**
        discriminate the derivation's placement: hoisting
        ``factory = independent_sessionmaker(self._db)`` above ``_report_api_health``'s
        ``try`` was measured against the whole unit suite and killed nothing. What the test
        still holds is the end-to-end statement in its own right — no shape of injected
        session, including one whose ``bind`` cannot be read at all, changes what the sweep
        returns — which is a joint property of the helper and the reporter and would break
        if either side stopped swallowing.

        The shape is deliberately synthetic: no production caller can produce it, because
        every injected session comes from an ``AsyncEngine``-bound sessionmaker.

        spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — the report
            'is a side effect of the sweep, not a step of the pipeline above', so no shape
            of injected session may propagate out of the reporter.
        spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — 'the helper is total
            -- it never propagates', which is why the bind read is contained wherever the
            derivation sits.
        """

        class _BindRaises:
            """Stands in for a session whose ``bind`` is unreadable (e.g. detached)."""

            @property
            def bind(self) -> object:
                raise RuntimeError("session is detached")

        # Stubbed so the writer this shape reaches is a harmless one rather than a real
        # session; recorded so the report is provably still attempted.
        seen: list[tuple[str, str]] = []

        async def _record(_db, name, status, error=None):  # type: ignore[no-untyped-def]
            seen.append((name, status))
            return None

        service = self._service(db=_BindRaises())
        with patch("src.backend.admin.peripheral_health.report_peripheral_health", _record):
            summary = await service.sync()

        assert summary == {"sources_synced": 1}, (
            f"a bind that raises must not change the sweep's outcome; got {summary!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
        # The row is still written, and that is the discriminating half. The sweep's
        # outcome alone would also survive a helper that re-raised the unreadable bind —
        # the reporter's own ``except`` would swallow it — leaving the row silently
        # unwritten under a shape the spec says falls back. Asserting the report happened
        # is what distinguishes "contained" from "skipped".
        assert seen == [("datahub-api", "ok")], (
            f"an unreadable bind must still resolve to the module-level factory and write "
            f"the row; the writer saw {seen!r}. spec: feature/BACKEND.md §Shared Services "
            "(PostgreSQL row) — 'A session with no usable bind falls back to the "
            "module-level factory, the only address available in that case, and the helper "
            "is total -- it never propagates.' spec: feature/BACKEND.md §Health reporting — "
            "the sweep is the writer of the ``datahub-api`` row."
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
