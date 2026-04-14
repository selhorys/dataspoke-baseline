"""Validation service — config CRUD, assertion-layer run pipeline, results, and events."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.assertions import (
    build_assertion_urn,
    build_assertion_info,
    build_run_event,
    register_assertion,
    report_result,
)
from src.backend.validation.rules import RuleEvaluation, evaluate_rule
from src.shared.cache.client import RedisClient
from src.shared.config import VALIDATION_RESULT_CACHE_TTL
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, ValidationConfig, ValidationResult
from src.shared.events import (
    VALIDATION_COMPLETE,
    VALIDATION_CONFIG_CREATE,
    VALIDATION_CONFIG_DELETE,
    VALIDATION_CONFIG_UPDATE,
    VALIDATION_PREFIX,
)
from src.shared.db.registry import ensure_dataset_registered
from src.shared.exceptions import ConflictError, EntityNotFoundError

logger = logging.getLogger(__name__)


# ── Value objects ─────────────────────────────────────────────────────────────


class ValidationConfigRecord(BaseModel):
    """Value object mirroring the ORM ValidationConfig."""

    id: str
    dataset_urn: str
    rules: list[dict[str, Any]]
    schedule_tier: str | None = None
    is_active: bool = False
    owner: str
    created_at: datetime
    updated_at: datetime


class ValidationResultRecord(BaseModel):
    """Value object mirroring the ORM ValidationResult (per-rule result)."""

    id: str
    dataset_urn: str
    rule_id: str
    partition: dict[str, Any]
    values: dict[str, Any]
    validation: dict[str, bool] | None = None
    assertion_result: str
    issues: list[dict[str, Any]] = []
    run_id: str
    measured_at: datetime


class ValidationRunSummary(BaseModel):
    """Summary of a full validation run across all rules."""

    run_id: str
    status: str  # "success" | "failure" | "error"
    total: int
    passed: int
    failed: int
    errored: int
    results: list[ValidationResultRecord] = []


# ── ORM row converters ────────────────────────────────────────────────────────


def _config_from_row(row: ValidationConfig) -> ValidationConfigRecord:
    rules = row.rules if isinstance(row.rules, list) else []
    return ValidationConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        rules=rules,
        schedule_tier=row.schedule_tier,
        is_active=row.is_active,
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: ValidationResult) -> ValidationResultRecord:
    return ValidationResultRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        rule_id=row.rule_id,
        partition=row.partition,
        values=row.values,
        validation=row.validation,
        assertion_result=row.assertion_result,
        issues=row.issues if isinstance(row.issues, list) else [],
        run_id=str(row.run_id),
        measured_at=row.measured_at,
    )


# ── Service ───────────────────────────────────────────────────────────────────


class ValidationService:
    """Config CRUD, assertion-layer run pipeline, results query, and event recording."""

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
        rules: list[dict[str, Any]],
        schedule_tier: str | None,
        is_active: bool,
        owner: str,
    ) -> tuple[ValidationConfigRecord, bool]:
        await ensure_dataset_registered(self._db, self._datahub, dataset_urn, require_in_datahub=True)

        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.rules = rules
            existing.schedule_tier = schedule_tier
            existing.is_active = is_active
            existing.owner = owner
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = ValidationConfig(
                dataset_urn=dataset_urn,
                rules=rules,
                schedule_tier=schedule_tier,
                is_active=is_active,
                owner=owner,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)

        event_type = VALIDATION_CONFIG_CREATE if created else VALIDATION_CONFIG_UPDATE
        await self._record_event(
            dataset_urn,
            event_type,
            "success",
            {
                "operation": "PUT",
                "config_id": str(existing.id),
                "rule_count": len(rules),
                "is_active": existing.is_active,
            },
        )

        return _config_from_row(existing), created

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> ValidationConfigRecord:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        if "rules" in patch and patch["rules"] is not None:
            row.rules = patch["rules"]
        if "schedule_tier" in patch:
            row.schedule_tier = patch["schedule_tier"]
        if "is_active" in patch and patch["is_active"] is not None:
            row.is_active = patch["is_active"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            dataset_urn,
            VALIDATION_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "config_id": str(row.id),
                "fields_changed": list(patch.keys()),
            },
        )

        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        config_id = str(row.id)
        await self._db.delete(row)
        await self._db.commit()

        await self._record_event(
            dataset_urn,
            VALIDATION_CONFIG_DELETE,
            "success",
            {"operation": "DELETE", "config_id": config_id},
        )

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        is_active_filter: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[ValidationConfigRecord], int]:
        base = select(ValidationConfig)
        if is_active_filter is not None:
            base = base.where(ValidationConfig.is_active == is_active_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = ValidationConfig.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_config_from_row(r) for r in rows], total_count

    # ── Results ──────────────────────────────────────────────────────────────

    async def get_results(
        self,
        dataset_urn: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        partition_filter: dict[str, Any] | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[ValidationResultRecord], int]:
        base = select(ValidationResult).where(ValidationResult.dataset_urn == dataset_urn)

        if from_dt is not None:
            base = base.where(ValidationResult.measured_at >= from_dt)
        if to_dt is not None:
            base = base.where(ValidationResult.measured_at <= to_dt)
        if partition_filter is not None:
            base = base.where(ValidationResult.partition.contains(partition_filter))

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = ValidationResult.measured_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_result_from_row(r) for r in rows], total_count

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        dataset_urn: str,
        partition: dict[str, Any] | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> ValidationRunSummary:
        """Execute all rules for a dataset against the specified partition.

        Pipeline per rule:
        1. Publish progress to Redis pub/sub
        2. Evaluate rule via evaluate_rule()
        3. Build assertion URN, info, and run event
        4. Register assertion in DataHub (best-effort)
        5. Report result to DataHub (best-effort)
        6. Persist ValidationResult row to PostgreSQL
        7. Publish rule_result to Redis pub/sub
        8. Publish summary to Redis pub/sub
        9. Cache summary in Redis
        10. Record VALIDATION.COMPLETE event

        If ``dry_run`` is True, validate that the config exists and return a
        success summary without executing any rules.
        """
        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("validation_config", dataset_urn)

        if dry_run:
            return ValidationRunSummary(
                run_id=run_id or str(uuid.uuid4()),
                status="success",
                total=len(config.rules),
                passed=0,
                failed=0,
                errored=0,
                results=[],
            )

        if run_id is None:
            run_id = str(uuid.uuid4())

        resolved_partition: dict[str, Any] = partition if partition else {}
        rules: list[dict[str, Any]] = config.rules

        result_records: list[ValidationResultRecord] = []
        passed = 0
        failed = 0
        errored = 0

        for rule in rules:
            rule_id = rule.get("rule_id", str(uuid.uuid4()))

            # Publish progress
            try:
                await self._cache.publish(
                    f"ws:validation:{dataset_urn}",
                    json.dumps(
                        {
                            "type": "progress",
                            "run_id": run_id,
                            "rule_id": rule_id,
                            "status": "running",
                        }
                    ),
                )
            except Exception:
                logger.warning(
                    "validation_pubsub_progress_failed",
                    exc_info=True,
                    extra={"dataset_urn": dataset_urn, "rule_id": rule_id},
                )

            # Evaluate rule
            evaluation: RuleEvaluation = await evaluate_rule(
                self._datahub, dataset_urn, rule, resolved_partition, db=self._db
            )

            # DataHub assertion registration and reporting (best-effort)
            assertion_urn = build_assertion_urn(dataset_urn, rule_id)
            assertion_info = build_assertion_info(dataset_urn, rule)
            run_event = build_run_event(
                assertion_urn=assertion_urn,
                dataset_urn=dataset_urn,
                run_id=run_id,
                result=evaluation.assertion_result,
                values=evaluation.values,
                partition=resolved_partition,
            )
            await register_assertion(self._datahub, assertion_urn, assertion_info)
            await report_result(self._datahub, assertion_urn, run_event)

            # Persist result to PostgreSQL
            result_row = ValidationResult(
                dataset_urn=dataset_urn,
                rule_id=rule_id,
                partition=resolved_partition,
                values=evaluation.values,
                validation=evaluation.validation,
                assertion_result=evaluation.assertion_result,
                issues=evaluation.issues,
                run_id=uuid.UUID(run_id),
                measured_at=datetime.now(tz=UTC),
            )
            self._db.add(result_row)
            await self._db.commit()
            await self._db.refresh(result_row)

            record = _result_from_row(result_row)
            result_records.append(record)

            # Count outcomes
            if evaluation.assertion_result == "SUCCESS":
                passed += 1
            elif evaluation.assertion_result == "FAILURE":
                failed += 1
            else:
                errored += 1

            # Publish rule result
            try:
                await self._cache.publish(
                    f"ws:validation:{dataset_urn}",
                    json.dumps(
                        {
                            "type": "rule_result",
                            "run_id": run_id,
                            "rule_id": rule_id,
                            "assertion_result": evaluation.assertion_result,
                            "issues": evaluation.issues,
                        }
                    ),
                )
            except Exception:
                logger.warning(
                    "validation_pubsub_rule_result_failed",
                    exc_info=True,
                    extra={"dataset_urn": dataset_urn, "rule_id": rule_id},
                )

        total = len(rules)
        overall_status = (
            "success" if failed == 0 and errored == 0 else ("error" if errored > 0 else "failure")
        )

        summary = ValidationRunSummary(
            run_id=run_id,
            status=overall_status,
            total=total,
            passed=passed,
            failed=failed,
            errored=errored,
            results=result_records,
        )

        # Publish summary
        try:
            await self._cache.publish(
                f"ws:validation:{dataset_urn}",
                json.dumps(
                    {
                        "type": "summary",
                        "run_id": run_id,
                        "status": overall_status,
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "errored": errored,
                    }
                ),
            )
        except Exception:
            logger.warning(
                "validation_pubsub_summary_failed",
                exc_info=True,
                extra={"dataset_urn": dataset_urn},
            )

        # Cache summary
        try:
            await self._cache.set(
                f"validation:{dataset_urn}:result",
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": overall_status,
                        "total": total,
                        "passed": passed,
                        "failed": failed,
                        "errored": errored,
                    }
                ),
                ttl_seconds=VALIDATION_RESULT_CACHE_TTL,
            )
        except Exception:
            logger.warning(
                "validation_cache_failed",
                exc_info=True,
                extra={"dataset_urn": dataset_urn},
            )

        # Record event
        await self._record_event(
            dataset_urn,
            VALIDATION_COMPLETE,
            overall_status,
            {
                "run_id": run_id,
                "total": total,
                "passed": passed,
                "failed": failed,
                "errored": errored,
            },
        )

        return summary

    # ── Events ────────────────────────────────────────────────────────────────

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
            Event.event_type.startswith(VALIDATION_PREFIX),
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


# ── Standalone helpers ────────────────────────────────────────────────────────


async def run_validation_with_lock(
    service: ValidationService,
    cache: RedisClient,
    dataset_urn: str,
    partition: dict | None = None,
    dry_run: bool = False,
) -> ValidationRunSummary:
    """Run validation with a Redis concurrency guard.

    Shared by the public API routes (validation.py and data.py).
    Raises ConflictError if validation is already running for the dataset.
    """
    lock_key = f"validation:running:{dataset_urn}"
    acquired = await cache.set_nx(lock_key, "1", ttl_seconds=3600)
    if not acquired:
        raise ConflictError(
            "VALIDATION_RUNNING", f"Validation is already running for {dataset_urn}"
        )
    try:
        return await service.run(dataset_urn, partition=partition, dry_run=dry_run)
    finally:
        await cache.delete(lock_key)
