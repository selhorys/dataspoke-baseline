"""Metrics service — metric CRUD, run pipeline, and event recording.

Metrics are pure aggregation over pre-existing data (DataHub metadata and
validation results). The ``metric_type`` dispatches to a registered measurer.

Spec: spec/feature/BACKEND.md §Metrics Service
"""

import logging
import re as _re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, func, select
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
    METRIC_CONFIG_CREATE,
    METRIC_CONFIG_DELETE,
    METRIC_CONFIG_UPDATE,
    METRIC_PREFIX,
    METRIC_RUN_COMPLETE,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)

logger = logging.getLogger(__name__)

_DATASET_URN_RE = _re.compile(r"^urn:li:dataset:\(.+\)$")
_DATASET_FILTER_LIST_CAP = 1000

# Keys emitted by each built-in metric type (mirrors the schema-layer constant).
_EMITTED_KEYS: dict[str, set[str]] = {
    "ingestion-freshness": {"total", "ingested_in_time"},
    "validation-score": {"total", "validation_score_sum"},
    "doc-health": {"total", "doc_health"},
}


def _validate_dataset_filter(dataset_filter: dict[str, Any]) -> None:
    """Validate dataset_filter dimensions.

    Raises PreconditionFailedError when any list dimension exceeds the cap.
    Raises InvalidDatasetUrnError when a dataset URN is malformed.
    """
    for key in ("tags", "glossary_terms", "dataset_urns"):
        val = dataset_filter.get(key)
        if val is not None and len(val) > _DATASET_FILTER_LIST_CAP:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"dataset_filter.{key} may not exceed {_DATASET_FILTER_LIST_CAP} entries",
            )
    for urn in dataset_filter.get("dataset_urns", []) or []:
        if not _DATASET_URN_RE.match(str(urn)):
            raise InvalidDatasetUrnError(str(urn))


class MetricDefinitionRecord(BaseModel):
    """Value object mirroring the ORM MetricDefinition."""

    id: str
    mode: str
    metric_type: str
    title: str
    description: str
    metrics: list[str]
    metric_conf: dict[str, Any]
    dataset_filter: dict[str, Any]
    schedule_tier: str | None = None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class MetricResultRecord(BaseModel):
    """Value object mirroring the ORM MetricResult."""

    id: str
    metric_id: str
    values: dict[str, float]
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
        mode=row.mode,
        metric_type=row.metric_type,
        title=row.title,
        description=row.description,
        metrics=row.metrics or [],
        metric_conf=row.metric_conf or {},
        dataset_filter=row.dataset_filter or {},
        schedule_tier=row.schedule_tier,
        is_enabled=row.is_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: MetricResult) -> MetricResultRecord:
    return MetricResultRecord(
        id=str(row.id),
        metric_id=row.metric_id,
        values=row.values or {},
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

    # ── Config CRUD ──────────────────────────────────────────────────────────

    async def list_metrics(
        self,
        offset: int = 0,
        limit: int = 20,
        metric_type_filter: str | None = None,
        mode_filter: str | None = None,
        is_enabled_filter: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[MetricDefinitionRecord], int]:
        base = select(MetricDefinition)
        if metric_type_filter is not None:
            base = base.where(MetricDefinition.metric_type == metric_type_filter)
        if mode_filter is not None:
            base = base.where(MetricDefinition.mode == mode_filter)
        if is_enabled_filter is not None:
            base = base.where(MetricDefinition.is_enabled == is_enabled_filter)

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
            raise EntityNotFoundError("metric", metric_id)
        return _definition_from_row(row)

    async def get_metric_attr(self, metric_id: str) -> dict[str, Any]:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric", metric_id)

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
            "mode": row.mode,
            "metric_type": row.metric_type,
            "title": row.title,
            "is_enabled": row.is_enabled,
            "schedule_tier": row.schedule_tier,
            "latest_values": latest_row.values if latest_row else None,
            "latest_measured_at": latest_row.measured_at if latest_row else None,
        }

    async def get_metric_config(self, metric_id: str) -> MetricDefinitionRecord:
        return await self.get_metric(metric_id)

    async def upsert_metric_config(
        self,
        metric_id: str,
        mode: str,
        metric_type: str,
        title: str,
        description: str,
        metrics: list[str],
        metric_conf: dict[str, Any],
        dataset_filter: dict[str, Any],
        schedule_tier: str | None = None,
        is_enabled: bool = False,
    ) -> tuple[MetricDefinitionRecord, bool]:
        _validate_dataset_filter(dataset_filter)

        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.mode = mode
            existing.metric_type = metric_type
            existing.title = title
            existing.description = description
            existing.metrics = metrics
            existing.metric_conf = metric_conf
            existing.dataset_filter = dataset_filter
            existing.schedule_tier = schedule_tier
            existing.is_enabled = is_enabled
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = MetricDefinition(
                id=metric_id,
                mode=mode,
                metric_type=metric_type,
                title=title,
                description=description,
                metrics=metrics,
                metric_conf=metric_conf,
                dataset_filter=dataset_filter,
                schedule_tier=schedule_tier,
                is_enabled=is_enabled,
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
            raise EntityNotFoundError("metric", metric_id)

        if "dataset_filter" in patch and patch["dataset_filter"] is not None:
            _validate_dataset_filter(patch["dataset_filter"])

        for field_name in (
            "mode",
            "metric_type",
            "title",
            "description",
            "metrics",
            "metric_conf",
            "dataset_filter",
            "schedule_tier",
            "is_enabled",
        ):
            if field_name in patch and patch[field_name] is not None:
                setattr(row, field_name, patch[field_name])

        # Cross-field re-validation on merged state (mirrors PUT-side invariants).
        merged_type: str = row.metric_type
        merged_conf: dict[str, Any] = row.metric_conf or {}
        merged_metrics: list[str] = row.metrics or []

        if merged_type in ("ingestion-freshness", "validation-score"):
            tw = merged_conf.get("time_window_sec")
            if tw is None or not isinstance(tw, int) or tw <= 0:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER",
                    f"metric_conf.time_window_sec must be a positive int for metric_type '{merged_type}'",
                )
        elif merged_type == "doc-health":
            if merged_conf != {}:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER",
                    "metric_conf must be {} for metric_type 'doc-health'",
                )

        allowed_keys = _EMITTED_KEYS.get(merged_type, set())
        unknown_keys = set(merged_metrics) - allowed_keys
        if unknown_keys:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                (
                    f"metrics[] contains keys not emitted by '{merged_type}': "
                    f"{sorted(unknown_keys)}. Allowed: {sorted(allowed_keys)}"
                ),
            )

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            metric_id,
            METRIC_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "metric_id": metric_id,
                "fields_changed": list(patch.keys()),
            },
        )

        return _definition_from_row(row)

    async def delete_metric_config(self, metric_id: str) -> None:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric", metric_id)

        await self._db.execute(
            delete(MetricResult).where(MetricResult.metric_id == metric_id)
        )
        await self._db.delete(row)
        await self._db.commit()

        await self._record_event(
            metric_id,
            METRIC_CONFIG_DELETE,
            "success",
            {"operation": "DELETE", "metric_id": metric_id},
        )

    async def list_active_for_tier(self, tier: str) -> list[MetricDefinitionRecord]:
        """Return all is_enabled=True metric definitions for the given schedule_tier.

        Used by the metrics-{hourly,daily,weekly} DAGs.
        """
        result = await self._db.execute(
            select(MetricDefinition).where(
                MetricDefinition.is_enabled.is_(True),
                MetricDefinition.schedule_tier == tier,
            )
        )
        return [_definition_from_row(r) for r in result.scalars().all()]

    # ── Results ──────────────────────────────────────────────────────────────

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

    # ── Run pipeline ─────────────────────────────────────────────────────────

    async def run(
        self,
        metric_id: str,
        dry_run: bool = False,
    ) -> MetricRunResult:
        lock_key = f"metrics:running:{metric_id}"
        lock_token: str | None = None

        if self._cache is not None:
            lock_token = secrets.token_urlsafe(16)
            acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=3600)
            if not acquired:
                raise ConflictError(
                    "METRIC_RUNNING",
                    f"Metric measurement is already running for {metric_id}",
                )

        try:
            return await self._run_inner(metric_id, dry_run)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(self, metric_id: str, dry_run: bool) -> MetricRunResult:
        definition = await self.get_metric(metric_id)

        if not definition.is_enabled and not dry_run:
            raise ConflictError(
                "METRIC_DISABLED",
                f"Metric {metric_id} is disabled; only dry-run is permitted",
            )

        run_id = str(uuid.uuid4())

        values, breakdown, unresolved_urns = await self._measure(definition)

        detail: dict[str, Any] = {
            "run_id": run_id,
            "metric_id": metric_id,
            "values": values,
            "dry_run": dry_run,
            "unresolved_urns": unresolved_urns,
            "breakdown_summary": {
                "dataset_count": breakdown.get("dataset_count", 0),
                "affected_count": len(breakdown.get("datasets", [])),
            },
        }

        if dry_run:
            return MetricRunResult(run_id=run_id, status="success", detail=detail)

        result_row = MetricResult(
            metric_id=metric_id,
            values=values,
            breakdown=breakdown,
            measured_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()

        await self._record_event(metric_id, METRIC_RUN_COMPLETE, "success", detail)

        return MetricRunResult(run_id=run_id, status="success", detail=detail)

    # ── Events ───────────────────────────────────────────────────────────────

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

    # ── Measurement internals ────────────────────────────────────────────────

    async def _measure(
        self,
        definition: MetricDefinitionRecord,
    ) -> tuple[dict[str, float], dict[str, Any], list[str]]:
        """Run measurement and return (values, breakdown, unresolved_urns).

        Resolves dataset_filter (origin, tags, glossary_terms, dataset_urns).
        Explicit dataset_urns that don't resolve in DataHub at runtime, or whose
        origin segment mismatches the requested origin, are accumulated into
        unresolved_urns.
        """
        dataset_filter = definition.dataset_filter or {}
        origin: str | None = dataset_filter.get("origin") or None
        tags: list[str] = dataset_filter.get("tags") or []
        glossary_terms: list[str] = dataset_filter.get("glossary_terms") or []
        explicit_urns: list[str] = dataset_filter.get("dataset_urns") or []

        resolved_urns: set[str] = set()
        unresolved_urns: list[str] = []

        if not tags and not glossary_terms and not explicit_urns:
            all_datasets = await self._datahub.enumerate_datasets(origin=origin)
            resolved_urns.update(all_datasets)
        else:
            if tags or glossary_terms:
                matched = await self._datahub.enumerate_datasets(
                    tags=tags if tags else None,
                    glossary_terms=glossary_terms if glossary_terms else None,
                    origin=origin,
                )
                resolved_urns.update(matched)

            for urn in explicit_urns:
                if origin is not None:
                    urn_origin = self._datahub.origin_from_dataset_urn(urn)
                    if urn_origin != origin:
                        logger.debug(
                            "metrics_explicit_urn_origin_mismatch",
                            extra={"urn": urn, "urn_origin": urn_origin, "requested_origin": origin},
                        )
                        unresolved_urns.append(urn)
                        continue
                try:
                    from datahub.metadata.schema_classes import DatasetPropertiesClass

                    props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
                    if props is not None:
                        resolved_urns.add(urn)
                    else:
                        unresolved_urns.append(urn)
                except Exception:
                    logger.warning(
                        "metrics_explicit_urn_check_failed",
                        extra={"urn": urn},
                        exc_info=True,
                    )
                    unresolved_urns.append(urn)

        datasets = sorted(resolved_urns)

        measurer = get_measurer(definition.metric_type)
        if measurer is None:
            supported = ", ".join(f"'{m}'" for m in list_measurers())
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"Unsupported metric_type: '{definition.metric_type}'. Supported: {supported}.",
            )

        all_values, breakdown = await measurer(
            datasets, definition.metric_conf, datahub=self._datahub, db=self._db
        )

        # Filter values to the subset declared in definition.metrics
        if definition.metrics:
            filtered_values = {k: v for k, v in all_values.items() if k in definition.metrics}
        else:
            filtered_values = all_values

        return filtered_values, breakdown, unresolved_urns
