"""Metadata Generation service — UC4 LLM-powered documentation proposal pipeline.

Spec: spec/feature/BACKEND.md §Metadata Generation Service
      spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects
"""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metagen.debate import run_debate
from src.backend.metagen.debate_models import MetagenLLMOutput
from src.backend.metagen.prompts import build_run_prompt
from src.backend.metagen.reviewer import build_metagen_review_tool
from src.backend.metagen.validator import build_metagen_validate_tool
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.datahub.documents import fetch_related_documents
from src.shared.db.models import (
    DatasetNodeMap,
    Event,
    MetagenBoundary,
    MetagenCandidate,
    MetagenCandidateEmbedding,
    MetagenConfig,
    MetagenItem,
    OntogenNode,
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
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from src.shared.llm.client import LLMClient
from src.shared.settings import settings
from src.shared.vector.client import PgVectorManager

logger = logging.getLogger(__name__)

_LOCK_KEY = "metagen:running:singleton"
_LOCK_TTL_SECONDS = 3600

_DATASET_URN_RE = re.compile(r"^urn:li:dataset:\(.+\)$")
_VALID_SCHEDULE_TIERS: frozenset[str] = frozenset({"hourly", "daily", "weekly"})
_VALID_KINDS: frozenset[str] = frozenset({"dataset.description", "column.description"})


# ── Value objects ─────────────────────────────────────────────────────────────


class MetagenGlobalConfDTO(BaseModel):
    is_enabled: bool
    schedule_tier: str | None
    dataset_filter: dict[str, Any]
    result_limit: int
    overwrite_pending: bool
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
    status: str
    dry_run: bool
    unresolved_urns: list[str]
    counts: dict[str, int]
    debate_outcome: str | None = None
    producer_iterations: int | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_dataset_urn(urn: str) -> None:
    if not _DATASET_URN_RE.match(urn):
        raise InvalidDatasetUrnError(urn)


def _validate_dataset_filter(dataset_filter: dict[str, Any]) -> None:
    for urn in dataset_filter.get("dataset_urns", []) or []:
        _validate_dataset_urn(str(urn))


def _validate_schedule_tier(tier: str | None) -> None:
    if tier is not None and tier not in _VALID_SCHEDULE_TIERS:
        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            f"schedule_tier must be one of {sorted(_VALID_SCHEDULE_TIERS)} or null, got {tier!r}",
        )


def _conf_to_dto(row: MetagenConfig) -> MetagenGlobalConfDTO:
    return MetagenGlobalConfDTO(
        is_enabled=row.is_enabled,
        schedule_tier=row.schedule_tier,
        dataset_filter=dict(row.dataset_filter) if row.dataset_filter else {},
        result_limit=row.result_limit,
        overwrite_pending=row.overwrite_pending,
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


def _candidate_to_dto(row: MetagenCandidate) -> CandidateDTO:
    return CandidateDTO(
        candidate_id=str(row.candidate_id),
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
    """Global singleton metadata generation service.

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

    # ── Singleton conf CRUD ───────────────────────────────────────────────────

    async def get_global_conf(self) -> MetagenGlobalConfDTO | None:
        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == 1))
        row = result.scalar_one_or_none()
        return _conf_to_dto(row) if row else None

    async def put_global_conf(self, conf: dict[str, Any]) -> MetagenGlobalConfDTO:
        """Full replacement of the singleton conf. Emits METAGEN.CONFIG_CREATE or UPDATE."""
        dataset_filter = conf.get("dataset_filter", {}) or {}
        _validate_dataset_filter(dataset_filter)
        _validate_schedule_tier(conf.get("schedule_tier"))

        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == 1))
        existing = result.scalar_one_or_none()
        created = existing is None

        if existing is None:
            existing = MetagenConfig(id=1)
            self._db.add(existing)

        existing.is_enabled = conf.get("is_enabled", False)
        existing.schedule_tier = conf.get("schedule_tier")
        existing.dataset_filter = dataset_filter
        existing.result_limit = conf.get("result_limit", 3)
        existing.overwrite_pending = conf.get("overwrite_pending", True)
        existing.updated_at = datetime.now(tz=UTC)

        await self._db.commit()
        await self._db.refresh(existing)

        event_type = METAGEN_CONFIG_CREATE if created else METAGEN_CONFIG_UPDATE
        await self._record_metagen_event(
            "singleton",
            event_type,
            "success",
            {"operation": "PUT"},
        )
        return _conf_to_dto(existing)

    async def patch_global_conf(self, partial: dict[str, Any]) -> MetagenGlobalConfDTO:
        """Partial update of the singleton conf. Emits METAGEN.CONFIG_UPDATE."""
        if "dataset_filter" in partial and partial["dataset_filter"] is not None:
            _validate_dataset_filter(partial["dataset_filter"])
        if "schedule_tier" in partial:
            _validate_schedule_tier(partial["schedule_tier"])

        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_conf", "singleton")

        for field_name in ("is_enabled", "schedule_tier", "dataset_filter", "result_limit", "overwrite_pending"):
            if field_name in partial and partial[field_name] is not None:
                setattr(row, field_name, partial[field_name])
            elif field_name == "schedule_tier" and field_name in partial and partial[field_name] is None:
                row.schedule_tier = None

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_metagen_event(
            "singleton",
            METAGEN_CONFIG_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(partial.keys())},
        )
        return _conf_to_dto(row)

    async def delete_global_conf(self) -> None:
        """Delete the singleton conf. Emits METAGEN.CONFIG_DELETE."""
        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is not None:
            await self._db.delete(row)
            await self._db.commit()

        await self._record_metagen_event(
            "singleton",
            METAGEN_CONFIG_DELETE,
            "success",
            {"operation": "DELETE"},
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
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ItemSummaryDTO], int]:
        base = select(MetagenItem)
        if dataset_urn is not None:
            base = base.where(MetagenItem.dataset_urn == dataset_urn)
        if kind is not None:
            base = base.where(MetagenItem.kind == kind)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(MetagenItem.updated_at.desc()).offset(offset).limit(limit)
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
        self, urn: str, *, offset: int = 0, limit: int = 20
    ) -> tuple[list[ItemSummaryDTO], int]:
        return await self.list_items(dataset_urn=urn, offset=offset, limit=limit)

    async def get_item_for_dataset(self, urn: str, item_id: str) -> ItemDetailDTO:
        return await self.get_item(urn, item_id)

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        *,
        tier: str | None = None,
        dataset_urns: list[str] | None = None,
        dry_run: bool = False,
    ) -> RunResultDTO:
        """Global metagen inference pipeline, serialised by Redis lock.

        - Raises ConflictError(METAGEN_RUNNING) if already running.
        - Raises ConflictError(METAGEN_DISABLED) if conf.is_enabled=false and not dry_run.
        - Returns early (no-op success) when tier is provided but conf.schedule_tier != tier.
        """
        run_id = str(uuid.uuid4())
        lock_token = secrets.token_urlsafe(16)
        acquired = await self._cache.set_nx(_LOCK_KEY, lock_token, ttl_seconds=_LOCK_TTL_SECONDS)
        if not acquired:
            raise ConflictError("METAGEN_RUNNING", "Metagen inference is already running")

        try:
            return await self._run_inner(
                tier=tier, dataset_urns=dataset_urns, dry_run=dry_run, run_id=run_id
            )
        except ConflictError:
            raise
        except Exception as exc:
            logger.error("metagen_run_failed", exc_info=True)
            try:
                await self._record_metagen_event(
                    "singleton",
                    METAGEN_RUN_FAILED,
                    "failure",
                    {"error": str(exc), "run_id": run_id},
                )
            except Exception:
                logger.warning("metagen_run_failed_event_emit_failed", exc_info=True)
            raise
        finally:
            await self._cache.delete_if_value(_LOCK_KEY, lock_token)

    async def _run_inner(
        self,
        *,
        tier: str | None,
        dataset_urns: list[str] | None,
        dry_run: bool,
        run_id: str,
    ) -> RunResultDTO:
        conf = await self._load_conf_or_default()

        # Tier short-circuit: DAG-triggered runs check schedule_tier before doing work.
        # This runs before the enabled check so disabled+wrong-tier runs silently no-op.
        if tier is not None and conf.schedule_tier != tier:
            return RunResultDTO(
                run_id=run_id,
                status="skipped",
                dry_run=dry_run,
                unresolved_urns=[],
                counts={"items_considered": 0},
            )

        if not conf.is_enabled and not dry_run:
            raise ConflictError("METAGEN_DISABLED", "Metagen is disabled; only dry-run is permitted")

        # Step 1: Enumerate in-scope datasets
        in_scope_urns, unresolved_urns = await self._enumerate_in_scope_datasets(
            conf, dataset_urns
        )

        run_uuid = uuid.UUID(run_id)
        items_considered = 0
        candidates_added = 0
        candidates_evicted = 0
        rejected_cleared = 0
        debate_outcome: str | None = None
        producer_iterations: int | None = None

        # Step 2: Clear rejected candidates across in-scope datasets
        if not dry_run:
            rejected_cleared = await self._clear_rejected_candidates(in_scope_urns)

        for urn in in_scope_urns:
            # Step 3: Fetch evidence
            evidence = await self._fetch_evidence(urn)

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
                max_turns=settings.metagen_debate_max_turns,
                rag_k=settings.metagen_debate_rag_k,
                reviewer_model=settings.metagen_debate_reviewer_model,
                producer_schema=MetagenLLMOutput,
                producer_max_iterations=settings.metagen_llm_max_iterations,
                run_id=run_id,
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
                c for c in candidate_output.candidates
                if c.confidence_score >= settings.metagen_confidence_threshold
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
            "singleton",
            METAGEN_RUN_COMPLETE,
            "success",
            {
                "run_id": run_id,
                "unresolved_urns": unresolved_urns,
                "counts": counts,
                "dry_run": dry_run,
                "producer_iterations": producer_iterations,
                "debate_outcome": debate_outcome,
            },
        )

        return RunResultDTO(
            run_id=run_id,
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
                f"Dataset {dataset_urn!r} has no active boundary — add one via PUT .../attr/metagen/conf",
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
                    "Cannot reject an approved candidate — approve a different sibling to demote it",
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

        return _candidate_to_dto(cand)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _load_conf_or_default(self) -> MetagenGlobalConfDTO:
        """Return the singleton conf or a disabled-default if not yet created."""
        result = await self._db.execute(select(MetagenConfig).where(MetagenConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = MetagenConfig(
                id=1,
                is_enabled=False,
                dataset_filter={},
                result_limit=3,
                overwrite_pending=True,
            )
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
        return _conf_to_dto(row)

    async def _enumerate_in_scope_datasets(
        self,
        conf: MetagenGlobalConfDTO,
        override_urns: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """Intersect DataHub dataset_filter with metagen_boundary.is_enabled=true rows.

        Returns (in_scope_urns, unresolved_urns).
        """
        dataset_filter = conf.dataset_filter or {}
        tags: list[str] = dataset_filter.get("tags") or []
        glossary_terms: list[str] = dataset_filter.get("glossary_terms") or []
        explicit_urns: list[str] = override_urns or dataset_filter.get("dataset_urns") or []

        # Resolve from DataHub
        if not tags and not glossary_terms and not explicit_urns:
            try:
                all_urns = await self._datahub.enumerate_datasets()
                datahub_urn_set: set[str] = set(all_urns)
            except Exception:
                logger.warning("metagen_enumerate_all_datasets_failed", exc_info=True)
                datahub_urn_set = set()
        else:
            datahub_urn_set = set()
            try:
                if tags or glossary_terms:
                    matched = await self._datahub.enumerate_datasets(
                        tags=tags if tags else None,
                        glossary_terms=glossary_terms if glossary_terms else None,
                    )
                    datahub_urn_set.update(matched)
            except Exception:
                logger.warning("metagen_enumerate_filtered_datasets_failed", exc_info=True)

        unresolved: list[str] = []
        for urn in explicit_urns:
            try:
                from datahub.metadata.schema_classes import DatasetPropertiesClass

                props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
                if props is not None:
                    datahub_urn_set.add(urn)
                else:
                    unresolved.append(urn)
            except Exception:
                logger.warning(
                    "metagen_explicit_urn_check_failed", extra={"urn": urn}, exc_info=True
                )
                unresolved.append(urn)

        if not datahub_urn_set:
            return [], unresolved

        # Intersect with boundary rows where is_enabled=true
        bnd_result = await self._db.execute(
            select(MetagenBoundary.dataset_urn).where(MetagenBoundary.is_enabled.is_(True))
        )
        enabled_boundary_urns: set[str] = {r.dataset_urn for r in bnd_result.fetchall()}

        in_scope = sorted(datahub_urn_set & enabled_boundary_urns)
        return in_scope, unresolved

    async def _clear_rejected_candidates(self, in_scope_urns: list[str]) -> int:
        """Delete rejected candidates across in-scope datasets. Returns deleted row count.

        Embeddings rows in `metagen_candidate_embeddings` carry a FK to
        `metagen_candidates.candidate_id`; delete them first so the candidate
        delete does not raise ForeignKeyViolationError.
        """
        from sqlalchemy.engine import CursorResult

        await self._db.execute(
            delete(MetagenCandidateEmbedding).where(
                MetagenCandidateEmbedding.candidate_id.in_(
                    select(MetagenCandidate.candidate_id).where(
                        MetagenCandidate.dataset_urn.in_(in_scope_urns),
                        MetagenCandidate.status == "rejected",
                    )
                )
            )
        )
        raw = await self._db.execute(
            delete(MetagenCandidate).where(
                MetagenCandidate.dataset_urn.in_(in_scope_urns),
                MetagenCandidate.status == "rejected",
            )
        )
        await self._db.commit()
        return (raw.rowcount or 0) if isinstance(raw, CursorResult) else 0

    async def _fetch_evidence(self, urn: str) -> dict[str, Any]:
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
                evidence["glossaryTerms"] = [
                    getattr(t, "urn", str(t)) for t in glossary.terms
                ]
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
                        {"id": n.id, "name": n.name, "description": n.description}
                        for n in nodes
                    ]
                }
        except Exception:
            logger.warning(
                "metagen_ontology_context_fetch_failed", extra={"urn": urn}, exc_info=True
            )

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
        conf: MetagenGlobalConfDTO,
    ) -> tuple[bool, bool]:
        """Ensure the per-item budget and persist the new candidate.

        Returns (added, evicted).

        When budget is full and overwrite_pending=true: evict oldest llm_approved,
        then add. When budget is full and overwrite_pending=false: skip (no add, no evict).
        """
        # Materialise the item row (upsert)
        item_result = await self._db.execute(
            select(MetagenItem).where(
                MetagenItem.dataset_urn == urn, MetagenItem.item_id == item_id
            )
        )
        item_row = item_result.scalar_one_or_none()
        if item_row is None:
            kind = "dataset.description" if item_id == "dataset.description" else "column.description"
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

        # Count non-rejected candidates
        count_result = await self._db.execute(
            select(func.count()).where(
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
            # Evict oldest llm_approved
            oldest_result = await self._db.execute(
                select(MetagenCandidate)
                .where(
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
                existing = await self._datahub.get_aspect(
                    urn, EditableSchemaMetadataClass
                )
                if existing and hasattr(existing, "editableSchemaFieldInfo"):
                    field_infos = list(existing.editableSchemaFieldInfo)
                    for fi in field_infos:
                        if getattr(fi, "fieldPath", None) == field_path:
                            fi.description = value
                            break
                    else:
                        field_infos.append(
                            EditableSchemaFieldInfoClass(
                                fieldPath=field_path, description=value
                            )
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
            kind = "dataset.description" if cand.item_id == "dataset.description" else "column.description"
            vec = await self._llm.embed(cand.value)
            await _upsert_candidate_embedding(
                self._vector, str(cand.candidate_id), kind, vec
            )
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

        if status_filter is not None and not cands:
            return None

        return ItemSummaryDTO(
            dataset_urn=row.dataset_urn,
            item_id=row.item_id,
            kind=row.kind,
            field_path=row.field_path,
            candidate_count=len(cands),
            has_approved=has_approved,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

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
        candidates = [_candidate_to_dto(c) for c in cands_result.scalars().all()]
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
    from sqlalchemy import text

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
