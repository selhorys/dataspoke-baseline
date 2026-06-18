"""Metadata Generation service — UC4 LLM-powered documentation proposal pipeline.

Spec: spec/feature/BACKEND.md §Metadata Generation Service
      spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.backend._dataset_filter import resolve_dataset_scope, validate_dataset_filter_service
from src.backend.admin.config_service import RuntimeConfigDTO, get_runtime_config
from src.backend.metagen.debate import run_debate
from src.backend.metagen.debate_models import MetagenLLMOutput
from src.backend.metagen.prompts import build_run_prompt
from src.backend.metagen.reviewer import build_metagen_review_tool
from src.backend.metagen.validator import build_metagen_validate_tool
from src.backend.ontogen.embedding_search import (
    search_edge_embeddings,
    search_node_embeddings,
    search_triple_embeddings,
)
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.datahub.documents import fetch_related_documents
from src.shared.db.models import (
    DatasetNodeMap,
    DatasetRegistry,
    Event,
    MetagenBoundary,
    MetagenCandidate,
    MetagenCandidateEmbedding,
    MetagenConfig,
    MetagenItem,
    OntogenEdge,
    OntogenNode,
    OntogenTriple,
)
from src.shared.events import (
    METAGEN_CANDIDATE_APPROVE,
    METAGEN_CANDIDATE_REJECT,
    METAGEN_CONFIG_CREATE,
    METAGEN_CONFIG_DELETE,
    METAGEN_CONFIG_UPDATE,
    METAGEN_RUN_COMPLETE,
    METAGEN_RUN_FAILED,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    PreconditionFailedError,
)
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager

logger = logging.getLogger(__name__)

_LOCK_TTL_SECONDS = 3600

_VALID_KINDS: frozenset[str] = frozenset({"dataset.description", "column.description"})


def _lock_key(conf_id: str) -> str:
    """Per-conf Redis lock key (one in-flight run per conf)."""
    return f"metagen:running:{conf_id}"


# ── Value objects ─────────────────────────────────────────────────────────────


class MetagenConfDTO(BaseModel):
    id: str
    name: str
    is_enabled: bool
    schedule_tier: str | None
    dataset_filter: dict[str, Any]
    result_limit: int
    overwrite_pending: bool
    created_at: datetime
    updated_at: datetime


class MetagenBoundaryDTO(BaseModel):
    dataset_urn: str
    is_enabled: bool
    allowed: list[str]
    owner: str | None
    created_at: datetime
    updated_at: datetime


class CandidateDTO(BaseModel):
    candidate_id: str
    conf_id: str | None
    conf_name: str | None
    dataset_urn: str
    item_id: str
    run_id: str
    value: str
    confidence_score: float
    status: str
    evidence: dict[str, Any]
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_id: str | None


class ItemSummaryDTO(BaseModel):
    dataset_urn: str
    item_id: str
    kind: str
    field_path: str | None
    candidate_count: int
    non_rejected_count: int
    has_approved: bool
    created_at: datetime
    updated_at: datetime


class ItemDetailDTO(BaseModel):
    dataset_urn: str
    item_id: str
    kind: str
    field_path: str | None
    created_at: datetime
    updated_at: datetime
    candidates: list[CandidateDTO]


class RunResultDTO(BaseModel):
    run_id: str
    conf_id: str
    status: str
    dry_run: bool
    unresolved_urns: list[str]
    counts: dict[str, int]
    debate_outcome: str | None = None
    producer_iterations: int | None = None


class UncoveredRowDTO(BaseModel):
    dataset_urn: str
    reason: str


# ── Helpers ───────────────────────────────────────────────────────────────────


def _conf_to_dto(row: MetagenConfig) -> MetagenConfDTO:
    return MetagenConfDTO(
        id=str(row.id),
        name=row.name,
        is_enabled=row.is_enabled,
        schedule_tier=row.schedule_tier,
        dataset_filter=dict(row.dataset_filter) if row.dataset_filter else {},
        result_limit=row.result_limit,
        overwrite_pending=row.overwrite_pending,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _boundary_to_dto(row: MetagenBoundary) -> MetagenBoundaryDTO:
    return MetagenBoundaryDTO(
        dataset_urn=row.dataset_urn,
        is_enabled=row.is_enabled,
        allowed=list(row.allowed),
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _candidate_to_dto(row: MetagenCandidate, conf_name: str | None = None) -> CandidateDTO:
    return CandidateDTO(
        candidate_id=str(row.candidate_id),
        conf_id=str(row.conf_id) if row.conf_id is not None else None,
        conf_name=conf_name,
        dataset_urn=row.dataset_urn,
        item_id=row.item_id,
        run_id=str(row.run_id),
        value=row.value,
        confidence_score=row.confidence_score,
        status=row.status,
        evidence=dict(row.evidence) if row.evidence else {},
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
        reviewer_id=row.reviewer_id,
    )


def _event_row(
    entity_type: str,
    entity_id: str,
    event_type: str,
    status: str,
    detail: dict[str, Any],
) -> Event:
    return Event(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        status=status,
        detail=detail,
        occurred_at=datetime.now(tz=UTC),
    )


# ── Service ───────────────────────────────────────────────────────────────────


class MetagenService:
    """Metadata generation service over a managed conf collection.

    Constructor-injected dependencies (stateless service pattern per
    spec/feature/BACKEND.md §Service Pattern):

    - datahub: DataHubClient
    - db: AsyncSession
    - cache: RedisClient
    - llm: LLMClient
    - vector: PgVectorManager
    """

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
        llm: LLMClient,
        vector: PgVectorManager,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache
        self._llm = llm
        self._vector = vector

    # ── Conf collection CRUD ──────────────────────────────────────────────────

    async def _load_conf_row(self, conf_id: str) -> MetagenConfig:
        """Load a conf row by id or raise 404 METAGEN_CONF_NOT_FOUND."""
        try:
            cid = uuid.UUID(conf_id)
        except ValueError:
            raise EntityNotFoundError("metagen_conf", conf_id)
        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == cid))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_conf", conf_id)
        return row

    async def list_confs(
        self, *, offset: int = 0, limit: int = 20, order_by: Any = None
    ) -> tuple[list[MetagenConfDTO], int]:
        """List confs (paginated, newest first)."""
        count_q = select(func.count()).select_from(MetagenConfig)
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetagenConfig.created_at.desc()
        rows_q = (
            select(MetagenConfig)
            .order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return [_conf_to_dto(r) for r in rows], int(total)

    async def get_conf(self, conf_id: str) -> MetagenConfDTO:
        row = await self._load_conf_row(conf_id)
        return _conf_to_dto(row)

    async def create_conf(self, data: dict[str, Any]) -> MetagenConfDTO:
        """Create a new conf. Raises 409 METAGEN_CONF_EXISTS on duplicate name.

        `schedule_tier` values are constrained at the API schema layer
        (`MetagenConfCreateRequest`).
        """
        dataset_filter = data.get("dataset_filter", {}) or {}
        validate_dataset_filter_service(dataset_filter)

        name = data["name"]
        existing = await self._db.execute(
            select(MetagenConfig.id).where(MetagenConfig.name == name)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("METAGEN_CONF_EXISTS", f"Metagen conf {name!r} already exists")

        now = datetime.now(tz=UTC)
        row = MetagenConfig(
            id=uuid.uuid4(),
            name=name,
            is_enabled=data.get("is_enabled", False),
            schedule_tier=data.get("schedule_tier"),
            dataset_filter=dataset_filter,
            result_limit=data.get("result_limit", 3),
            overwrite_pending=data.get("overwrite_pending", True),
            created_at=now,
            updated_at=now,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_metagen_event(
            str(row.id),
            METAGEN_CONFIG_CREATE,
            "success",
            {"operation": "POST", "conf_id": str(row.id), "conf_name": row.name},
        )
        return _conf_to_dto(row)

    async def put_conf(self, conf_id: str, data: dict[str, Any]) -> MetagenConfDTO:
        """Full replacement of a conf. Raises 404 when absent, 409 on name collision."""
        dataset_filter = data.get("dataset_filter", {}) or {}
        validate_dataset_filter_service(dataset_filter)

        row = await self._load_conf_row(conf_id)

        name = data["name"]
        if name != row.name:
            clash = await self._db.execute(
                select(MetagenConfig.id).where(
                    MetagenConfig.name == name, MetagenConfig.id != row.id
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise ConflictError("METAGEN_CONF_EXISTS", f"Metagen conf {name!r} already exists")

        row.name = name
        row.is_enabled = data.get("is_enabled", False)
        row.schedule_tier = data.get("schedule_tier")
        row.dataset_filter = dataset_filter
        row.result_limit = data.get("result_limit", 3)
        row.overwrite_pending = data.get("overwrite_pending", True)
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_metagen_event(
            str(row.id),
            METAGEN_CONFIG_UPDATE,
            "success",
            {"operation": "PUT", "conf_id": str(row.id), "conf_name": row.name},
        )
        return _conf_to_dto(row)

    async def patch_conf(self, conf_id: str, partial: dict[str, Any]) -> MetagenConfDTO:
        """Partial update of a conf. Raises 404 when absent, 409 on name collision."""
        if "dataset_filter" in partial and partial["dataset_filter"] is not None:
            validate_dataset_filter_service(partial["dataset_filter"])

        row = await self._load_conf_row(conf_id)

        if "name" in partial and partial["name"] is not None and partial["name"] != row.name:
            clash = await self._db.execute(
                select(MetagenConfig.id).where(
                    MetagenConfig.name == partial["name"], MetagenConfig.id != row.id
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise ConflictError(
                    "METAGEN_CONF_EXISTS", f"Metagen conf {partial['name']!r} already exists"
                )

        for field_name in (
            "name",
            "is_enabled",
            "dataset_filter",
            "result_limit",
            "overwrite_pending",
        ):
            if field_name in partial and partial[field_name] is not None:
                setattr(row, field_name, partial[field_name])
        if "schedule_tier" in partial:
            row.schedule_tier = partial["schedule_tier"]

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_metagen_event(
            str(row.id),
            METAGEN_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "conf_id": str(row.id),
                "conf_name": row.name,
                "fields_changed": list(partial.keys()),
            },
        )
        return _conf_to_dto(row)

    async def delete_conf(self, conf_id: str) -> None:
        """Delete a conf. Deletes this conf's non-approved candidates and SET NULLs
        conf_id on its approved candidates (already emitted to DataHub). Emits
        METAGEN.CONFIG_DELETE.
        """
        row = await self._load_conf_row(conf_id)
        cid = row.id

        # Drop embeddings for this conf's non-approved candidates first (FK).
        await self._db.execute(
            delete(MetagenCandidateEmbedding).where(
                MetagenCandidateEmbedding.candidate_id.in_(
                    select(MetagenCandidate.candidate_id).where(
                        MetagenCandidate.conf_id == cid,
                        MetagenCandidate.status != "approved",
                    )
                )
            )
        )
        await self._db.execute(
            delete(MetagenCandidate).where(
                MetagenCandidate.conf_id == cid,
                MetagenCandidate.status != "approved",
            )
        )
        # Orphan the approved candidates (retain as DataHub-emitted history).
        await self._db.execute(
            MetagenCandidate.__table__.update()
            .where(
                MetagenCandidate.conf_id == cid,
                MetagenCandidate.status == "approved",
            )
            .values(conf_id=None)
        )

        await self._db.delete(row)
        await self._db.commit()

        await self._record_metagen_event(
            conf_id,
            METAGEN_CONFIG_DELETE,
            "success",
            {"operation": "DELETE", "conf_id": conf_id},
        )

    # ── Boundary CRUD ─────────────────────────────────────────────────────────

    async def get_boundary(self, urn: str) -> MetagenBoundaryDTO | None:
        result = await self._db.execute(
            select(MetagenBoundary).where(MetagenBoundary.dataset_urn == urn)
        )
        row = result.scalar_one_or_none()
        return _boundary_to_dto(row) if row else None

    async def put_boundary(self, urn: str, boundary: dict[str, Any]) -> MetagenBoundaryDTO:
        """Create or replace a boundary row."""
        allowed = boundary.get("allowed", [])
        for kind in allowed:
            if kind not in _VALID_KINDS:
                raise PreconditionFailedError(
                    "INVALID_PARAMETER",
                    f"allowed kind {kind!r} must be one of {sorted(_VALID_KINDS)}",
                )

        result = await self._db.execute(
            select(MetagenBoundary).where(MetagenBoundary.dataset_urn == urn)
        )
        existing = result.scalar_one_or_none()

        if existing is None:
            existing = MetagenBoundary(dataset_urn=urn)
            self._db.add(existing)

        existing.is_enabled = boundary.get("is_enabled", False)
        existing.allowed = allowed
        existing.owner = boundary.get("owner")
        existing.updated_at = datetime.now(tz=UTC)

        await self._db.commit()
        await self._db.refresh(existing)
        return _boundary_to_dto(existing)

    async def patch_boundary(self, urn: str, patch: dict[str, Any]) -> MetagenBoundaryDTO:
        """Partial update of a boundary row."""
        if "allowed" in patch and patch["allowed"] is not None:
            for kind in patch["allowed"]:
                if kind not in _VALID_KINDS:
                    raise PreconditionFailedError(
                        "INVALID_PARAMETER",
                        f"allowed kind {kind!r} must be one of {sorted(_VALID_KINDS)}",
                    )

        result = await self._db.execute(
            select(MetagenBoundary).where(MetagenBoundary.dataset_urn == urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_boundary", urn)

        for field_name in ("is_enabled", "allowed", "owner"):
            if field_name in patch and patch[field_name] is not None:
                setattr(row, field_name, patch[field_name])
            elif field_name == "owner" and "owner" in patch and patch["owner"] is None:
                row.owner = None

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return _boundary_to_dto(row)

    async def delete_boundary(self, urn: str) -> None:
        """Delete a boundary row."""
        result = await self._db.execute(
            select(MetagenBoundary).where(MetagenBoundary.dataset_urn == urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_boundary", urn)
        await self._db.delete(row)
        await self._db.commit()

    # ── Items ─────────────────────────────────────────────────────────────────

    async def list_items(
        self,
        *,
        dataset_urn: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        conf_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[ItemSummaryDTO], int]:
        base = select(MetagenItem)
        if dataset_urn is not None:
            base = base.where(MetagenItem.dataset_urn == dataset_urn)
        if kind is not None:
            base = base.where(MetagenItem.kind == kind)
        if conf_id is not None:
            try:
                cid = uuid.UUID(conf_id)
            except ValueError:
                raise EntityNotFoundError("metagen_conf", conf_id)
            base = base.where(
                select(MetagenCandidate.candidate_id)
                .where(
                    MetagenCandidate.conf_id == cid,
                    MetagenCandidate.dataset_urn == MetagenItem.dataset_urn,
                    MetagenCandidate.item_id == MetagenItem.item_id,
                )
                .exists()
            )
        if status is not None:
            # Only items with at least one candidate of the requested status are
            # returned (matching _build_item_summary), so the EXISTS keeps the
            # count consistent with the materialised page.
            base = base.where(
                select(MetagenCandidate.candidate_id)
                .where(
                    MetagenCandidate.status == status,
                    MetagenCandidate.dataset_urn == MetagenItem.dataset_urn,
                    MetagenCandidate.item_id == MetagenItem.item_id,
                )
                .exists()
            )

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetagenItem.updated_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()

        summaries: list[ItemSummaryDTO] = []
        for row in rows:
            summary = await self._build_item_summary(row, status_filter=status)
            if summary is not None:
                summaries.append(summary)

        return summaries, total

    async def get_item(self, dataset_urn: str, item_id: str) -> ItemDetailDTO:
        result = await self._db.execute(
            select(MetagenItem).where(
                MetagenItem.dataset_urn == dataset_urn,
                MetagenItem.item_id == item_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_item", f"{dataset_urn}::{item_id}")
        return await self._build_item_detail(row)

    async def list_items_for_dataset(
        self, urn: str, *, offset: int = 0, limit: int = 20, order_by: Any = None
    ) -> tuple[list[ItemSummaryDTO], int]:
        return await self.list_items(dataset_urn=urn, offset=offset, limit=limit, order_by=order_by)

    async def get_item_for_dataset(self, urn: str, item_id: str) -> ItemDetailDTO:
        return await self.get_item(urn, item_id)

    # ── Uncovered view ────────────────────────────────────────────────────────

    async def list_uncovered(
        self,
        *,
        include_disallowed: bool = False,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[UncoveredRowDTO], int]:
        """Registered datasets not documented by any enabled conf.

        Default (`include_disallowed=false`): a registered dataset matched by no
        enabled conf's `dataset_filter` → `reason="no_conf_match"`.

        With `include_disallowed=true`: additionally includes datasets matched by
        some enabled conf but blocked by their boundary (missing, `is_enabled=false`,
        or empty `allowed`) → `reason="boundary_blocked"`. A dataset matched and
        writable by at least one enabled conf is never listed.
        """
        # Registered datasets (the UC1 unmanaged analogue base set).
        reg_result = await self._db.execute(
            select(DatasetRegistry.dataset_urn).where(DatasetRegistry.datahub_registered.is_(True))
        )
        registered: set[str] = {r[0] for r in reg_result.all()}

        # Union of URNs matched by any enabled conf's dataset_filter.
        confs_result = await self._db.execute(
            select(MetagenConfig).where(MetagenConfig.is_enabled.is_(True))
        )
        enabled_confs = confs_result.scalars().all()

        matched: set[str] = set()
        for conf in enabled_confs:
            scope = await resolve_dataset_scope(
                self._datahub,
                dict(conf.dataset_filter) if conf.dataset_filter else {},
                swallow_enumerate_errors=True,
            )
            matched.update(scope.resolved_urns)
        matched &= registered

        # Writable boundary set: is_enabled=true with non-empty allowed.
        bnd_result = await self._db.execute(
            select(MetagenBoundary.dataset_urn).where(
                MetagenBoundary.is_enabled.is_(True),
                func.cardinality(MetagenBoundary.allowed) > 0,
            )
        )
        writable_boundary: set[str] = {r[0] for r in bnd_result.all()}

        # Default order: dataset_urn ascending. ?sort=dataset_urn_desc reverses
        # the in-memory ordering (the uncovered set is computed in Python).
        from sqlalchemy.sql import operators

        reverse = order_by is not None and getattr(order_by, "modifier", None) is operators.desc_op
        rows: list[UncoveredRowDTO] = []
        for urn in sorted(registered, reverse=reverse):
            if urn not in matched:
                rows.append(UncoveredRowDTO(dataset_urn=urn, reason="no_conf_match"))
            elif include_disallowed and urn not in writable_boundary:
                rows.append(UncoveredRowDTO(dataset_urn=urn, reason="boundary_blocked"))

        total = len(rows)
        return rows[offset : offset + limit], total

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        conf_id: str,
        *,
        dataset_urns: list[str] | None = None,
        dry_run: bool = False,
    ) -> RunResultDTO:
        """Per-conf metagen inference pipeline, serialised by a per-conf Redis lock.

        - Raises EntityNotFoundError(metagen_conf) if the conf is absent.
        - Raises ConflictError(METAGEN_RUNNING) if this conf is already running.
        - Raises ConflictError(METAGEN_DISABLED) if conf.is_enabled=false and not dry_run.
        """
        conf = await self.get_conf(conf_id)

        run_id = str(uuid.uuid4())
        lock_key = _lock_key(conf_id)
        lock_token = secrets.token_urlsafe(16)
        acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=_LOCK_TTL_SECONDS)
        if not acquired:
            raise ConflictError("METAGEN_RUNNING", f"Metagen conf {conf_id} is already running")

        try:
            return await self._run_inner(
                conf=conf, dataset_urns=dataset_urns, dry_run=dry_run, run_id=run_id
            )
        except ConflictError:
            raise
        except Exception as exc:
            logger.error("metagen_run_failed", exc_info=True)
            try:
                await self._record_metagen_event(
                    conf_id,
                    METAGEN_RUN_FAILED,
                    "failure",
                    {
                        "error": str(exc),
                        "run_id": run_id,
                        "conf_id": conf_id,
                        "conf_name": conf.name,
                    },
                )
            except Exception:
                logger.warning("metagen_run_failed_event_emit_failed", exc_info=True)
            raise
        finally:
            await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(
        self,
        *,
        conf: MetagenConfDTO,
        dataset_urns: list[str] | None,
        dry_run: bool,
        run_id: str,
    ) -> RunResultDTO:
        rc = await get_runtime_config(self._db)

        if not conf.is_enabled and not dry_run:
            raise ConflictError(
                "METAGEN_DISABLED", "Metagen conf is disabled; only dry-run is permitted"
            )

        conf_uuid = uuid.UUID(conf.id)

        # Step 1: Enumerate in-scope datasets
        in_scope_urns, unresolved_urns = await self._enumerate_in_scope_datasets(conf, dataset_urns)

        run_uuid = uuid.UUID(run_id)
        items_considered = 0
        candidates_added = 0
        candidates_evicted = 0
        rejected_cleared = 0
        debate_outcome: str | None = None
        producer_iterations: int | None = None

        # Step 2: Clear this conf's rejected candidates across in-scope datasets
        if not dry_run:
            rejected_cleared = await self._clear_rejected_candidates(in_scope_urns, conf_uuid)

        for urn in in_scope_urns:
            # Step 3: Fetch evidence
            evidence = await self._fetch_evidence(urn, rc=rc)

            # Step 4: Enumerate target items for this dataset
            boundary_result = await self._db.execute(
                select(MetagenBoundary).where(MetagenBoundary.dataset_urn == urn)
            )
            boundary = boundary_result.scalar_one_or_none()
            if boundary is None or not boundary.is_enabled:
                continue

            # Get approved items to skip in producer
            approved_result = await self._db.execute(
                select(MetagenCandidate.dataset_urn, MetagenCandidate.item_id).where(
                    MetagenCandidate.dataset_urn == urn,
                    MetagenCandidate.status == "approved",
                )
            )
            approved_item_ids: frozenset[tuple[str, str]] = frozenset(
                (r.dataset_urn, r.item_id) for r in approved_result.fetchall()
            )

            target_items = list(
                self._enumerate_target_items(urn, boundary, evidence, approved_item_ids)
            )
            items_considered += len(target_items)

            if not target_items:
                continue

            # Step 5: Run adversarial debate
            run_nonce = secrets.token_hex(8)
            prompt = build_run_prompt(
                evidence_per_dataset={urn: evidence},
                target_items=target_items,
                nonce=run_nonce,
            )

            schema_field_paths = {
                urn: {
                    f.get("fieldPath", "")
                    for f in evidence.get("schemaMetadata", {}).get("fields", [])
                    if f.get("fieldPath")
                }
            }
            boundary_allowed = {urn: list(boundary.allowed)}

            validate_tool = build_metagen_validate_tool(
                in_scope_urns=frozenset([urn]),
                boundary_allowed=boundary_allowed,
                schema_field_paths=schema_field_paths,
                approved_item_ids=approved_item_ids,
            )
            review_tool = build_metagen_review_tool()

            debate_result = await run_debate(
                llm=self._llm,
                vector=self._vector,
                db=self._db,
                producer_prompt=prompt,
                validate_tool=validate_tool,
                review_tool=review_tool,
                in_scope_urns=frozenset([urn]),
                max_turns=rc.metagen_debate_max_turns,
                rag_k=rc.metagen_debate_rag_k,
                reviewer_model=rc.metagen_debate_reviewer_model,
                llm_provider=rc.llm_provider,
                llm_base_model=rc.llm_model,
                producer_schema=MetagenLLMOutput,
                producer_max_iterations=rc.metagen_llm_max_iterations,
                run_id=run_id,
                stub_llm_client=rc.stub_llm_client,
            )
            debate_outcome = debate_result.outcome
            producer_iterations = debate_result.transcript.get("producer_iterations", 1)

            # Only accept outcome persists candidates; drop on turns_exhausted / cycle_detected
            if debate_result.outcome != "accept":
                logger.info(
                    "metagen_debate_candidates_dropped",
                    extra={
                        "dataset_urn": urn,
                        "outcome": debate_result.outcome,
                        "run_id": run_id,
                    },
                )
                continue

            try:
                candidate_output = MetagenLLMOutput.model_validate(debate_result.payload)
            except PydanticValidationError:
                logger.warning(
                    "metagen_llm_output_validation_failed",
                    extra={"dataset_urn": urn},
                )
                continue

            accepted = [
                c
                for c in candidate_output.candidates
                if c.confidence_score >= rc.metagen_confidence_threshold
            ]

            if dry_run:
                candidates_added += len(accepted)
                continue

            for cand in accepted:
                added, evicted = await self._apply_per_item_budget(
                    urn=cand.dataset_urn,
                    item_id=cand.item_id,
                    new_candidate_value=cand.value,
                    new_candidate_confidence=cand.confidence_score,
                    new_candidate_evidence=dict(debate_result.transcript),
                    run_id=run_uuid,
                    conf_id=conf_uuid,
                    conf=conf,
                )
                if added:
                    candidates_added += 1
                if evicted:
                    candidates_evicted += 1

        if dry_run:
            counts: dict[str, int] = {
                "items_considered": items_considered,
                "candidates_proposed": candidates_added,
            }
        else:
            counts = {
                "items_considered": items_considered,
                "candidates_added": candidates_added,
                "candidates_evicted": candidates_evicted,
                "rejected_cleared": rejected_cleared,
            }

        await self._record_metagen_event(
            conf.id,
            METAGEN_RUN_COMPLETE,
            "success",
            {
                "run_id": run_id,
                "conf_id": conf.id,
                "conf_name": conf.name,
                "unresolved_urns": unresolved_urns,
                "counts": counts,
                "dry_run": dry_run,
                "producer_iterations": producer_iterations,
                "debate_outcome": debate_outcome,
            },
        )

        return RunResultDTO(
            run_id=run_id,
            conf_id=conf.id,
            status="success",
            dry_run=dry_run,
            unresolved_urns=unresolved_urns,
            counts=counts,
            debate_outcome=debate_outcome,
            producer_iterations=producer_iterations,
        )

    # ── Review ────────────────────────────────────────────────────────────────

    async def review_candidate(
        self,
        *,
        dataset_urn: str,
        item_id: str,
        candidate_id: str,
        verdict: Literal["approve", "reject"],
        reason: str,
        reviewer_id: str | None = None,
    ) -> CandidateDTO:
        """Apply a human review verdict to a candidate.

        - approve: demote any existing approved sibling; flip target to approved;
          emit to DataHub editable aspect; emit METAGEN.CANDIDATE_APPROVE.
        - reject: only valid for llm_approved; raises 409 if already approved;
          emit METAGEN.CANDIDATE_REJECT.
        - Raises 422 METAGEN_DATASET_NOT_IN_BOUNDARY if boundary absent/disabled.
        """
        # Boundary guard
        bnd_result = await self._db.execute(
            select(MetagenBoundary).where(MetagenBoundary.dataset_urn == dataset_urn)
        )
        boundary = bnd_result.scalar_one_or_none()
        if boundary is None or not boundary.is_enabled:
            raise PreconditionFailedError(
                "METAGEN_DATASET_NOT_IN_BOUNDARY",
                f"Dataset {dataset_urn!r} has no active boundary — "
                f"add one via PUT .../attr/metagen/boundary",
            )

        try:
            cand_uuid = uuid.UUID(candidate_id)
        except ValueError:
            raise EntityNotFoundError("metagen_candidate", candidate_id)

        cand_result = await self._db.execute(
            select(MetagenCandidate).where(
                MetagenCandidate.candidate_id == cand_uuid,
                MetagenCandidate.dataset_urn == dataset_urn,
                MetagenCandidate.item_id == item_id,
            )
        )
        cand = cand_result.scalar_one_or_none()
        if cand is None:
            raise EntityNotFoundError("metagen_candidate", candidate_id)

        now = datetime.now(tz=UTC)

        if verdict == "approve":
            # Atomically demote any existing approved sibling
            sibling_result = await self._db.execute(
                select(MetagenCandidate).where(
                    MetagenCandidate.dataset_urn == dataset_urn,
                    MetagenCandidate.item_id == item_id,
                    MetagenCandidate.status == "approved",
                    MetagenCandidate.candidate_id != cand_uuid,
                )
            )
            sibling = sibling_result.scalar_one_or_none()
            if sibling is not None:
                sibling.status = "llm_approved"
                sibling.reviewed_at = now
                self._db.add(sibling)
                # Flush the demotion first so the partial unique index
                # (UNIQUE (dataset_urn, item_id) WHERE status='approved')
                # does not see two approved rows simultaneously.
                await self._db.flush()

            cand.status = "approved"
            cand.reviewed_at = now
            cand.reviewer_id = reviewer_id
            self._db.add(cand)
            await self._db.commit()
            await self._db.refresh(cand)

            # Emit to DataHub editable aspect (best-effort)
            await self._emit_to_datahub(
                urn=dataset_urn,
                item_id=item_id,
                value=cand.value,
            )

            # Refresh embedding for the newly approved candidate
            await self._refresh_candidate_embedding(cand)

            await self._record_dataset_event(
                dataset_urn,
                METAGEN_CANDIDATE_APPROVE,
                "success",
                {
                    "item_id": item_id,
                    "candidate_id": candidate_id,
                    "reason": reason,
                },
            )

        elif verdict == "reject":
            if cand.status == "approved":
                raise ConflictError(
                    "METAGEN_CANNOT_REJECT_APPROVED",
                    "Cannot reject an approved candidate — "
                    "approve a different sibling to demote it",
                )

            cand.status = "rejected"
            cand.reviewed_at = now
            cand.reviewer_id = reviewer_id
            self._db.add(cand)
            await self._db.commit()
            await self._db.refresh(cand)

            await self._record_dataset_event(
                dataset_urn,
                METAGEN_CANDIDATE_REJECT,
                "success",
                {
                    "item_id": item_id,
                    "candidate_id": candidate_id,
                    "reason": reason,
                },
            )

        conf_name: str | None = None
        if cand.conf_id is not None:
            name_map = await self._conf_name_map({cand.conf_id})
            conf_name = name_map.get(cand.conf_id)
        return _candidate_to_dto(cand, conf_name)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _enumerate_in_scope_datasets(
        self,
        conf: MetagenConfDTO,
        override_urns: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """Intersect DataHub dataset_filter with metagen_boundary.is_enabled=true rows.

        Returns (in_scope_urns, unresolved_urns).
        """
        scope = await resolve_dataset_scope(
            self._datahub,
            conf.dataset_filter or {},
            explicit_urns_override=override_urns if override_urns else None,
            swallow_enumerate_errors=True,
        )
        datahub_urn_set: set[str] = set(scope.resolved_urns)
        unresolved: list[str] = scope.unresolved_urns

        if not datahub_urn_set:
            return [], unresolved

        # Intersect with boundary rows where is_enabled=true
        bnd_result = await self._db.execute(
            select(MetagenBoundary.dataset_urn).where(MetagenBoundary.is_enabled.is_(True))
        )
        enabled_boundary_urns: set[str] = {r.dataset_urn for r in bnd_result.fetchall()}

        in_scope = sorted(datahub_urn_set & enabled_boundary_urns)
        return in_scope, unresolved

    async def _clear_rejected_candidates(self, in_scope_urns: list[str], conf_id: uuid.UUID) -> int:
        """Delete this conf's rejected candidates across in-scope datasets.

        Returns deleted row count. Embeddings rows in
        `metagen_candidate_embeddings` carry a FK to
        `metagen_candidates.candidate_id`; delete them first so the candidate
        delete does not raise ForeignKeyViolationError.
        """
        from sqlalchemy.engine import CursorResult

        await self._db.execute(
            delete(MetagenCandidateEmbedding).where(
                MetagenCandidateEmbedding.candidate_id.in_(
                    select(MetagenCandidate.candidate_id).where(
                        MetagenCandidate.conf_id == conf_id,
                        MetagenCandidate.dataset_urn.in_(in_scope_urns),
                        MetagenCandidate.status == "rejected",
                    )
                )
            )
        )
        raw = await self._db.execute(
            delete(MetagenCandidate).where(
                MetagenCandidate.conf_id == conf_id,
                MetagenCandidate.dataset_urn.in_(in_scope_urns),
                MetagenCandidate.status == "rejected",
            )
        )
        await self._db.commit()
        return (raw.rowcount or 0) if isinstance(raw, CursorResult) else 0

    async def _fetch_evidence(self, urn: str, *, rc: RuntimeConfigDTO) -> dict[str, Any]:
        """Fetch DataHub aspects for a single dataset.

        Collects: datasetProperties, schemaMetadata, editableDatasetProperties,
        editableSchemaMetadata, glossaryTerms, plus UC3-approved nodes/triples.
        """
        evidence: dict[str, Any] = {}
        try:
            from datahub.metadata.schema_classes import (
                DatasetPropertiesClass,
                EditableDatasetPropertiesClass,
                EditableSchemaMetadataClass,
                GlossaryTermsClass,
                SchemaMetadataClass,
            )

            props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
            if props:
                evidence["datasetProperties"] = {
                    "name": getattr(props, "name", None),
                    "description": getattr(props, "description", None),
                    "tags": getattr(props, "tags", None),
                }

            schema = await self._datahub.get_aspect(urn, SchemaMetadataClass)
            if schema and hasattr(schema, "fields"):
                evidence["schemaMetadata"] = {
                    "fields": [
                        {
                            "fieldPath": getattr(f, "fieldPath", ""),
                            "type": str(getattr(f, "type", "")),
                            "description": getattr(f, "description", None),
                        }
                        for f in schema.fields
                    ]
                }

            editable_props = await self._datahub.get_aspect(urn, EditableDatasetPropertiesClass)
            if editable_props:
                evidence["editableDatasetProperties"] = {
                    "description": getattr(editable_props, "description", None),
                }

            editable_schema = await self._datahub.get_aspect(urn, EditableSchemaMetadataClass)
            if editable_schema and hasattr(editable_schema, "editableSchemaFieldInfo"):
                evidence["editableSchemaMetadata"] = {
                    "editableSchemaFieldInfo": [
                        {
                            "fieldPath": getattr(f, "fieldPath", ""),
                            "description": getattr(f, "description", None),
                        }
                        for f in editable_schema.editableSchemaFieldInfo
                    ]
                }

            glossary = await self._datahub.get_aspect(urn, GlossaryTermsClass)
            if glossary and hasattr(glossary, "terms"):
                evidence["glossaryTerms"] = [getattr(t, "urn", str(t)) for t in glossary.terms]
        except Exception:
            logger.warning("metagen_evidence_fetch_failed", extra={"urn": urn}, exc_info=True)

        # Related document entities (best-effort)
        try:
            evidence["related_documents"] = await fetch_related_documents(urn, self._datahub)
        except Exception:
            logger.warning(
                "metagen_evidence_related_documents_failed", extra={"urn": urn}, exc_info=True
            )
            evidence.setdefault("related_documents", [])

        # UC3-approved ontology context (human-approved nodes only)
        try:
            node_map_result = await self._db.execute(
                select(DatasetNodeMap).where(
                    DatasetNodeMap.dataset_urn == urn,
                    DatasetNodeMap.status == "approved",
                )
            )
            approved_node_ids = [r.node_id for r in node_map_result.scalars().all()]
            if approved_node_ids:
                nodes_result = await self._db.execute(
                    select(OntogenNode).where(
                        OntogenNode.id.in_(approved_node_ids),
                        OntogenNode.status == "approved",
                    )
                )
                nodes = nodes_result.scalars().all()
                evidence["ontology"] = {
                    "approved_nodes": [
                        {"id": n.id, "name": n.name, "description": n.description} for n in nodes
                    ]
                }
        except Exception:
            logger.warning(
                "metagen_ontology_context_fetch_failed", extra={"urn": urn}, exc_info=True
            )

        # Per-dataset ontology RAG via approved vector collections (best-effort)
        try:
            props = evidence.get("datasetProperties", {})
            schema_fields = evidence.get("schemaMetadata", {}).get("fields", [])
            query_text = (
                f"{urn} "
                f"{props.get('name', '')} "
                f"{props.get('description', '')} "
                f"{' '.join(f.get('fieldPath', '') for f in schema_fields)}"
            )
            query_vec = await self._llm.embed(query_text)

            node_k = rc.metagen_ontology_rag_node_k
            edge_k = rc.metagen_ontology_rag_edge_k
            triple_k = rc.metagen_ontology_rag_triple_k

            node_hits = (
                await search_node_embeddings(self._vector, query_vec, top_k=node_k, threshold=None)
                if node_k > 0
                else []
            )
            edge_hits = (
                await search_edge_embeddings(self._vector, query_vec, top_k=edge_k, threshold=None)
                if edge_k > 0
                else []
            )
            triple_hits = (
                await search_triple_embeddings(
                    self._vector, query_vec, top_k=triple_k, threshold=None
                )
                if triple_k > 0
                else []
            )

            # Hydrate nodes
            rag_nodes: list[dict[str, Any]] = []
            if node_hits:
                node_ids = [h.dataset_urn for h in node_hits]
                score_by_node_id = {h.dataset_urn: h.score for h in node_hits}
                node_rows_result = await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id.in_(node_ids))
                )
                node_rows = node_rows_result.scalars().all()
                rag_nodes = [
                    {
                        "id": n.id,
                        "name": n.name,
                        "description": n.description,
                        "score": score_by_node_id.get(n.id, 0.0),
                    }
                    for n in node_rows
                ]

            # Hydrate edges
            rag_edges: list[dict[str, Any]] = []
            if edge_hits:
                edge_ids = [h.dataset_urn for h in edge_hits]
                score_by_edge_id = {h.dataset_urn: h.score for h in edge_hits}
                edge_rows_result = await self._db.execute(
                    select(OntogenEdge).where(OntogenEdge.id.in_(edge_ids))
                )
                edge_rows = edge_rows_result.scalars().all()
                rag_edges = [
                    {
                        "id": e.id,
                        "label": e.label,
                        "score": score_by_edge_id.get(e.id, 0.0),
                    }
                    for e in edge_rows
                ]

            # Hydrate triples (resolve subject/edge/object names via joined load)
            rag_triples: list[dict[str, Any]] = []
            if triple_hits:
                triple_ids = [h.dataset_urn for h in triple_hits]
                score_by_triple_id = {h.dataset_urn: h.score for h in triple_hits}
                triple_rows_result = await self._db.execute(
                    select(OntogenTriple)
                    .options(
                        joinedload(OntogenTriple.subject_node),
                        joinedload(OntogenTriple.edge),
                        joinedload(OntogenTriple.object_node),
                    )
                    .where(OntogenTriple.id.in_(triple_ids))
                )
                triple_rows = triple_rows_result.unique().scalars().all()
                rag_triples = [
                    {
                        "subject_name": t.subject_node.name,
                        "edge_label": t.edge.label,
                        "object_name": t.object_node.name,
                        "score": score_by_triple_id.get(t.id, 0.0),
                    }
                    for t in triple_rows
                ]

            evidence["ontology_rag"] = {
                "nodes": rag_nodes,
                "edges": rag_edges,
                "triples": rag_triples,
            }
        except Exception:
            logger.warning(
                "metagen_evidence_ontology_rag_failed", extra={"urn": urn}, exc_info=True
            )
            evidence["ontology_rag"] = {"nodes": [], "edges": [], "triples": []}

        return evidence

    def _enumerate_target_items(
        self,
        urn: str,
        boundary: MetagenBoundary,
        evidence: dict[str, Any],
        approved_item_ids: frozenset[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Yield target item dicts for the producer prompt.

        Skips items whose kind is not in boundary.allowed.
        Skips items that already have an approved candidate.
        """
        items: list[dict[str, Any]] = []
        allowed = set(boundary.allowed)

        if "dataset.description" in allowed:
            item_id = "dataset.description"
            if (urn, item_id) not in approved_item_ids:
                items.append(
                    {
                        "dataset_urn": urn,
                        "item_id": item_id,
                        "kind": "dataset.description",
                        "field_path": None,
                    }
                )

        if "column.description" in allowed:
            for field in evidence.get("schemaMetadata", {}).get("fields", []):
                field_path = field.get("fieldPath", "")
                if not field_path:
                    continue
                item_id = f"column.{field_path}.description"
                if (urn, item_id) not in approved_item_ids:
                    items.append(
                        {
                            "dataset_urn": urn,
                            "item_id": item_id,
                            "kind": "column.description",
                            "field_path": field_path,
                        }
                    )

        return items

    async def _apply_per_item_budget(
        self,
        urn: str,
        item_id: str,
        new_candidate_value: str,
        new_candidate_confidence: float,
        new_candidate_evidence: dict[str, Any],
        run_id: uuid.UUID,
        conf_id: uuid.UUID,
        conf: MetagenConfDTO,
    ) -> tuple[bool, bool]:
        """Ensure the per-(conf, item) budget and persist the new candidate.

        Returns (added, evicted). The budget counts and eviction are scoped to
        this conf's candidates on the item; other confs' candidates are untouched.

        When budget is full and overwrite_pending=true: evict this conf's oldest
        llm_approved, then add. When full and overwrite_pending=false: skip.
        """
        # Materialise the item row (upsert)
        item_result = await self._db.execute(
            select(MetagenItem).where(
                MetagenItem.dataset_urn == urn, MetagenItem.item_id == item_id
            )
        )
        item_row = item_result.scalar_one_or_none()
        if item_row is None:
            kind = (
                "dataset.description" if item_id == "dataset.description" else "column.description"
            )
            field_path: str | None = None
            if kind == "column.description":
                field_path = item_id[len("column.") : -len(".description")]
            item_row = MetagenItem(
                dataset_urn=urn,
                item_id=item_id,
                kind=kind,
                field_path=field_path,
            )
            self._db.add(item_row)
            await self._db.flush()

        # Count this conf's non-rejected candidates on the item
        count_result = await self._db.execute(
            select(func.count()).where(
                MetagenCandidate.conf_id == conf_id,
                MetagenCandidate.dataset_urn == urn,
                MetagenCandidate.item_id == item_id,
                MetagenCandidate.status != "rejected",
            )
        )
        non_rejected_count = count_result.scalar() or 0

        evicted = False
        if non_rejected_count >= conf.result_limit:
            if not conf.overwrite_pending:
                return False, False
            # Evict this conf's oldest llm_approved
            oldest_result = await self._db.execute(
                select(MetagenCandidate)
                .where(
                    MetagenCandidate.conf_id == conf_id,
                    MetagenCandidate.dataset_urn == urn,
                    MetagenCandidate.item_id == item_id,
                    MetagenCandidate.status == "llm_approved",
                )
                .order_by(MetagenCandidate.created_at.asc())
                .limit(1)
            )
            oldest = oldest_result.scalar_one_or_none()
            if oldest is None:
                # No llm_approved to evict (all are approved); skip
                return False, False
            await self._db.delete(oldest)
            await self._db.flush()
            evicted = True

        new_cand = MetagenCandidate(
            candidate_id=uuid.uuid4(),
            conf_id=conf_id,
            dataset_urn=urn,
            item_id=item_id,
            run_id=run_id,
            value=new_candidate_value,
            confidence_score=new_candidate_confidence,
            status="llm_approved",
            evidence=new_candidate_evidence,
        )
        self._db.add(new_cand)
        await self._db.commit()
        await self._db.refresh(new_cand)

        # Refresh embedding for the new candidate (best-effort)
        await self._refresh_candidate_embedding(new_cand)

        return True, evicted

    async def _emit_to_datahub(self, urn: str, item_id: str, value: str) -> None:
        """Write the approved value to the appropriate DataHub editable aspect.

        dataset.description → editableDatasetProperties.description
        column.<fieldPath>.description → editableSchemaMetadata[fieldPath].description
        """
        try:
            if item_id == "dataset.description":
                from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

                await self._datahub.emit_aspect(
                    urn, EditableDatasetPropertiesClass(description=value)
                )

            elif item_id.startswith("column.") and item_id.endswith(".description"):
                field_path = item_id[len("column.") : -len(".description")]
                from datahub.metadata.schema_classes import (
                    EditableSchemaFieldInfoClass,
                    EditableSchemaMetadataClass,
                )

                # Fetch existing to merge (preserve other field edits)
                existing = await self._datahub.get_aspect(urn, EditableSchemaMetadataClass)
                if existing and hasattr(existing, "editableSchemaFieldInfo"):
                    field_infos = list(existing.editableSchemaFieldInfo)
                    for fi in field_infos:
                        if getattr(fi, "fieldPath", None) == field_path:
                            fi.description = value
                            break
                    else:
                        field_infos.append(
                            EditableSchemaFieldInfoClass(fieldPath=field_path, description=value)
                        )
                else:
                    field_infos = [
                        EditableSchemaFieldInfoClass(fieldPath=field_path, description=value)
                    ]
                await self._datahub.emit_aspect(
                    urn, EditableSchemaMetadataClass(editableSchemaFieldInfo=field_infos)
                )
        except Exception:
            logger.warning(
                "metagen_datahub_emit_failed",
                extra={"urn": urn, "item_id": item_id},
                exc_info=True,
            )

    async def _refresh_candidate_embedding(self, cand: MetagenCandidate) -> None:
        """Embed the candidate value and upsert metagen_candidate_embeddings (best-effort)."""
        try:
            kind = (
                "dataset.description"
                if cand.item_id == "dataset.description"
                else "column.description"
            )
            vec = await self._llm.embed(cand.value)
            await _upsert_candidate_embedding(self._vector, str(cand.candidate_id), kind, vec)
        except Exception:
            logger.warning(
                "metagen_candidate_embedding_upsert_failed",
                extra={"candidate_id": str(cand.candidate_id)},
                exc_info=True,
            )

    async def _build_item_summary(
        self, row: MetagenItem, status_filter: str | None = None
    ) -> ItemSummaryDTO | None:
        """Build an ItemSummaryDTO from a MetagenItem row."""
        q = select(MetagenCandidate).where(
            MetagenCandidate.dataset_urn == row.dataset_urn,
            MetagenCandidate.item_id == row.item_id,
        )
        if status_filter is not None:
            q = q.where(MetagenCandidate.status == status_filter)

        cands = (await self._db.execute(q)).scalars().all()

        has_approved = any(c.status == "approved" for c in cands)
        non_rejected_count = sum(1 for c in cands if c.status != "rejected")

        if status_filter is not None and not cands:
            return None

        return ItemSummaryDTO(
            dataset_urn=row.dataset_urn,
            item_id=row.item_id,
            kind=row.kind,
            field_path=row.field_path,
            candidate_count=len(cands),
            non_rejected_count=non_rejected_count,
            has_approved=has_approved,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _conf_name_map(self, conf_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Resolve a set of conf ids to their names (absent ids omitted)."""
        if not conf_ids:
            return {}
        result = await self._db.execute(
            select(MetagenConfig.id, MetagenConfig.name).where(MetagenConfig.id.in_(conf_ids))
        )
        return {r.id: r.name for r in result.all()}

    async def _build_item_detail(self, row: MetagenItem) -> ItemDetailDTO:
        """Build an ItemDetailDTO from a MetagenItem row (includes candidates)."""
        cands_result = await self._db.execute(
            select(MetagenCandidate)
            .where(
                MetagenCandidate.dataset_urn == row.dataset_urn,
                MetagenCandidate.item_id == row.item_id,
            )
            .order_by(MetagenCandidate.created_at.desc())
        )
        cand_rows = cands_result.scalars().all()
        name_map = await self._conf_name_map(
            {c.conf_id for c in cand_rows if c.conf_id is not None}
        )
        candidates = [
            _candidate_to_dto(c, name_map.get(c.conf_id) if c.conf_id else None) for c in cand_rows
        ]
        return ItemDetailDTO(
            dataset_urn=row.dataset_urn,
            item_id=row.item_id,
            kind=row.kind,
            field_path=row.field_path,
            created_at=row.created_at,
            updated_at=row.updated_at,
            candidates=candidates,
        )

    async def _record_metagen_event(
        self,
        entity_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = _event_row("metagen", entity_id, event_type, status, detail)
        self._db.add(event)
        await self._db.commit()

    async def _record_dataset_event(
        self,
        dataset_urn: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = _event_row("dataset", dataset_urn, event_type, status, detail)
        self._db.add(event)
        await self._db.commit()


# ── pgvector helper for metagen_candidate_embeddings ─────────────────────────


async def _upsert_candidate_embedding(
    vector: PgVectorManager,
    candidate_id: str,
    kind: str,
    embedding: list[float],
) -> None:
    """Upsert a row in metagen_candidate_embeddings for *candidate_id*."""
    vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
    sql = text(
        """
        INSERT INTO dataspoke.metagen_candidate_embeddings (candidate_id, kind, embedding)
        VALUES (CAST(:candidate_id AS uuid), :kind, CAST(:embedding AS vector))
        ON CONFLICT (candidate_id) DO UPDATE SET
            kind      = EXCLUDED.kind,
            embedding = EXCLUDED.embedding
        """
    )
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                sql,
                {
                    "candidate_id": candidate_id,
                    "kind": kind,
                    "embedding": vector_literal,
                },
            )
