"""Peripheral configuration service — DB-backed connection settings for DataHub and Langfuse.

Each peripheral stores its non-secret connection fields in the ``peripheral_config``
table (one row per name).  Secret fields live in dedicated K8s Secrets and are
accessed via ``datahub_secret`` / ``langfuse_secret``.

Public surface:
    get_peripheral_config(db, name) -> DatahubConfigDTO | LangfuseConfigDTO | None
    patch_peripheral_config(db, name, **partial) -> DatahubConfigDTO | LangfuseConfigDTO
    invalidate_peripheral_config_cache(name=None)
"""

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import PeripheralConfig

PERIPHERAL_NAMES: set[str] = {"datahub", "langfuse"}

# ── DTOs ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatahubConfigDTO:
    gms_url: str
    kafka_brokers: str


@dataclass(frozen=True)
class LangfuseConfigDTO:
    host: str
    public_key: str


_DTO_TYPE = DatahubConfigDTO | LangfuseConfigDTO

# ── Process-level per-name cache ──────────────────────────────────────────────

_CACHE_TTL_SECONDS: float = 30.0

# name -> (dto | None, expires_at_monotonic)
_cache: dict[str, tuple[_DTO_TYPE | None, float]] = {}


def invalidate_peripheral_config_cache(name: str | None = None) -> None:
    """Evict one or all peripheral config cache entries."""
    global _cache
    if name is None:
        _cache.clear()
    else:
        _cache.pop(name, None)


def _row_to_dto(row: PeripheralConfig) -> _DTO_TYPE:
    s = row.settings or {}
    if row.name == "datahub":
        return DatahubConfigDTO(
            gms_url=s.get("gms_url", ""),
            kafka_brokers=s.get("kafka_brokers", ""),
        )
    return LangfuseConfigDTO(
        host=s.get("host", ""),
        public_key=s.get("public_key", ""),
    )


# ── Service functions ─────────────────────────────────────────────────────────


async def get_peripheral_config(
    db: AsyncSession, name: str
) -> DatahubConfigDTO | LangfuseConfigDTO | None:
    """Return the peripheral config DTO for *name*, or None when unconfigured.

    Uses a short-TTL process-level cache so repeated calls within a single
    request or activity task avoid repeated DB round-trips.
    """
    now = time.monotonic()
    cached = _cache.get(name)
    if cached is not None:
        dto, expires_at = cached
        if now < expires_at:
            return dto

    result = await db.execute(
        select(PeripheralConfig).where(PeripheralConfig.name == name)
    )
    row = result.scalar_one_or_none()
    dto = _row_to_dto(row) if row is not None else None
    _cache[name] = (dto, now + _CACHE_TTL_SECONDS)
    return dto


async def patch_peripheral_config(
    db: AsyncSession, name: str, **partial: Any
) -> DatahubConfigDTO | LangfuseConfigDTO | None:
    """Upsert the peripheral config row for *name* with the supplied fields.

    No-op when *partial* is empty — returns the current config (or None when
    unconfigured) without creating a spurious empty row.  This prevents a
    token-only PATCH (which routes the secret out-of-band before the DB write)
    from creating a row with empty settings.

    Creates the row lazily on the first non-empty call.  Only keys present in
    *partial* are written; the existing ``settings`` JSONB is merged (shallow
    update).  Commits the session and refreshes the process-level cache.
    """
    if not partial:
        return await get_peripheral_config(db, name)

    result = await db.execute(
        select(PeripheralConfig).where(PeripheralConfig.name == name)
    )
    row = result.scalar_one_or_none()

    if row is None:
        new_settings: dict[str, Any] = {k: v for k, v in partial.items() if v is not None}
        try:
            row = PeripheralConfig(name=name, settings=new_settings)
            db.add(row)
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(PeripheralConfig).where(PeripheralConfig.name == name)
            )
            row = result.scalar_one()
            merged = dict(row.settings or {})
            for k, v in partial.items():
                if v is not None:
                    merged[k] = v
            row.settings = merged
    else:
        merged = dict(row.settings or {})
        for k, v in partial.items():
            if v is not None:
                merged[k] = v
        row.settings = merged

    await db.commit()
    await db.refresh(row)

    dto = _row_to_dto(row)
    _cache[name] = (dto, time.monotonic() + _CACHE_TTL_SECONDS)
    return dto
