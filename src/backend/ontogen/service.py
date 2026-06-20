"""Ontology Generation service — UC3 triple ontology pipeline.

Spec: spec/feature/BACKEND.md §Ontology Generation Service
      spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend._dataset_filter import resolve_dataset_scope, validate_dataset_filter_service
from src.backend.admin.config_service import get_runtime_config
from src.backend.ontogen.debate import run_debate
from src.backend.ontogen.embedding_search import search_node_embeddings as _search_node_embeddings
from src.backend.ontogen.evidence import gather_evidence
from src.backend.ontogen.models import (
    OntogenLLMEdge,
    OntogenLLMNode,
    OntogenLLMOutput,
    OntogenLLMTriple,
)
from src.backend.ontogen.prompts import build_run_prompt
from src.backend.ontogen.reviewer import build_ontogen_review_tool
from src.backend.ontogen.slug import assert_edge_id, assert_node_id, make_snake_id
from src.backend.ontogen.validator import (
    ValidationError,
    build_ontogen_validate_tool,
    partition_clean_rows,
)
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
    EntityNotFoundError,
    PreconditionFailedError,
)
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


# ── Value objects ─────────────────────────────────────────────────────────────


class SeedPreview(BaseModel):
    """Summary row returned by list_seeds()."""

    seed_id: str
    is_enabled: bool
    updated_at: datetime
    preview: str  # first 200 chars of body_md, newlines normalised


class OntogenRunSummary(BaseModel):
    """Outcome of a run() call."""

    status: str
    dry_run: bool
    unresolved_urns: list[str]
    counts: dict[str, int]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _status_for_outcome(score: float, debate_outcome: str) -> str:
    """Return the LLM-gated status for a newly persisted ontogen row.

    Returns ``"llm_approved"`` when the debate Reviewer accepted the item and
    the confidence score meets the threshold.  All other outcomes (low
    confidence accept, reject, exhaustion, cycle) return ``"llm_pending"``,
    placing the row in the human review queue.
    """
    if debate_outcome == "accept" and score >= ONTOLOGY_CONFIDENCE_THRESHOLD:
        return "llm_approved"
    return "llm_pending"


def _preview(body_md: str) -> str:
    normalised = " ".join(body_md.splitlines())
    return normalised[:200]


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

    async def get_conf(self) -> OntogenConfig:
        """Return the singleton conf row, creating defaults if absent."""
        result = await self._db.execute(select(OntogenConfig).where(OntogenConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            stmt = (
                pg_insert(OntogenConfig)
                .values(id=1, is_enabled=False, dataset_filter={})
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await self._db.execute(stmt)
            await self._db.commit()
            result = await self._db.execute(select(OntogenConfig).where(OntogenConfig.id == 1))
            row = result.scalar_one()
        return row

    async def put_conf(self, conf: dict[str, Any]) -> OntogenConfig:
        """Full replacement of the singleton conf.

        Validates dataset_filter.dataset_urns format — raises
        InvalidDatasetUrnError for malformed entries. `schedule_tier` values
        are constrained at the API schema layer (`OntogenConfPutRequest`).
        Emits ONTOGEN.CONFIG_CREATE or ONTOGEN.CONFIG_UPDATE.
        """
        dataset_filter = conf.get("dataset_filter", {}) or {}
        validate_dataset_filter_service(dataset_filter)

        result = await self._db.execute(select(OntogenConfig).where(OntogenConfig.id == 1))
        existing = result.scalar_one_or_none()
        created = existing is None

        fields = {
            "is_enabled": conf.get("is_enabled", False),
            "schedule_tier": conf.get("schedule_tier"),
            "dataset_filter": dataset_filter,
            "default_run_prompt": conf.get("default_run_prompt"),
            "updated_at": datetime.now(tz=UTC),
        }
        stmt = (
            pg_insert(OntogenConfig)
            .values(id=1, **fields)
            .on_conflict_do_update(index_elements=["id"], set_=fields)
        )
        await self._db.execute(stmt)
        await self._db.commit()

        # populate_existing refreshes the identity-map instance loaded above with the
        # row just written by the Core upsert; without it the expire_on_commit=False
        # session would return the stale pre-upsert object.
        result = await self._db.execute(
            select(OntogenConfig)
            .where(OntogenConfig.id == 1)
            .execution_options(populate_existing=True)
        )
        existing = result.scalar_one()

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

        Validates dataset_filter.dataset_urns format if provided. `schedule_tier`
        values are constrained at the API schema layer (`OntogenConfPatchRequest`).
        Emits ONTOGEN.CONFIG_UPDATE.
        """
        if "dataset_filter" in partial and partial["dataset_filter"] is not None:
            validate_dataset_filter_service(partial["dataset_filter"])

        row = await self.get_conf()

        for field_name in (
            "is_enabled",
            "schedule_tier",
            "dataset_filter",
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

        result = await self._db.execute(select(OntogenConfig).where(OntogenConfig.id == 1))
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
        self, offset: int = 0, limit: int = 20, order_by: Any = None
    ) -> tuple[list[SeedPreview], int]:
        """Return paginated seed previews — enabled and disabled.

        Default order: updated_at desc. Each preview carries its ``is_enabled``
        state so a disabled seed can be found and re-enabled.
        """
        base = select(OntogenSeed)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0

        default_order = OntogenSeed.updated_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()

        previews = [
            SeedPreview(
                seed_id=str(r.id),
                is_enabled=r.is_enabled,
                updated_at=r.updated_at,
                preview=_preview(r.body_md),
            )
            for r in rows
        ]
        return previews, total

    async def create_seed(self, body_md: str) -> OntogenSeed:
        """Create a new seed (disabled by default) and emit ONTOGEN.SEED_CREATE.

        A new seed ships ``is_enabled=False``: it does not participate in
        inference until explicitly enabled, consistent with the conf/metric
        factory default-false convention.
        """
        seed = OntogenSeed(body_md=body_md, is_enabled=False)
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
        result = await self._db.execute(select(OntogenSeed).where(OntogenSeed.id == seed_uuid))
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

    async def set_seed_enabled(self, seed_id: str, is_enabled: bool) -> OntogenSeed:
        """Enable or disable a seed and emit ONTOGEN.SEED_UPDATE.

        A disabled seed is retained and fully visible but excluded from the
        inference pipeline. Toggling is reversible both ways.
        """
        row = await self.get_seed(seed_id)
        row.is_enabled = is_enabled
        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)

        await self._record_ontogen_event(
            f"seed:{seed_id}",
            ONTOGEN_SEED_UPDATE,
            "success",
            {"seed_id": seed_id, "is_enabled": is_enabled},
        )
        return row

    async def delete_seed(self, seed_id: str) -> None:
        """Hard-delete a seed and emit ONTOGEN.SEED_DELETE."""
        row = await self.get_seed(seed_id)
        await self._db.delete(row)
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
        5. Load enabled seeds.
        6. Build LLM prompt.
        7. Load approved nodes/edges + embeddings for reuse.
        8. Call LLM, process proposals (node/edge/triple reuse, confidence scoring).
        9. If dry_run: return summary without persisting.
        10. Persist nodes/edges/triples; refresh node/edge/triple embeddings.
        11. Refresh dataset_embeddings for in-scope datasets.
        12. Emit ONTOGEN.RUN_COMPLETE.
        """
        run_id = str(uuid.uuid4())

        # Step 1: Acquire Redis SETNX guard (CAS token prevents cross-worker deletion)
        lock_token = secrets.token_urlsafe(16)
        acquired = await self._cache.set_nx(_LOCK_KEY, lock_token, ttl_seconds=_LOCK_TTL_SECONDS)
        if not acquired:
            raise ConflictError("ONTOGEN_RUNNING", "Ontogen inference is already running")

        try:
            return await self._run_inner(prompt_md=prompt_md, dry_run=dry_run, run_id=run_id)
        except ConflictError:
            raise
        except Exception as exc:
            logger.error("ontogen_run_failed", exc_info=True)
            try:
                await self._record_ontogen_event(
                    "singleton",
                    ONTOGEN_RUN_FAILED,
                    "failure",
                    {"error": str(exc), "run_id": run_id},
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
        run_id: str,
    ) -> OntogenRunSummary:
        """Inner run logic (called inside the SETNX guard)."""
        # Step 2: Load conf and resolve one-shot prompt
        conf = await self.get_conf()

        if not conf.is_enabled and not dry_run:
            raise ConflictError(
                "ONTOGEN_DISABLED",
                "Ontogen is disabled; only dry-run is permitted",
            )

        effective_prompt = prompt_md or conf.default_run_prompt

        # Step 3: Enumerate datasets matching dataset_filter
        dataset_filter = conf.dataset_filter or {}
        dataset_urns, unresolved_urns = await self._enumerate_datasets(dataset_filter)

        # Step 4: Gather evidence per dataset (best-effort)
        evidence_per_dataset: dict[str, dict[str, Any]] = {}
        for urn in dataset_urns:
            try:
                evidence_per_dataset[urn] = await gather_evidence(urn, self._datahub)
            except Exception:
                logger.warning(
                    "ontogen_evidence_gather_failed",
                    extra={"dataset_urn": urn},
                    exc_info=True,
                )

        # Step 5: Load enabled seeds
        seed_rows = (
            (await self._db.execute(select(OntogenSeed).where(OntogenSeed.is_enabled)))
            .scalars()
            .all()
        )
        seeds_md = "\n\n---\n\n".join(r.body_md for r in seed_rows)

        # Step 6: Build prompt (with per-run nonce for prompt-injection hardening)
        run_nonce = secrets.token_hex(8)
        prompt = build_run_prompt(seeds_md, evidence_per_dataset, effective_prompt, nonce=run_nonce)

        # Step 7: Load approved + pending nodes/edges for reuse. Approved rows
        # are the "carried-forward" ontology the new run can extend; pending
        # rows from prior runs also count for direct-match reuse so the
        # unique-name / unique-label DB constraints don't reject a re-proposal
        # of the same business concept before the human reviewer has acted on
        # the original pending row. Rejected rows are excluded so they aren't
        # implicitly revived.
        eligible_nodes = (
            (
                await self._db.execute(
                    select(OntogenNode).where(
                        OntogenNode.status.in_(["approved", "llm_approved", "llm_pending"])
                    )
                )
            )
            .scalars()
            .all()
        )
        eligible_edges = (
            (
                await self._db.execute(
                    select(OntogenEdge).where(
                        OntogenEdge.status.in_(["approved", "llm_approved", "llm_pending"])
                    )
                )
            )
            .scalars()
            .all()
        )
        approved_triples = (
            (
                await self._db.execute(
                    select(OntogenTriple).where(
                        OntogenTriple.status.in_(["approved", "llm_approved", "llm_pending"])
                    )
                )
            )
            .scalars()
            .all()
        )

        approved_nodes = [n for n in eligible_nodes if n.status in {"approved", "llm_approved"}]
        approved_edges = [e for e in eligible_edges if e.status in {"approved", "llm_approved"}]

        existing_node_ids: set[str] = {n.id for n in eligible_nodes}
        existing_edge_ids: set[str] = {e.id for e in eligible_edges}
        existing_triple_ids: set[str] = {t.id for t in approved_triples}

        # Name/label → id lookup for direct-match reuse (approved + pending).
        # Approved entries shadow pending entries when both exist with the same
        # name/label (shouldn't happen given the unique constraints, but is the
        # safer precedence).
        node_name_to_id: dict[str, str] = {
            n.name: n.id for n in eligible_nodes if n.status == "llm_pending"
        }
        node_name_to_id.update({n.name: n.id for n in approved_nodes})
        edge_label_to_id: dict[str, str] = {
            e.label: e.id for e in eligible_edges if e.status == "llm_pending"
        }
        edge_label_to_id.update({e.label: e.id for e in approved_edges})

        # Step 8: Run adversarial Producer/Reviewer debate loop
        run_at_iso = datetime.now(tz=UTC).isoformat()
        in_scope_urns = frozenset(evidence_per_dataset.keys())
        validate_tool = build_ontogen_validate_tool(in_scope_urns)
        review_tool = build_ontogen_review_tool()

        rc = await get_runtime_config(self._db)

        # Infra exceptions propagate to run()'s outer handler which emits RUN_FAILED.
        debate_result = await run_debate(
            llm=self._llm,
            vector=self._vector,
            db=self._db,
            producer_prompt=prompt,
            validate_tool=validate_tool,
            review_tool=review_tool,
            in_scope_urns=in_scope_urns,
            max_turns=rc.ontogen_debate_max_turns,
            rag_k=rc.ontogen_debate_rag_k,
            reviewer_model=rc.ontogen_debate_reviewer_model,
            llm_provider=rc.llm_provider,
            llm_base_model=rc.llm_model,
            producer_schema=OntogenLLMOutput,
            producer_max_iterations=rc.ontogen_llm_max_iterations,
            run_id=run_id,
            stub_llm_client=rc.stub_llm_client,
        )

        try:
            candidate_output = OntogenLLMOutput.model_validate(debate_result.payload)
        except PydanticValidationError:
            logger.warning(
                "ontogen_llm_output_validation_failed",
                extra={"run_nonce": run_nonce},
            )
            candidate_output = OntogenLLMOutput(nodes=[], edges=[], triples=[])

        # Use producer inner-loop trace for validation metrics
        producer_iterations = debate_result.transcript.get("producer_iterations", 1)
        producer_errors_dropped = debate_result.transcript.get("producer_errors_dropped", 0)

        # Partition out rows that still have validation errors after the final producer turn
        final_validation_errors: list[ValidationError] = []
        llm_run_result, dropped_count = partition_clean_rows(
            candidate_output, final_validation_errors
        )

        logger.info(
            "ontogen_llm_loop_complete",
            extra={
                "run_nonce": run_nonce,
                "producer_iterations": producer_iterations,
                "producer_errors_dropped": producer_errors_dropped,
                "debate_outcome": debate_result.outcome,
            },
        )

        proposed_nodes: list[OntogenLLMNode] = llm_run_result.nodes
        proposed_edges: list[OntogenLLMEdge] = llm_run_result.edges
        proposed_triples: list[OntogenLLMTriple] = llm_run_result.triples

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
            final_id = make_snake_id(name, all_known_ids)

            # Direct-match reuse first — if a node with this exact name already
            # exists (approved or pending), reuse its id. Cheaper than embedding
            # search and avoids the unique_name DB-constraint violation that
            # would otherwise fire when the LLM re-proposes a concept whose
            # pending row from a prior run hasn't been reviewed yet.
            reused_id: str | None = node_name_to_id.get(name)

            # Fall back to embedding-based reuse for semantic matches across
            # *different* names (e.g. "Customer" vs "Buyer"). Only approved
            # rows are indexed, so this is a no-op for pending rows.
            if reused_id is None:
                try:
                    embed_text = f"{name} {description}"
                    query_vec = await self._llm.embed(embed_text)
                    hits = await _search_node_embeddings(self._vector, query_vec, top_k=5)
                    if hits:
                        reused_id = hits[0].dataset_urn  # dataset_urn stores node_id here
                except Exception:
                    logger.warning(
                        "ontogen_node_embedding_search_failed",
                        extra={"node_name": name},
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
                    "status": _status_for_outcome(confidence, debate_result.outcome),
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
            slugged_label = make_snake_id(label, all_known_edge_ids)

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
                    "status": _status_for_outcome(edge_confidence, debate_result.outcome),
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
                assert_node_id(subj_id)
                assert_edge_id(edge_id_val)
                assert_node_id(obj_id)
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
                    "status": _status_for_outcome(triple_confidence, debate_result.outcome),
                    "run_at": run_at_iso,
                }
            )

        # Step 9: dry_run short-circuit — emit RUN_COMPLETE before returning
        if dry_run:
            counts_dict = {
                "nodes_proposed": len(nodes_to_upsert),
                "edges_proposed": len(edges_to_upsert),
                "triples_proposed": len(triples_to_upsert),
            }
            summary = OntogenRunSummary(
                status="success",
                dry_run=True,
                unresolved_urns=unresolved_urns,
                counts=counts_dict,
            )
            await self._record_ontogen_event(
                "singleton",
                ONTOGEN_RUN_COMPLETE,
                "success",
                {
                    "run_id": run_id,
                    "unresolved_urns": unresolved_urns,
                    "counts": counts_dict,
                    "dry_run": True,
                    "producer_iterations": producer_iterations,
                    "producer_errors_dropped": producer_errors_dropped,
                    "debate_outcome": debate_result.outcome,
                },
            )
            return summary

        # Step 10: Persist nodes
        nodes_added = 0
        for n in nodes_to_upsert:
            node_id = n["id"]

            # Fix #3: defence-in-depth slug check before any DB write
            try:
                assert_node_id(node_id)
            except ValueError:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "invalid_node_id_at_persist", "node_id": node_id},
                )
                continue

            # Fix #12: compact evidence JSONB
            _node_reviewer_verdicts = [
                iv
                for iv in (debate_result.transcript.get("item_verdicts") or [])
                if iv.get("item_kind") == "node" and iv.get("item_id") == node_id
            ]
            _node_evidence: dict[str, Any] = {
                "datasets": [u[:1024] for u in (n.get("dataset_urns") or [])],
                "run_at": n.get("run_at", ""),
                "debate": debate_result.transcript,
                "reviewer_verdicts": _node_reviewer_verdicts,
            }

            if n["is_reuse"]:
                # An approved or pending node already exists with this name or
                # by embedding-similarity. Leave its row content + evidence
                # untouched; only refresh DatasetNodeMap so the new run's
                # dataset coverage merges with whatever was there before.
                await self._upsert_dataset_node_maps(
                    node_id, n.get("dataset_urns") or [], n["confidence_score"]
                )
                continue

            existing = (
                await self._db.execute(select(OntogenNode).where(OntogenNode.id == node_id))
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
                assert_edge_id(edge_id)
            except ValueError:
                logger.warning(
                    "ontogen_invalid_llm_proposal_skipped",
                    extra={"reason": "invalid_edge_id_at_persist", "edge_id": edge_id},
                )
                continue

            # Fix #12: compact evidence JSONB
            _edge_reviewer_verdicts = [
                iv
                for iv in (debate_result.transcript.get("item_verdicts") or [])
                if iv.get("item_kind") == "edge" and iv.get("item_id") == edge_id
            ]
            _edge_evidence: dict[str, Any] = {
                "run_at": e.get("run_at", ""),
                "debate": debate_result.transcript,
                "reviewer_verdicts": _edge_reviewer_verdicts,
            }

            existing_edge: OntogenEdge | None = (
                await self._db.execute(select(OntogenEdge).where(OntogenEdge.id == edge_id))
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
                await self._db.execute(select(OntogenEdge).where(OntogenEdge.id == t["edge_id"]))
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
                await self._db.execute(select(OntogenTriple).where(OntogenTriple.id == t["id"]))
            ).scalar_one_or_none()

            if existing_triple is None:
                # Fix #12: compact evidence JSONB for triple
                _triple_reviewer_verdicts = [
                    iv
                    for iv in (debate_result.transcript.get("item_verdicts") or [])
                    if iv.get("item_kind") == "triple" and iv.get("item_id") == t["id"]
                ]
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
                    "debate": debate_result.transcript,
                    "reviewer_verdicts": _triple_reviewer_verdicts,
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

        await self._db.commit()

        # Step 10 (cont): Refresh embeddings for new/changed nodes, edges, triples
        await self._refresh_node_embeddings(nodes_to_upsert)
        await self._refresh_edge_embeddings(edges_to_upsert)
        await self._refresh_triple_embeddings(triples_to_upsert)

        # Step 11: Refresh dataset_embeddings for in-scope datasets
        await self._refresh_dataset_embeddings(dataset_urns)

        # Step 12: Emit ONTOGEN.RUN_COMPLETE
        counts_dict = {
            "nodes_added": nodes_added,
            "edges_added": edges_added,
            "triples_added": triples_added,
        }
        summary = OntogenRunSummary(
            status="success",
            dry_run=False,
            unresolved_urns=unresolved_urns,
            counts=counts_dict,
        )
        await self._record_ontogen_event(
            "singleton",
            ONTOGEN_RUN_COMPLETE,
            "success",
            {
                "run_id": run_id,
                "unresolved_urns": unresolved_urns,
                "counts": counts_dict,
                "dry_run": False,
                "producer_iterations": producer_iterations,
                "producer_errors_dropped": producer_errors_dropped,
                "debate_outcome": debate_result.outcome,
            },
        )
        return summary

    # ── Node reads ────────────────────────────────────────────────────────────

    async def list_nodes(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[OntogenNode], int]:
        base = select(OntogenNode)
        if status_filter:
            base = base.where(OntogenNode.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        default_order = OntogenNode.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_node(self, node_id: str) -> OntogenNode:
        cached = await self._cache.get(f"ontogen:node:{node_id}")
        if cached:
            # Return ORM row freshly from DB (cache just signals hot path)
            pass

        result = await self._db.execute(select(OntogenNode).where(OntogenNode.id == node_id))
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
        }

    async def list_node_events(
        self,
        node_id: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import NODE_PREFIX

        return await self._list_events(
            "node",
            node_id,
            NODE_PREFIX,
            offset,
            limit,
            from_dt=from_dt,
            to_dt=to_dt,
            order_by=order_by,
        )

    # ── Edge reads ────────────────────────────────────────────────────────────

    async def list_edges(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[OntogenEdge], int]:
        base = select(OntogenEdge)
        if status_filter:
            base = base.where(OntogenEdge.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        default_order = OntogenEdge.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_edge(self, edge_id: str) -> OntogenEdge:
        result = await self._db.execute(select(OntogenEdge).where(OntogenEdge.id == edge_id))
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
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import EDGE_PREFIX

        return await self._list_events(
            "edge",
            edge_id,
            EDGE_PREFIX,
            offset,
            limit,
            from_dt=from_dt,
            to_dt=to_dt,
            order_by=order_by,
        )

    # ── Triple reads ──────────────────────────────────────────────────────────

    async def list_triples(
        self,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 20,
        order_by: Any = None,
    ) -> tuple[list[OntogenTriple], int]:
        base = select(OntogenTriple)
        if status_filter:
            base = base.where(OntogenTriple.status == status_filter)
        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar() or 0
        default_order = OntogenTriple.created_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._db.execute(rows_q)).scalars().all()
        return list(rows), total

    async def get_triple(self, triple_id: str) -> OntogenTriple:
        result = await self._db.execute(select(OntogenTriple).where(OntogenTriple.id == triple_id))
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
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        from src.shared.events import TRIPLE_PREFIX

        return await self._list_events(
            "triple",
            triple_id,
            TRIPLE_PREFIX,
            offset,
            limit,
            from_dt=from_dt,
            to_dt=to_dt,
            order_by=order_by,
        )

    # ── Reviews ───────────────────────────────────────────────────────────────

    async def review_node(
        self,
        node_id: str,
        verdict: str,
        reason: str | None = None,
    ) -> OntogenNode:
        """Approve or reject a node.

        On approval: status mutation and dataset_node_map status update only.
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
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)

            # Fix #2: set status='approved' on all DatasetNodeMap rows for this node
            maps = (
                (
                    await self._db.execute(
                        select(DatasetNodeMap).where(DatasetNodeMap.node_id == node_id)
                    )
                )
                .scalars()
                .all()
            )
            for dm in maps:
                dm.status = "approved"
                self._db.add(dm)

            await self._db.commit()
            await self._db.refresh(row)

            event_type = NODE_APPROVE
        else:
            row.status = "rejected"
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)

            # Fix #2: set status='rejected' on all DatasetNodeMap rows for this node
            maps = (
                (
                    await self._db.execute(
                        select(DatasetNodeMap).where(DatasetNodeMap.node_id == node_id)
                    )
                )
                .scalars()
                .all()
            )
            for dm in maps:
                dm.status = "rejected"
                self._db.add(dm)

            await self._db.commit()
            await self._db.refresh(row)
            event_type = NODE_REJECT

        await self._record_review_event("node", node_id, event_type, verdict, reason)
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

        await self._record_review_event("edge", edge_id, event_type, verdict, reason)
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
                await self._db.execute(select(OntogenEdge).where(OntogenEdge.id == row.edge_id))
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

            event_type = TRIPLE_APPROVE
        else:
            row.status = "rejected"
            row.updated_at = datetime.now(tz=UTC)
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
            event_type = TRIPLE_REJECT

        await self._record_review_event("triple", triple_id, event_type, verdict, reason)
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
        scope = await resolve_dataset_scope(
            self._datahub, dataset_filter, swallow_enumerate_errors=True
        )
        return scope.resolved_urns, scope.unresolved_urns

    async def _refresh_node_embeddings(self, nodes_to_upsert: list[dict[str, Any]]) -> None:
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

    async def _refresh_edge_embeddings(self, edges_to_upsert: list[dict[str, Any]]) -> None:
        """Embed and upsert edge_embeddings for new/changed edges (best-effort)."""
        for e in edges_to_upsert:
            edge_id = e["id"]
            label = e.get("label", "")
            semantics = e.get("semantics") or ""
            if not label:
                continue
            try:
                embed_text = f"{label} {semantics}".strip()
                vec = await self._llm.embed(embed_text)
                await _upsert_edge_embedding(self._vector, edge_id, vec, label, e["status"])
            except Exception:
                logger.warning(
                    "ontogen_edge_embedding_upsert_failed",
                    extra={"edge_id": edge_id},
                    exc_info=True,
                )

    async def _refresh_triple_embeddings(self, triples_to_upsert: list[dict[str, Any]]) -> None:
        """Embed and upsert triple_embeddings for new triples (best-effort).

        Resolves subject node, edge, and object node from the DB to build
        composite embed text at refresh time.
        """
        for t in triples_to_upsert:
            triple_id = t["id"]
            try:
                subj = (
                    await self._db.execute(
                        select(OntogenNode).where(OntogenNode.id == t["subject_node_id"])
                    )
                ).scalar_one_or_none()
                edge_row = (
                    await self._db.execute(
                        select(OntogenEdge).where(OntogenEdge.id == t["edge_id"])
                    )
                ).scalar_one_or_none()
                obj = (
                    await self._db.execute(
                        select(OntogenNode).where(OntogenNode.id == t["object_node_id"])
                    )
                ).scalar_one_or_none()

                if not subj or not edge_row or not obj:
                    continue

                composite = " ".join(
                    part
                    for part in [
                        subj.name,
                        subj.description or "",
                        edge_row.label,
                        edge_row.semantics or "",
                        obj.name,
                        obj.description or "",
                    ]
                    if part
                )
                vec = await self._llm.embed(composite)
                await _upsert_triple_embedding(self._vector, triple_id, vec, t["status"])
            except Exception:
                logger.warning(
                    "ontogen_triple_embedding_upsert_failed",
                    extra={"triple_id": triple_id},
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
        ``status='llm_approved'``; otherwise ``status='llm_pending'``.
        """
        if not dataset_urns:
            return
        status = "llm_approved" if confidence >= ONTOLOGY_CONFIDENCE_THRESHOLD else "llm_pending"
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

    async def list_global_events(
        self,
        entity_type: str,
        entity_id: str,
        event_prefix: str,
        offset: int,
        limit: int,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Public method for listing events — used by routers."""
        return await self._list_events(
            entity_type,
            entity_id,
            event_prefix,
            offset,
            limit,
            from_dt=from_dt,
            to_dt=to_dt,
            order_by=order_by,
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
        order_by: Any = None,
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

        default_order = Event.occurred_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
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
        VALUES (:node_id, CAST(:embedding AS vector), :name, :status, CAST(:updated_at AS timestamptz))
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
                    "updated_at": datetime.now(tz=UTC),
                },
            )


# ── pgvector helpers for edge_embeddings table ────────────────────────────────


async def _upsert_edge_embedding(
    vector: PgVectorManager,
    edge_id: str,
    embedding: list[float],
    label: str,
    status: str,
) -> None:
    """Upsert a row in edge_embeddings for *edge_id*."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
    sql = text(
        """
        INSERT INTO dataspoke.edge_embeddings (edge_id, embedding, label, status, updated_at)
        VALUES (:edge_id, CAST(:embedding AS vector), :label, :status, CAST(:updated_at AS timestamptz))
        ON CONFLICT (edge_id) DO UPDATE SET
            embedding  = EXCLUDED.embedding,
            label      = EXCLUDED.label,
            status     = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at
        """
    )
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                sql,
                {
                    "edge_id": edge_id,
                    "embedding": vector_literal,
                    "label": label,
                    "status": status,
                    "updated_at": datetime.now(tz=UTC),
                },
            )


# ── pgvector helpers for triple_embeddings table ──────────────────────────────


async def _upsert_triple_embedding(
    vector: PgVectorManager,
    triple_id: str,
    embedding: list[float],
    status: str,
) -> None:
    """Upsert a row in triple_embeddings for *triple_id*."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"
    sql = text(
        """
        INSERT INTO dataspoke.triple_embeddings (triple_id, embedding, status, updated_at)
        VALUES (:triple_id, CAST(:embedding AS vector), :status, CAST(:updated_at AS timestamptz))
        ON CONFLICT (triple_id) DO UPDATE SET
            embedding  = EXCLUDED.embedding,
            status     = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at
        """
    )
    async with vector._session_factory() as session:
        async with session.begin():
            await session.execute(
                sql,
                {
                    "triple_id": triple_id,
                    "embedding": vector_literal,
                    "status": status,
                    "updated_at": datetime.now(tz=UTC),
                },
            )
