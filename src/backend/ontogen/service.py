"""Ontology Generation service — UC3 triple ontology pipeline.

Spec: spec/feature/BACKEND.md §Ontology Generation Service
      spec/DATAHUB_INTEGRATION.md §Aspect Reference (glossaryTerms, dataProductProperties)
"""

import logging
import re
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, constr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ontogen.evidence import gather_evidence
from src.backend.ontogen.prompts import build_run_prompt
from src.shared.cache.client import RedisClient
from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DatasetNodeMap,
    Event,
    OntogenConfig,
    OntogenEdge,
    OntogenNode,
    OntogenSeed,
    OntogenTriple,
)
from src.shared.events import (
    EDGE_APPROVE,
    EDGE_REJECT,
    NODE_APPROVE,
    NODE_REJECT,
    ONTOGEN_CONFIG_CREATE,
    ONTOGEN_CONFIG_UPDATE,
    ONTOGEN_RUN_COMPLETE,
    ONTOGEN_RUN_FAILED,
    ONTOGEN_SEED_CREATE,
    ONTOGEN_SEED_DELETE,
    ONTOGEN_SEED_UPDATE,
    TRIPLE_APPROVE,
    TRIPLE_REJECT,
)
from src.shared.exceptions import (
    ConflictError,
    DataSpokeError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from src.shared.graph.client import AgeGraph
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager, VectorHit

logger = logging.getLogger(__name__)

# Redis key for the ontogen singleton lock
_LOCK_KEY = "ontogen:running:singleton"
_LOCK_TTL_SECONDS = 3600  # 1 hour

# Redis cache TTL for hot node/edge/triple lookups
_CACHE_TTL = 300

# Node embedding collection (node_embeddings table)
_NODE_EMBEDDING_COLLECTION = "node_embeddings"

# Reuse threshold for node embedding similarity search
# (uses ONTOLOGY_CONFIDENCE_THRESHOLD from config as default)
_NODE_REUSE_THRESHOLD = ONTOLOGY_CONFIDENCE_THRESHOLD


# ── Value objects ─────────────────────────────────────────────────────────────


class SeedPreview(BaseModel):
    """Summary row returned by list_seeds()."""

    seed_id: str
    updated_at: datetime
    preview: str  # first 200 chars of body_md, newlines normalised


class OntogenRunSummary(BaseModel):
    """Outcome of a run() call."""

    status: str
    dry_run: bool
    unresolved_urns: list[str]
    counts: dict[str, int]


# ── Helpers ───────────────────────────────────────────────────────────────────


_DATASET_URN_RE = re.compile(r"^urn:li:dataset:\(.+\)$")


def _validate_dataset_urn(urn: str) -> None:
    """Raise InvalidDatasetUrnError if *urn* does not look like a dataset URN."""
    if not _DATASET_URN_RE.match(urn):
        raise InvalidDatasetUrnError(urn)


def _validate_dataset_filter(dataset_filter: dict[str, Any]) -> None:
    """Validate all explicit URNs in dataset_filter; raise for malformed ones."""
    for urn in dataset_filter.get("dataset_urns", []) or []:
        _validate_dataset_urn(str(urn))


_VALID_SCHEDULE_TIERS: frozenset[str] = frozenset({"hourly", "daily", "weekly"})


def _validate_schedule_tier(tier: str | None) -> None:
    """Raise PreconditionFailedError if *tier* is not a valid schedule tier.

    ``None`` is allowed (disables scheduling).
    """
    if tier is not None and tier not in _VALID_SCHEDULE_TIERS:
        from src.shared.exceptions import PreconditionFailedError

        raise PreconditionFailedError(
            "INVALID_PARAMETER",
            f"schedule_tier must be one of {sorted(_VALID_SCHEDULE_TIERS)} or null, "
            f"got {tier!r}",
        )


def _to_slug(text: str) -> str:
    """Convert *text* to a lowercase ASCII slug (kebab-case, no double underscores)."""
    # Normalise unicode to ASCII-safe form
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    # Replace double hyphens; reject if resulting slug is empty
    slug = re.sub(r"-+", "-", slug)
    return slug or "node"


def _make_slug_id(name: str, existing_ids: set[str]) -> str:
    """Derive a unique slug ID from *name*, avoiding *existing_ids*."""
    base = _to_slug(name)
    # Strip double underscore (reserved)
    base = base.replace("__", "-")
    candidate = base
    counter = 1
    while candidate in existing_ids or "__" in candidate:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _status_for_confidence(score: float) -> str:
    if score >= ONTOLOGY_CONFIDENCE_THRESHOLD:
        return "approved"
    return "pending_review"


def _preview(body_md: str) -> str:
    normalised = " ".join(body_md.splitlines())
    return normalised[:200]


_SLUG_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _assert_node_id(node_id: str) -> None:
    """Raise ValueError if *node_id* is not a valid slug (defence-in-depth)."""
    if not _SLUG_ID_RE.match(node_id):
        raise ValueError(
            f"node_id {node_id!r} is not a valid slug (allowed: a-z0-9_-, max 64 chars)"
        )


def _assert_edge_id(edge_id: str) -> None:
    """Raise ValueError if *edge_id* is not a valid slug (defence-in-depth)."""
    if not _SLUG_ID_RE.match(edge_id):
        raise ValueError(
            f"edge_id {edge_id!r} is not a valid slug (allowed: a-z0-9_-, max 64 chars)"
        )


# ── Pydantic models for LLM JSON output validation ───────────────────────────


class _LLMNode(BaseModel):
    id: str | None = None  # optional hint; we always re-slug from name
    name: constr(strip_whitespace=True, min_length=1, max_length=200)  # type: ignore[valid-type]
    description: constr(max_length=4000) = ""  # type: ignore[valid-type]
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    dataset_urns: list[str] = []  # supporting dataset URNs per Fix #2


class _LLMEdge(BaseModel):
    id: str | None = None
    label: constr(strip_whitespace=True, min_length=1, max_length=200)  # type: ignore[valid-type]
    semantics: constr(max_length=4000) = ""  # type: ignore[valid-type]
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)


class _LLMTriple(BaseModel):
    subject_node_id: str
    edge_id: str
    object_node_id: str
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)


class _LLMRunResult(BaseModel):
    nodes: list[_LLMNode] = []
    edges: list[_LLMEdge] = []
    triples: list[_LLMTriple] = []


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


class OntogenService:
    """Singleton-config LLM pipeline for the triple ontology.

    Constructor-injected dependencies (stateless service pattern per
    spec/feature/BACKEND.md §Service Pattern):

    - datahub: DataHubClient
    - db: AsyncSession
    - cache: RedisClient
    - llm: LLMClient
    - age: AgeGraph
    - vector: PgVectorManager
    """

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
        llm: LLMClient,
        age: AgeGraph,
        vector: PgVectorManager,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache
        self._llm = llm
        self._age = age
        self._vector = vector

    # ── Singleton conf CRUD ───────────────────────────────────────────────────

    async def get_conf(self) -> OntogenConfig:
        """Return the singleton conf row, creating defaults if absent."""
        result = await self._db.execute(
            select(OntogenConfig).where(OntogenConfig.id == 1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = OntogenConfig(
                id=1,
                is_enabled=False,
                dataset_filter={},
                max_manual_queries_per_dataset=20,
                max_system_queries_per_dataset=10,
            )
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
        return row

    async def put_conf(self, conf: dict[str, Any]) -> OntogenConfig:
        """Full replacement of the singleton conf.

        Validates dataset_filter.dataset_urns format — raises
        InvalidDatasetUrnError for malformed entries.
        Validates schedule_tier value — raises PreconditionFailedError for
        unknown tiers.
        Emits ONTOGEN.CONFIG_CREATE or ONTOGEN.CONFIG_UPDATE.
        """
        dataset_filter = conf.get("dataset_filter", {}) or {}
        _validate_dataset_filter(dataset_filter)
        _validate_schedule_tier(conf.get("schedule_tier"))

        result = await self._db.execute(
            select(OntogenConfig).where(OntogenConfig.id == 1)
        )
        existing = result.scalar_one_or_none()
        created = existing is None

        if existing is None:
            existing = OntogenConfig(id=1)
            self._db.add(existing)

        existing.is_enabled = conf.get("is_enabled", False)
        existing.schedule_tier = conf.get("schedule_tier")
        existing.dataset_filter = dataset_filter
        existing.max_manual_queries_per_dataset = conf.get(
            "max_manual_queries_per_dataset", 20
        )
        existing.max_system_queries_per_dataset = conf.get(
            "max_system_queries_per_dataset", 10
        )
        existing.default_run_prompt = conf.get("default_run_prompt")
        existing.updated_at = datetime.now(tz=UTC)

        await self._db.commit()
        await self._db.refresh(existing)

        event_type = ONTOGEN_CONFIG_CREATE if created else ONTOGEN_CONFIG_UPDATE
        await self._record_ontogen_event(
            "singleton",
            event_type,
            "success",
            {"operation": "PUT"},
        )
        return existing

    async def patch_conf(self, partial: dict[str, Any]) -> OntogenConfig:
        """Partial update of the singleton conf.

        Validates dataset_filter.dataset_urns format if provided.
        Validates schedule_tier value if provided.
        Emits ONTOGEN.CONFIG_UPDATE.
        """
        if "dataset_filter" in partial and partial["dataset_filter"] is not None:
            _validate_dataset_filter(partial["dataset_filter"])
        if "schedule_tier" in partial:
            _validate_schedule_tier(partial["schedule_tier"])

        row = await self.get_conf()

        for field_name in (
            "is_enabled",
            "schedule_tier",
            "dataset_filter",
            "max_manual_queries_per_dataset",
            "max_system_queries_per_dataset",
            "default_run_prompt",
        ):
            if field_name in partial and partial[field_name] is not None:
                setattr(row, field_name, partial[field_name])
            elif field_name in partial and partial[field_name] is None:
                # Allow explicit null for nullable fields
                setattr(row, field_name, None)

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_ontogen_event(
            "singleton",
            ONTOGEN_CONFIG_UPDATE,
            "success",
            {"operation": "PATCH", "fields_changed": list(partial.keys())},
        )
        return row

    async def delete_conf(self) -> None:
        """Reset conf to defaults (delete + re-create defaults on next get_conf).

        Emits ONTOGEN.CONFIG_DELETE.
        """
        from src.shared.events import ONTOGEN_CONFIG_DELETE

        result = await self._db.execute(
            select(OntogenConfig).where(OntogenConfig.id == 1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await self._db.delete(row)
            await self._db.commit()

        await self._record_ontogen_event(
            "singleton",
            ONTOGEN_CONFIG_DELETE,
            "success",
            {"operation": "DELETE"},
        )

    # ── Seed CRUD ─────────────────────────────────────────────────────────────

    async def list_seeds(
        self, offset: int = 0, limit: int = 20
    ) -> tuple[list[SeedPreview], int]:
        """Return paginated seed previews sorted by updated_at desc."""
        base = select(OntogenSeed).where(OntogenSeed.status == "active")
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        rows_q = (
            base.order_by(OntogenSeed.updated_at.desc()).offset(offset).limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()

        previews = [
            SeedPreview(
                seed_id=str(r.id),
                updated_at=r.updated_at,
                preview=_preview(r.body_md),
            )
            for r in rows
        ]
        return previews, total

    async def create_seed(self, body_md: str) -> OntogenSeed:
        """Create a new active seed and emit ONTOGEN.SEED_CREATE."""
        seed = OntogenSeed(body_md=body_md, status="active")
        self._db.add(seed)
        await self._db.commit()
        await self._db.refresh(seed)

        await self._record_ontogen_event(
            f"seed:{seed.id}",
            ONTOGEN_SEED_CREATE,
            "success",
            {"seed_id": str(seed.id)},
        )
        return seed

    async def get_seed(self, seed_id: str) -> OntogenSeed:
        """Return a seed row; raises EntityNotFoundError if absent or UUID malformed."""
        try:
            seed_uuid = uuid.UUID(seed_id)
        except ValueError:
            raise EntityNotFoundError("seed", seed_id)
        result = await self._db.execute(
            select(OntogenSeed).where(OntogenSeed.id == seed_uuid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("seed", seed_id)
        return row

    async def patch_seed(self, seed_id: str, body_md: str) -> OntogenSeed:
        """Update seed Markdown body and emit ONTOGEN.SEED_UPDATE."""
        row = await self.get_seed(seed_id)
        row.body_md = body_md
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_ontogen_event(
            f"seed:{seed_id}",
            ONTOGEN_SEED_UPDATE,
            "success",
            {"seed_id": seed_id},
        )
        return row

    async def delete_seed(self, seed_id: str) -> None:
        """Retire (soft-delete) a seed and emit ONTOGEN.SEED_DELETE."""
        row = await self.get_seed(seed_id)
        row.status = "retired"
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()

        await self._record_ontogen_event(
            f"seed:{seed_id}",
            ONTOGEN_SEED_DELETE,
            "success",
            {"seed_id": seed_id},
        )

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(
        self,
        prompt_md: str | None = None,
        *,
        dry_run: bool = False,
    ) -> OntogenRunSummary:
        """Full ontogen inference pipeline.

        Steps (see spec/feature/BACKEND.md §Inference Pipeline):
        1. Acquire Redis SETNX guard.
        2. Resolve effective one-shot prompt.
        3. Enumerate datasets per dataset_filter.
        4. Gather evidence per dataset.
        5. Load active seeds.
        6. Build LLM prompt.
        7. Load approved nodes/edges + embeddings for reuse.
        8. Call LLM, process proposals (node/edge/triple reuse, confidence scoring).
        9. If dry_run: return summary without persisting.
        10. Persist nodes/edges/triples; materialise AGE edges; refresh embeddings.
        11. Refresh dataset_embeddings for in-scope datasets.
        12. Emit ONTOGEN.RUN_COMPLETE.
        """
        # Step 1: Acquire Redis SETNX guard (CAS token prevents cross-worker deletion)
        lock_token = secrets.token_urlsafe(16)
        acquired = await self._cache.set_nx(_LOCK_KEY, lock_token, ttl_seconds=_LOCK_TTL_SECONDS)
        if not acquired:
            raise ConflictError("ONTOGEN_RUNNING", "Ontogen inference is already running")

        try:
            return await self._run_inner(prompt_md=prompt_md, dry_run=dry_run)
        except ConflictError:
            raise
        except Exception as exc:
            logger.error("ontogen_run_failed", exc_info=True)
            try:
                await self._record_ontogen_event(
                    "singleton",
                    ONTOGEN_RUN_FAILED,
                    "failure",
                    {"error": str(exc)},
                )
            except Exception:
                logger.warning("ontogen_run_failed_event_emit_failed", exc_info=True)
            raise
        finally:
            await self._cache.delete_if_value(_LOCK_KEY, lock_token)

    async def _run_inner(
        self,
        prompt_md: str | None,
        dry_run: bool,
    ) -> OntogenRunSummary:
        """Inner run logic (called inside the SETNX guard)."""
        # Step 2: Load conf and resolve one-shot prompt
        conf = await self.get_conf()
        effective_prompt = prompt_md or conf.default_run_prompt

        # Step 3: Enumerate datasets matching dataset_filter
        dataset_filter = conf.dataset_filter or {}
        dataset_urns, unresolved_urns = await self._enumerate_datasets(dataset_filter)

        # Step 4: Gather evidence per dataset (best-effort)
        evidence_per_dataset: dict[str, dict[str, Any]] = {}
        for urn in dataset_urns:
            try:
                evidence_per_dataset[urn] = await gather_evidence(urn, self._datahub, conf)
            except Exception:
                logger.warning(
                    "ontogen_evidence_gather_failed",
                    extra={"dataset_urn": urn},
                    exc_info=True,
                )

        # Step 5: Load active seeds
        seed_rows = (
            await self._db.execute(
                select(OntogenSeed).where(OntogenSeed.status == "active")
            )
        ).scalars().all()
        seeds_md = "\n\n---\n\n".join(r.body_md for r in seed_rows)

        # Step 6: Build prompt (with per-run nonce for prompt-injection hardening)
        run_nonce = secrets.token_hex(8)
        prompt = build_run_prompt(
            seeds_md, evidence_per_dataset, effective_prompt, nonce=run_nonce
        )

        # Step 7: Load approved nodes/edges for reuse
        approved_nodes = (
            await self._db.execute(
                select(OntogenNode).where(OntogenNode.status == "approved")
            )
        ).scalars().all()
        approved_edges = (
            await self._db.execute(
                select(OntogenEdge).where(OntogenEdge.status == "approved")
            )
        ).scalars().all()
        approved_triples = (
            await self._db.execute(
                select(OntogenTriple).where(OntogenTriple.status == "approved")
            )
        ).scalars().all()

        existing_node_ids: set[str] = {n.id for n in approved_nodes}
        existing_edge_ids: set[str] = {e.id for e in approved_edges}
        existing_triple_ids: set[str] = {t.id for t in approved_triples}

        # Edge label → id lookup for reuse
        edge_label_to_id: dict[str, str] = {e.label: e.id for e in approved_edges}

        # Step 8: Call LLM and validate output with Pydantic schema (Fix #5)
        run_at_iso = datetime.now(tz=UTC).isoformat()
        llm_run_result = _LLMRunResult()
        try:
            from pydantic import ValidationError as PydanticValidationError

            raw_llm = await self._llm.complete_json(prompt)
            try:
                llm_run_result = _LLMRunResult.model_validate(raw_llm)
            except PydanticValidationError:
                logger.warning(
                    "ontogen_llm_output_validation_failed",
                    extra={"raw_keys": list(raw_llm.keys()) if isinstance(raw_llm, dict) else []},
                    exc_info=True,
                )
                llm_run_result = _LLMRunResult()
        except Exception:
            logger.warning("ontogen_llm_call_failed", exc_info=True)

        proposed_nodes: list[_LLMNode] = llm_run_result.nodes
        proposed_edges: list[_LLMEdge] = llm_run_result.edges
        proposed_triples: list[_LLMTriple] = llm_run_result.triples

        # ── Process nodes ──────────────────────────────────────────────────

        # Maps temporary LLM-proposed id → final resolved id
        node_id_remap: dict[str, str] = {}
        nodes_to_upsert: list[dict[str, Any]] = []
        all_known_ids = set(existing_node_ids)

        for p_node in proposed_nodes:
            name: str = p_node.name.strip()
            description: str = p_node.description or ""
            confidence: float = p_node.confidence_score
            dataset_urns_for_node: list[str] = p_node.dataset_urns or []

            # Fix #11: skip proposals with empty name (after strip)
            if not name:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "empty_name", "proposal": p_node.model_dump()},
                )
                continue

            # Fix #3: always re-slug from name regardless of LLM-supplied id
            final_id = _make_slug_id(name, all_known_ids)

            # Try embedding-based reuse
            reused_id: str | None = None
            try:
                embed_text = f"{name} {description}"
                query_vec = await self._llm.embed(embed_text)
                hits = await _search_node_embeddings(self._vector, query_vec, top_k=5)
                if hits:
                    reused_id = hits[0].dataset_urn  # dataset_urn stores node_id here
            except Exception:
                logger.warning(
                    "ontogen_node_embedding_search_failed",
                    extra={"name": name},
                    exc_info=True,
                )

            if reused_id:
                final_id = reused_id
            else:
                all_known_ids.add(final_id)

            # Use the LLM's id as a hint key only for triple remapping
            llm_hint_id = p_node.id or name
            node_id_remap[llm_hint_id] = final_id
            # Also map the re-slugged id to itself for direct triple references
            node_id_remap[final_id] = final_id

            nodes_to_upsert.append(
                {
                    "id": final_id,
                    "name": name,
                    "description": description,
                    "confidence_score": confidence,
                    "status": _status_for_confidence(confidence),
                    "is_reuse": reused_id is not None,
                    "dataset_urns": dataset_urns_for_node,
                    "run_at": run_at_iso,
                }
            )

        # ── Process edges ──────────────────────────────────────────────────

        edge_id_remap: dict[str, str] = {}
        edges_to_upsert: list[dict[str, Any]] = []
        all_known_edge_ids = set(existing_edge_ids)

        for p_edge in proposed_edges:
            label: str = p_edge.label.strip()
            edge_confidence: float = p_edge.confidence_score
            semantics: str | None = p_edge.semantics or None

            # Fix #11: skip empty labels
            if not label:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "empty_label", "proposal": p_edge.model_dump()},
                )
                continue

            # Fix #3: always re-slug from label
            slugged_label = _make_slug_id(label, all_known_edge_ids)

            # Reuse by label match
            if label in edge_label_to_id:
                final_id = edge_label_to_id[label]
            else:
                final_id = slugged_label
                all_known_edge_ids.add(final_id)

            llm_hint_id = p_edge.id or label
            edge_id_remap[llm_hint_id] = final_id
            edge_id_remap[final_id] = final_id

            edges_to_upsert.append(
                {
                    "id": final_id,
                    "label": label,
                    "semantics": semantics,
                    "confidence_score": edge_confidence,
                    "status": _status_for_confidence(edge_confidence),
                    "run_at": run_at_iso,
                }
            )

        # ── Process triples ────────────────────────────────────────────────

        triples_to_upsert: list[dict[str, Any]] = []

        for p_triple in proposed_triples:
            raw_subj = p_triple.subject_node_id
            raw_edge = p_triple.edge_id
            raw_obj = p_triple.object_node_id
            triple_confidence: float = p_triple.confidence_score

            # Remap LLM-proposed ids to final resolved ids
            subj_id = node_id_remap.get(raw_subj, raw_subj)
            edge_id_val = edge_id_remap.get(raw_edge, raw_edge)
            obj_id = node_id_remap.get(raw_obj, raw_obj)

            # Fix #3: validate all three component IDs are valid slugs
            try:
                _assert_node_id(subj_id)
                _assert_edge_id(edge_id_val)
                _assert_node_id(obj_id)
            except ValueError:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={
                        "reason": "invalid_slug_in_triple",
                        "subj_id": subj_id,
                        "edge_id": edge_id_val,
                        "obj_id": obj_id,
                    },
                )
                continue

            triple_id = f"{subj_id}__{edge_id_val}__{obj_id}"
            if triple_id in existing_triple_ids:
                continue  # reuse existing approved triple, skip

            triples_to_upsert.append(
                {
                    "id": triple_id,
                    "subject_node_id": subj_id,
                    "edge_id": edge_id_val,
                    "object_node_id": obj_id,
                    "confidence_score": triple_confidence,
                    "status": _status_for_confidence(triple_confidence),
                    "run_at": run_at_iso,
                }
            )

        # Step 9: dry_run short-circuit
        if dry_run:
            return OntogenRunSummary(
                status="success",
                dry_run=True,
                unresolved_urns=unresolved_urns,
                counts={
                    "nodes_proposed": len(nodes_to_upsert),
                    "edges_proposed": len(edges_to_upsert),
                    "triples_proposed": len(triples_to_upsert),
                },
            )

        # Step 10: Persist nodes
        nodes_added = 0
        for n in nodes_to_upsert:
            node_id = n["id"]

            # Fix #3: defence-in-depth slug check before any DB write
            try:
                _assert_node_id(node_id)
            except ValueError:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "invalid_node_id_at_persist", "node_id": node_id},
                )
                continue

            # Fix #12: compact evidence JSONB
            _node_evidence: dict[str, Any] = {
                "datasets": [
                    u[:1024] for u in (n.get("dataset_urns") or [])
                ],
                "run_at": n.get("run_at", ""),
            }

            if n["is_reuse"]:
                # Approved node already exists; upsert DatasetNodeMap rows only
                await self._upsert_dataset_node_maps(
                    node_id, n.get("dataset_urns") or [], n["confidence_score"]
                )
                continue

            existing = (
                await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id == node_id)
                )
            ).scalar_one_or_none()

            if existing is None:
                orm_node = OntogenNode(
                    id=node_id,
                    name=n["name"],
                    description=n["description"],
                    confidence_score=n["confidence_score"],
                    status=n["status"],
                    evidence=_node_evidence,
                )
                self._db.add(orm_node)
                nodes_added += 1
            else:
                # Update if description/name changed; always refresh evidence
                if existing.description != n["description"] or existing.name != n["name"]:
                    existing.name = n["name"]
                    existing.description = n["description"]
                    existing.confidence_score = n["confidence_score"]
                    existing.updated_at = datetime.now(tz=UTC)
                existing.evidence = _node_evidence
                self._db.add(existing)

        await self._db.flush()

        # Persist DatasetNodeMap rows for new/updated nodes (Fix #2)
        for n in nodes_to_upsert:
            if not n["is_reuse"]:
                await self._upsert_dataset_node_maps(
                    n["id"], n.get("dataset_urns") or [], n["confidence_score"]
                )

        await self._db.commit()

        # Persist edges
        edges_added = 0
        for e in edges_to_upsert:
            edge_id = e["id"]

            # Fix #3: defence-in-depth slug check
            try:
                _assert_edge_id(edge_id)
            except ValueError:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "invalid_edge_id_at_persist", "edge_id": edge_id},
                )
                continue

            # Fix #12: compact evidence JSONB
            _edge_evidence: dict[str, Any] = {"run_at": e.get("run_at", "")}

            existing_edge: OntogenEdge | None = (
                await self._db.execute(
                    select(OntogenEdge).where(OntogenEdge.id == edge_id)
                )
            ).scalar_one_or_none()

            if existing_edge is None:
                orm_edge = OntogenEdge(
                    id=edge_id,
                    label=e["label"],
                    semantics=e.get("semantics"),
                    confidence_score=e["confidence_score"],
                    status=e["status"],
                    evidence=_edge_evidence,
                )
                self._db.add(orm_edge)
                edges_added += 1
            else:
                # Refresh evidence on every run (cheaply)
                existing_edge.evidence = _edge_evidence
                self._db.add(existing_edge)

        await self._db.commit()

        # Persist triples + AGE materialisation
        triples_added = 0
        for t in triples_to_upsert:
            # Verify referenced nodes and edges exist
            subj_row = (
                await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id == t["subject_node_id"])
                )
            ).scalar_one_or_none()
            obj_row = (
                await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id == t["object_node_id"])
                )
            ).scalar_one_or_none()
            edge_row = (
                await self._db.execute(
                    select(OntogenEdge).where(OntogenEdge.id == t["edge_id"])
                )
            ).scalar_one_or_none()

            if not subj_row or not obj_row or not edge_row:
                logger.warning(
                    "ontogen_triple_missing_refs",
                    extra={
                        "triple_id": t["id"],
                        "has_subj": subj_row is not None,
                        "has_obj": obj_row is not None,
                        "has_edge": edge_row is not None,
                    },
                )
                continue

            existing_triple = (
                await self._db.execute(
                    select(OntogenTriple).where(OntogenTriple.id == t["id"])
                )
            ).scalar_one_or_none()

            if existing_triple is None:
                # Fix #12: compact evidence JSONB for triple
                _triple_evidence: dict[str, Any] = {
                    "datasets": sorted(
                        {
                            u
                            for n in nodes_to_upsert
                            if n["id"] in (t["subject_node_id"], t["object_node_id"])
                            for u in (n.get("dataset_urns") or [])
                        }
                    ),
                    "run_at": t.get("run_at", ""),
                }

                orm_triple = OntogenTriple(
                    id=t["id"],
                    subject_node_id=t["subject_node_id"],
                    edge_id=t["edge_id"],
                    object_node_id=t["object_node_id"],
                    confidence_score=t["confidence_score"],
                    status=t["status"],
                    evidence=_triple_evidence,
                )
                self._db.add(orm_triple)
                triples_added += 1

                # Materialise in AGE if triple is auto-approved
                if t["status"] == "approved":
                    try:
                        await self._age.materialize_triple(
                            subject_id=t["subject_node_id"],
                            edge_id=t["edge_id"],
                            object_id=t["object_node_id"],
                            edge_label=edge_row.label,
                        )
                    except DataSpokeError:
                        logger.warning(
                            "ontogen_age_materialise_failed",
                            extra={"triple_id": t["id"]},
                            exc_info=True,
                        )

        await self._db.commit()

        # Step 10 (cont): Refresh node embeddings for new/changed nodes
        await self._refresh_node_embeddings(nodes_to_upsert)

        # Step 11: Refresh dataset_embeddings for in-scope datasets
        await self._refresh_dataset_embeddings(dataset_urns)

        # Step 12: Emit ONTOGEN.RUN_COMPLETE
        summary = OntogenRunSummary(
            status="success",
            dry_run=False,
            unresolved_urns=unresolved_urns,
            counts={
                "nodes_added": nodes_added,
                "edges_added": edges_added,
                "triples_added": triples_added,
            },
        )
        await self._record_ontogen_event(
            "singleton",
            ONTOGEN_RUN_COMPLETE,
            "success",
            {
                "unresolved_urns": unresolved_urns,
                "counts": summary.counts,
            },
        )
        return summary

    # ── Node reads ────────────────────────────────────────────────────────────

    async def list_nodes(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OntogenNode], int]:
        base = select(OntogenNode)
        if status_filter:
            base = base.where(OntogenNode.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        rows_q = base.order_by(OntogenNode.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_node(self, node_id: str) -> OntogenNode:
        cached = await self._cache.get(f"ontogen:node:{node_id}")
        if cached:
            # Return ORM row freshly from DB (cache just signals hot path)
            pass

        result = await self._db.execute(
            select(OntogenNode).where(OntogenNode.id == node_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("node", node_id)

        try:
            import json
            await self._cache.set(
                f"ontogen:node:{node_id}",
                json.dumps({"id": node_id, "status": row.status}),
                ttl_seconds=_CACHE_TTL,
            )
        except Exception:
            pass

        return row

    async def get_node_attr(self, node_id: str) -> dict[str, Any]:
        """Return confidence + evidence for a node."""
        row = await self.get_node(node_id)
        return {
            "node_id": node_id,
            "confidence_score": row.confidence_score,
            "evidence": row.evidence,
            "glossary_term_urn": row.glossary_term_urn,
        }

    async def list_node_events(
        self,
        node_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import NODE_PREFIX

        return await self._list_events(
            "node", node_id, NODE_PREFIX, offset, limit,
            from_dt=from_dt, to_dt=to_dt,
        )

    # ── Edge reads ────────────────────────────────────────────────────────────

    async def list_edges(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OntogenEdge], int]:
        base = select(OntogenEdge)
        if status_filter:
            base = base.where(OntogenEdge.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        rows_q = base.order_by(OntogenEdge.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_edge(self, edge_id: str) -> OntogenEdge:
        result = await self._db.execute(
            select(OntogenEdge).where(OntogenEdge.id == edge_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("edge", edge_id)
        return row

    async def get_edge_attr(self, edge_id: str) -> dict[str, Any]:
        row = await self.get_edge(edge_id)
        return {
            "edge_id": edge_id,
            "confidence_score": row.confidence_score,
            "evidence": row.evidence,
        }

    async def list_edge_events(
        self,
        edge_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import EDGE_PREFIX

        return await self._list_events(
            "edge", edge_id, EDGE_PREFIX, offset, limit,
            from_dt=from_dt, to_dt=to_dt,
        )

    # ── Triple reads ──────────────────────────────────────────────────────────

    async def list_triples(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OntogenTriple], int]:
        base = select(OntogenTriple)
        if status_filter:
            base = base.where(OntogenTriple.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        rows_q = base.order_by(OntogenTriple.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_triple(self, triple_id: str) -> OntogenTriple:
        result = await self._db.execute(
            select(OntogenTriple).where(OntogenTriple.id == triple_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("triple", triple_id)
        return row

    async def get_triple_attr(self, triple_id: str) -> dict[str, Any]:
        row = await self.get_triple(triple_id)
        return {
            "triple_id": triple_id,
            "subject_node_id": row.subject_node_id,
            "edge_id": row.edge_id,
            "object_node_id": row.object_node_id,
            "confidence_score": row.confidence_score,
            "evidence": row.evidence,
        }

    async def list_triple_events(
        self,
        triple_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import TRIPLE_PREFIX

        return await self._list_events(
            "triple", triple_id, TRIPLE_PREFIX, offset, limit,
            from_dt=from_dt, to_dt=to_dt,
        )

    # ── Reviews ───────────────────────────────────────────────────────────────

    async def review_node(
        self,
        node_id: str,
        verdict: str,
        reason: str | None = None,
    ) -> OntogenNode:
        """Approve or reject a node.

        On approval:
          - Write ``glossaryTerms`` aspect to each member dataset in
            ``dataset_node_map`` (best-effort).
          - Set ``glossary_term_urn`` on the node row.
        On reject: status mutation only.
        Emits NODE.APPROVE or NODE.REJECT.
        """
        row = await self.get_node(node_id)

        # Fix #9: validate verdict
        if verdict not in ("approve", "reject"):
            from src.shared.exceptions import PreconditionFailedError as _PE
            raise _PE(
                "INVALID_PARAMETER",
                f"verdict must be 'approve' or 'reject', got {verdict!r}",
            )

        if verdict == "approve":
            row.status = "approved"
            # Fix #3: validate node_id slug before constructing glossary URN
            _assert_node_id(node_id)
            glossary_urn = f"urn:li:glossaryTerm:{node_id}"
            row.glossary_term_urn = glossary_urn
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)

            # Fix #2: set status='approved' on all DatasetNodeMap rows for this node
            maps = (
                await self._db.execute(
                    select(DatasetNodeMap).where(DatasetNodeMap.node_id == node_id)
                )
            ).scalars().all()
            for dm in maps:
                dm.status = "approved"
                self._db.add(dm)

            await self._db.commit()
            await self._db.refresh(row)

            # Best-effort: attach glossary term to member datasets
            await self._attach_node_glossary_term(node_id, glossary_urn)

            event_type = NODE_APPROVE
        else:
            row.status = "rejected"
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)

            # Fix #2: set status='rejected' on all DatasetNodeMap rows for this node
            maps = (
                await self._db.execute(
                    select(DatasetNodeMap).where(DatasetNodeMap.node_id == node_id)
                )
            ).scalars().all()
            for dm in maps:
                dm.status = "rejected"
                self._db.add(dm)

            await self._db.commit()
            await self._db.refresh(row)
            event_type = NODE_REJECT

        await self._record_review_event(
            "node", node_id, event_type, verdict, reason
        )
        # Invalidate cache
        try:
            await self._cache.delete(f"ontogen:node:{node_id}")
        except Exception:
            pass

        return row

    async def review_edge(
        self,
        edge_id: str,
        verdict: str,
        reason: str | None = None,
    ) -> OntogenEdge:
        """Approve or reject an edge (no DataHub write on approval).

        Emits EDGE.APPROVE or EDGE.REJECT.
        """
        row = await self.get_edge(edge_id)

        # Fix #9: validate verdict
        if verdict not in ("approve", "reject"):
            from src.shared.exceptions import PreconditionFailedError as _PE
            raise _PE(
                "INVALID_PARAMETER",
                f"verdict must be 'approve' or 'reject', got {verdict!r}",
            )

        if verdict == "approve":
            row.status = "approved"
            event_type = EDGE_APPROVE
        else:
            row.status = "rejected"
            event_type = EDGE_REJECT

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_review_event(
            "edge", edge_id, event_type, verdict, reason
        )
        try:
            await self._cache.delete(f"ontogen:edge:{edge_id}")
        except Exception:
            pass

        return row

    async def review_triple(
        self,
        triple_id: str,
        verdict: str,
        reason: str | None = None,
    ) -> OntogenTriple:
        """Approve or reject a triple.

        On approval: requires both endpoint nodes and the edge to be approved;
        otherwise raises PreconditionFailedError(ONTOGEN_TRIPLE_DEPENDENCY_PENDING).
        On approval: materialise AGE edge (best-effort); emit glossary-term
        relationship between subject and object terms (best-effort).
        Emits TRIPLE.APPROVE or TRIPLE.REJECT.
        """
        row = await self.get_triple(triple_id)

        # Fix #9: validate verdict
        if verdict not in ("approve", "reject"):
            from src.shared.exceptions import PreconditionFailedError as _PE
            raise _PE(
                "INVALID_PARAMETER",
                f"verdict must be 'approve' or 'reject', got {verdict!r}",
            )

        if verdict == "approve":
            # Dependency gate
            subj_row = (
                await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id == row.subject_node_id)
                )
            ).scalar_one_or_none()
            edge_row = (
                await self._db.execute(
                    select(OntogenEdge).where(OntogenEdge.id == row.edge_id)
                )
            ).scalar_one_or_none()
            obj_row = (
                await self._db.execute(
                    select(OntogenNode).where(OntogenNode.id == row.object_node_id)
                )
            ).scalar_one_or_none()

            subj_approved = subj_row and subj_row.status == "approved"
            edge_approved = edge_row and edge_row.status == "approved"
            obj_approved = obj_row and obj_row.status == "approved"

            if not (subj_approved and edge_approved and obj_approved):
                raise PreconditionFailedError(
                    "ONTOGEN_TRIPLE_DEPENDENCY_PENDING",
                    f"Triple '{triple_id}' cannot be approved: endpoint nodes "
                    f"or edge are not yet approved.",
                )

            row.status = "approved"
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)

            # Best-effort AGE materialisation
            try:
                await self._age.materialize_triple(
                    subject_id=row.subject_node_id,
                    edge_id=row.edge_id,
                    object_id=row.object_node_id,
                    edge_label=edge_row.label if edge_row else row.edge_id,
                )
            except DataSpokeError:
                logger.warning(
                    "ontogen_age_materialise_failed_review",
                    extra={"triple_id": triple_id},
                    exc_info=True,
                )

            # Best-effort: glossary-term relationship between subject and object
            await self._emit_glossary_term_relationship(
                row.subject_node_id,
                row.object_node_id,
                edge_row.label if edge_row else row.edge_id,
            )

            event_type = TRIPLE_APPROVE
        else:
            row.status = "rejected"
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
            event_type = TRIPLE_REJECT

        await self._record_review_event(
            "triple", triple_id, event_type, verdict, reason
        )
        try:
            await self._cache.delete(f"ontogen:triple:{triple_id}")
        except Exception:
            pass

        return row

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _enumerate_datasets(
        self, dataset_filter: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Resolve dataset_filter to a list of URNs.

        Returns (resolved_urns, unresolved_urns).  Unresolved = explicit URNs
        that don't match any dataset in DataHub at runtime.
        """
        tags: list[str] = dataset_filter.get("tags") or []
        glossary_terms: list[str] = dataset_filter.get("glossary_terms") or []
        explicit_urns: list[str] = dataset_filter.get("dataset_urns") or []

        # If filter is empty, enumerate all datasets
        if not tags and not glossary_terms and not explicit_urns:
            try:
                all_urns = await self._datahub.enumerate_datasets()
                return all_urns, []
            except Exception:
                logger.warning("ontogen_enumerate_all_datasets_failed", exc_info=True)
                return [], []

        # OR-enumerate by tags and glossary_terms
        urn_set: set[str] = set()
        try:
            if tags or glossary_terms:
                matched = await self._datahub.enumerate_datasets(
                    tags=tags if tags else None,
                    glossary_terms=glossary_terms if glossary_terms else None,
                )
                urn_set.update(matched)
        except Exception:
            logger.warning("ontogen_enumerate_filtered_datasets_failed", exc_info=True)

        # Validate explicit URNs by checking DataHub (best-effort)
        unresolved: list[str] = []
        for urn in explicit_urns:
            try:
                from datahub.metadata.schema_classes import DatasetPropertiesClass

                props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
                if props is not None:
                    urn_set.add(urn)
                else:
                    unresolved.append(urn)
            except Exception:
                logger.warning(
                    "ontogen_explicit_urn_check_failed",
                    extra={"urn": urn},
                    exc_info=True,
                )
                unresolved.append(urn)

        return sorted(urn_set), unresolved

    async def _refresh_node_embeddings(
        self, nodes_to_upsert: list[dict[str, Any]]
    ) -> None:
        """Embed and upsert node_embeddings for new/changed nodes (best-effort)."""
        for n in nodes_to_upsert:
            if n.get("is_reuse"):
                continue
            node_id = n["id"]
            name = n.get("name", "")
            description = n.get("description", "")
            if not name:
                continue
            try:
                embed_text = f"{name} {description}"
                vec = await self._llm.embed(embed_text)
                await _upsert_node_embedding(self._vector, node_id, vec, name, n["status"])
            except Exception:
                logger.warning(
                    "ontogen_node_embedding_upsert_failed",
                    extra={"node_id": node_id},
                    exc_info=True,
                )

    async def _refresh_dataset_embeddings(self, dataset_urns: list[str]) -> None:
        """Rebuild dataset_embeddings for in-scope datasets (best-effort)."""
        from src.shared.config import EMBEDDING_COLLECTION

        for urn in dataset_urns:
            try:
                from datahub.metadata.schema_classes import (
                    DatasetPropertiesClass,
                    SchemaMetadataClass,
                )

                props = await self._datahub.get_aspect(urn, DatasetPropertiesClass)
                schema = await self._datahub.get_aspect(urn, SchemaMetadataClass)

                parts = [urn]
                if props:
                    if props.name:
                        parts.append(props.name)
                    desc = getattr(props, "description", None)
                    if desc:
                        parts.append(str(desc))
                if schema and hasattr(schema, "fields"):
                    for f in schema.fields[:30]:
                        fp = getattr(f, "fieldPath", "")
                        if fp:
                            parts.append(fp)
                        fdesc = getattr(f, "description", None)
                        if fdesc:
                            parts.append(str(fdesc))

                embed_text = " ".join(p for p in parts if p)
                vec = await self._llm.embed(embed_text)

                hit = VectorHit(
                    dataset_urn=urn,
                    score=0.0,
                    embedding=vec,
                )
                await self._vector.upsert(EMBEDDING_COLLECTION, [hit])
            except Exception:
                logger.warning(
                    "ontogen_dataset_embedding_refresh_failed",
                    extra={"dataset_urn": urn},
                    exc_info=True,
                )

    async def _upsert_dataset_node_maps(
        self,
        node_id: str,
        dataset_urns: list[str],
        confidence: float,
    ) -> None:
        """Upsert DatasetNodeMap rows for *node_id* and its supporting datasets (Fix #2).

        The first URN in the list is marked ``is_primary=True``.  If the node
        confidence meets ONTOLOGY_CONFIDENCE_THRESHOLD the rows start as
        ``status='approved'``; otherwise ``status='pending'``.
        """
        if not dataset_urns:
            return
        status = "approved" if confidence >= ONTOLOGY_CONFIDENCE_THRESHOLD else "pending"
        for idx, urn in enumerate(dataset_urns):
            existing_map = (
                await self._db.execute(
                    select(DatasetNodeMap).where(
                        DatasetNodeMap.dataset_urn == urn,
                        DatasetNodeMap.node_id == node_id,
                    )
                )
            ).scalar_one_or_none()

            if existing_map is None:
                dm = DatasetNodeMap(
                    dataset_urn=urn,
                    node_id=node_id,
                    confidence_score=confidence,
                    status=status,
                    is_primary=(idx == 0),
                )
                self._db.add(dm)
            else:
                existing_map.confidence_score = confidence
                existing_map.status = status
                self._db.add(existing_map)

    async def _attach_node_glossary_term(
        self, node_id: str, glossary_urn: str
    ) -> None:
        """Attach *glossary_urn* to each member dataset in dataset_node_map (best-effort)."""
        try:
            maps = (
                await self._db.execute(
                    select(DatasetNodeMap).where(DatasetNodeMap.node_id == node_id)
                )
            ).scalars().all()

            for dm in maps:
                dataset_urn = dm.dataset_urn
                try:
                    from datahub.metadata.schema_classes import (
                        AuditStampClass,
                        GlossaryTermAssociationClass,
                        GlossaryTermsClass,
                    )

                    existing = await self._datahub.get_aspect(
                        dataset_urn, GlossaryTermsClass
                    )
                    existing_terms = list(existing.terms) if existing else []
                    # Avoid duplicates
                    existing_urns = {str(t.urn) for t in existing_terms}
                    if glossary_urn not in existing_urns:
                        new_term = GlossaryTermAssociationClass(urn=glossary_urn)
                        audit = AuditStampClass(
                            time=int(datetime.now(tz=UTC).timestamp() * 1000),
                            actor="urn:li:corpuser:datahub",
                        )
                        new_terms = GlossaryTermsClass(
                            terms=existing_terms + [new_term],
                            auditStamp=audit,
                        )
                        await self._datahub.emit_aspect(dataset_urn, new_terms)
                except Exception:
                    logger.warning(
                        "ontogen_glossary_term_attach_failed",
                        extra={"dataset_urn": dataset_urn, "node_id": node_id},
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "ontogen_attach_glossary_term_failed",
                extra={"node_id": node_id},
                exc_info=True,
            )

    async def _emit_glossary_term_relationship(
        self,
        subject_node_id: str,
        object_node_id: str,
        edge_label: str,
    ) -> None:
        """Emit a glossary-term relationship between subject and object terms (best-effort).

        DataHub does not have a first-class "glossary term relationship" REST write path
        in the current SDK at the time of this implementation; we log a warning and
        continue per BACKEND.md §Best-Effort Operations.
        """
        try:
            # Fix #3: validate node IDs before constructing glossary URNs
            _assert_node_id(subject_node_id)
            _assert_node_id(object_node_id)
            # Glossary term URNs derived from node IDs
            subj_term_urn = f"urn:li:glossaryTerm:{subject_node_id}"
            obj_term_urn = f"urn:li:glossaryTerm:{object_node_id}"

            # Attempt to use GlossaryRelatedTermsClass if available in the SDK
            try:
                from datahub.metadata.schema_classes import (
                    GlossaryRelatedTermsClass,
                )

                related = GlossaryRelatedTermsClass(
                    isRelatedTerms=[obj_term_urn]
                )
                await self._datahub.emit_aspect(subj_term_urn, related)
            except (ImportError, AttributeError):
                logger.warning(
                    "ontogen_glossary_relationship_api_unavailable",
                    extra={
                        "subject_node_id": subject_node_id,
                        "object_node_id": object_node_id,
                        "edge_label": edge_label,
                    },
                )
        except Exception:
            logger.warning(
                "ontogen_glossary_term_relationship_failed",
                extra={
                    "subject_node_id": subject_node_id,
                    "object_node_id": object_node_id,
                },
                exc_info=True,
            )

    async def list_global_events(
        self,
        entity_type: str,
        entity_id: str,
        event_prefix: str,
        offset: int,
        limit: int,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Public method for listing events — used by routers."""
        return await self._list_events(
            entity_type, entity_id, event_prefix, offset, limit,
            from_dt=from_dt, to_dt=to_dt,
        )

    async def _list_events(
        self,
        entity_type: str,
        entity_id: str,
        event_prefix: str,
        offset: int,
        limit: int,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == entity_type,
            Event.entity_id == entity_id,
            Event.event_type.startswith(event_prefix),
        )
        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        rows_q = (
            base.order_by(Event.occurred_at.desc()).offset(offset).limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        events = [
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
        ]
        return events, total

    async def _record_ontogen_event(
        self,
        entity_id: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = _event_row("ontogen", entity_id, event_type, status, detail)
        self._db.add(event)
        await self._db.commit()

    async def _record_review_event(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        verdict: str,
        reason: str | None,
    ) -> None:
        detail: dict[str, Any] = {"verdict": verdict}
        if reason:
            detail["reason"] = reason
        event = _event_row(entity_type, entity_id, event_type, "success", detail)
        self._db.add(event)
        await self._db.commit()


# ── pgvector helpers for node_embeddings table ────────────────────────────────


async def _search_node_embeddings(
    vector: PgVectorManager,
    query_vec: list[float],
    top_k: int = 5,
) -> list[VectorHit]:
    """Search the node_embeddings table for approved nodes similar to *query_vec*.

    Returns up to *top_k* hits above ONTOLOGY_NODE_REUSE_THRESHOLD.
    Falls back to empty list on any error.
    """
    try:
        from sqlalchemy import text

        async with vector._session_factory() as session:
            vector_literal = "[" + ",".join(str(v) for v in query_vec) + "]"
            sql = text(
                """
                SELECT
                    node_id,
                    name,
                    status,
                    GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector)) AS score
                FROM dataspoke.node_embeddings
                WHERE status = 'approved'
                  AND GREATEST(0.0, 1.0 - (embedding <=> :query_vector::vector)) >= :threshold
                ORDER BY score DESC
                LIMIT :limit
                """
            )
            result = await session.execute(
                sql,
                {
                    "query_vector": vector_literal,
                    "threshold": _NODE_REUSE_THRESHOLD,
                    "limit": top_k,
                },
            )
            rows = result.fetchall()

        # Reuse VectorHit; store node_id in dataset_urn field for consistency
        return [
            VectorHit(dataset_urn=row.node_id, score=float(row.score))
            for row in rows
        ]
    except Exception:
        logger.warning("ontogen_node_embedding_search_error", exc_info=True)
        return []


async def _upsert_node_embedding(
    vector: PgVectorManager,
    node_id: str,
    embedding: list[float],
    name: str,
    status: str,
) -> None:
    """Upsert a row in node_embeddings for *node_id*."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
    sql = text(
        """
        INSERT INTO dataspoke.node_embeddings (node_id, embedding, name, status, updated_at)
        VALUES (:node_id, :embedding::vector, :name, :status, :updated_at)
        ON CONFLICT (node_id) DO UPDATE SET
            embedding  = EXCLUDED.embedding,
            name       = EXCLUDED.name,
            status     = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at
        """
    )
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                sql,
                {
                    "node_id": node_id,
                    "embedding": vector_literal,
                    "name": name,
                    "status": status,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                },
            )
