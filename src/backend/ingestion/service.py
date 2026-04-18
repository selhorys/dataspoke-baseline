"""Ingestion service — config CRUD, run pipeline, and event recording."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.extractors import run_datahub_ingestion
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, IngestionConfig
from src.shared.db.registry import ensure_dataset_registered, mark_registered
from src.shared.events import (
    INGESTION_COMPLETE,
    INGESTION_CONFIG_CREATE,
    INGESTION_CONFIG_DELETE,
    INGESTION_CONFIG_UPDATE,
    INGESTION_FAIL,
    INGESTION_PREFIX,
)
from src.shared.exceptions import ConflictError, EntityNotFoundError

logger = logging.getLogger(__name__)


class IngestionConfigRecord(BaseModel):
    """Value object mirroring the ORM IngestionConfig."""

    id: str
    dataset_urn: str
    platform: str
    locator: dict[str, Any]
    identifier: dict[str, Any]
    auth: dict[str, Any] | None = None
    is_active: bool
    schedule_tier: str | None = None
    enrichment_sources: dict[str, Any] | None = None
    custom_extractors: dict[str, Any] | None = None
    workflow_dag_id: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class IngestionRunResult(BaseModel):
    """Value object for the outcome of an ingestion run."""

    run_id: str
    status: str
    detail: dict[str, Any]


def _record_from_row(row: IngestionConfig) -> IngestionConfigRecord:
    return IngestionConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        platform=row.platform,
        locator=row.locator,
        identifier=row.identifier,
        auth=row.auth,
        is_active=row.is_active,
        schedule_tier=row.schedule_tier,
        enrichment_sources=row.enrichment_sources,
        custom_extractors=row.custom_extractors,
        workflow_dag_id=row.workflow_dag_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class IngestionService:
    """Config CRUD, run pipeline, and event recording for ingestion."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
    ) -> None:
        self._datahub = datahub
        self._db = db

    # ── Config CRUD ──────────────────────────────────────────────────────

    async def get_config(self, dataset_urn: str) -> IngestionConfigRecord | None:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _record_from_row(row)

    async def upsert_config(
        self,
        dataset_urn: str,
        platform: str,
        locator: dict[str, Any],
        identifier: dict[str, Any],
        auth: dict[str, Any] | None,
        is_active: bool,
        schedule_tier: str | None,
        enrichment_sources: dict[str, Any] | None = None,
        custom_extractors: dict[str, Any] | None = None,
    ) -> tuple[IngestionConfigRecord, bool]:
        await ensure_dataset_registered(self._db, self._datahub, dataset_urn, require_in_datahub=False)

        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.platform = platform
            existing.locator = locator
            existing.identifier = identifier
            existing.auth = auth
            existing.is_active = is_active
            existing.schedule_tier = schedule_tier
            existing.enrichment_sources = enrichment_sources
            existing.custom_extractors = custom_extractors
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = IngestionConfig(
                dataset_urn=dataset_urn,
                platform=platform,
                locator=locator,
                identifier=identifier,
                auth=auth,
                is_active=is_active,
                schedule_tier=schedule_tier,
                enrichment_sources=enrichment_sources,
                custom_extractors=custom_extractors,
            )
            self._db.add(existing)
            created = True

        existing.status = "OK"
        self._db.add(existing)
        await self._db.commit()
        await self._db.refresh(existing)

        # Record config CRUD event
        event_type = INGESTION_CONFIG_CREATE if created else INGESTION_CONFIG_UPDATE
        await self._record_event(
            dataset_urn,
            event_type,
            "success",
            {
                "operation": "PUT",
                "config_id": str(existing.id),
                "is_active": existing.is_active,
                "schedule_tier": existing.schedule_tier,
                "workflow_dag_id": existing.workflow_dag_id,
            },
        )

        return _record_from_row(existing), created

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> IngestionConfigRecord:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        if "platform" in patch and patch["platform"] is not None:
            row.platform = patch["platform"]
        if "locator" in patch and patch["locator"] is not None:
            row.locator = patch["locator"]
        if "identifier" in patch and patch["identifier"] is not None:
            row.identifier = patch["identifier"]
        if "auth" in patch:
            row.auth = patch["auth"]
        if "is_active" in patch and patch["is_active"] is not None:
            row.is_active = patch["is_active"]
        if "schedule_tier" in patch:
            row.schedule_tier = patch["schedule_tier"]
        if "enrichment_sources" in patch:
            row.enrichment_sources = patch["enrichment_sources"]
        if "custom_extractors" in patch:
            row.custom_extractors = patch["custom_extractors"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        # Record config CRUD event
        await self._record_event(
            dataset_urn,
            INGESTION_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "config_id": str(row.id),
                "fields_changed": list(patch.keys()),
                "is_active": row.is_active,
                "schedule_tier": row.schedule_tier,
                "workflow_dag_id": row.workflow_dag_id,
            },
        )

        return _record_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        # Capture fields before deletion for the event
        config_id = str(row.id)
        schedule_tier = row.schedule_tier
        workflow_dag_id = row.workflow_dag_id

        await self._db.delete(row)
        await self._db.commit()

        # Record deletion event
        await self._record_event(
            dataset_urn,
            INGESTION_CONFIG_DELETE,
            "success",
            {
                "operation": "DELETE",
                "config_id": config_id,
                "schedule_tier": schedule_tier,
                "workflow_dag_id": workflow_dag_id,
            },
        )

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        status_filter: str | None = None,
        order_by: Any = None,
    ) -> tuple[list[IngestionConfigRecord], int]:
        base = select(IngestionConfig)
        if status_filter is not None:
            base = base.where(IngestionConfig.status == status_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = IngestionConfig.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_record_from_row(r) for r in rows], total_count

    async def list_periodic_configs(self) -> list[IngestionConfigRecord]:
        """Return all configs where is_active=true."""
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.is_active.is_(True))
        )
        rows = result.scalars().all()
        return [_record_from_row(r) for r in rows]

    async def list_periodic_datasets(self, schedule_tier: str) -> list[str]:
        """Return dataset URNs where is_active=true and schedule_tier matches the given tier."""
        result = await self._db.execute(
            select(IngestionConfig.dataset_urn).where(
                IngestionConfig.is_active.is_(True),
                IngestionConfig.schedule_tier == schedule_tier,
            )
        )
        return list(result.scalars().all())

    # ── Run pipeline ──────────────────────────────────────────────────────

    async def run(self, dataset_urn: str, dry_run: bool = False) -> IngestionRunResult:
        """Run the full ingestion pipeline.

        1. Load config from PostgreSQL.
        2. Call run_datahub_ingestion() for platform via acryl-datahub SDK.
        3. Skip enrichment/custom extractors (TBD).
        4. If not dry_run, emit results to DataHub.
        5. Record run event in PostgreSQL.
        6. Return IngestionRunResult.
        """
        run_id = str(uuid.uuid4())

        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        ingestion_result = await run_datahub_ingestion(
            datahub=self._datahub,
            platform=config.platform,
            locator=config.locator,
            identifier=config.identifier,
            auth=config.auth,
            dataset_urn=dataset_urn,
            dry_run=dry_run,
        )

        errors = ingestion_result.errors
        warnings = ingestion_result.warnings

        # A real (non-dry-run) ingestion that produced zero entities is a failure,
        # even when the extractor reported only warnings (e.g. table/topic not found).
        if not dry_run and ingestion_result.entities_ingested == 0 and not errors:
            errors = list(warnings) or ["No entities ingested from source"]

        if errors:
            status = "error"
        else:
            status = "success"

        if status == "success" and not dry_run:
            await mark_registered(self._db, dataset_urn)
            await self._db.commit()

        detail: dict[str, Any] = {
            "run_id": run_id,
            "platform": config.platform,
            "entities_ingested": ingestion_result.entities_ingested,
            "dry_run": dry_run,
        }
        if errors:
            detail["errors"] = errors
        if warnings:
            detail["warnings"] = warnings

        event_type = INGESTION_COMPLETE if status != "error" else INGESTION_FAIL
        await self._record_event(dataset_urn, event_type, status, detail)

        return IngestionRunResult(run_id=run_id, status=status, detail=detail)

    # ── Events ───────────────────────────────────────────────────────────

    async def get_events(
        self,
        dataset_urn: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
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

        events = [
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
        ]
        return events, total_count

    async def _record_event(
        self,
        dataset_urn: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="dataset",
            entity_id=dataset_urn,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()


async def run_ingestion_with_lock(
    service: IngestionService,
    cache: RedisClient,
    dataset_urn: str,
    dry_run: bool = False,
) -> IngestionRunResult:
    """Run ingestion with a Redis concurrency guard.

    Shared by the public API route and the internal Airflow activity.
    """
    lock_key = f"ingestion:running:{dataset_urn}"
    acquired = await cache.set_nx(lock_key, "1", ttl_seconds=3600)
    if not acquired:
        raise ConflictError(
            "INGESTION_RUNNING", f"Ingestion is already running for {dataset_urn}"
        )
    try:
        return await service.run(dataset_urn, dry_run=dry_run)
    finally:
        await cache.delete(lock_key)
