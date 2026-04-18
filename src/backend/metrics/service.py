"""Metrics service — metric CRUD, run pipeline, and event recording.

Metrics are pure aggregation over pre-existing data (DataHub metadata and
validation results). The only supported metric types are:
- ``poorly_documented``: datasets with description shorter than 20 characters
- ``stale_datasets``: datasets lacking a freshness validation rule or whose
  latest freshness result is a FAILURE
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.measurers import get_measurer, list_measurers
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    Event,
    MetricDefinition,
    MetricResult,
)
from src.shared.events import (
    METRIC_ACTIVATE,
    METRIC_CONFIG_CREATE,
    METRIC_CONFIG_DELETE,
    METRIC_CONFIG_UPDATE,
    METRIC_DEACTIVATE,
    METRIC_PREFIX,
    METRIC_RUN_COMPLETE,
)
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionError


class MetricDefinitionRecord(BaseModel):
    """Value object mirroring the ORM MetricDefinition."""

    id: str
    title: str
    description: str
    theme: str
    measurement_query: dict[str, Any]
    schedule_tier: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MetricResultRecord(BaseModel):
    """Value object mirroring the ORM MetricResult."""

    id: str
    metric_id: str
    value: float
    breakdown: dict[str, Any] | None = None
    measured_at: datetime


class MetricRunResult(BaseModel):
    """Value object for the outcome of a metric run."""

    run_id: str
    status: str
    detail: dict[str, Any]


def _definition_from_row(row: MetricDefinition) -> MetricDefinitionRecord:
    return MetricDefinitionRecord(
        id=row.id,
        title=row.title,
        description=row.description,
        theme=row.theme,
        measurement_query=row.measurement_query,
        schedule_tier=row.schedule_tier,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: MetricResult) -> MetricResultRecord:
    return MetricResultRecord(
        id=str(row.id),
        metric_id=row.metric_id,
        value=row.value,
        breakdown=row.breakdown,
        measured_at=row.measured_at,
    )


class MetricsService:
    """Metric CRUD, run pipeline, and event recording."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    # ── Config CRUD ──────────────────────────────────────────────────────

    async def list_metrics(
        self,
        offset: int = 0,
        limit: int = 20,
        theme_filter: str | None = None,
        is_active_filter: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[MetricDefinitionRecord], int]:
        base = select(MetricDefinition)
        if theme_filter is not None:
            base = base.where(MetricDefinition.theme == theme_filter)
        if is_active_filter is not None:
            base = base.where(MetricDefinition.is_active == is_active_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetricDefinition.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_definition_from_row(r) for r in rows], total_count

    async def get_metric(self, metric_id: str) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)
        return _definition_from_row(row)

    async def get_metric_attr(self, metric_id: str) -> dict[str, Any]:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)

        # Fetch latest result
        latest_q = (
            select(MetricResult)
            .where(MetricResult.metric_id == metric_id)
            .order_by(MetricResult.measured_at.desc())
            .limit(1)
        )
        latest_result = await self._db.execute(latest_q)
        latest_row = latest_result.scalar_one_or_none()

        return {
            "id": row.id,
            "title": row.title,
            "theme": row.theme,
            "is_active": row.is_active,
            "schedule_tier": row.schedule_tier,
            "latest_value": latest_row.value if latest_row else None,
            "latest_measured_at": latest_row.measured_at if latest_row else None,
        }

    async def get_metric_config(self, metric_id: str) -> MetricDefinitionRecord:
        return await self.get_metric(metric_id)

    async def upsert_metric_config(
        self,
        metric_id: str,
        title: str,
        description: str,
        theme: str,
        measurement_query: dict[str, Any],
        schedule_tier: str | None = None,
        is_active: bool = True,
    ) -> tuple[MetricDefinitionRecord, bool]:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.title = title
            existing.description = description
            existing.theme = theme
            existing.measurement_query = measurement_query
            existing.schedule_tier = schedule_tier
            existing.is_active = is_active
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = MetricDefinition(
                id=metric_id,
                title=title,
                description=description,
                theme=theme,
                measurement_query=measurement_query,
                schedule_tier=schedule_tier,
                is_active=is_active,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)

        event_type = METRIC_CONFIG_CREATE if created else METRIC_CONFIG_UPDATE
        await self._record_event(
            metric_id,
            event_type,
            "success",
            {"operation": "PUT", "metric_id": metric_id},
        )

        return _definition_from_row(existing), created

    async def patch_metric_config(
        self, metric_id: str, patch: dict[str, Any]
    ) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)

        for field in (
            "title",
            "description",
            "theme",
            "measurement_query",
            "schedule_tier",
            "is_active",
        ):
            if field in patch and patch[field] is not None:
                setattr(row, field, patch[field])

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            metric_id,
            METRIC_CONFIG_UPDATE,
            "success",
            {"operation": "PATCH", "metric_id": metric_id, "fields_changed": list(patch.keys())},
        )

        return _definition_from_row(row)

    async def delete_metric_config(self, metric_id: str) -> None:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)

        await self._db.delete(row)
        await self._db.commit()

        await self._record_event(
            metric_id,
            METRIC_CONFIG_DELETE,
            "success",
            {"operation": "DELETE", "metric_id": metric_id},
        )

    # ── Results ──────────────────────────────────────────────────────────

    async def get_results(
        self,
        metric_id: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[MetricResultRecord], int]:
        base = select(MetricResult).where(MetricResult.metric_id == metric_id)

        if from_dt is not None:
            base = base.where(MetricResult.measured_at >= from_dt)
        if to_dt is not None:
            base = base.where(MetricResult.measured_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetricResult.measured_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_result_from_row(r) for r in rows], total_count

    # ── Run pipeline ─────────────────────────────────────────────────────

    async def run(self, metric_id: str, dry_run: bool = False) -> MetricRunResult:
        definition = await self.get_metric(metric_id)
        run_id = str(uuid.uuid4())

        # 1. Measure
        value, breakdown = await self._measure(definition.measurement_query)

        # 2. Build detail
        detail: dict[str, Any] = {
            "run_id": run_id,
            "metric_id": metric_id,
            "value": value,
            "dry_run": dry_run,
            "breakdown_summary": {
                "dataset_count": breakdown.get("dataset_count", 0),
                "affected_count": len(breakdown.get("datasets", [])),
            },
        }

        if dry_run:
            return MetricRunResult(run_id=run_id, status="success", detail=detail)

        # 3. Persist result
        result_row = MetricResult(
            metric_id=metric_id,
            value=value,
            breakdown=breakdown,
            measured_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()

        # 4. Record event
        await self._record_event(metric_id, METRIC_RUN_COMPLETE, "success", detail)

        return MetricRunResult(run_id=run_id, status="success", detail=detail)

    # ── Activate / Deactivate ────────────────────────────────────────────

    async def activate(self, metric_id: str) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)
        if row.is_active:
            raise ConflictError("ALREADY_ACTIVE", f"Metric '{metric_id}' is already active")

        row.is_active = True
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(metric_id, METRIC_ACTIVATE, "success", {"metric_id": metric_id})
        return _definition_from_row(row)

    async def deactivate(self, metric_id: str) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)
        if not row.is_active:
            raise ConflictError("ALREADY_INACTIVE", f"Metric '{metric_id}' is already inactive")

        row.is_active = False
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            metric_id, METRIC_DEACTIVATE, "success", {"metric_id": metric_id}
        )
        return _definition_from_row(row)

    # ── Events ───────────────────────────────────────────────────────────

    async def get_events(
        self,
        metric_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "metric",
            Event.entity_id == metric_id,
            Event.event_type.startswith(METRIC_PREFIX),
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
        metric_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="metric",
            entity_id=metric_id,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()

    # ── Measurement internals ────────────────────────────────────────────

    async def _measure(self, measurement_query: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        metric_type = measurement_query.get("type", "")
        dataset_filter = measurement_query.get("dataset_filter") or {}
        tags = dataset_filter.get("tags")
        glossary_terms = dataset_filter.get("glossary_terms")

        datasets = await self._datahub.enumerate_datasets(
            tags=tags, glossary_terms=glossary_terms
        )

        measurer = get_measurer(metric_type)
        if measurer is None:
            supported = ", ".join(f"'{m}'" for m in list_measurers())
            raise PreconditionError(
                "UNSUPPORTED_METRIC_TYPE",
                f"Unsupported metric type: '{metric_type}'. "
                f"Supported types: {supported}.",
            )

        return await measurer(datasets, datahub=self._datahub, db=self._db)
