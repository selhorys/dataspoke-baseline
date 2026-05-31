"""Ingestion service — per-source CRUD, run pipeline, and event recording.

Spec: spec/feature/BACKEND.md §Ingestion Service
Schema: spec/feature/BACKEND_SCHEMA.md §ingestion_source + §ingestion_source_dataset
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from datahub.metadata.schema_classes import (  # type: ignore
    AuditStampClass,
    DataProcessInstanceOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRelationshipsClass,
    DataProcessInstanceRunEventClass,
    DataProcessInstanceRunResultClass,
    DataProcessRunStatusClass,
    DataProcessTypeClass,
    RunResultTypeClass,
    SystemMetadataClass,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.extractors import run_extractor
from src.backend.ingestion.secret_resolver import (
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
    resolve_recipe_secrets,
    verify_secret_ref,
)
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, IngestionSource, IngestionSourceDataset
from src.shared.events import (
    INGESTION_COMPLETE,
    INGESTION_FAIL,
    INGESTION_PREFIX,
    INGESTION_SOURCE_CREATE,
    INGESTION_SOURCE_DELETE,
    INGESTION_SOURCE_UPDATE,
)
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError, StorageUnavailableError
from src.shared.models.ingestion import Mode, extract_secret_refs, parse_recipe, cron_to_tier

logger = logging.getLogger(__name__)


# ── Value objects ─────────────────────────────────────────────────────────────


class IngestionSourceRecord(BaseModel):
    """Service-layer value object mirroring the ingestion_source ORM row.

    ``schedule_tier`` is internal (never in API responses) but returned here
    for DAG dispatch and metrics tier resolution.
    """

    id: str
    mode: str
    name: str
    platform: str
    recipe: dict[str, Any]
    schedule: str | None
    schedule_tier: str | None
    datahub_source_urn: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionRunResult(BaseModel):
    """Value object for the outcome of an ingestion run."""

    run_id: str
    status: str          # 'success' | 'error'
    dry_run: bool
    entities_ingested: int
    emitted_urns: list[str]
    errors: list[str]
    warnings: list[str]


class IngestionSourceDatasetRecord(BaseModel):
    """Value object for one ingestion_source_dataset row."""

    source_id: str
    dataset_urn: str
    origin: str
    first_seen_at: datetime
    last_seen_at: datetime


# ── ORM row converters ────────────────────────────────────────────────────────


def _source_from_row(row: IngestionSource) -> IngestionSourceRecord:
    return IngestionSourceRecord(
        id=str(row.id),
        mode=row.mode,
        name=row.name,
        platform=row.platform,
        recipe=dict(row.recipe) if row.recipe else {},
        schedule=row.schedule,
        schedule_tier=row.schedule_tier,
        datahub_source_urn=row.datahub_source_urn,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _dataset_from_row(row: IngestionSourceDataset) -> IngestionSourceDatasetRecord:
    return IngestionSourceDatasetRecord(
        source_id=str(row.source_id),
        dataset_urn=row.dataset_urn,
        origin=row.origin,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_and_derive_tier(
    mode: str,
    schedule: str | None,
) -> str | None:
    """For ACTIVE_CUSTOM_MANAGED, validate schedule and return its tier.

    Returns None for null schedule (manual-only) or non-ACTIVE_CUSTOM_MANAGED
    modes (PASSIVE / DATAHUB_MANAGED have no tier constraint).

    Raises:
        ValueError: if mode == ACTIVE_CUSTOM_MANAGED and schedule is a non-None
            string that does not map to one of the three allowed tiers.
    """
    if mode != Mode.ACTIVE_CUSTOM_MANAGED.value:
        return None
    return cron_to_tier(schedule)


def _verify_recipe_secret_refs(recipe: dict[str, Any]) -> None:
    """Verify every ${name__key} ref in recipe.source.config exists in k8s.

    Raises:
        PreconditionFailedError(SECRET_REF_MALFORMED): malformed ref token.
        PreconditionFailedError(SECRET_REF_NOT_FOUND): missing secret/key.
    """
    refs = extract_secret_refs(recipe)
    for ref in refs:
        try:
            verify_secret_ref(ref)
        except SecretRefMalformed as exc:
            raise PreconditionFailedError(
                "SECRET_REF_MALFORMED",
                f"Secret reference '${{{ref}}}' is malformed: {exc}",
                detail={"ref": ref},
            ) from exc
        except SecretRefNotFound as exc:
            raise PreconditionFailedError(
                "SECRET_REF_NOT_FOUND",
                f"Secret reference '${{{ref}}}' not found: {exc}",
                detail={"ref": ref},
            ) from exc
        except SecretResolverUnavailable as exc:
            raise StorageUnavailableError(
                f"Secret resolver unavailable: {exc}"
            ) from exc


def _reject_if_datahub_managed(mode: str, source_id: str) -> None:
    """Raise ConflictError if the source is DATAHUB_MANAGED (DataHub is SSOT)."""
    if mode == Mode.DATAHUB_MANAGED.value:
        raise ConflictError(
            "INGESTION_SOURCE_READONLY",
            f"Source {source_id!r} is DATAHUB_MANAGED and read-only in DataSpoke. "
            "Edit the source in DataHub directly.",
        )


# ── Service ───────────────────────────────────────────────────────────────────


class IngestionService:
    """Per-source ingestion CRUD, run pipeline, and event recording."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient | None = None,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    # ── Source CRUD ──────────────────────────────────────────────────────────

    async def get_source(self, source_id: str) -> IngestionSourceRecord:
        """Return the source record for *source_id*.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(
            select(IngestionSource).where(IngestionSource.id == uid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)
        return _source_from_row(row)

    async def list_sources(
        self,
        offset: int = 0,
        limit: int = 20,
        mode_filter: str | None = None,
        order_by: Any = None,
    ) -> tuple[list[IngestionSourceRecord], int]:
        """Return a paginated list of sources.

        Args:
            mode_filter: When provided, filter to sources with this mode value.
        """
        base = select(IngestionSource)
        if mode_filter is not None:
            base = base.where(IngestionSource.mode == mode_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = IngestionSource.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()
        return [_source_from_row(r) for r in rows], total_count

    async def create_source(
        self,
        mode: str,
        name: str,
        schedule: str | None,
        recipe: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Create a new ingestion source.

        Validates:
        - recipe shape (parse_recipe)
        - every ${name__key} ref via verify_secret_ref (422 on failure)
        - schedule -> tier mapping for ACTIVE_CUSTOM_MANAGED (ValueError -> 422)

        DATAHUB_MANAGED rows are created only by the sync sweep (not via this
        method). Calling create_source with mode=DATAHUB_MANAGED raises
        ConflictError(INGESTION_SOURCE_READONLY) to enforce the SSOT split.

        Records INGESTION.SOURCE_CREATE event.
        """
        # Reject user-initiated creation of DATAHUB_MANAGED rows.
        _reject_if_datahub_managed(mode, "<new>")

        # Validate recipe shape.
        source_type, _ = parse_recipe(recipe)

        # Validate schedule -> tier for ACTIVE_CUSTOM_MANAGED.
        try:
            tier = _validate_and_derive_tier(mode, schedule)
        except ValueError as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                str(exc),
                detail={"schedule": schedule},
            ) from exc

        # Verify all secret refs at save time.
        _verify_recipe_secret_refs(recipe)

        row = IngestionSource(
            id=uuid.uuid4(),
            mode=mode,
            name=name,
            platform=source_type,
            recipe=recipe,
            schedule=schedule,
            schedule_tier=tier,
            status="OK",
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            str(row.id),
            INGESTION_SOURCE_CREATE,
            "success",
            {"name": name, "mode": mode, "platform": source_type},
        )
        return _source_from_row(row)

    async def replace_source(
        self,
        source_id: str,
        mode: str,
        name: str,
        schedule: str | None,
        recipe: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Full replacement of an existing source (PUT semantics).

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if mode is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(
            select(IngestionSource).where(IngestionSource.id == uid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        source_type, _ = parse_recipe(recipe)

        try:
            tier = _validate_and_derive_tier(mode, schedule)
        except ValueError as exc:
            raise PreconditionFailedError(
                "INVALID_PARAMETER", str(exc), detail={"schedule": schedule}
            ) from exc

        _verify_recipe_secret_refs(recipe)

        row.mode = mode
        row.name = name
        row.platform = source_type
        row.recipe = recipe
        row.schedule = schedule
        row.schedule_tier = tier
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_UPDATE,
            "success",
            {"name": name, "mode": mode, "platform": source_type, "operation": "PUT"},
        )
        return _source_from_row(row)

    async def patch_source(
        self,
        source_id: str,
        patch: dict[str, Any],
    ) -> IngestionSourceRecord:
        """Partial update of an existing source (PATCH semantics).

        Accepted patch keys: ``name``, ``schedule``, ``recipe``.
        ``mode`` is not patchable — use replace_source for a mode change.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if the row is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(
            select(IngestionSource).where(IngestionSource.id == uid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        if "name" in patch and patch["name"] is not None:
            row.name = patch["name"]

        if "recipe" in patch and patch["recipe"] is not None:
            new_recipe = patch["recipe"]
            source_type, _ = parse_recipe(new_recipe)
            _verify_recipe_secret_refs(new_recipe)
            row.recipe = new_recipe
            row.platform = source_type

        if "schedule" in patch:
            new_schedule = patch["schedule"]
            try:
                tier = _validate_and_derive_tier(row.mode, new_schedule)
            except ValueError as exc:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER", str(exc), detail={"schedule": new_schedule}
                ) from exc
            row.schedule = new_schedule
            row.schedule_tier = tier

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(patch.keys())},
        )
        return _source_from_row(row)

    async def delete_source(self, source_id: str) -> None:
        """Hard-delete a source (and cascades to ingestion_source_dataset).

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_SOURCE_READONLY): if the row is DATAHUB_MANAGED.
        """
        try:
            uid = uuid.UUID(source_id)
        except ValueError:
            raise EntityNotFoundError("ingestion_source", source_id)

        result = await self._db.execute(
            select(IngestionSource).where(IngestionSource.id == uid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_source", source_id)

        _reject_if_datahub_managed(row.mode, source_id)

        mode = row.mode
        name = row.name
        await self._db.delete(row)
        await self._db.commit()

        await self._record_source_event(
            source_id,
            INGESTION_SOURCE_DELETE,
            "success",
            {"name": name, "mode": mode, "operation": "DELETE"},
        )

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        source_id: str,
        dry_run: bool = False,
    ) -> IngestionRunResult:
        """Run the full ingestion pipeline for an ACTIVE_CUSTOM_MANAGED source.

        Pipeline:
        1. Load source; reject if mode != ACTIVE_CUSTOM_MANAGED (409 INGESTION_RUN_NOT_APPLICABLE).
        2. Redis SETNX guard key ``ingestion:running:{source_id}`` (409 INGESTION_RUNNING).
        3. Resolve ``${name__key}`` recipe secrets into plaintext in-memory.
        4. Emit DataProcessInstance STARTED (non-dry-run).
        5. Dispatch to extractor for recipe.source.type.
        6. Emit DataProcessInstance COMPLETE/FAILED (non-dry-run).
        7. Upsert emitted URNs into ingestion_source_dataset (origin='emitted', non-dry-run).
        8. Record INGESTION.COMPLETE / INGESTION.FAIL event.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if not found.
            ConflictError(INGESTION_RUN_NOT_APPLICABLE): non-ACTIVE_CUSTOM_MANAGED mode.
            ConflictError(INGESTION_RUNNING): concurrent run already in progress.
        """
        lock_key = f"ingestion:running:{source_id}"
        lock_token: str | None = None

        if self._cache is not None:
            lock_token = secrets.token_urlsafe(16)
            acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=3600)
            if not acquired:
                raise ConflictError(
                    "INGESTION_RUNNING",
                    f"Ingestion is already running for source {source_id}",
                )

        try:
            return await self._run_inner(source_id, dry_run)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(self, source_id: str, dry_run: bool) -> IngestionRunResult:
        """Inner run logic (executes inside the Redis SETNX guard)."""
        run_id = str(uuid.uuid4())

        source = await self.get_source(source_id)

        if source.mode != Mode.ACTIVE_CUSTOM_MANAGED.value:
            raise ConflictError(
                "INGESTION_RUN_NOT_APPLICABLE",
                f"Source {source_id!r} has mode={source.mode}. "
                "method/run is only valid for ACTIVE_CUSTOM_MANAGED sources.",
            )

        # Resolve secrets: plaintext only in-memory.
        try:
            resolved_recipe = resolve_recipe_secrets(source.recipe)
        except (SecretRefMalformed, SecretRefNotFound) as exc:
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "error": str(exc), "phase": "secret_resolution"},
            )
            return IngestionRunResult(
                run_id=run_id,
                status="error",
                dry_run=dry_run,
                entities_ingested=0,
                emitted_urns=[],
                errors=[str(exc)],
                warnings=[],
            )
        except SecretResolverUnavailable as exc:
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "error": str(exc), "phase": "secret_resolution"},
            )
            return IngestionRunResult(
                run_id=run_id,
                status="error",
                dry_run=dry_run,
                entities_ingested=0,
                emitted_urns=[],
                errors=[f"SecretResolverUnavailable: {exc}"],
                warnings=[],
            )

        # DPI URN and system metadata — stamped with source_id as pipelineName.
        dpi_urn = f"urn:li:dataProcessInstance:{source_id}-{run_id}"
        start_ms = int(time.time() * 1000)
        sysmeta = SystemMetadataClass(
            runId=f"dataspoke-{source_id}-{run_id}",
            pipelineName=source_id,
            lastObserved=start_ms,
        )

        # Emit DPI STARTED (non-dry-run only).
        if not dry_run:
            try:
                await self._emit_dpi_started(dpi_urn, source, run_id, start_ms, sysmeta)
            except Exception as exc:
                logger.warning("DPI STARTED emission failed (non-fatal): %s", exc)

        # Run extractor.
        try:
            ingestion_result = await run_extractor(
                datahub=self._datahub,
                source_id=source_id,
                recipe=resolved_recipe,
                dry_run=dry_run,
                run_id=run_id,
            )
        except Exception as exc:
            if not dry_run:
                await self._emit_dpi_terminal(
                    dpi_urn,
                    sysmeta,
                    start_ms=start_ms,
                    success=False,
                )
            await self._record_source_event(
                source_id,
                INGESTION_FAIL,
                "failure",
                {"run_id": run_id, "platform": source.platform, "exception": str(exc)},
            )
            raise

        errors = ingestion_result.errors
        warnings = ingestion_result.warnings

        # A non-dry-run that ingested zero entities is treated as failure.
        if not dry_run and ingestion_result.entities_ingested == 0 and not errors:
            errors = list(warnings) or ["No entities ingested from source"]

        status = "error" if errors else "success"

        # Emit DPI terminal event (non-dry-run).
        if not dry_run:
            await self._emit_dpi_terminal(
                dpi_urn,
                sysmeta,
                start_ms=start_ms,
                success=(status == "success"),
            )

        # Record emitted URNs into ingestion_source_dataset (non-dry-run, success only).
        if not dry_run and status == "success" and ingestion_result.emitted_urns:
            await self._upsert_dataset_mappings(
                source_id=source_id,
                dataset_urns=ingestion_result.emitted_urns,
                origin="emitted",
            )

        event_type = INGESTION_COMPLETE if status == "success" else INGESTION_FAIL
        await self._record_source_event(
            source_id,
            event_type,
            status,
            {
                "run_id": run_id,
                "platform": source.platform,
                "entities_ingested": ingestion_result.entities_ingested,
                "dry_run": dry_run,
                "errors": errors,
                "warnings": warnings,
            },
        )

        return IngestionRunResult(
            run_id=run_id,
            status=status,
            dry_run=dry_run,
            entities_ingested=ingestion_result.entities_ingested,
            emitted_urns=ingestion_result.emitted_urns,
            errors=errors,
            warnings=warnings,
        )

    # ── DPI emission helpers ──────────────────────────────────────────────────

    async def _emit_dpi_started(
        self,
        dpi_urn: str,
        source: IngestionSourceRecord,
        run_id: str,
        start_ms: int,
        sysmeta: SystemMetadataClass,
    ) -> None:
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"dataspoke-{source.platform}-{run_id}",
                type=DataProcessTypeClass.BATCH_SCHEDULED,
                created=AuditStampClass(time=start_ms, actor="urn:li:corpuser:dataspoke"),
            ),
            system_metadata=sysmeta,
        )
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstanceRelationshipsClass(
                upstreamInstances=[],
                parentTemplate=None,
            ),
            system_metadata=sysmeta,
        )
        await self._datahub.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.STARTED,
                timestampMillis=start_ms,
            ),
            system_metadata=sysmeta,
        )

    async def _emit_dpi_terminal(
        self,
        dpi_urn: str,
        sysmeta: SystemMetadataClass,
        start_ms: int,
        success: bool,
    ) -> None:
        end_ms = int(time.time() * 1000)
        result_type = RunResultTypeClass.SUCCESS if success else RunResultTypeClass.FAILURE
        try:
            await self._datahub.emit_aspect(
                dpi_urn,
                DataProcessInstanceRunEventClass(
                    status=DataProcessRunStatusClass.COMPLETE,
                    timestampMillis=end_ms,
                    result=DataProcessInstanceRunResultClass(
                        type=result_type,
                        nativeResultType="dataspoke",
                    ),
                    durationMillis=end_ms - start_ms,
                ),
                system_metadata=sysmeta,
            )
        except Exception as exc:
            # Best-effort terminal emission; never let the failure path raise.
            logger.warning("DPI terminal emission failed (best-effort): %s", exc)

    # ── Dataset mapping helpers ───────────────────────────────────────────────

    async def _upsert_dataset_mappings(
        self,
        source_id: str,
        dataset_urns: list[str],
        origin: str,
    ) -> None:
        """Upsert dataset URNs into ingestion_source_dataset.

        On insert: sets both first_seen_at and last_seen_at to now().
        On conflict (same source_id + dataset_urn): updates last_seen_at.
        """
        if not dataset_urns:
            return

        uid = uuid.UUID(source_id)
        now = datetime.now(tz=UTC)

        for dataset_urn in dataset_urns:
            stmt = (
                pg_insert(IngestionSourceDataset)
                .values(
                    source_id=uid,
                    dataset_urn=dataset_urn,
                    origin=origin,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["source_id", "dataset_urn"],
                    set_={"last_seen_at": now},
                )
            )
            await self._db.execute(stmt)

        await self._db.commit()

    # ── Tier DAG support ─────────────────────────────────────────────────────

    async def list_active_sources_for_tier(
        self, tier: str
    ) -> list[IngestionSourceRecord]:
        """Return ACTIVE_CUSTOM_MANAGED sources for the given schedule tier.

        Used by the ingestion-active-{hourly,daily,weekly} Airflow DAGs.
        Sources with schedule=None (manual-only) are excluded.
        """
        result = await self._db.execute(
            select(IngestionSource).where(
                IngestionSource.mode == Mode.ACTIVE_CUSTOM_MANAGED.value,
                IngestionSource.schedule_tier == tier,
            )
        )
        return [_source_from_row(r) for r in result.scalars().all()]

    # ── Dataset mapping queries ───────────────────────────────────────────────

    async def list_datasets_for_source(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[IngestionSourceDatasetRecord], int]:
        """List dataset mappings for a given source.

        Raises:
            EntityNotFoundError('ingestion_source', source_id): if source not found.
        """
        await self.get_source(source_id)  # raises if not found
        uid = uuid.UUID(source_id)

        count_q = select(func.count()).where(
            IngestionSourceDataset.source_id == uid
        )
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = (
            select(IngestionSourceDataset)
            .where(IngestionSourceDataset.source_id == uid)
            .order_by(IngestionSourceDataset.last_seen_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()
        return [_dataset_from_row(r) for r in rows], total_count

    async def reverse_lookup(
        self,
        dataset_urn: str,
    ) -> IngestionSourceRecord | None:
        """Return the owning source for a dataset URN.

        Priority rule (per spec): ``emitted`` > ``pipeline_name`` > ``matcher``.
        When multiple sources map the same dataset at the same priority, returns
        the one with the most recent ``last_seen_at``.

        Returns None when no source claims this dataset.
        """
        result = await self._db.execute(
            select(IngestionSourceDataset, IngestionSource)
            .join(IngestionSource, IngestionSourceDataset.source_id == IngestionSource.id)
            .where(IngestionSourceDataset.dataset_urn == dataset_urn)
        )
        rows = result.all()
        if not rows:
            return None

        _ORIGIN_PRIORITY = {"emitted": 0, "pipeline_name": 1, "matcher": 2}

        def _key(pair: tuple[IngestionSourceDataset, IngestionSource]) -> tuple[int, datetime]:
            mapping, _ = pair
            priority = _ORIGIN_PRIORITY.get(mapping.origin, 99)
            # Negate last_seen_at so that most recent sorts first within the same priority.
            return (priority, -mapping.last_seen_at.timestamp())

        best_mapping, best_source = sorted(rows, key=_key)[0]
        return _source_from_row(best_source)

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_events_for_source(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ingestion events for a source (paginated).

        Events are stored with ``entity_type='ingestion_source'`` and
        ``entity_id=source_id``.
        """
        base = select(Event).where(
            Event.entity_type == "ingestion_source",
            Event.entity_id == source_id,
            Event.event_type.startswith(INGESTION_PREFIX),
        )
        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = Event.occurred_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "event_type": row.event_type,
                "status": row.status,
                "detail": row.detail,
                "occurred_at": row.occurred_at,
            }
            for row in rows
        ], total_count

    async def _record_source_event(
        self,
        source_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="ingestion_source",
            entity_id=source_id,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()

    # ── Phase 2b stubs ────────────────────────────────────────────────────────
    # The following methods are placeholders for the DataHub-source sync sweep
    # implemented in Phase 2b. They are defined here so the API layer and Airflow
    # DAGs can reference the service without import errors.

    async def sync(self) -> dict[str, Any]:
        """Sync DATAHUB_MANAGED source definitions and mapping sweep.

        TODO(phase-2b): Implement the full ingestion-sync-hourly pipeline:
          1. listIngestionSources → upsert DATAHUB_MANAGED rows.
          2. Rebuild ingestion_source_dataset for all modes via filter-matcher.
          3. Enrich via systemMetadata.pipelineName observation.
          4. Mirror run events for DATAHUB_MANAGED / PASSIVE.
        """
        raise NotImplementedError(
            "IngestionService.sync() is implemented in Phase 2b "
            "(ingestion-sync-hourly DAG integration)."
        )
