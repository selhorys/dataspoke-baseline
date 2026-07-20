"""Peripheral configuration service — DB-backed connection settings for DataHub, Langfuse, and SMTP.

Each peripheral stores its non-secret connection fields in the ``peripheral_config``
table (one row per name).  Secret fields live in dedicated K8s Secrets and are
accessed via ``datahub_secret`` / ``langfuse_secret`` / ``smtp_secret``.

Public surface:
    get_peripheral_config(db, name) -> DatahubConfigDTO | LangfuseConfigDTO | SmtpConfigDTO | None
    patch_peripheral_config(db, name, bump_kafka_sasl_password_version=False, **partial)
        -> DatahubConfigDTO | LangfuseConfigDTO | SmtpConfigDTO
    invalidate_peripheral_config_cache(name=None)
"""

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import PeripheralConfig

PERIPHERAL_NAMES: set[str] = {"datahub", "langfuse", "smtp"}

# ── DTOs ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatahubConfigDTO:
    gms_url: str
    kafka_brokers: str
    service_corpuser_urn: str = ""
    default_env: str = ""
    # Browser-facing DataHub UI URL. Distinct from ``gms_url``, which addresses
    # the GMS service and routinely differs in host, port, and scheme.
    frontend_url: str = ""
    # Kafka security tuple consumed by the DataHub event consumer.  Every field
    # defaults, so a DataHub row without these keys resolves to an unsecured
    # PLAINTEXT connection.  The matching credential
    # (``kafka_sasl_password``) lives in the K8s Secret, never here;
    # ``kafka_sasl_password_version`` is the counter that makes a Secret-only
    # rotation visible as a DB-plane change.
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_sasl_mechanism: str = ""
    kafka_sasl_username: str = ""
    kafka_aws_region: str = ""
    kafka_sasl_password_version: int = 0


@dataclass(frozen=True)
class LangfuseConfigDTO:
    host: str
    public_key: str
    project_id: str = ""
    environment_tag: str = ""


@dataclass(frozen=True)
class SmtpConfigDTO:
    host: str
    port: int
    username: str
    from_address: str
    use_tls: bool


_DTO_TYPE = DatahubConfigDTO | LangfuseConfigDTO | SmtpConfigDTO

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
            service_corpuser_urn=s.get("service_corpuser_urn", ""),
            default_env=s.get("default_env", ""),
            frontend_url=s.get("frontend_url", ""),
            kafka_security_protocol=s.get("kafka_security_protocol", "PLAINTEXT"),
            kafka_sasl_mechanism=s.get("kafka_sasl_mechanism", ""),
            kafka_sasl_username=s.get("kafka_sasl_username", ""),
            kafka_aws_region=s.get("kafka_aws_region", ""),
            kafka_sasl_password_version=int(s.get("kafka_sasl_password_version", 0)),
        )
    if row.name == "smtp":
        return SmtpConfigDTO(
            host=s.get("host", ""),
            port=int(s.get("port", 587)),
            username=s.get("username", ""),
            from_address=s.get("from_address", ""),
            use_tls=bool(s.get("use_tls", True)),
        )
    return LangfuseConfigDTO(
        host=s.get("host", ""),
        public_key=s.get("public_key", ""),
        project_id=s.get("project_id", ""),
        environment_tag=s.get("environment_tag", ""),
    )


# ── Service functions ─────────────────────────────────────────────────────────


async def get_peripheral_config(
    db: AsyncSession, name: str
) -> DatahubConfigDTO | LangfuseConfigDTO | SmtpConfigDTO | None:
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
    db: AsyncSession,
    name: str,
    *,
    bump_kafka_sasl_password_version: bool = False,
    **partial: Any,
) -> DatahubConfigDTO | LangfuseConfigDTO | SmtpConfigDTO | None:
    """Upsert the peripheral config row for *name* with the supplied fields.

    No-op when *partial* is empty and no counter bump is requested — returns the
    current config (or None when unconfigured) without creating a spurious empty
    row.  This prevents a token-only PATCH (which routes the secret out-of-band
    before the DB write) from creating a row with empty settings.

    Creates the row lazily on the first non-empty call.  Only keys present in
    *partial* are written; the existing ``settings`` JSONB is merged (shallow
    update).  Commits the session and refreshes the process-level cache.

    ``bump_kafka_sasl_password_version`` increments the DataHub rotation counter
    as a read-modify-write **inside this transaction**, over a row locked with
    ``FOR UPDATE``.  The caller cannot compute the new value itself: the API runs
    multiple replicas, each with its own 30-second config cache, so two
    concurrent rotations reading a stale ``1`` would both write ``2`` and the
    consumer would never observe a change — defeating the counter's only purpose.
    """
    if not partial and not bump_kafka_sasl_password_version:
        return await get_peripheral_config(db, name)

    stmt = select(PeripheralConfig).where(PeripheralConfig.name == name)
    if bump_kafka_sasl_password_version:
        # Serialize concurrent rotations of the same peripheral across replicas.
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    def _merge(base: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for k, v in partial.items():
            if v is not None:
                merged[k] = v
        if bump_kafka_sasl_password_version:
            merged["kafka_sasl_password_version"] = (
                int(base.get("kafka_sasl_password_version", 0) or 0) + 1
            )
        return merged

    if row is None:
        try:
            row = PeripheralConfig(name=name, settings=_merge({}))
            db.add(row)
            await db.flush()
        except IntegrityError:
            await db.rollback()
            stmt = select(PeripheralConfig).where(PeripheralConfig.name == name)
            if bump_kafka_sasl_password_version:
                stmt = stmt.with_for_update()
            result = await db.execute(stmt)
            row = result.scalar_one()
            row.settings = _merge(dict(row.settings or {}))
    else:
        row.settings = _merge(dict(row.settings or {}))

    await db.commit()
    await db.refresh(row)

    dto = _row_to_dto(row)
    _cache[name] = (dto, time.monotonic() + _CACHE_TTL_SECONDS)
    return dto
