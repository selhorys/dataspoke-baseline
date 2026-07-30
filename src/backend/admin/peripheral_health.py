"""Peripheral liveness service — the last self-report of a peripheral's connection.

``peripheral_config.is_configured`` states only that settings are present; a wrong
SASL mechanism or an unauthorized IAM role produces a fully "configured" DataHub
peripheral whose event consumer never connects.  Long-running connection holders
report their outcome here and ``GET /admin/peripherals/datahub`` reads it back.

Rows are keyed per **transport**: ``datahub`` is DataHub's Kafka event stream and
``datahub-api`` its GMS metadata API, reported by the event consumer and the
hourly sync sweep respectively.

Absence of a row and ``status='unknown'`` mean the same thing to readers: nothing
has reported yet — which covers every deployment that runs no consumer at all.

Public surface:
    get_peripheral_health(db, name) -> PeripheralHealthDTO
    report_peripheral_health(db, name, status, error=None) -> PeripheralHealthDTO
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.models import PeripheralHealth
from src.shared.redaction import sanitize_error_message

HEALTH_STATUSES: set[str] = {"unknown", "ok", "error"}

# Failure messages are operator-facing and land in an HTTP response; cap them so a
# verbose librdkafka error cannot bloat the row or the payload.
_MAX_ERROR_LENGTH = 1024


@dataclass(frozen=True)
class PeripheralHealthDTO:
    status: str
    last_error: str | None
    last_ok_at: datetime | None
    updated_at: datetime | None


UNKNOWN_HEALTH = PeripheralHealthDTO(
    status="unknown", last_error=None, last_ok_at=None, updated_at=None
)


def _row_to_dto(row: PeripheralHealth) -> PeripheralHealthDTO:
    return PeripheralHealthDTO(
        status=row.status,
        last_error=row.last_error,
        last_ok_at=row.last_ok_at,
        updated_at=row.updated_at,
    )


async def get_peripheral_health(db: AsyncSession, name: str) -> PeripheralHealthDTO:
    """Return the health row for *name*, or the ``unknown`` sentinel when absent."""
    result = await db.execute(select(PeripheralHealth).where(PeripheralHealth.name == name))
    row = result.scalar_one_or_none()
    return _row_to_dto(row) if row is not None else UNKNOWN_HEALTH


async def report_peripheral_health(
    db: AsyncSession,
    name: str,
    status: str,
    error: str | None = None,
) -> PeripheralHealthDTO:
    """Upsert the health row for *name*.

    ``ok`` stamps ``last_ok_at`` and clears ``last_error``; ``error`` records the
    message and leaves the previous ``last_ok_at`` intact so a reader can see how
    long ago the peripheral last worked.

    ``last_error`` is bounded and credential-free as a property of the *table*,
    not of any one reporter: every writer funnels through here, so the redaction
    and the length cap are applied at this single choke point. A reporter that
    holds the live credential (the event consumer, ``DataHubClient``) additionally
    scrubs it by exact value before calling — a strictly stronger control than the
    pattern layer, but one only that reporter can apply.
    """
    if status not in HEALTH_STATUSES:
        raise ValueError(f"Unknown peripheral health status: {status!r}")

    now = datetime.now(tz=UTC)
    fields: dict[str, Any] = {"status": status, "updated_at": now}
    if status == "ok":
        fields["last_ok_at"] = now
        fields["last_error"] = None
    else:
        sanitized = sanitize_error_message(error) or ""
        fields["last_error"] = sanitized[:_MAX_ERROR_LENGTH] or None

    stmt = (
        pg_insert(PeripheralHealth)
        .values(name=name, **fields)
        .on_conflict_do_update(index_elements=["name"], set_=fields)
    )
    await db.execute(stmt)
    await db.commit()

    # populate_existing refreshes any identity-map instance with the row the Core
    # upsert just wrote; without it the expire_on_commit=False session hands back
    # the stale pre-upsert object.
    result = await db.execute(
        select(PeripheralHealth)
        .where(PeripheralHealth.name == name)
        .execution_options(populate_existing=True)
    )
    return _row_to_dto(result.scalar_one())
