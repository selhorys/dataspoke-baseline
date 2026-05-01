"""Ingestion service — config CRUD, run pipeline, passive sync, and event recording.

Spec: spec/feature/BACKEND.md §Ingestion Service
"""

import logging
import secrets
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
    mode: str
    platform: str
    locator: dict[str, Any]
    identifier: dict[str, Any]
    auth: dict[str, Any] | None = None
    is_enabled: bool
    schedule_tier: str | None = None
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
        mode=row.mode,
        platform=row.platform,
        locator=row.locator,
        identifier=row.identifier,
        auth=row.auth,
        is_enabled=row.is_enabled,
        schedule_tier=row.schedule_tier,
        workflow_dag_id=row.workflow_dag_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class IngestionService:
    """Config CRUD, run pipeline, passive sync, and event recording for ingestion."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient | None = None,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    # ── Config CRUD ──────────────────────────────────────────────────────────

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
        mode: str,
        platform: str,
        locator: dict[str, Any],
        identifier: dict[str, Any],
        auth: dict[str, Any] | None,
        is_enabled: bool,
        schedule_tier: str | None,
    ) -> tuple[IngestionConfigRecord, bool]:
        await ensure_dataset_registered(
            self._db, self._datahub, dataset_urn, require_in_datahub=False
        )

        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.mode = mode
            existing.platform = platform
            existing.locator = locator
            existing.identifier = identifier
            existing.auth = auth
            existing.is_enabled = is_enabled
            existing.schedule_tier = schedule_tier
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = IngestionConfig(
                dataset_urn=dataset_urn,
                mode=mode,
                platform=platform,
                locator=locator,
                identifier=identifier,
                auth=auth,
                is_enabled=is_enabled,
                schedule_tier=schedule_tier,
            )
            self._db.add(existing)
            created = True

        existing.status = "OK"
        self._db.add(existing)
        await self._db.commit()
        await self._db.refresh(existing)

        event_type = INGESTION_CONFIG_CREATE if created else INGESTION_CONFIG_UPDATE
        await self._record_event(
            dataset_urn,
            event_type,
            "success",
            {
                "operation": "PUT",
                "config_id": str(existing.id),
                "mode": existing.mode,
                "is_enabled": existing.is_enabled,
                "schedule_tier": existing.schedule_tier,
                "workflow_dag_id": existing.workflow_dag_id,
            },
        )

        return _record_from_row(existing), created

    async def patch_config(
        self, dataset_urn: str, patch: dict[str, Any]
    ) -> IngestionConfigRecord:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        if "mode" in patch and patch["mode"] is not None:
            row.mode = patch["mode"]
        if "platform" in patch and patch["platform"] is not None:
            row.platform = patch["platform"]
        if "locator" in patch and patch["locator"] is not None:
            row.locator = patch["locator"]
        if "identifier" in patch and patch["identifier"] is not None:
            row.identifier = patch["identifier"]
        if "auth" in patch:
            row.auth = patch["auth"]
        if "is_enabled" in patch and patch["is_enabled"] is not None:
            row.is_enabled = patch["is_enabled"]
        if "schedule_tier" in patch:
            row.schedule_tier = patch["schedule_tier"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            dataset_urn,
            INGESTION_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "config_id": str(row.id),
                "fields_changed": list(patch.keys()),
                "is_enabled": row.is_enabled,
                "schedule_tier": row.schedule_tier,
            },
        )

        return _record_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        config_id = str(row.id)
        schedule_tier = row.schedule_tier
        workflow_dag_id = row.workflow_dag_id

        await self._db.delete(row)
        await self._db.commit()

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

    async def list_active_for_tier(self, tier: str) -> list[str]:
        """Return dataset URNs where is_enabled=True, mode='active', and schedule_tier matches.

        Used by the ingestion-active-{hourly,daily,weekly} DAGs.
        """
        result = await self._db.execute(
            select(IngestionConfig.dataset_urn).where(
                IngestionConfig.is_enabled.is_(True),
                IngestionConfig.mode == "active",
                IngestionConfig.schedule_tier == tier,
            )
        )
        return list(result.scalars().all())

    async def list_passive_configs(self) -> list[IngestionConfigRecord]:
        """Return all mode='passive' configs (any is_enabled state).

        Used by sync_passive_status() and the passive-hourly DAG.
        """
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.mode == "passive")
        )
        return [_record_from_row(r) for r in result.scalars().all()]

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(self, dataset_urn: str, dry_run: bool = False) -> IngestionRunResult:
        """Run the full ingestion pipeline with a Redis SETNX concurrency guard.

        1. Acquire Redis SETNX guard (if cache is available).
        2. Load config from PostgreSQL.
        3. Call run_datahub_ingestion() for platform via acryl-datahub SDK.
        4. If not dry_run: emit results to DataHub; mark dataset registered.
        5. Record run event in PostgreSQL.
        6. Return IngestionRunResult.

        Raises ConflictError("INGESTION_RUNNING") when the lock is already held.
        The lock is released via CAS (compare-and-swap) in the finally block,
        preventing a worker whose TTL expired from deleting a successor's lock.
        """
        lock_key = f"ingestion:running:{dataset_urn}"
        lock_token: str | None = None

        if self._cache is not None:
            lock_token = secrets.token_urlsafe(16)
            acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=3600)
            if not acquired:
                raise ConflictError(
                    "INGESTION_RUNNING",
                    f"Ingestion is already running for {dataset_urn}",
                )

        try:
            return await self._run_inner(dataset_urn, dry_run)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(self, dataset_urn: str, dry_run: bool) -> IngestionRunResult:
        """Inner run logic (called inside the SETNX guard)."""
        run_id = str(uuid.uuid4())

        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("config", dataset_urn)

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

        # A real (non-dry-run) ingestion that produced zero entities is a failure
        if not dry_run and ingestion_result.entities_ingested == 0 and not errors:
            errors = list(warnings) or ["No entities ingested from source"]

        status = "error" if errors else "success"

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

    # ── Passive status sync ───────────────────────────────────────────────────

    async def sync_passive_status(self) -> dict[str, Any]:
        """Mirror DataHub ingestion run history for all passive-mode configs.

        Called by the ``ingestion-passive-hourly`` DAG.

        For each passive config:
          - Query DataHub for ingestion run history of the dataset URN.
          - Insert any new runs as rows in the unified ``events`` table with
            ``event_type = INGESTION.COMPLETE`` or ``INGESTION.FAIL``.
          - Deduplication: checks ``(entity_id, event_type, occurred_at)``
            before inserting.

        Best-effort per dataset — failures are logged at WARNING and the loop
        continues to the next dataset.

        Spec: spec/feature/BACKEND.md §Passive status-sync pipeline
        """
        configs = await self.list_passive_configs()
        synced = 0
        skipped = 0
        errors = 0

        for cfg in configs:
            try:
                new_events = await self._sync_one_passive(cfg.dataset_urn)
                synced += new_events
            except Exception:
                logger.warning(
                    "ingestion_passive_sync_failed",
                    extra={"dataset_urn": cfg.dataset_urn},
                    exc_info=True,
                )
                errors += 1

        return {"synced_events": synced, "skipped": skipped, "errors": errors}

    async def _sync_one_passive(self, dataset_urn: str) -> int:
        """Sync DataHub run history for one passive-mode dataset.

        Returns the number of new event rows inserted.
        """
        # Query DataHub ingestion run history via GraphQL.
        # DataHub does not have a dedicated "get ingestion run history for a dataset URN"
        # REST endpoint; we use the executions GraphQL API.
        run_history = await self._fetch_datahub_run_history(dataset_urn)

        inserted = 0
        for run in run_history:
            run_id: str = run.get("run_id", "")
            run_status: str = run.get("status", "")
            datahub_run_at: datetime | None = run.get("run_at")

            if datahub_run_at is None:
                continue

            event_type = (
                INGESTION_COMPLETE if run_status in ("SUCCEEDED", "SUCCESS") else INGESTION_FAIL
            )

            # Deduplicate by (entity_id, event_type, occurred_at)
            existing = (
                await self._db.execute(
                    select(Event).where(
                        Event.entity_id == dataset_urn,
                        Event.event_type == event_type,
                        Event.occurred_at == datahub_run_at,
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                continue

            detail: dict[str, Any] = {
                "source": "passive",
                "run_id": run_id,
                "datahub_run_at": datahub_run_at.isoformat(),
                "datahub_status": run_status,
            }
            event = Event(
                entity_type="dataset",
                entity_id=dataset_urn,
                event_type=event_type,
                status="success" if event_type == INGESTION_COMPLETE else "failure",
                detail=detail,
                occurred_at=datahub_run_at,
            )
            self._db.add(event)
            inserted += 1

        if inserted > 0:
            await self._db.commit()

        return inserted

    async def _fetch_datahub_run_history(
        self, dataset_urn: str
    ) -> list[dict[str, Any]]:
        """Query DataHub for ingestion execution history for *dataset_urn*.

        Returns a list of dicts with: run_id, status, run_at (datetime | None).

        Best-effort — raises on DataHub connectivity failures (caller wraps).
        """
        gql = """
        query getIngestionRunHistory($urn: String!) {
            dataset(urn: $urn) {
                runs(start: 0, count: 50) {
                    runs {
                        urn
                        state
                        aspectList {
                            name
                            payload
                        }
                    }
                }
            }
        }
        """
        try:
            result = await self._datahub._with_retry(
                self._datahub._graph.execute_graphql,
                gql,
                variables={"urn": dataset_urn},
            )
        except Exception:
            logger.warning(
                "ingestion_passive_datahub_query_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )
            return []

        runs_data = (
            (result or {}).get("dataset", {}) or {}
        ).get("runs", {}).get("runs", []) or []

        out: list[dict[str, Any]] = []
        for run in runs_data:
            run_urn = run.get("urn", "")
            state = run.get("state", "")
            run_at: datetime | None = None

            # Attempt to extract timestamp from aspectList
            for aspect in run.get("aspectList") or []:
                if aspect.get("name") in ("executionRequestResult", "dataHubIngestionRunSummary"):
                    try:
                        import json as _json

                        payload = aspect.get("payload") or {}
                        if isinstance(payload, str):
                            payload = _json.loads(payload)
                        ts_ms = (
                            payload.get("startTimeMs")
                            or payload.get("timestampMillis")
                            or payload.get("startedAt")
                        )
                        if ts_ms and isinstance(ts_ms, (int, float)):
                            run_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                    except Exception:
                        pass

            out.append(
                {
                    "run_id": run_urn,
                    "status": state,
                    "run_at": run_at or datetime.now(tz=UTC),
                }
            )
        return out

    # ── Events ───────────────────────────────────────────────────────────────

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


# ── Standalone helpers ────────────────────────────────────────────────────────


async def run_ingestion_with_lock(
    service: IngestionService,
    cache: RedisClient,
    dataset_urn: str,
    dry_run: bool = False,
) -> IngestionRunResult:
    """Run ingestion with a Redis SETNX concurrency guard.

    Convenience wrapper for callers that construct IngestionService without
    injecting cache (e.g., legacy call sites).  Prefers using the service's
    own guard if the service already has a cache; otherwise temporarily injects
    the supplied cache and delegates to ``service.run()``.

    For new call sites, prefer constructing IngestionService with a ``cache``
    argument so the guard is self-contained.
    """
    if service._cache is None:
        service._cache = cache
        try:
            return await service.run(dataset_urn, dry_run=dry_run)
        finally:
            service._cache = None
    else:
        return await service.run(dataset_urn, dry_run=dry_run)
