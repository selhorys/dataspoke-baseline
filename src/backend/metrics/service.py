"""Metrics service — metric CRUD, run pipeline, alarm evaluation, and event recording."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, MetricDefinition, MetricResult
from src.shared.exceptions import ConflictError, EntityNotFoundError
from src.shared.notifications.service import NotificationService

_MAX_BREAKDOWN_AFFECTED = 100


class MetricDefinitionRecord:
    """Value object mirroring the ORM MetricDefinition."""

    __slots__ = (
        "id",
        "title",
        "description",
        "theme",
        "measurement_query",
        "schedule",
        "alarm_enabled",
        "alarm_threshold",
        "active",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        id: str,
        title: str,
        description: str,
        theme: str,
        measurement_query: dict[str, Any],
        schedule: str | None,
        alarm_enabled: bool,
        alarm_threshold: dict[str, Any] | None,
        active: bool,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.theme = theme
        self.measurement_query = measurement_query
        self.schedule = schedule
        self.alarm_enabled = alarm_enabled
        self.alarm_threshold = alarm_threshold
        self.active = active
        self.created_at = created_at
        self.updated_at = updated_at


class MetricResultRecord:
    """Value object mirroring the ORM MetricResult."""

    __slots__ = (
        "id",
        "metric_id",
        "value",
        "breakdown",
        "alarm_triggered",
        "run_id",
        "measured_at",
    )

    def __init__(
        self,
        id: str,
        metric_id: str,
        value: float,
        breakdown: dict[str, Any] | None,
        alarm_triggered: bool,
        run_id: str,
        measured_at: datetime,
    ) -> None:
        self.id = id
        self.metric_id = metric_id
        self.value = value
        self.breakdown = breakdown
        self.alarm_triggered = alarm_triggered
        self.run_id = run_id
        self.measured_at = measured_at


class MetricRunResult:
    """Value object for the outcome of a metric run."""

    __slots__ = ("run_id", "status", "detail")

    def __init__(self, run_id: str, status: str, detail: dict[str, Any]) -> None:
        self.run_id = run_id
        self.status = status
        self.detail = detail


def _definition_from_row(row: MetricDefinition) -> MetricDefinitionRecord:
    return MetricDefinitionRecord(
        id=row.id,
        title=row.title,
        description=row.description,
        theme=row.theme,
        measurement_query=row.measurement_query,
        schedule=row.schedule,
        alarm_enabled=row.alarm_enabled,
        alarm_threshold=row.alarm_threshold,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: MetricResult) -> MetricResultRecord:
    return MetricResultRecord(
        id=str(row.id),
        metric_id=row.metric_id,
        value=row.value,
        breakdown=row.breakdown,
        alarm_triggered=row.alarm_triggered,
        run_id=str(row.run_id),
        measured_at=row.measured_at,
    )


_THRESHOLD_OPS: dict[str, Any] = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
}


class MetricsService:
    """Metric CRUD, run pipeline, alarm evaluation, and event recording."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
        notification: NotificationService | None = None,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache
        self._notification = notification

    # ── Config CRUD ──────────────────────────────────────────────────────

    async def list_metrics(
        self,
        offset: int = 0,
        limit: int = 20,
        theme_filter: str | None = None,
        active_filter: bool | None = None,
    ) -> tuple[list[MetricDefinitionRecord], int]:
        base = select(MetricDefinition)
        if theme_filter is not None:
            base = base.where(MetricDefinition.theme == theme_filter)
        if active_filter is not None:
            base = base.where(MetricDefinition.active == active_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(MetricDefinition.created_at.desc()).offset(offset).limit(limit)
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
            "active": row.active,
            "alarm_enabled": row.alarm_enabled,
            "schedule": row.schedule,
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
        schedule: str | None = None,
        alarm_enabled: bool = False,
        alarm_threshold: dict[str, Any] | None = None,
        active: bool = True,
    ) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.title = title
            existing.description = description
            existing.theme = theme
            existing.measurement_query = measurement_query
            existing.schedule = schedule
            existing.alarm_enabled = alarm_enabled
            existing.alarm_threshold = alarm_threshold
            existing.active = active
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
        else:
            existing = MetricDefinition(
                id=metric_id,
                title=title,
                description=description,
                theme=theme,
                measurement_query=measurement_query,
                schedule=schedule,
                alarm_enabled=alarm_enabled,
                alarm_threshold=alarm_threshold,
                active=active,
            )
            self._db.add(existing)

        await self._db.commit()
        await self._db.refresh(existing)
        return _definition_from_row(existing)

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
            "schedule",
            "alarm_enabled",
            "alarm_threshold",
            "active",
        ):
            if field in patch and patch[field] is not None:
                setattr(row, field, patch[field])

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
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

    # ── Results ──────────────────────────────────────────────────────────

    async def get_results(
        self,
        metric_id: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[MetricResultRecord], int]:
        base = select(MetricResult).where(MetricResult.metric_id == metric_id)

        if from_dt is not None:
            base = base.where(MetricResult.measured_at >= from_dt)
        if to_dt is not None:
            base = base.where(MetricResult.measured_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(MetricResult.measured_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_result_from_row(r) for r in rows], total_count

    # ── Run pipeline ─────────────────────────────────────────────────────

    async def run(self, metric_id: str, dry_run: bool = False) -> MetricRunResult:
        definition = await self.get_metric(metric_id)
        run_id = str(uuid.uuid4())

        # 1. Measure
        value, breakdown = await self._measure(definition.measurement_query)

        # 2. Delta detection against previous run
        prev_q = (
            select(MetricResult)
            .where(MetricResult.metric_id == metric_id)
            .order_by(MetricResult.measured_at.desc())
            .limit(1)
        )
        prev_result = await self._db.execute(prev_q)
        prev_row = prev_result.scalar_one_or_none()

        delta = self._compute_delta(breakdown, prev_row)
        if delta:
            breakdown["delta"] = delta

        # 3. Alarm evaluation
        alarm_triggered = self._check_threshold(value, definition.alarm_threshold)

        detail: dict[str, Any] = {
            "run_id": run_id,
            "metric_id": metric_id,
            "value": value,
            "alarm_triggered": alarm_triggered,
            "dry_run": dry_run,
            "breakdown_summary": {
                "metric_type": breakdown.get("metric_type"),
                "scanned_count": breakdown.get("scanned_count", 0),
                "affected_count": len(breakdown.get("affected_datasets", [])),
            },
        }

        if dry_run:
            return MetricRunResult(run_id=run_id, status="success", detail=detail)

        # 4. Persist result
        result_row = MetricResult(
            metric_id=metric_id,
            value=value,
            breakdown=breakdown,
            alarm_triggered=alarm_triggered,
            run_id=uuid.UUID(run_id),
            measured_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()

        # 5. Record events
        await self._record_event(metric_id, "metric.run.completed", "success", detail)

        if alarm_triggered:
            alarm_detail = {
                "metric_id": metric_id,
                "value": value,
                "threshold": definition.alarm_threshold,
            }
            await self._record_event(metric_id, "metric.alarm.triggered", "warning", alarm_detail)

            if self._notification and definition.alarm_enabled:
                threshold_val = (definition.alarm_threshold or {}).get("value", value)
                try:
                    await self._notification.send_alarm(
                        recipients=[],
                        metric_id=metric_id,
                        value=value,
                        threshold=threshold_val,
                    )
                except Exception:
                    pass

        # 6. Findings event
        new_findings = (delta or {}).get("new_findings", [])
        if new_findings:
            findings_detail = {
                "metric_id": metric_id,
                "finding_count": len(new_findings),
                "affected_urns": new_findings[:20],
            }
            await self._record_event(metric_id, "metric.findings.detected", "info", findings_detail)

        return MetricRunResult(run_id=run_id, status="success", detail=detail)

    # ── Activate / Deactivate ────────────────────────────────────────────

    async def activate(self, metric_id: str) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)
        if row.active:
            raise ConflictError("ALREADY_ACTIVE", f"Metric '{metric_id}' is already active")

        row.active = True
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(metric_id, "metric.activated", "success", {"metric_id": metric_id})
        return _definition_from_row(row)

    async def deactivate(self, metric_id: str) -> MetricDefinitionRecord:
        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metric_definition", metric_id)
        if not row.active:
            raise ConflictError("ALREADY_INACTIVE", f"Metric '{metric_id}' is already inactive")

        row.active = False
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            metric_id, "metric.deactivated", "success", {"metric_id": metric_id}
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
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "metric",
            Event.entity_id == metric_id,
        )

        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(Event.occurred_at.desc()).offset(offset).limit(limit)
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
        metric_type = measurement_query.get("type", "dataset_count")
        platform = measurement_query.get("platform")

        datasets = await self._datahub.enumerate_datasets(platform=platform)

        if metric_type == "dataset_count":
            return float(len(datasets)), {
                "metric_type": metric_type,
                "scanned_count": len(datasets),
                "affected_datasets": [],
            }

        if metric_type == "poorly_documented":
            return await self._measure_poorly_documented(datasets)

        if metric_type == "stale_datasets":
            return await self._measure_stale_datasets(datasets)

        if metric_type == "low_quality":
            return await self._measure_low_quality(datasets)

        if metric_type == "unowned_datasets":
            return await self._measure_unowned(datasets)

        if metric_type == "tag_coverage":
            return await self._measure_tag_coverage(datasets)

        return float(len(datasets)), {
            "metric_type": metric_type,
            "scanned_count": len(datasets),
            "affected_datasets": [],
        }

    async def _measure_poorly_documented(self, datasets: list[str]) -> tuple[float, dict[str, Any]]:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        affected: list[dict[str, Any]] = []
        for urn in datasets:
            props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
            desc = getattr(props, "description", None) or "" if props else ""
            if len(desc) < 20:
                affected.append(
                    {"urn": urn, "reason": "description < 20 chars", "current_value": desc}
                )
                if len(affected) >= _MAX_BREAKDOWN_AFFECTED:
                    break

        return float(len(affected)), {
            "metric_type": "poorly_documented",
            "scanned_count": len(datasets),
            "affected_datasets": affected,
        }

    async def _measure_stale_datasets(self, datasets: list[str]) -> tuple[float, dict[str, Any]]:
        from datahub.metadata.schema_classes import OperationClass

        affected: list[dict[str, Any]] = []
        now = datetime.now(tz=UTC)
        for urn in datasets:
            operations = await self._datahub.get_timeseries(urn, OperationClass, limit=1)
            if not operations:
                affected.append({"urn": urn, "reason": "no operations recorded"})
                if len(affected) >= _MAX_BREAKDOWN_AFFECTED:
                    break
                continue
            last_ts = getattr(operations[0], "lastUpdatedTimestamp", None) or getattr(
                operations[0], "timestampMillis", None
            )
            if last_ts is not None:
                last_dt = datetime.fromtimestamp(last_ts / 1000, tz=UTC)
                days_ago = (now - last_dt).total_seconds() / 86400
                if days_ago > 7:
                    affected.append(
                        {
                            "urn": urn,
                            "reason": f"stale ({days_ago:.1f} days)",
                            "days_since_update": round(days_ago, 1),
                        }
                    )
                    if len(affected) >= _MAX_BREAKDOWN_AFFECTED:
                        break

        return float(len(affected)), {
            "metric_type": "stale_datasets",
            "scanned_count": len(datasets),
            "affected_datasets": affected,
        }

    async def _measure_low_quality(self, datasets: list[str]) -> tuple[float, dict[str, Any]]:
        from src.backend.validation.scoring import compute_quality_score

        affected: list[dict[str, Any]] = []
        for urn in datasets:
            try:
                score = await compute_quality_score(self._datahub, urn, cache=self._cache)
                if score.overall_score < 50:
                    affected.append(
                        {
                            "urn": urn,
                            "reason": f"quality score {score.overall_score}",
                            "score": score.overall_score,
                        }
                    )
                    if len(affected) >= _MAX_BREAKDOWN_AFFECTED:
                        break
            except Exception:
                continue

        return float(len(affected)), {
            "metric_type": "low_quality",
            "scanned_count": len(datasets),
            "affected_datasets": affected,
        }

    async def _measure_unowned(self, datasets: list[str]) -> tuple[float, dict[str, Any]]:
        from datahub.metadata.schema_classes import OwnershipClass

        affected: list[dict[str, Any]] = []
        for urn in datasets:
            ownership = await self._datahub.get_aspect(urn, OwnershipClass)
            has_owner = ownership is not None and bool(getattr(ownership, "owners", []))
            if not has_owner:
                affected.append({"urn": urn, "reason": "no owner assigned"})
                if len(affected) >= _MAX_BREAKDOWN_AFFECTED:
                    break

        return float(len(affected)), {
            "metric_type": "unowned_datasets",
            "scanned_count": len(datasets),
            "affected_datasets": affected,
        }

    async def _measure_tag_coverage(self, datasets: list[str]) -> tuple[float, dict[str, Any]]:
        from datahub.metadata.schema_classes import GlobalTagsClass

        tagged_count = 0
        affected: list[dict[str, Any]] = []
        for urn in datasets:
            tags = await self._datahub.get_aspect(urn, GlobalTagsClass)
            has_tags = tags is not None and bool(getattr(tags, "tags", []))
            if has_tags:
                tagged_count += 1
            else:
                if len(affected) < _MAX_BREAKDOWN_AFFECTED:
                    affected.append({"urn": urn, "reason": "no tags"})

        coverage = (tagged_count / len(datasets) * 100) if datasets else 0.0
        return round(coverage, 2), {
            "metric_type": "tag_coverage",
            "scanned_count": len(datasets),
            "affected_datasets": affected,
        }

    @staticmethod
    def _check_threshold(value: float, threshold: dict[str, Any] | None) -> bool:
        if not threshold:
            return False
        operator = threshold.get("operator")
        threshold_value = threshold.get("value")
        if operator is None or threshold_value is None:
            return False
        op_fn = _THRESHOLD_OPS.get(operator)
        if op_fn is None:
            return False
        return op_fn(value, threshold_value)

    @staticmethod
    def _compute_delta(
        breakdown: dict[str, Any], prev_row: MetricResult | None
    ) -> dict[str, Any] | None:
        if prev_row is None:
            return None
        prev_breakdown = prev_row.breakdown or {}
        current_urns = {d["urn"] for d in breakdown.get("affected_datasets", []) if "urn" in d}
        prev_urns = {d["urn"] for d in prev_breakdown.get("affected_datasets", []) if "urn" in d}
        new_findings = sorted(current_urns - prev_urns)
        resolved = sorted(prev_urns - current_urns)
        if not new_findings and not resolved:
            return None
        return {"new_findings": new_findings, "resolved_since_last": resolved}
