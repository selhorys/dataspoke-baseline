"""Validation service — passive result-store model."""

import logging
import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.assertions import (
    build_assertion_info,
    build_assertion_urn,
    build_run_event,
    register_assertion,
    report_result,
)
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DEFAULT_VALIDATION_ATTRIBUTE,
    DatasetRegistry,
    Event,
    ValidationConfig,
    ValidationResult,
)
from src.shared.db.registry import ensure_dataset_registered
from src.shared.events import (
    VALIDATION_CONFIG_CREATE,
    VALIDATION_CONFIG_UPDATE,
    VALIDATION_PREFIX,
    VALIDATION_RESULT_RECORDED,
)
from src.shared.exceptions import (
    DataHubUnavailableError,
    EntityNotFoundError,
    PreconditionFailedError,
)
from src.workflows._common import read_datahub_actor_urn

logger = logging.getLogger(__name__)

_RESULT_LIMIT_DEFAULT = 1000
_RESULT_LIMIT_CAP = 10_000


# ── Value objects ─────────────────────────────────────────────────────────────


class ValidationConfigRecord(BaseModel):
    """Value object for a validation configuration row.

    ``variables`` is a list of ``{"name": ..., "description": ...}`` dicts,
    matching the JSONB column shape; the API response model coerces each entry
    into its own ``ValidationVariable``. ``parameter`` entries are
    ``{"name", "value", "description"}`` dicts — one field wider than
    ``variables`` — and the list is ``None`` when the section was never
    declared — a state the API response renders by omitting the key rather
    than by serializing a null.
    """

    dataset_urn: str
    description: str
    variables: list[dict[str, str]]
    #: Never absent — the column is NOT NULL and a conf written without the
    #: section stores the all-defaults object, so the default here states the
    #: same contract rather than papering over a missing value.
    attribute: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_VALIDATION_ATTRIBUTE)
    )
    parameter: list[dict[str, str]] | None = None
    created_at: datetime
    updated_at: datetime


class ValidationResultRecord(BaseModel):
    """Value object for a single result row (collapsed last-write-wins)."""

    data_time: datetime
    score: float
    variables: dict[str, Any]
    score_note: str | None = None


class ValidationListItem(BaseModel):
    """Value object for the cross-dataset list view.

    ``description``/``variable_count``/``latest_*`` are ``None`` for uncovered
    rows (registered datasets with no validation conf) under
    ``coverage=uncovered|both``.
    """

    dataset_urn: str
    description: str | None = None
    variable_count: int | None = None
    latest_data_time: datetime | None = None
    latest_score: float | None = None
    updated_at: datetime | None = None


# ── ORM row converters ────────────────────────────────────────────────────────


def _config_from_row(row: ValidationConfig) -> ValidationConfigRecord:
    return ValidationConfigRecord(
        dataset_urn=row.dataset_urn,
        description=row.description,
        variables=[dict(v) for v in (row.variables or [])],
        attribute=dict(row.attribute or DEFAULT_VALIDATION_ATTRIBUTE),
        parameter=(
            [dict(p) for p in row.parameter] if row.parameter is not None else None
        ),
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
        """Return the config record, ``None`` if the slot does not exist.

        The caller raises ``EntityNotFoundError`` (404 ``CONFIG_NOT_FOUND``) on
        ``None``.
        """
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
        description: str,
        variables: list[dict[str, str]],
        attribute: dict[str, int] | None = None,
        parameter: list[dict[str, str]] | None = None,
    ) -> tuple[ValidationConfigRecord, bool]:
        """Create or replace the validation configuration for a dataset.

        Precondition: dataset must be registered in DataHub
        (``dataset_registry.datahub_registered=true``).

        An existing slot is replaced (200); an absent slot is created (201).
        Every section is replaced wholesale, ``attribute`` and ``parameter``
        included: ``attribute=None`` stores the all-defaults cadence rather than
        preserving a previous one, and ``parameter=None`` clears the section.

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

        # Defaults fill whatever the caller did not name — including everything,
        # when the section was omitted entirely — so the stored object is always
        # complete and the validation-score measurer never branches on absence.
        stored_attribute: dict[str, int] = {
            **DEFAULT_VALIDATION_ATTRIBUTE,
            **(attribute or {}),
        }

        if existing:
            existing.description = description
            existing.variables = variables
            existing.attribute = stored_attribute
            existing.parameter = parameter
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = ValidationConfig(
                dataset_urn=dataset_urn,
                description=description,
                variables=variables,
                attribute=stored_attribute,
                parameter=parameter,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)

        assertion_urn = build_assertion_urn(dataset_urn)
        actor_urn = await read_datahub_actor_urn(self._db)
        info = build_assertion_info(dataset_urn, description, variables, actor_urn=actor_urn)
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

        A never-created slot raises ``EntityNotFoundError`` (404
        ``CONFIG_NOT_FOUND``).

        *patch* comes from ``model_dump(exclude_unset=True)``, so key **presence**
        carries meaning where key **value** cannot: ``parameter`` is cleared only
        by an explicit ``null`` and left untouched when the key is absent, which
        an ``is not None`` test alone could not tell apart. ``attribute`` is
        replaced wholesale when supplied — the request schema has already
        defaulted its unnamed fields — so a patch naming one cadence field resets
        the other to its default.
        """
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        if "description" in patch and patch["description"] is not None:
            row.description = patch["description"]
        if "variables" in patch and patch["variables"] is not None:
            row.variables = patch["variables"]
        if "attribute" in patch and patch["attribute"] is not None:
            # Defaults fill the unnamed fields, never the previously stored value:
            # the section is replaced outright, so patching one cadence field
            # resets the other to its default rather than deep-merging.
            row.attribute = {**DEFAULT_VALIDATION_ATTRIBUTE, **patch["attribute"]}
        if "parameter" in patch:
            # Present-and-null clears the section; a non-empty list replaces it.
            # An empty list never reaches here — the schema rejects it, so null
            # stays the single spelling of "clear".
            row.parameter = patch["parameter"]

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        assertion_urn = build_assertion_urn(dataset_urn)
        actor_urn = await read_datahub_actor_urn(self._db)
        info = build_assertion_info(
            dataset_urn, row.description, list(row.variables), actor_urn=actor_urn
        )
        await register_assertion(self._datahub, assertion_urn, info)

        await self._record_event(
            dataset_urn,
            VALIDATION_CONFIG_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(patch.keys())},
        )

        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        """Hard-delete a dataset's validation slot, cascading its history.

        In a single transaction this removes the dataset's
        ``validation_results`` rows, its validation events (``VALIDATION.*``
        only — other features' events for the same dataset are untouched), and
        the ``validation_configs`` row itself. The DataHub assertion entity is
        then hard-deleted from DataHub; on DataHub failure the exception
        propagates (502/503) after the local cascade has committed. No event is
        recorded — the cascade wipes the dataset's validation event history.

        Returns ``404 CONFIG_NOT_FOUND`` when the slot does not exist.
        """
        result = await self._db.execute(
            select(ValidationConfig).where(ValidationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        await self._db.execute(
            delete(ValidationResult).where(ValidationResult.dataset_urn == dataset_urn)
        )
        await self._db.execute(
            delete(Event).where(
                Event.entity_type == "dataset",
                Event.entity_id == dataset_urn,
                Event.event_type.startswith(VALIDATION_PREFIX),
            )
        )
        await self._db.delete(row)
        await self._db.commit()

        assertion_urn = build_assertion_urn(dataset_urn)
        await self._datahub.hard_delete_entity(assertion_urn)

    # ── Results ──────────────────────────────────────────────────────────────

    async def record_result(
        self,
        dataset_urn: str,
        data_time: datetime,
        score: float,
        variables: dict[str, float],
        score_note: str | None = None,
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

        declared = {v["name"] for v in (config_row.variables or [])}
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
            score_note=score_note,
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
            score_note=score_note,
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
            score_note=row.score_note,
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
        the number of distinct data_time partitions in the window (matches the
        collapsed-row count). limit is clamped to [1, 10000] server-side.
        """
        effective_limit = max(1, min(limit, _RESULT_LIMIT_CAP))

        count_q = select(func.count(func.distinct(ValidationResult.data_time))).where(
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
                ValidationResult.score_note,
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

        sub = sub.subquery()  # type: ignore[assignment]  # SQLAlchemy Select -> Subquery rebind.

        rows_q = (
            select(sub.c.data_time, sub.c.score, sub.c.variables, sub.c.score_note)
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
                score_note=r.score_note,
            )
            for r in rows
        ]
        return collapsed, total_count

    async def _latest_results_by_urn(
        self, urns: list[str]
    ) -> dict[str, tuple[datetime, float]]:
        """Batch latest-result lookup (last-write-wins per dataset) for ``urns``.

        One windowed query keyed on the page's URNs — shared by the ``covered``
        and ``both`` coverage branches so neither does a per-URN N+1.
        """
        if not urns:
            return {}
        latest_sub = (
            select(
                ValidationResult.dataset_urn,
                ValidationResult.data_time,
                ValidationResult.score,
                func.row_number()
                .over(
                    partition_by=ValidationResult.dataset_urn,
                    order_by=(
                        ValidationResult.data_time.desc(),
                        ValidationResult.ingestion_time.desc(),
                    ),
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
        result = await self._db.execute(latest_q)
        return {r.dataset_urn: (r.data_time, r.score) for r in result.all()}

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
        coverage: str = "covered",
    ) -> tuple[list[ValidationListItem], int]:
        """List validation rows across datasets (paginated), per ``coverage``.

        ``covered`` (default) — datasets that hold a validation conf, with the
        latest result joined (``dataset_urn``, ``description``, ``variable_count``,
        ``latest_data_time``, ``latest_score``, ``updated_at``).

        ``uncovered`` — registered datasets (``dataset_registry``,
        ``datahub_registered=True``) with no validation conf, ordered by
        ``dataset_urn``; conf/result fields are null and ``updated_at`` is the
        registry row's.

        ``both`` — the union (registered datasets LEFT JOIN validation conf),
        ordered ``updated_at DESC NULLS LAST`` then ``dataset_urn`` so uncovered
        rows sort last and paging stays deterministic.

        Every branch is SQL-paginated.
        """
        if coverage == "uncovered":
            return await self._list_uncovered(offset, limit)
        if coverage == "both":
            return await self._list_both(offset, limit)
        return await self._list_covered(offset, limit, order_by)

    async def _list_covered(
        self,
        offset: int,
        limit: int,
        order_by: Any,
    ) -> tuple[list[ValidationListItem], int]:
        base = select(ValidationConfig)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = ValidationConfig.updated_at.desc()
        config_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        config_rows = (await self._db.execute(config_q)).scalars().all()
        if not config_rows:
            return [], total_count

        urns = [r.dataset_urn for r in config_rows]
        latest_by_urn = await self._latest_results_by_urn(urns)

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
                    updated_at=row.updated_at,
                )
            )
        return items, total_count

    async def _list_uncovered(
        self,
        offset: int,
        limit: int,
    ) -> tuple[list[ValidationListItem], int]:
        # Registered datasets with no validation conf (the /unmanaged analogue).
        conf_subq = select(ValidationConfig.dataset_urn).scalar_subquery()
        base = select(
            DatasetRegistry.dataset_urn,
            DatasetRegistry.updated_at,
        ).where(
            DatasetRegistry.datahub_registered.is_(True),
            DatasetRegistry.dataset_urn.not_in(conf_subq),
        )

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = (
            base.order_by(DatasetRegistry.dataset_urn.asc()).offset(offset).limit(limit)
        )
        rows = (await self._db.execute(rows_q)).all()
        items = [
            ValidationListItem(
                dataset_urn=r.dataset_urn,
                description=None,
                variable_count=None,
                latest_data_time=None,
                latest_score=None,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
        return items, total_count

    async def _list_both(
        self,
        offset: int,
        limit: int,
    ) -> tuple[list[ValidationListItem], int]:
        # Registered datasets LEFT JOIN their validation conf. Conf fields are
        # NULL for uncovered rows (ValidationConfig.description is NOT NULL, so a
        # NULL description reliably marks an uncovered row).
        base = (
            select(
                DatasetRegistry.dataset_urn,
                DatasetRegistry.updated_at.label("registry_updated_at"),
                ValidationConfig.description,
                ValidationConfig.variables,
                ValidationConfig.updated_at.label("conf_updated_at"),
            )
            .select_from(DatasetRegistry)
            .outerjoin(
                ValidationConfig,
                ValidationConfig.dataset_urn == DatasetRegistry.dataset_urn,
            )
            .where(DatasetRegistry.datahub_registered.is_(True))
        )

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        # Conf updated_at DESC NULLS LAST (uncovered rows last), tiebreak by
        # dataset_urn so paging stays deterministic across the union.
        rows_q = (
            base.order_by(
                ValidationConfig.updated_at.desc().nullslast(),
                DatasetRegistry.dataset_urn.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).all()
        if not rows:
            return [], total_count

        covered_urns = [r.dataset_urn for r in rows if r.description is not None]
        latest_by_urn = await self._latest_results_by_urn(covered_urns)

        items: list[ValidationListItem] = []
        for r in rows:
            if r.description is None:
                items.append(
                    ValidationListItem(
                        dataset_urn=r.dataset_urn,
                        description=None,
                        variable_count=None,
                        latest_data_time=None,
                        latest_score=None,
                        updated_at=r.registry_updated_at,
                    )
                )
            else:
                latest = latest_by_urn.get(r.dataset_urn)
                items.append(
                    ValidationListItem(
                        dataset_urn=r.dataset_urn,
                        description=r.description,
                        variable_count=len(r.variables) if r.variables else 0,
                        latest_data_time=latest[0] if latest else None,
                        latest_score=latest[1] if latest else None,
                        updated_at=r.conf_updated_at,
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
