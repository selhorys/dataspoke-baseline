"""Validation service — config CRUD, run pipeline, results, and event recording."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.scoring import compute_quality_score
from src.shared.cache.client import RedisClient
from src.shared.config import VALIDATION_RESULT_CACHE_TTL
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, ValidationConfig, ValidationResult
from src.shared.exceptions import EntityNotFoundError
from src.shared.models.quality import QualityScore


class ValidationConfigRecord(BaseModel):
    """Value object mirroring the ORM ValidationConfig."""

    id: str
    dataset_urn: str
    rules: dict[str, Any]
    schedule: str | None = None
    sla_target: dict[str, Any] | None = None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class ValidationRunResult(BaseModel):
    """Value object for the outcome of a validation run."""

    run_id: str
    status: str
    detail: dict[str, Any]


class ValidationResultRecord(BaseModel):
    """Value object mirroring the ORM ValidationResult."""

    id: str
    dataset_urn: str
    quality_score: float
    dimensions: dict[str, float]
    issues: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    recommendations: list[str] = []
    alternatives: list[str] = []
    run_id: str
    measured_at: datetime


def _config_from_row(row: ValidationConfig) -> ValidationConfigRecord:
    return ValidationConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        rules=row.rules,
        schedule=row.schedule,
        sla_target=row.sla_target,
        status=row.status,
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: ValidationResult) -> ValidationResultRecord:
    return ValidationResultRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        quality_score=row.quality_score,
        dimensions=row.dimensions,
        issues=row.issues,
        anomalies=row.anomalies,
        recommendations=row.recommendations,
        alternatives=row.alternatives,
        run_id=str(row.run_id),
        measured_at=row.measured_at,
    )


class ValidationService:
    """Config CRUD, run pipeline, results query, and event recording for validation."""

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

    async def get_config(self, dataset_urn: str) -> ValidationConfigRecord | None:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _config_from_row(row)

    async def upsert_config(
        self,
        dataset_urn: str,
        rules: dict[str, Any],
        schedule: str | None,
        sla_target: dict[str, Any] | None,
        owner: str,
    ) -> ValidationConfigRecord:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.rules = rules
            existing.schedule = schedule
            existing.sla_target = sla_target
            existing.owner = owner
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
        else:
            existing = ValidationConfig(
                dataset_urn=dataset_urn,
                rules=rules,
                schedule=schedule,
                sla_target=sla_target,
                owner=owner,
            )
            self._db.add(existing)

        await self._db.commit()
        await self._db.refresh(existing)
        return _config_from_row(existing)

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> ValidationConfigRecord:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        if "rules" in patch and patch["rules"] is not None:
            row.rules = patch["rules"]
        if "schedule" in patch and patch["schedule"] is not None:
            row.schedule = patch["schedule"]
        if "sla_target" in patch and patch["sla_target"] is not None:
            row.sla_target = patch["sla_target"]
        if "status" in patch and patch["status"] is not None:
            row.status = patch["status"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        await self._db.delete(row)
        await self._db.commit()

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        status_filter: str | None = None,
    ) -> tuple[list[ValidationConfigRecord], int]:
        base = select(ValidationConfig)
        if status_filter is not None:
            base = base.where(ValidationConfig.status == status_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(ValidationConfig.created_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_config_from_row(r) for r in rows], total_count

    # ── Results ──────────────────────────────────────────────────────────

    async def get_results(
        self,
        dataset_urn: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ValidationResultRecord], int]:
        base = select(ValidationResult).where(ValidationResult.dataset_urn == dataset_urn)

        if from_dt is not None:
            base = base.where(ValidationResult.measured_at >= from_dt)
        if to_dt is not None:
            base = base.where(ValidationResult.measured_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(ValidationResult.measured_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_result_from_row(r) for r in rows], total_count

    # ── Run pipeline ─────────────────────────────────────────────────────

    async def run(
        self,
        dataset_urn: str,
        config_id: str | None = None,
        dry_run: bool = False,
    ) -> ValidationRunResult:
        # TODO: dispatch to Temporal workflow
        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        run_id = str(uuid.uuid4())

        # 1. Compute quality score
        score: QualityScore = await compute_quality_score(
            self._datahub, dataset_urn, cache=self._cache
        )

        # 2. Build issues from low-scoring dimensions
        issues: list[dict[str, Any]] = []
        for dim_name, dim_score in score.dimensions.items():
            if dim_score < 50:
                issues.append(
                    {
                        "dimension": dim_name,
                        "score": dim_score,
                        "severity": "critical" if dim_score < 25 else "warning",
                        "message": f"{dim_name} score is low ({dim_score})",
                    }
                )

        # 3. Anomaly detection
        from datahub.metadata.schema_classes import DatasetProfileClass, OperationClass

        from src.backend.validation.anomaly import detect_anomalies

        profiles = await self._datahub.get_timeseries(dataset_urn, DatasetProfileClass, limit=30)
        operations = await self._datahub.get_timeseries(dataset_urn, OperationClass, limit=30)

        anomaly_method = config.rules.get("anomaly_method", "prophet")
        anomaly_results = await detect_anomalies(profiles, operations, method=anomaly_method)
        anomalies: list[dict[str, Any]] = [
            {
                "metric_name": a.metric_name,
                "is_anomaly": a.is_anomaly,
                "expected_value": a.expected_value,
                "actual_value": a.actual_value,
                "confidence": a.confidence,
                "detected_at": a.detected_at.isoformat(),
            }
            for a in anomaly_results
        ]

        # 3b. SLA check (if SLA target configured)
        sla_check = None
        if config.sla_target:
            from src.backend.validation.sla import check_sla

            sla_check = await check_sla(
                datahub=self._datahub,
                dataset_urn=dataset_urn,
                sla_target=config.sla_target,
                history=profiles,
                quality_score=score.overall_score,
            )

        # 4. Recommendations from issues
        recommendations: list[str] = []
        for issue in issues:
            dim = issue["dimension"]
            if dim == "completeness":
                recommendations.append("Add descriptions to undocumented schema fields")
            elif dim == "freshness":
                recommendations.append("Verify data pipeline is running on schedule")
            elif dim == "schema_stability":
                recommendations.append("Review recent schema changes for unintended modifications")
            elif dim == "data_quality":
                recommendations.append("Investigate high null ratios or missing rows")
            elif dim == "ownership_tags":
                recommendations.append("Assign an owner and add classification tags")

        # 5. Upstream lineage for root cause (if issues found)
        upstream: list[str] = []
        downstream: list[str] = []
        if issues:
            try:
                upstream = await self._datahub.get_upstream_lineage(dataset_urn)
            except Exception:
                pass
            try:
                downstream = await self._datahub.get_downstream_lineage(dataset_urn)
            except Exception:
                pass

        # 6. Alternatives search (stub)
        alternatives: list[str] = []
        # TODO: implement Qdrant similarity search for alternative datasets

        # Add SLA violations to recommendations
        if sla_check is not None and sla_check.violations:
            for v in sla_check.violations:
                recommendations.append(v)

        detail: dict[str, Any] = {
            "run_id": run_id,
            "quality_score": score.overall_score,
            "dimensions": score.dimensions,
            "dimension_details": score.dimension_details,
            "issues_count": len(issues),
            "anomalies_count": len(anomalies),
            "upstream_count": len(upstream),
            "downstream_count": len(downstream),
            "dry_run": dry_run,
        }

        if sla_check is not None:
            detail["sla"] = {
                "is_breaching": sla_check.is_breaching,
                "is_pre_breach": sla_check.is_pre_breach,
                "current_freshness_hours": sla_check.current_freshness_hours,
                "violations": sla_check.violations,
            }

        if dry_run:
            return ValidationRunResult(run_id=run_id, status="success", detail=detail)

        # 7. Publish progress to Redis pub/sub
        try:
            await self._cache.publish(
                f"ws:validation:{dataset_urn}",
                json.dumps({"run_id": run_id, "status": "completed", "score": score.overall_score}),
            )
        except Exception:
            pass

        # 8. Cache result
        try:
            await self._cache.set(
                f"validation:{dataset_urn}:result",
                json.dumps(detail),
                ttl_seconds=VALIDATION_RESULT_CACHE_TTL,
            )
        except Exception:
            pass

        # 9. Persist result in PostgreSQL
        result_row = ValidationResult(
            dataset_urn=dataset_urn,
            quality_score=score.overall_score,
            dimensions=score.dimensions,
            dimension_details=score.dimension_details,
            issues=issues,
            anomalies=anomalies,
            recommendations=recommendations,
            alternatives=alternatives,
            run_id=uuid.UUID(run_id),
            measured_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()

        # 10. Record event
        event_type = "validation.completed"
        await self._record_event(dataset_urn, event_type, "success", detail)

        return ValidationRunResult(run_id=run_id, status="success", detail=detail)

    # ── Events ───────────────────────────────────────────────────────────

    async def get_events(
        self,
        dataset_urn: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
            Event.event_type.startswith("validation."),
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
