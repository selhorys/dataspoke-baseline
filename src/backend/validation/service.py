"""Validation service — passive result-store model."""

import logging
import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.assertions import (
    build_assertion_info,
    build_assertion_urn,
    build_run_event,
    register_assertion,
    report_result,
    tombstone_assertion,
)
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, ValidationConfig, ValidationResult
from src.shared.db.registry import ensure_dataset_registered
from src.shared.events import (
    VALIDATION_CONFIG_CREATE,
    VALIDATION_CONFIG_DELETE,
    VALIDATION_CONFIG_UPDATE,
    VALIDATION_PREFIX,
    VALIDATION_RESULT_RECORDED,
)
from src.shared.exceptions import (
    DataHubUnavailableError,
    EntityNotFoundError,
    PreconditionFailedError,
)

logger = logging.getLogger(__name__)

_RESULT_LIMIT_DEFAULT = 1000
_RESULT_LIMIT_CAP = 10_000


# ── Value objects ─────────────────────────────────────────────────────────────


class ValidationConfigRecord(BaseModel):
    """Value object for a validation configuration row."""

    dataset_urn: str
    description: str
    variables: list[str]
    is_removed: bool
    created_at: datetime
    updated_at: datetime


class ValidationResultRecord(BaseModel):
    """Value object for a single result row (collapsed last-write-wins)."""

    data_time: datetime
    score: float
    variables: dict[str, Any]


class ValidationListItem(BaseModel):
    """Value object for the cross-dataset list view."""

    dataset_urn: str
    description: str
    variable_count: int
    latest_data_time: datetime | None
    latest_score: float | None
    is_removed: bool
    updated_at: datetime


# ── ORM row converters ────────────────────────────────────────────────────────


def _config_from_row(row: ValidationConfig) -> ValidationConfigRecord:
    return ValidationConfigRecord(
        dataset_urn=row.dataset_urn,
        description=row.description,
        variables=list(row.variables) if row.variables else [],
        is_removed=row.is_removed,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── Service ───────────────────────────────────────────────────────────────────


class ValidationService:
    """Config CRUD, result recording, historical query, and event log."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
    ) -> None:
        self._datahub = datahub
        self._db = db

    # ── Config CRUD ──────────────────────────────────────────────────────────

    async def get_config(self, dataset_urn: str) -> ValidationConfigRecord | None:
        result = await self._db.execute(
            select(ValidationConfig).where(
                ValidationConfig.dataset_urn == dataset_urn,
                ValidationConfig.is_removed.is_(False),
            )
        )
        row = result.scalar_one_or_none()
        return _config_from_row(row) if row is not None else None

    async def upsert_config(
        self,
        dataset_urn: str,
        description: str,
        variables: list[str],
    ) -> tuple[ValidationConfigRecord, bool]:
        """Create or replace the validation configuration for a dataset.

        Precondition: dataset must be registered in DataHub
        (``dataset_registry.datahub_registered=true``).

        DB write is committed first, then assertionInfo + status(removed=False)
        emitted to DataHub. On DataHub failure the exception propagates (502/503).
        """
        await ensure_dataset_registered(
            self._db, self._datahub, dataset_urn, require_in_datahub=True
        )

        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            was_soft_deleted = existing.is_removed
            existing.description = description
            existing.variables = variables
            existing.is_removed = False
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            # A soft-deleted rule is consumer-absent (GET returns 404 per
            # spec §Rule Configuration), so resurrecting it via PUT is a create.
            created = was_soft_deleted
        else:
            existing = ValidationConfig(
                dataset_urn=dataset_urn,
                description=description,
                variables=variables,
                is_removed=False,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)

        assertion_urn = build_assertion_urn(dataset_urn)
        info = build_assertion_info(dataset_urn, description, variables)
        await register_assertion(self._datahub, assertion_urn, info)

        event_type = VALIDATION_CONFIG_CREATE if created else VALIDATION_CONFIG_UPDATE
        await self._record_event(
            dataset_urn,
            event_type,
            "success",
            {"operation": "PUT", "variable_count": len(variables)},
        )

        return _config_from_row(existing), created

    async def patch_config(
        self,
        dataset_urn: str,
        patch: dict[str, Any],
    ) -> ValidationConfigRecord:
        """Partially update the validation configuration.

        A soft-deleted slot is invisible to PATCH — the select filters
        `is_removed=False` so a tombstoned row mirrors the GET resource view
        and the call raises EntityNotFoundError. Use PUT to resurrect.
        """
        result = await self._db.execute(
            select(ValidationConfig).where(
                ValidationConfig.dataset_urn == dataset_urn,
                ValidationConfig.is_removed.is_(False),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        if "description" in patch and patch["description"] is not None:
            row.description = patch["description"]
        if "variables" in patch and patch["variables"] is not None:
            row.variables = patch["variables"]

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        assertion_urn = build_assertion_urn(dataset_urn)
        info = build_assertion_info(dataset_urn, row.description, list(row.variables))
        await register_assertion(self._datahub, assertion_urn, info)

        await self._record_event(
            dataset_urn,
            VALIDATION_CONFIG_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(patch.keys())},
        )

        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        """Soft-delete: set is_removed=True and emit status(removed=True) to DataHub."""
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        row.is_removed = True
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()

        assertion_urn = build_assertion_urn(dataset_urn)
        await tombstone_assertion(self._datahub, assertion_urn)

        await self._record_event(
            dataset_urn,
            VALIDATION_CONFIG_DELETE,
            "success",
            {"operation": "DELETE"},
        )

    # ── Results ──────────────────────────────────────────────────────────────

    async def record_result(
        self,
        dataset_urn: str,
        data_time: datetime,
        score: float,
        variables: dict[str, float],
    ) -> ValidationResultRecord:
        """Validate and persist a pipeline-emitted result, then emit to DataHub.

        Validation:
        - score must be in [0.0, 1.0]; else PreconditionFailedError(INVALID_SCORE)
        - variable keys must be a subset of conf.variables; else
          PreconditionFailedError(UNKNOWN_VARIABLE)

        The row is inserted regardless of DataHub emit success (local store is
        the historical-baseline cache).  On emit failure the DataHubUnavailableError
        is re-raised so the API layer can return 502/503.
        VALIDATION.RESULT_RECORDED is only recorded after a successful emit.
        """
        if not math.isfinite(score) or score < 0.0 or score > 1.0:
            raise PreconditionFailedError(
                "INVALID_SCORE",
                f"score must be in [0.0, 1.0], got {score}",
                detail={"score": score if math.isfinite(score) else repr(score)},
            )

        config_result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        config_row = config_result.scalar_one_or_none()
        if config_row is None:
            raise EntityNotFoundError("config", dataset_urn)

        declared = set(config_row.variables or [])
        unknown = sorted(k for k in variables if k not in declared)
        if unknown:
            raise PreconditionFailedError(
                "UNKNOWN_VARIABLE",
                f"unknown variable keys: {unknown}",
                detail={"unknown": unknown},
            )

        row = ValidationResult(
            dataset_urn=dataset_urn,
            data_time=data_time,
            score=score,
            variables=variables,
            ingestion_time=datetime.now(tz=UTC),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        assertion_urn = build_assertion_urn(dataset_urn)
        run_event = build_run_event(
            assertion_urn=assertion_urn,
            dataset_urn=dataset_urn,
            data_time=data_time,
            score=score,
            variables=variables,
        )

        emitted = await report_result(self._datahub, assertion_urn, run_event)
        if not emitted:
            raise DataHubUnavailableError(
                f"assertionRunEvent emit failed for {assertion_urn}"
            )

        await self._record_event(
            dataset_urn,
            VALIDATION_RESULT_RECORDED,
            "success",
            {
                "data_time": data_time.isoformat(),
                "score": score,
                "variable_count": len(variables),
            },
        )

        return ValidationResultRecord(
            data_time=row.data_time,
            score=row.score,
            variables=dict(row.variables) if row.variables else {},
        )

    async def get_results(
        self,
        dataset_urn: str,
        from_dt: datetime | None = None,
        until_dt: datetime | None = None,
        limit: int = _RESULT_LIMIT_DEFAULT,
    ) -> tuple[list[ValidationResultRecord], int]:
        """Return collapsed (last-write-wins per data_time) historical results.

        Returns a tuple of (collapsed_rows, total_count) where total_count is
        the number of underlying rows in the window BEFORE de-duplication/collapse.
        limit is clamped to [1, 10000] server-side.
        """
        effective_limit = max(1, min(limit, _RESULT_LIMIT_CAP))

        # Pre-collapse count: total raw rows in the window (no de-dup)
        count_q = select(func.count()).where(
            ValidationResult.dataset_urn == dataset_urn
        )
        if from_dt is not None:
            count_q = count_q.where(ValidationResult.data_time >= from_dt)
        if until_dt is not None:
            count_q = count_q.where(ValidationResult.data_time < until_dt)
        total_count: int = (await self._db.execute(count_q)).scalar() or 0

        sub = (
            select(
                ValidationResult.data_time,
                ValidationResult.score,
                ValidationResult.variables,
                func.row_number()
                .over(
                    partition_by=ValidationResult.data_time,
                    order_by=ValidationResult.ingestion_time.desc(),
                )
                .label("rn"),
            )
            .where(ValidationResult.dataset_urn == dataset_urn)
        )

        if from_dt is not None:
            sub = sub.where(ValidationResult.data_time >= from_dt)
        if until_dt is not None:
            sub = sub.where(ValidationResult.data_time < until_dt)

        sub = sub.subquery()

        rows_q = (
            select(sub.c.data_time, sub.c.score, sub.c.variables)
            .where(sub.c.rn == 1)
            .order_by(sub.c.data_time.desc())
            .limit(effective_limit)
        )

        result = await self._db.execute(rows_q)
        rows = result.all()

        collapsed = [
            ValidationResultRecord(
                data_time=r.data_time,
                score=r.score,
                variables=dict(r.variables) if r.variables else {},
            )
            for r in rows
        ]
        return collapsed, total_count

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        removed_filter: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[ValidationListItem], int]:
        """List configs with latest result joined per dataset.

        Each row contains dataset_urn, description, variable_count,
        latest_data_time, latest_score, is_removed, updated_at.
        """
        base = select(ValidationConfig)
        if removed_filter is not None:
            base = base.where(ValidationConfig.is_removed == removed_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = ValidationConfig.updated_at.desc()
        config_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        config_result = await self._db.execute(config_q)
        config_rows = config_result.scalars().all()

        if not config_rows:
            return [], total_count

        urns = [r.dataset_urn for r in config_rows]

        latest_sub = (
            select(
                ValidationResult.dataset_urn,
                ValidationResult.data_time,
                ValidationResult.score,
                func.row_number()
                .over(
                    partition_by=ValidationResult.dataset_urn,
                    order_by=ValidationResult.data_time.desc(),
                )
                .label("rn"),
            )
            .where(ValidationResult.dataset_urn.in_(urns))
            .subquery()
        )
        latest_q = select(
            latest_sub.c.dataset_urn,
            latest_sub.c.data_time,
            latest_sub.c.score,
        ).where(latest_sub.c.rn == 1)

        latest_result = await self._db.execute(latest_q)
        latest_by_urn: dict[str, tuple[datetime, float]] = {}
        for r in latest_result.all():
            latest_by_urn[r.dataset_urn] = (r.data_time, r.score)

        items: list[ValidationListItem] = []
        for row in config_rows:
            latest = latest_by_urn.get(row.dataset_urn)
            items.append(
                ValidationListItem(
                    dataset_urn=row.dataset_urn,
                    description=row.description,
                    variable_count=len(row.variables) if row.variables else 0,
                    latest_data_time=latest[0] if latest else None,
                    latest_score=latest[1] if latest else None,
                    is_removed=row.is_removed,
                    updated_at=row.updated_at,
                )
            )

        return items, total_count

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
