"""Metrics service — metric CRUD, run pipeline, and event recording.

Metrics are pure aggregation over pre-existing data (DataHub metadata and
validation results). The ``metric_type`` dispatches to a registered measurer.

Spec: spec/feature/BACKEND.md §Metrics Service
"""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import sqlalchemy.exc
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend._dataset_filter import (
    dataset_filter_clause,
    resolve_dataset_scope,
    validate_dataset_filter_service,
)
from src.backend.metrics.measurers import DatasetVerdict, get_measurer, list_measurers
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DatasetRegistry,
    Event,
    MetricDatasetResult,
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
    PreconditionFailedError,
)
from src.shared.metric_conf import is_valid_time_window_sec, time_window_sec_error

logger = logging.getLogger(__name__)

# Keys emitted by each built-in metric type (mirrors the schema-layer constant).
_EMITTED_KEYS: dict[str, set[str]] = {
    "ingestion-freshness": {"total", "ingested_in_time"},
    "validation-score": {"valid_confd", "valid_in_time"},
    "doc-health": {"total", "doc_health"},
}

_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: The tri-state `met` vocabulary of GET /spoke/governance/metric/{id}/dataset.
_MET_STATES: frozenset[str] = frozenset({"true", "false", "unknown"})

#: Rows per verdict-insert round trip, so one run over a large estate does not
#: build a single statement with an unbounded parameter count.
_VERDICT_CHUNK = 500


class MetricDefinitionRecord(BaseModel):
    """Value object mirroring the ORM MetricDefinition."""

    id: str
    mode: str
    metric_type: str
    title: str
    description: str
    #: Series descriptors — ``[{"name": …, "color": "#RRGGBB", "idx": 1}, …]``.
    metrics: list[dict[str, Any]]
    metric_conf: dict[str, Any]
    dataset_filter: str
    schedule_tier: str | None = None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None


class MetricDatasetRecord(BaseModel):
    """One row of ``GET /spoke/governance/metric/{metric_id}/dataset``.

    ``met`` is tri-state: ``"unknown"`` means the dataset is in the metric's
    current scope but carries no verdict — the metric has never run, the dataset
    entered scope after the last run, or, for ``validation-score``, the dataset
    has no validation configuration and is therefore never evaluated on any run.
    """

    dataset_urn: str
    met: str
    last_check_at: datetime | None = None
    detail: dict[str, Any] | None = None


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


def _definition_from_row(
    row: MetricDefinition, last_run_at: datetime | None = None
) -> MetricDefinitionRecord:
    return MetricDefinitionRecord(
        id=row.id,
        mode=row.mode,
        metric_type=row.metric_type,
        title=row.title,
        description=row.description,
        metrics=list(row.metrics or []),
        metric_conf=row.metric_conf or {},
        dataset_filter=row.dataset_filter or "",
        schedule_tier=row.schedule_tier,
        is_enabled=row.is_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_run_at=last_run_at,
    )


def _validate_series(metric_type: str, series: list[dict[str, Any]]) -> None:
    """Validate ``metrics[]`` series descriptors against *metric_type*.

    Mirrors the schema-layer rules (``src/api/schemas/metrics.py``) for callers
    that reach the service without a request body — and for PATCH, whose merged
    state is only knowable here: every ``name`` is one of the type's emitted
    keys, and ``name`` and ``idx`` are each unique within the metric.
    """
    allowed = _EMITTED_KEYS.get(metric_type, set())
    names: list[str] = []
    idxs: list[int] = []

    for entry in series:
        if not isinstance(entry, dict):
            raise PreconditionFailedError(
                "INVALID_PARAMETER", "metrics[] entries must be {name, color, idx} objects"
            )
        name = entry.get("name")
        color = entry.get("color")
        idx = entry.get("idx")
        if not isinstance(name, str) or not name:
            raise PreconditionFailedError("INVALID_PARAMETER", "metrics[].name is required")
        if not isinstance(color, str) or not _COLOR_RE.match(color):
            raise PreconditionFailedError(
                "INVALID_PARAMETER", "metrics[].color must be a #RRGGBB hex string"
            )
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 1:
            raise PreconditionFailedError(
                "INVALID_PARAMETER", "metrics[].idx must be a positive integer"
            )
        names.append(name)
        idxs.append(idx)

    unknown = set(names) - allowed
    if unknown:
        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            (
                f"metrics[] contains keys not emitted by '{metric_type}': "
                f"{sorted(unknown)}. Allowed: {sorted(allowed)}"
            ),
        )
    if len(set(names)) != len(names):
        raise PreconditionFailedError("INVALID_PARAMETER", "metrics[].name must be unique")
    if len(set(idxs)) != len(idxs):
        raise PreconditionFailedError("INVALID_PARAMETER", "metrics[].idx must be unique")


def _breakdown_from_verdicts(
    verdicts: list[DatasetVerdict], dataset_count: int
) -> dict[str, Any]:
    """Derive ``metric_results.breakdown`` from the run's verdicts.

    ``datasets[]`` lists only the failed ones — membership in the list is itself
    the classification — while ``dataset_count`` is the total **scanned**, passed
    in by the caller rather than read off ``len(verdicts)``: a measurer may
    evaluate a strict subset of its scan (``validation-score`` leaves the
    unconfigured datasets verdict-less), so the two counts are not the same
    quantity. Deriving the breakdown here rather than having each measurer build
    both is what keeps the stored breakdown and ``metric_dataset_results`` from
    ever disagreeing.
    """
    return {
        "dataset_count": dataset_count,
        "datasets": [
            {"urn": verdict.urn, "detail": verdict.detail}
            for verdict in verdicts
            if not verdict.met
        ],
    }


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

        # last_run_at: newest METRIC.RUN_COMPLETE per metric, resolved for ONLY the
        # page's ids in one batch query (page-bounded, mirrors the catalog pattern).
        metric_ids = [r.id for r in rows]
        last_run_by_id: dict[str, datetime] = {}
        if metric_ids:
            lr_q = (
                select(Event.entity_id, func.max(Event.occurred_at))
                .where(
                    Event.entity_type == "metric",
                    Event.event_type == METRIC_RUN_COMPLETE,
                    Event.entity_id.in_(metric_ids),
                )
                .group_by(Event.entity_id)
            )
            lr_result = await self._db.execute(lr_q)
            last_run_by_id = {eid: ts for eid, ts in lr_result.all()}

        return [
            _definition_from_row(r, last_run_at=last_run_by_id.get(r.id)) for r in rows
        ], total_count

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

    async def create_metric_config(
        self,
        metric_id: str,
        mode: str,
        metric_type: str,
        title: str,
        description: str,
        metrics: list[dict[str, Any]],
        metric_conf: dict[str, Any],
        dataset_filter: str,
        schedule_tier: str | None = None,
        is_enabled: bool = False,
    ) -> MetricDefinitionRecord:
        """Create a new metric definition.

        Raises ``ConflictError("METRIC_EXISTS", ...)`` when a definition with
        ``metric_id`` already exists.  The concurrent-create race (two callers
        pass the SELECT check simultaneously) is closed by catching the
        primary-key ``IntegrityError`` on commit.
        """
        validate_dataset_filter_service(dataset_filter)
        _validate_series(metric_type, metrics)

        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        if result.scalar_one_or_none() is not None:
            raise ConflictError("METRIC_EXISTS", f"Metric {metric_id} already exists")

        row = MetricDefinition(
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
        self._db.add(row)

        try:
            await self._db.commit()
        except sqlalchemy.exc.IntegrityError:
            await self._db.rollback()
            raise ConflictError("METRIC_EXISTS", f"Metric {metric_id} already exists")

        await self._db.refresh(row)

        await self._record_event(
            metric_id,
            METRIC_CONFIG_CREATE,
            "success",
            {"operation": "POST", "metric_id": metric_id},
        )

        return _definition_from_row(row)

    async def replace_metric_config(
        self,
        metric_id: str,
        mode: str,
        metric_type: str,
        title: str,
        description: str,
        metrics: list[dict[str, Any]],
        metric_conf: dict[str, Any],
        dataset_filter: str,
        schedule_tier: str | None = None,
        is_enabled: bool = False,
    ) -> MetricDefinitionRecord:
        """Replace an existing metric definition.

        Raises ``EntityNotFoundError("metric", metric_id)`` when the definition
        does not exist.  Use ``create_metric_config`` to create new entries.
        """
        validate_dataset_filter_service(dataset_filter)
        _validate_series(metric_type, metrics)

        result = await self._db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise EntityNotFoundError("metric", metric_id)

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

        await self._db.commit()
        await self._db.refresh(existing)

        await self._record_event(
            metric_id,
            METRIC_CONFIG_UPDATE,
            "success",
            {"operation": "PUT", "metric_id": metric_id},
        )

        return _definition_from_row(existing)

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
            validate_dataset_filter_service(patch["dataset_filter"])

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
        merged_metrics: list[dict[str, Any]] = list(row.metrics or [])

        if merged_type in ("ingestion-freshness", "validation-score"):
            # The bound and its message come from src.shared.metric_conf so this
            # PATCH-merge path and the create/replace path cannot drift apart.
            if not is_valid_time_window_sec(merged_conf.get("time_window_sec")):
                raise PreconditionFailedError(
                    "INVALID_PARAMETER",
                    time_window_sec_error(merged_type),
                )
        elif merged_type == "doc-health":
            if merged_conf != {}:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER",
                    "metric_conf must be {} for metric_type 'doc-health'",
                )

        _validate_series(merged_type, merged_metrics)

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
        await self._db.execute(
            delete(MetricDatasetResult).where(MetricDatasetResult.metric_id == metric_id)
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
        scheduled_at: datetime | None = None,
    ) -> MetricRunResult:
        """Measure one metric.

        ``scheduled_at`` is the run's **measurement instant** when the trigger has
        a schedule to anchor to — a periodic tier DAG forwards its
        ``data_interval_end``, so a retried or backlogged run measures the
        interval it is *for* rather than the one it happened to execute in. An
        on-demand run supplies none and falls back to wall-clock ``now()``
        (spec/feature/BACKEND.md §Metrics Service — Measurement instant).
        """
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
            return await self._run_inner(metric_id, dry_run, scheduled_at)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(
        self,
        metric_id: str,
        dry_run: bool,
        scheduled_at: datetime | None = None,
    ) -> MetricRunResult:
        definition = await self.get_metric(metric_id)

        if not definition.is_enabled and not dry_run:
            raise ConflictError(
                "METRIC_DISABLED",
                f"Metric {metric_id} is disabled; only dry-run is permitted",
            )

        run_id = str(uuid.uuid4())

        # The measurement instant: one reading for the whole run, taken before
        # dispatch. A scheduled trigger supplies its own — see `run`.
        now = scheduled_at if scheduled_at is not None else datetime.now(tz=UTC)

        values, verdicts, unresolved_urns, dataset_count = await self._measure(definition, now)
        breakdown = _breakdown_from_verdicts(verdicts, dataset_count)

        detail: dict[str, Any] = {
            "run_id": run_id,
            "metric_id": metric_id,
            "values": values,
            "dry_run": dry_run,
            "unresolved_urns": unresolved_urns,
            "breakdown_summary": {
                "dataset_count": breakdown["dataset_count"],
                "affected_count": len(breakdown["datasets"]),
            },
        }

        # A dry run persists nothing — neither the result row nor the verdicts,
        # so `/dataset` after a dry run still reports the previous run's.
        if dry_run:
            return MetricRunResult(run_id=run_id, status="success", detail=detail)

        # A separate, later reading than the measurement instant above: it dates
        # the *result*, it does not define the window the run measured.
        measured_at = datetime.now(tz=UTC)
        result_row = MetricResult(
            metric_id=metric_id,
            values=values,
            breakdown=breakdown,
            measured_at=measured_at,
        )
        self._db.add(result_row)
        # The verdict store holds the latest run only, so the metric's rows are
        # replaced wholesale inside the same transaction as the result row: the
        # two can never describe different runs.
        await self._replace_dataset_verdicts(metric_id, verdicts, measured_at)
        await self._db.commit()

        await self._record_event(metric_id, METRIC_RUN_COMPLETE, "success", detail)

        return MetricRunResult(run_id=run_id, status="success", detail=detail)

    async def _replace_dataset_verdicts(
        self,
        metric_id: str,
        verdicts: list[DatasetVerdict],
        measured_at: datetime,
    ) -> None:
        """Replace this metric's ``metric_dataset_results`` rows. Does not commit."""
        await self._db.execute(
            delete(MetricDatasetResult).where(MetricDatasetResult.metric_id == metric_id)
        )
        if not verdicts:
            return

        payload = [
            {
                "metric_id": metric_id,
                "dataset_urn": verdict.urn,
                "met": verdict.met,
                "evidence_at": verdict.evidence_at,
                "detail": verdict.detail,
                "measured_at": measured_at,
            }
            for verdict in verdicts
        ]
        for start in range(0, len(payload), _VERDICT_CHUNK):
            await self._db.execute(
                sa.insert(MetricDatasetResult), payload[start : start + _VERDICT_CHUNK]
            )

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
        now: datetime,
    ) -> tuple[dict[str, float], list[DatasetVerdict], list[str], int]:
        """Run measurement and return (values, verdicts, unresolved_urns, scanned).

        Resolves ``dataset_filter`` against ``dataset_registry`` — one SQL query,
        no DataHub call — so the run's verdicts and the ``/dataset`` view can
        never disagree about scope. Literal ``dataset_urn`` values matching no
        registered dataset are accumulated into ``unresolved_urns``.

        ``scanned`` is the size of that resolved scope. It is returned alongside
        the verdicts because a measurer may evaluate a subset of what it scanned,
        which makes ``len(verdicts)`` the wrong source for the breakdown's
        ``dataset_count``. ``now`` is the run's measurement instant, passed
        through to the measurer.
        """
        scope = await resolve_dataset_scope(self._db, definition.dataset_filter)
        datasets = scope.resolved_urns
        unresolved_urns = scope.unresolved_urns

        measurer = get_measurer(definition.metric_type)
        if measurer is None:
            supported = ", ".join(f"'{m}'" for m in list_measurers())
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"Unsupported metric_type: '{definition.metric_type}'. Supported: {supported}.",
            )

        all_values, verdicts = await measurer(
            datasets, definition.metric_conf, datahub=self._datahub, db=self._db, now=now
        )

        # Filter values to the series declared in definition.metrics
        declared = {
            series["name"]
            for series in definition.metrics
            if isinstance(series, dict) and "name" in series
        }
        filtered_values = (
            {k: v for k, v in all_values.items() if k in declared} if declared else all_values
        )

        return filtered_values, verdicts, unresolved_urns, len(datasets)

    # ── Per-dataset verdicts ─────────────────────────────────────────────────

    async def list_metric_datasets(
        self,
        metric_id: str,
        *,
        met: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[MetricDatasetRecord], int, datetime | None]:
        """The metric's current scope joined to its latest per-dataset verdicts.

        Scope is resolved from the same registry the run resolved it from — the
        compiled filter clause is pushed into this paginated query rather than a
        URN list being materialised — so a dataset cannot appear here and be
        missing from the run, or the reverse. A dataset in scope with no verdict
        row reads ``met = "unknown"``.

        Returns ``(rows, total_count, attrs_synced_at)`` where ``attrs_synced_at``
        is the **maximum** ``dataset_registry.attrs_synced_at`` over the datasets
        in scope — scope-relative and unaffected by ``met`` filtering or paging,
        because it answers "how fresh is the scope this page is drawn from", not
        "how fresh is this page".

        Raises EntityNotFoundError("metric", metric_id) when the metric is absent.
        """
        definition = await self.get_metric(metric_id)
        clause = dataset_filter_clause(definition.dataset_filter)

        in_scope = (
            DatasetRegistry.datahub_registered.is_(True),
            clause,
        )
        verdict_join = sa.and_(
            MetricDatasetResult.dataset_urn == DatasetRegistry.dataset_urn,
            MetricDatasetResult.metric_id == metric_id,
        )

        base = (
            select(
                DatasetRegistry.dataset_urn,
                MetricDatasetResult.met,
                MetricDatasetResult.evidence_at,
                MetricDatasetResult.measured_at,
                MetricDatasetResult.detail,
            )
            .select_from(DatasetRegistry)
            .outerjoin(MetricDatasetResult, verdict_join)
            .where(*in_scope)
        )

        # Tri-state filter. `met IS NULL` is the left-join miss — in scope, never
        # evaluated. Selecting all three states adds no predicate.
        selected = set(met or _MET_STATES)
        unknown = selected - _MET_STATES
        if unknown:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"met must be one of {sorted(_MET_STATES)}; got {sorted(unknown)}",
            )
        if selected != _MET_STATES:
            conditions = []
            if "true" in selected:
                conditions.append(MetricDatasetResult.met.is_(True))
            if "false" in selected:
                conditions.append(MetricDatasetResult.met.is_(False))
            if "unknown" in selected:
                conditions.append(MetricDatasetResult.met.is_(None))
            base = base.where(sa.or_(*conditions) if conditions else sa.false())

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = DatasetRegistry.dataset_urn.asc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).all()

        synced_q = select(func.max(DatasetRegistry.attrs_synced_at)).where(*in_scope)
        attrs_synced_at = (await self._db.execute(synced_q)).scalar()

        return (
            [
                MetricDatasetRecord(
                    dataset_urn=row.dataset_urn,
                    met="unknown" if row.met is None else ("true" if row.met else "false"),
                    # last_check_at is the per-dataset evidence timestamp falling
                    # back to the run time — doc-health has no per-dataset
                    # timestamp, so it always reports the run.
                    last_check_at=row.evidence_at or row.measured_at,
                    detail=row.detail,
                )
                for row in rows
            ],
            total_count,
            attrs_synced_at,
        )
