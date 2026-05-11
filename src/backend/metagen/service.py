"""Metadata Generation service — UC4 LLM-powered documentation proposal pipeline.

Spec: spec/feature/BACKEND.md §Metadata Generation Service
      spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects
          §Document Aspects
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.metagen import CrossDataAction
from src.backend.metagen.cross_data import apply_actions, fetch_related_documents
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DatasetNodeMap,
    Event,
    MetagenConfig,
    MetagenResult,
    OntogenEdge,
    OntogenNode,
    OntogenTriple,
)
from src.shared.events import (
    METAGEN_APPROVE,
    METAGEN_COMPLETE,
    METAGEN_CONFIG_CREATE,
    METAGEN_CONFIG_DELETE,
    METAGEN_CONFIG_UPDATE,
    METAGEN_PREFIX,
    METAGEN_REJECT,
)
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError
from src.shared.llm.client import LLMClient

logger = logging.getLogger(__name__)

# Valid target values per spec/feature/BACKEND.md §Metadata Generation Service
_VALID_TARGETS = frozenset({"dataset.description", "column.description", "cross_data.md"})
_VALID_SCHEDULE_TIERS = frozenset({"hourly", "daily", "weekly"})
_VALID_VERDICTS = frozenset({"approve", "reject"})

# ── Input-validation helpers ──────────────────────────────────────────────────


def _validate_targets(targets: list[str]) -> None:
    """Raise PreconditionFailedError for any unknown target value (Fix #9)."""
    for t in targets:
        if t not in _VALID_TARGETS:
            raise PreconditionFailedError(
                "INVALID_PARAMETER",
                f"Unknown target {t!r}. Valid values: {sorted(_VALID_TARGETS)}",
            )


def _validate_metagen_schedule_tier(tier: str | None) -> None:
    """Raise PreconditionFailedError for unknown schedule_tier values (Fix #9)."""
    if tier is not None and tier not in _VALID_SCHEDULE_TIERS:
        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            f"schedule_tier must be one of {sorted(_VALID_SCHEDULE_TIERS)} or null, got {tier!r}",
        )


def _validate_metagen_verdict(verdict: str) -> None:
    """Raise PreconditionFailedError for unknown verdict values (Fix #9)."""
    if verdict not in _VALID_VERDICTS:
        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {verdict!r}",
        )


# ── Value objects ─────────────────────────────────────────────────────────────


class MetagenConfigRecord(BaseModel):
    """Value object mirroring ORM MetagenConfig."""

    id: str
    dataset_urn: str
    targets: list[str]
    code_refs: dict[str, Any] | None = None
    is_enabled: bool
    schedule_tier: str | None = None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class MetagenResultRecord(BaseModel):
    """Value object mirroring ORM MetagenResult."""

    id: str
    dataset_urn: str
    proposals: dict[str, Any]
    field_status: dict[str, Any]
    run_id: str
    generated_at: datetime
    last_reviewed_at: datetime | None = None


# ── ORM converters ────────────────────────────────────────────────────────────


def _config_from_row(row: MetagenConfig) -> MetagenConfigRecord:
    return MetagenConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        targets=list(row.targets),
        code_refs=row.code_refs,
        is_enabled=row.is_enabled,
        schedule_tier=row.schedule_tier,
        status=row.status,
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: MetagenResult) -> MetagenResultRecord:
    return MetagenResultRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        proposals=dict(row.proposals),
        field_status=dict(row.field_status),
        run_id=str(row.run_id),
        generated_at=row.generated_at,
        last_reviewed_at=row.last_reviewed_at,
    )


# ── Service ───────────────────────────────────────────────────────────────────


_METAGEN_LOCK_TTL = 3600  # 1 hour


class MetagenService:
    """Per-dataset metadata generation config CRUD, run pipeline, and review flow.

    Constructor-injected (stateless service pattern per BACKEND.md §Service Pattern):
    - datahub: DataHubClient
    - db: AsyncSession
    - cache: RedisClient (optional; used for SETNX concurrency guard on run())
    - llm: LLMClient
    """

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        llm: LLMClient,
        cache: RedisClient | None = None,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._llm = llm
        self._cache = cache

    # ── Config CRUD ───────────────────────────────────────────────────────────

    async def get_config(self, dataset_urn: str) -> MetagenConfigRecord | None:
        result = await self._db.execute(
            select(MetagenConfig).where(MetagenConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        return _config_from_row(row) if row else None

    async def upsert_config(
        self,
        dataset_urn: str,
        targets: list[str],
        code_refs: dict[str, Any] | None,
        is_enabled: bool,
        schedule_tier: str | None,
        owner: str,
    ) -> tuple[MetagenConfigRecord, bool]:
        """Create or replace a metagen config.  Emits METAGEN.CONFIG_CREATE/UPDATE."""
        # Fix #9: validate enum fields before touching the DB
        _validate_targets(targets)
        _validate_metagen_schedule_tier(schedule_tier)

        result = await self._db.execute(
            select(MetagenConfig).where(MetagenConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.targets = targets
            existing.code_refs = code_refs
            existing.is_enabled = is_enabled
            existing.schedule_tier = schedule_tier
            existing.owner = owner
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = MetagenConfig(
                dataset_urn=dataset_urn,
                targets=targets,
                code_refs=code_refs,
                is_enabled=is_enabled,
                schedule_tier=schedule_tier,
                owner=owner,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)

        event_type = METAGEN_CONFIG_CREATE if created else METAGEN_CONFIG_UPDATE
        await self._record_event(
            dataset_urn,
            event_type,
            "success",
            {
                "operation": "PUT",
                "config_id": str(existing.id),
                "targets": targets,
                "is_enabled": is_enabled,
            },
        )
        return _config_from_row(existing), created

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> MetagenConfigRecord:
        """Partial config update.  Emits METAGEN.CONFIG_UPDATE."""
        # Fix #9: validate enum fields if present in patch
        if "targets" in patch and patch["targets"] is not None:
            _validate_targets(patch["targets"])
        if "schedule_tier" in patch:
            _validate_metagen_schedule_tier(patch["schedule_tier"])

        result = await self._db.execute(
            select(MetagenConfig).where(MetagenConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        for field_name in ("targets", "code_refs", "schedule_tier", "owner"):
            if field_name in patch and patch[field_name] is not None:
                setattr(row, field_name, patch[field_name])
        if "is_enabled" in patch and patch["is_enabled"] is not None:
            row.is_enabled = patch["is_enabled"]

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_event(
            dataset_urn,
            METAGEN_CONFIG_UPDATE,
            "success",
            {
                "operation": "PATCH",
                "config_id": str(row.id),
                "fields_changed": list(patch.keys()),
            },
        )
        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        """Delete a metagen config.  Emits METAGEN.CONFIG_DELETE."""
        result = await self._db.execute(
            select(MetagenConfig).where(MetagenConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("config", dataset_urn)

        config_id = str(row.id)
        await self._db.delete(row)
        await self._db.commit()

        await self._record_event(
            dataset_urn,
            METAGEN_CONFIG_DELETE,
            "success",
            {"operation": "DELETE", "config_id": config_id},
        )

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        is_enabled_filter: bool | None = None,
        order_by: Any = None,
    ) -> tuple[list[MetagenConfigRecord], int]:
        base = select(MetagenConfig)
        if is_enabled_filter is not None:
            base = base.where(MetagenConfig.is_enabled == is_enabled_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetagenConfig.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return [_config_from_row(r) for r in rows], total

    async def list_active_for_tier(self, tier: str) -> list[MetagenConfigRecord]:
        """Return all is_enabled=True configs matching the given schedule_tier."""
        result = await self._db.execute(
            select(MetagenConfig).where(
                MetagenConfig.is_enabled.is_(True),
                MetagenConfig.schedule_tier == tier,
            )
        )
        return [_config_from_row(r) for r in result.scalars().all()]

    # ── Results ───────────────────────────────────────────────────────────────

    async def list_results(
        self,
        dataset_urn: str,
        latest: bool = False,
        approved: bool | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[MetagenResultRecord], int]:
        base = select(MetagenResult).where(MetagenResult.dataset_urn == dataset_urn)

        if from_dt is not None:
            base = base.where(MetagenResult.generated_at >= from_dt)
        if to_dt is not None:
            base = base.where(MetagenResult.generated_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = MetagenResult.generated_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(1 if latest else limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return [_result_from_row(r) for r in rows], total

    async def get_result(self, result_id: str) -> MetagenResultRecord:
        try:
            result_uuid = uuid.UUID(result_id)
        except ValueError:
            raise EntityNotFoundError("metagen_result", result_id)
        result = await self._db.execute(
            select(MetagenResult).where(MetagenResult.id == result_uuid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_result", result_id)
        return _result_from_row(row)

    async def list_metagen(
        self,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[MetagenResultRecord], int]:
        """Cross-dataset list view — one row per dataset (latest result).

        Uses PostgreSQL DISTINCT ON to return only the most recent
        MetagenResult per dataset_urn.  total_count reflects the number
        of distinct datasets, not the total result count.
        """
        # DISTINCT ON (dataset_urn) selects the first row per group when
        # ordered by (dataset_urn, generated_at desc) — i.e. the latest.
        dedup_q = (
            select(MetagenResult)
            .order_by(
                MetagenResult.dataset_urn,
                MetagenResult.generated_at.desc(),
            )
            .distinct(MetagenResult.dataset_urn)
        )
        # Count distinct datasets by wrapping the dedup query as a subquery
        count_q = select(func.count()).select_from(dedup_q.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        paginated_q = dedup_q.offset(offset).limit(limit)
        rows = (await self._db.execute(paginated_q)).scalars().all()
        return [_result_from_row(r) for r in rows], total

    # ── Run pipeline ──────────────────────────────────────────────────────────

    async def run(
        self,
        dataset_urn: str,
        *,
        dry_run: bool = False,
        run_id: str | None = None,
    ) -> MetagenResultRecord:
        """Generate metadata proposals for *dataset_urn*.

        Pipeline:
        1. Acquire Redis SETNX guard (if cache is available).
        2. Load metagen config; raise EntityNotFoundError if absent.
        3. Gather DataHub evidence (unified six-aspect proofread boundary).
        4. Resolve node membership from dataset_node_map (UC3 integration).
        5. Build LLM prompt per target; call LLM.
        6. For cross_data.md: inspect existing related document entities.
        7. Persist MetagenResult with all fields in ``pending`` status.
        8. Emit METAGEN.COMPLETE.

        Raises ConflictError("GENERATION_RUNNING") when a concurrent run is
        already in progress for the same dataset_urn.
        """
        # Step 1: Redis SETNX concurrency guard (CAS token prevents cross-worker deletion)
        lock_key = f"metagen:running:{dataset_urn}"
        lock_token: str | None = None
        if self._cache is not None:
            lock_token = secrets.token_urlsafe(16)
            acquired = await self._cache.set_nx(lock_key, lock_token, ttl_seconds=_METAGEN_LOCK_TTL)
            if not acquired:
                raise ConflictError(
                    "GENERATION_RUNNING",
                    f"Metadata generation is already running for {dataset_urn}",
                )

        try:
            return await self._run_inner(dataset_urn, dry_run=dry_run, run_id=run_id)
        finally:
            if self._cache is not None and lock_token is not None:
                await self._cache.delete_if_value(lock_key, lock_token)

    async def _run_inner(
        self,
        dataset_urn: str,
        *,
        dry_run: bool = False,
        run_id: str | None = None,
    ) -> MetagenResultRecord:
        """Inner run logic (called inside the SETNX guard)."""
        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("config", dataset_urn)

        if not config.is_enabled and not dry_run:
            raise ConflictError(
                "GENERATION_DISABLED",
                f"Metadata generation is disabled for {dataset_urn}; only dry-run is permitted",
            )

        run_id_str = run_id or str(uuid.uuid4())

        # Step 2: Gather evidence
        evidence = await self._gather_evidence(dataset_urn, config.targets)

        # Step 3: Node membership (UC3 integration)
        node_map = (
            (
                await self._db.execute(
                    select(DatasetNodeMap).where(
                        DatasetNodeMap.dataset_urn == dataset_urn,
                        DatasetNodeMap.status == "approved",
                    )
                )
            )
            .scalars()
            .all()
        )
        approved_node_ids = [m.node_id for m in node_map]
        evidence["ontogen_node_ids"] = approved_node_ids

        # Step 3b: Fetch approved triples touching these nodes (UC3 → UC4 integration).
        # Spec: spec/feature/BACKEND.md §Generation Pipeline; spec/USE_CASE_en.md §UC4 Inputs.
        if approved_node_ids:
            triples_result = await self._db.execute(
                select(OntogenTriple, OntogenEdge)
                .join(OntogenEdge, OntogenTriple.edge_id == OntogenEdge.id)
                .join(
                    OntogenNode,
                    or_(
                        OntogenTriple.subject_node_id == OntogenNode.id,
                        OntogenTriple.object_node_id == OntogenNode.id,
                    ),
                )
                .where(
                    OntogenTriple.status == "approved",
                    OntogenEdge.status == "approved",
                    OntogenNode.status == "approved",
                    or_(
                        OntogenTriple.subject_node_id.in_(approved_node_ids),
                        OntogenTriple.object_node_id.in_(approved_node_ids),
                    ),
                )
                .distinct()
            )
            evidence["ontogen_triples"] = [
                {
                    "subject_node_id": triple.subject_node_id,
                    "edge_id": triple.edge_id,
                    "edge_label": edge.label,
                    "object_node_id": triple.object_node_id,
                }
                for triple, edge in triples_result.all()
            ]
        else:
            evidence["ontogen_triples"] = []

        # Step 4+5: Call LLM per target
        proposals: dict[str, Any] = {}
        for target in config.targets:
            try:
                proposals[target] = await self._propose_target(target, dataset_urn, evidence)
            except Exception:
                logger.warning(
                    "metagen_proposal_failed",
                    extra={"target": target, "dataset_urn": dataset_urn},
                    exc_info=True,
                )
                proposals[target] = None

        if dry_run:
            # Emit METAGEN.COMPLETE before returning — dry-run is recorded same as real run
            await self._record_event(
                dataset_urn,
                METAGEN_COMPLETE,
                "success",
                {
                    "run_id": run_id_str,
                    "result_id": None,
                    "targets": config.targets,
                    "dry_run": True,
                },
            )
            return MetagenResultRecord(
                id=run_id_str,
                dataset_urn=dataset_urn,
                proposals=proposals,
                field_status={k: "pending" for k in proposals},
                run_id=run_id_str,
                generated_at=datetime.now(tz=UTC),
            )

        # Step 6: Build field_status — all fields start as pending
        field_status = _build_initial_field_status(proposals)

        # Step 6: Persist
        result_row = MetagenResult(
            dataset_urn=dataset_urn,
            proposals=proposals,
            field_status=field_status,
            run_id=uuid.UUID(run_id_str),
            generated_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()
        await self._db.refresh(result_row)

        # Step 7: Emit METAGEN.COMPLETE
        await self._record_event(
            dataset_urn,
            METAGEN_COMPLETE,
            "success",
            {
                "run_id": run_id_str,
                "result_id": str(result_row.id),
                "targets": config.targets,
                "dry_run": False,
            },
        )
        return _result_from_row(result_row)

    # ── Review ────────────────────────────────────────────────────────────────

    async def review_result(
        self,
        result_id: str,
        verdict: str,
        fields: list[str] | None = None,
        reason: str | None = None,
    ) -> MetagenResultRecord:
        """Apply a review verdict to a MetagenResult.

        ``verdict: "approve"`` + ``fields=None`` → approve all.
        ``verdict: "approve"`` + ``fields=[...]`` → approve only listed.
        ``verdict: "reject"`` + ``fields=None`` → reject all.
        ``verdict: "reject"`` + ``fields=[...]`` → reject only listed.

        On approval, writes to editable DataHub aspects via ``emit_mcp``
        (one emit per affected entity).  For cross_data.md actions, calls
        ``apply_actions``.

        Emits METAGEN.APPROVE or METAGEN.REJECT.
        """
        # Fix #9: validate verdict before any DB work
        _validate_metagen_verdict(verdict)

        try:
            result_uuid = uuid.UUID(result_id)
        except ValueError:
            raise EntityNotFoundError("metagen_result", result_id)
        result = await self._db.execute(
            select(MetagenResult).where(MetagenResult.id == result_uuid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("metagen_result", result_id)

        proposals: dict[str, Any] = dict(row.proposals)
        field_status: dict[str, Any] = dict(row.field_status)

        # Determine which fields to update
        if fields is None:
            # All fields
            target_fields = list(field_status.keys())
        else:
            target_fields = fields

        new_status = "approved" if verdict == "approve" else "rejected"
        for field_path in target_fields:
            if field_path in field_status:
                field_status[field_path] = new_status

        row.field_status = field_status
        row.last_reviewed_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        # On approval: write to DataHub
        if verdict == "approve":
            approved_fields = [f for f in target_fields if field_status.get(f) == "approved"]
            await self._apply_approved_fields(row.dataset_urn, proposals, approved_fields)

        event_type = METAGEN_APPROVE if verdict == "approve" else METAGEN_REJECT
        await self._record_event(
            row.dataset_urn,
            event_type,
            "success",
            {
                "result_id": result_id,
                "verdict": verdict,
                "fields": target_fields,
                "reason": reason,
            },
        )
        return _result_from_row(row)

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
            Event.event_type.startswith(METAGEN_PREFIX),
        )
        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = Event.occurred_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return [
            {
                "id": str(r.id),
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "event_type": r.event_type,
                "status": r.status,
                "detail": r.detail,
                "occurred_at": r.occurred_at,
            }
            for r in rows
        ], total

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _gather_evidence(
        self,
        dataset_urn: str,
        targets: list[str],
    ) -> dict[str, Any]:
        """Gather DataHub evidence for the LLM generation step (best-effort)."""
        evidence: dict[str, Any] = {}

        try:
            from datahub.metadata.schema_classes import DatasetPropertiesClass

            props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
            if props:
                evidence["dataset_name"] = getattr(props, "name", "") or ""
                evidence["description"] = getattr(props, "description", "") or ""
        except Exception:
            logger.warning(
                "metagen_evidence_props_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )

        try:
            from datahub.metadata.schema_classes import SchemaMetadataClass

            schema = await self._datahub.get_aspect(dataset_urn, SchemaMetadataClass)
            if schema and hasattr(schema, "fields"):
                evidence["schema_fields"] = [
                    {
                        "fieldPath": getattr(f, "fieldPath", ""),
                        "nativeDataType": getattr(f, "nativeDataType", "") or "",
                        "description": getattr(f, "description", "") or "",
                    }
                    for f in schema.fields
                ]
        except Exception:
            logger.warning(
                "metagen_evidence_schema_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )
            evidence.setdefault("schema_fields", [])

        try:
            from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

            editable_props = await self._datahub.get_aspect(
                dataset_urn, EditableDatasetPropertiesClass
            )
            if editable_props:
                evidence["editable_description"] = getattr(editable_props, "description", None)
        except Exception:
            logger.warning(
                "metagen_evidence_editable_props_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )

        try:
            from datahub.metadata.schema_classes import EditableSchemaMetadataClass

            editable_schema = await self._datahub.get_aspect(
                dataset_urn, EditableSchemaMetadataClass
            )
            if editable_schema and hasattr(editable_schema, "editableSchemaFieldInfo"):
                evidence["editable_field_descriptions"] = [
                    {
                        "fieldPath": getattr(f, "fieldPath", ""),
                        "description": getattr(f, "description", "") or "",
                    }
                    for f in editable_schema.editableSchemaFieldInfo
                    if getattr(f, "description", None)
                ]
            else:
                evidence["editable_field_descriptions"] = []
        except Exception:
            logger.warning(
                "metagen_evidence_editable_schema_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )
            evidence.setdefault("editable_field_descriptions", [])

        try:
            from datahub.metadata.schema_classes import GlossaryTermsClass

            glossary_terms = await self._datahub.get_aspect(dataset_urn, GlossaryTermsClass)
            if glossary_terms and hasattr(glossary_terms, "terms"):
                evidence["glossary_terms"] = [str(t.urn) for t in glossary_terms.terms]
            else:
                evidence["glossary_terms"] = []
        except Exception:
            logger.warning(
                "metagen_evidence_glossary_terms_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )
            evidence.setdefault("glossary_terms", [])

        # Gather related documents (always, not only for cross_data.md targets)
        # fetch_related_documents is best-effort and handles its own exceptions internally.
        try:
            evidence["related_documents"] = await fetch_related_documents(
                dataset_urn, self._datahub
            )
        except Exception:
            logger.warning(
                "metagen_evidence_related_documents_failed",
                extra={"dataset_urn": dataset_urn},
                exc_info=True,
            )
            evidence["related_documents"] = []

        return evidence

    async def _propose_target(
        self,
        target: str,
        dataset_urn: str,
        evidence: dict[str, Any],
    ) -> Any:
        """Call LLM to generate a proposal for one target.

        Returns:
          - dataset.description → str
          - column.description → dict[fieldPath, description]
          - cross_data.md → list[{action_id, action, ...}]
        """
        if target == "dataset.description":
            return await self._propose_dataset_description(evidence)
        elif target == "column.description":
            return await self._propose_column_descriptions(evidence)
        elif target == "cross_data.md":
            return await self._propose_cross_data(dataset_urn, evidence)
        else:
            logger.warning(
                "metagen_unknown_target",
                extra={"target": target, "dataset_urn": dataset_urn},
            )
            return None

    async def _propose_dataset_description(self, evidence: dict[str, Any]) -> str:
        name = evidence.get("dataset_name", "")
        current_desc = evidence.get("description", "")
        fields = evidence.get("schema_fields", [])
        field_summary = ", ".join(f.get("fieldPath", "") for f in fields[:20])
        node_ids = evidence.get("ontogen_node_ids", [])
        triples = evidence.get("ontogen_triples", [])

        triple_lines = "\n".join(
            f"  {t['subject_node_id']} --[{t['edge_label']}]--> {t['object_node_id']}"
            for t in triples[:30]
        )

        prompt = (
            f"Dataset: {name}\n"
            f"Current description: {current_desc!r}\n"
            f"Columns: {field_summary}\n"
            f"Ontology nodes: {node_ids}\n"
            f"Ontology triples:\n{triple_lines or '(none)'}\n\n"
            "Write a concise one-paragraph business description for this dataset. "
            "Return ONLY the description text — no JSON, no formatting."
        )
        return await self._llm.complete(prompt)

    async def _propose_column_descriptions(self, evidence: dict[str, Any]) -> dict[str, str]:
        name = evidence.get("dataset_name", "")
        schema_fields = evidence.get("schema_fields", [])
        if not schema_fields:
            return {}

        field_lines = "\n".join(
            f"  - {f['fieldPath']} ({f.get('nativeDataType', '')}): {f.get('description', '')}"
            for f in schema_fields[:40]
        )
        triples = evidence.get("ontogen_triples", [])
        triple_lines = "\n".join(
            f"  {t['subject_node_id']} --[{t['edge_label']}]--> {t['object_node_id']}"
            for t in triples[:30]
        )

        prompt = (
            f"Dataset: {name}\n"
            f"Schema:\n{field_lines}\n"
            f"Ontology triples:\n{triple_lines or '(none)'}\n\n"
            "For each column, write a concise business-friendly description. "
            "Return a JSON object mapping fieldPath to description string. "
            "Respond with ONLY the JSON object."
        )
        result = await self._llm.complete_json(prompt)
        # Ensure it's a flat string-to-string dict
        if isinstance(result, dict):
            return {str(k): str(v) for k, v in result.items()}
        return {}

    async def _propose_cross_data(
        self,
        dataset_urn: str,
        evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Propose cross-dataset document actions for cross_data.md.

        The raw LLM response is validated through CrossDataAction before being
        stored in metagen_results.proposals.  Invalid individual actions are
        dropped with a WARNING rather than failing the entire proposal.
        """
        related_docs = evidence.get("related_documents", [])
        dataset_name = evidence.get("dataset_name", dataset_urn)
        triples = evidence.get("ontogen_triples", [])

        doc_summary = "\n".join(
            f"  - URN: {doc['urn']}  Title: {doc.get('title', '')}  "
            f"Related assets: {doc.get('related_assets', [])}\n"
            f"    Body (excerpt): {doc.get('body', '')[:200]}"
            for doc in related_docs
        )
        triple_lines = "\n".join(
            f"  {t['subject_node_id']} --[{t['edge_label']}]--> {t['object_node_id']}"
            for t in triples[:30]
        )

        prompt = (
            f"Dataset: {dataset_name} ({dataset_urn})\n"
            f"Ontology triples:\n{triple_lines or '(none)'}\n"
            f"Existing documents referencing this dataset:\n{doc_summary or '(none)'}\n\n"
            "Propose a list of document actions to organize cross-data documentation "
            "for this dataset. "
            "Return a JSON array of action objects. Each object must have:\n"
            '  action_id (str, unique), action (one of: "create", "modify", "delete"),\n'
            "  confidence (float 0–1),\n"
            "  and action-specific fields:\n"
            "    create: title (str, ≤300 chars), body (str, Markdown, ≤50000 chars), "
            "related_assets (non-empty list of urn:li:dataset:* URNs)\n"
            "    modify: document_urn (urn:li:document:* URN), body (str, Markdown, ≤50000 chars), "
            "related_assets (list of urn:li:dataset:* URNs, optional)\n"
            "    delete: document_urn (urn:li:document:* URN)\n"
            "Return ONLY the JSON array."
        )
        raw_result = await self._llm.complete_json(prompt)

        # Normalise LLM output to a list
        raw_list: list[Any]
        if isinstance(raw_result, list):
            raw_list = raw_result
        elif isinstance(raw_result, dict):
            raw_list = []
            for key in ("actions", "proposals", "items"):
                val = raw_result.get(key)
                if isinstance(val, list):
                    raw_list = val
                    break
        else:
            raw_list = []

        # Validate each item through the schema — drop invalid ones.
        validated: list[dict[str, Any]] = []
        _adapter: TypeAdapter[CrossDataAction] = TypeAdapter(CrossDataAction)
        for idx, item in enumerate(raw_list):
            try:
                action = _adapter.validate_python(item)
                validated.append(action.model_dump())
            except ValidationError as exc:
                logger.warning(
                    "metagen_cross_data_llm_action_invalid",
                    extra={
                        "dataset_urn": dataset_urn,
                        "index": idx,
                        "error": str(exc),
                    },
                )
        return validated

    async def _apply_approved_fields(
        self,
        dataset_urn: str,
        proposals: dict[str, Any],
        approved_fields: list[str],
    ) -> None:
        """Write approved field proposals to DataHub editable aspects.

        Groups writes per entity and issues a single emit_mcp per entity.
        """
        # Separate dataset.description, column.description.{fieldPath}, cross_data.md.*
        dataset_desc: str | None = None
        column_descs: dict[str, str] = {}
        cross_data_action_ids: list[str] = []

        for field_path in approved_fields:
            if field_path == "dataset.description":
                val = proposals.get("dataset.description")
                if isinstance(val, str):
                    dataset_desc = val
            elif field_path.startswith("column.description."):
                fp = field_path[len("column.description.") :]
                col_proposals = proposals.get("column.description") or {}
                if isinstance(col_proposals, dict) and fp in col_proposals:
                    column_descs[fp] = str(col_proposals[fp])
            elif field_path.startswith("cross_data.md."):
                action_id = field_path[len("cross_data.md.") :]
                cross_data_action_ids.append(action_id)

        # Write dataset.description
        if dataset_desc:
            try:
                from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

                editable_props = EditableDatasetPropertiesClass(description=dataset_desc)
                await self._datahub.emit_aspect(dataset_urn, editable_props)
            except Exception:
                logger.warning(
                    "metagen_apply_dataset_description_failed",
                    extra={"dataset_urn": dataset_urn},
                    exc_info=True,
                )

        # Write column descriptions (read-modify-write to preserve prior approvals)
        # Fix #10: fetch existing aspect and splice new entries in by fieldPath
        if column_descs:
            try:
                from datahub.metadata.schema_classes import (
                    EditableSchemaFieldInfoClass,
                    EditableSchemaMetadataClass,
                )

                existing_schema = await self._datahub.get_aspect(
                    dataset_urn, EditableSchemaMetadataClass
                )
                if existing_schema is not None:
                    # Build a dict of existing entries keyed by fieldPath
                    existing_info: dict[str, Any] = {
                        getattr(f, "fieldPath", ""): f
                        for f in (existing_schema.editableSchemaFieldInfo or [])
                    }
                    # Splice in the new/updated entries
                    for fp, desc in column_descs.items():
                        existing_info[fp] = EditableSchemaFieldInfoClass(
                            fieldPath=fp, description=desc
                        )
                    merged_fields = list(existing_info.values())
                else:
                    merged_fields = [
                        EditableSchemaFieldInfoClass(fieldPath=fp, description=desc)
                        for fp, desc in column_descs.items()
                    ]

                editable_schema = EditableSchemaMetadataClass(editableSchemaFieldInfo=merged_fields)
                await self._datahub.emit_aspect(dataset_urn, editable_schema)
            except Exception:
                logger.warning(
                    "metagen_apply_column_descriptions_failed",
                    extra={"dataset_urn": dataset_urn},
                    exc_info=True,
                )

        # Apply cross_data.md actions
        if cross_data_action_ids:
            cross_data_proposals: list[dict[str, Any]] = proposals.get("cross_data.md") or []
            if not isinstance(cross_data_proposals, list):
                cross_data_proposals = []

            # Defense-in-depth: re-validate proposals from mutable JSONB before dispatch.
            _action_id_set = set(cross_data_action_ids)
            approved_actions: list[dict[str, Any]] = []
            _adapter: TypeAdapter[CrossDataAction] = TypeAdapter(CrossDataAction)
            for raw_action in cross_data_proposals:
                if not isinstance(raw_action, dict):
                    continue
                if raw_action.get("action_id") not in _action_id_set:
                    continue
                try:
                    action = _adapter.validate_python(raw_action)
                    approved_actions.append(action.model_dump())
                except ValidationError as exc:
                    logger.warning(
                        "metagen_apply_cross_data_revalidation_failed",
                        extra={
                            "dataset_urn": dataset_urn,
                            "action_id": raw_action.get("action_id"),
                            "error": str(exc),
                        },
                    )

            if approved_actions:
                try:
                    outcomes = await apply_actions(approved_actions, datahub=self._datahub)
                    logger.info(
                        "metagen_apply_cross_data_complete",
                        extra={
                            "dataset_urn": dataset_urn,
                            "action_ids": cross_data_action_ids,
                            "outcomes": outcomes,
                        },
                    )
                except Exception:
                    logger.warning(
                        "metagen_apply_cross_data_failed",
                        extra={"dataset_urn": dataset_urn, "action_ids": cross_data_action_ids},
                        exc_info=True,
                    )

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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_initial_field_status(proposals: dict[str, Any]) -> dict[str, Any]:
    """Build the initial field_status dict with every field set to 'pending'.

    Key format:
      - "dataset.description"  → single key
      - "column.description.{fieldPath}" → one key per column
      - "cross_data.md.{action_id}" → one key per action
    """
    status: dict[str, Any] = {}

    for target, value in proposals.items():
        if target == "dataset.description":
            status["dataset.description"] = "pending"
        elif target == "column.description":
            if isinstance(value, dict):
                for fp in value:
                    status[f"column.description.{fp}"] = "pending"
        elif target == "cross_data.md":
            if isinstance(value, list):
                for action in value:
                    action_id = action.get("action_id")
                    if action_id:
                        status[f"cross_data.md.{action_id}"] = "pending"
            elif isinstance(value, dict):
                action_id = value.get("action_id")
                if action_id:
                    status[f"cross_data.md.{action_id}"] = "pending"

    return status
