"""Runtime configuration service — DB-backed singleton tunables.

``RUNTIME_CONFIG_DEFAULTS`` is the single source of truth for the production
factory defaults, shared by the ORM column defaults, the lazy-seed values, and
test fixtures.
"""

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import RuntimeConfig

# ── Factory defaults ──────────────────────────────────────────────────────────

RUNTIME_CONFIG_DEFAULTS: dict[str, Any] = {
    "llm_provider": "gemini",
    "llm_model": "gemini-3.5-flash",
    "ontogen_llm_max_iterations": 3,
    "ontogen_debate_max_turns": 4,
    "ontogen_debate_rag_k": 5,
    "ontogen_debate_reviewer_model": None,
    "metagen_llm_max_iterations": 3,
    "metagen_debate_max_turns": 4,
    "metagen_debate_rag_k": 5,
    "metagen_debate_reviewer_model": None,
    "metagen_confidence_threshold": 0.7,
    "metagen_ontology_rag_node_k": 5,
    "metagen_ontology_rag_edge_k": 5,
    "metagen_ontology_rag_triple_k": 5,
    "validation_score_n_intervals": 3,
}

# ── DTO ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeConfigDTO:
    llm_provider: str
    llm_model: str
    ontogen_llm_max_iterations: int
    ontogen_debate_max_turns: int
    ontogen_debate_rag_k: int
    ontogen_debate_reviewer_model: str | None
    metagen_llm_max_iterations: int
    metagen_debate_max_turns: int
    metagen_debate_rag_k: int
    metagen_debate_reviewer_model: str | None
    metagen_confidence_threshold: float
    metagen_ontology_rag_node_k: int
    metagen_ontology_rag_edge_k: int
    metagen_ontology_rag_triple_k: int
    validation_score_n_intervals: int

    @classmethod
    def from_orm(cls, row: RuntimeConfig) -> "RuntimeConfigDTO":
        return cls(
            llm_provider=row.llm_provider,
            llm_model=row.llm_model,
            ontogen_llm_max_iterations=row.ontogen_llm_max_iterations,
            ontogen_debate_max_turns=row.ontogen_debate_max_turns,
            ontogen_debate_rag_k=row.ontogen_debate_rag_k,
            ontogen_debate_reviewer_model=row.ontogen_debate_reviewer_model,
            metagen_llm_max_iterations=row.metagen_llm_max_iterations,
            metagen_debate_max_turns=row.metagen_debate_max_turns,
            metagen_debate_rag_k=row.metagen_debate_rag_k,
            metagen_debate_reviewer_model=row.metagen_debate_reviewer_model,
            metagen_confidence_threshold=row.metagen_confidence_threshold,
            metagen_ontology_rag_node_k=row.metagen_ontology_rag_node_k,
            metagen_ontology_rag_edge_k=row.metagen_ontology_rag_edge_k,
            metagen_ontology_rag_triple_k=row.metagen_ontology_rag_triple_k,
            validation_score_n_intervals=row.validation_score_n_intervals,
        )


# ── Process-level cache ───────────────────────────────────────────────────────

_CACHE_TTL_SECONDS: float = 30.0

# (cached_dto, expires_at_monotonic)
_cache: tuple[RuntimeConfigDTO, float] | None = None


def invalidate_runtime_config_cache() -> None:
    """Evict the process-level runtime-config cache entry."""
    global _cache
    _cache = None


# ── Service functions ─────────────────────────────────────────────────────────


async def get_runtime_config(db: AsyncSession) -> RuntimeConfigDTO:
    """Return the singleton RuntimeConfig row as a DTO, creating it if absent.

    Uses a short-TTL process-level cache so repeated calls within a single
    Airflow activity task don't round-trip to the DB on every invocation.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None:
        cached_dto, expires_at = _cache
        if now < expires_at:
            return cached_dto

    result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        try:
            row = RuntimeConfig(id=1, **RUNTIME_CONFIG_DEFAULTS)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await db.rollback()
            result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
            row = result.scalar_one()

    dto = RuntimeConfigDTO.from_orm(row)
    _cache = (dto, now + _CACHE_TTL_SECONDS)
    return dto


async def patch_runtime_config(db: AsyncSession, **partial: Any) -> RuntimeConfigDTO:
    """Apply a partial update to the singleton RuntimeConfig row.

    Only keys present in ``partial`` are written; None values are skipped
    (callers exclude_unset before calling). Bound validation is enforced at
    the API schema layer (Pydantic Field constraints); this function trusts
    already-validated input.

    Commits the session and refreshes the process-level cache with the new value.
    """
    global _cache
    result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        try:
            row = RuntimeConfig(id=1, **RUNTIME_CONFIG_DEFAULTS)
            db.add(row)
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(select(RuntimeConfig).where(RuntimeConfig.id == 1))
            row = result.scalar_one()

    for field, value in partial.items():
        if hasattr(row, field):
            setattr(row, field, value)

    await db.commit()
    await db.refresh(row)

    dto = RuntimeConfigDTO.from_orm(row)
    _cache = (dto, time.monotonic() + _CACHE_TTL_SECONDS)
    return dto
