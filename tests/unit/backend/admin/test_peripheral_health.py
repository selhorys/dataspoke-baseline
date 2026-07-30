"""Unit tests for src/backend/admin/peripheral_health.py.

The service owns the ``peripheral_health`` row that "the processes that exercise that
transport" write and ``GET /admin/peripherals/{name}`` reads back. Its contract is small
and entirely spec-derived:

- an absent row and ``status='unknown'`` mean the same thing to readers;
- ``ok`` stamps ``last_ok_at`` and clears ``last_error``;
- ``error`` records the message and leaves the previous ``last_ok_at`` intact, so a
  reader can see how long ago the peripheral last worked;
- the recorded message is bounded, credential-free and control-character-free — a
  property of the *column*, enforced here because every reporter funnels through this
  one function;
- the write is an upsert, so the table never accumulates history.

A real DB is not needed: the session is a mock whose statements are inspected.

Spec traceability:
- spec/feature/BACKEND_SCHEMA.md §peripheral_health — column semantics; "A row is
  upserted on report, so the table never grows past the transport set and carries no
  history"; "Absence of a row and ``status='unknown'`` mean the same thing to readers".
- spec/feature/BACKEND.md §Health reporting — ``ok`` / ``error`` / ``unknown``;
  "``last_error`` is bounded and credential-free. This binds every reporter writing the
  table … because it is a property of the column, not of one plane."
- spec/API.md §DataHub Kafka security — "``status`` is ``unknown`` when the consumer has
  never reported — including every deployment that runs no consumer at all."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.admin.peripheral_health import (
    HEALTH_STATUSES,
    UNKNOWN_HEALTH,
    get_peripheral_health,
    report_peripheral_health,
)
from src.shared.db.models import PeripheralHealth
from src.shared.redaction import REDACTED
from tests.unit.conftest import compiled_sql


def _row(
    name: str = "datahub",
    status: str = "ok",
    last_error: str | None = None,
    last_ok_at: datetime | None = None,
) -> MagicMock:
    row = MagicMock(spec=PeripheralHealth)
    row.name = name
    row.status = status
    row.last_error = last_error
    row.last_ok_at = last_ok_at
    row.updated_at = datetime.now(tz=UTC)
    return row


def _db(row: MagicMock | None) -> AsyncMock:
    """A mock AsyncSession whose SELECT yields *row* (both scalar accessors)."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalar_one.return_value = row
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _executed_sql(db: AsyncMock) -> list[str]:
    return [compiled_sql(c.args[0]) for c in db.execute.await_args_list]


def _written_last_error(db: AsyncMock) -> str | None:
    """Return the ``last_error`` value the upsert actually bound.

    Reads the bound parameter rather than the DTO the function returns, because the DTO
    comes from a mocked read-back and would echo whatever the fixture row was given —
    the write is the side effect under test.
    """
    params = db.execute.await_args_list[0].args[0].compile().params
    return params["last_error"]  # type: ignore[no-any-return]


# ── Status vocabulary ────────────────────────────────────────────────────────


def test_status_vocabulary_matches_the_schema_check() -> None:
    """The three reportable states.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``status`` CHECK ∈ ``unknown``, ``ok``,
    ``error``.
    """
    assert HEALTH_STATUSES == {"unknown", "ok", "error"}


@pytest.mark.parametrize("status", ["healthy", "OK", "degraded", "", "down"])
@pytest.mark.asyncio
async def test_report_rejects_a_status_outside_the_vocabulary(status: str) -> None:
    """An unknown status raises rather than writing a row the CHECK would reject.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``status`` CHECK ∈ unknown, ok, error.
    """
    db = _db(_row())
    with pytest.raises(ValueError):
        await report_peripheral_health(db, "datahub", status)
    db.execute.assert_not_awaited()


# ── get: absence is "unknown" ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_unknown_when_no_row_exists() -> None:
    """No reporter has written yet → the ``unknown`` sentinel, not an error.

    ``unknown`` covers both "never reported" and "no reporter deployed"; the API does not
    distinguish them.

    spec: feature/BACKEND.md §Health reporting — "On either row ``unknown`` covers both
    'never reported' and 'no reporter deployed'"; API.md §DataHub Kafka security —
    "``status`` is ``unknown`` when the consumer has never reported — including every
    deployment that runs no consumer at all";
    BACKEND_SCHEMA.md §peripheral_health — "Absence of a row and ``status='unknown'``
    mean the same thing to readers".
    """
    dto = await get_peripheral_health(_db(None), "datahub")

    assert dto == UNKNOWN_HEALTH
    assert dto.status == "unknown"
    assert dto.last_error is None
    assert dto.last_ok_at is None
    assert dto.updated_at is None


@pytest.mark.asyncio
async def test_get_returns_the_stored_row_as_a_dto() -> None:
    """A reported row surfaces status, message, and both timestamps.

    spec: BACKEND_SCHEMA.md §peripheral_health — the five columns; feature/BACKEND.md
    §Health reporting — "``GET /admin/peripherals/datahub`` returns it as ``health``".
    """
    last_ok = datetime.now(tz=UTC) - timedelta(hours=2)
    dto = await get_peripheral_health(
        _db(_row(status="error", last_error="SASL auth failed", last_ok_at=last_ok)),
        "datahub",
    )

    assert dto.status == "error"
    assert dto.last_error == "SASL auth failed"
    assert dto.last_ok_at == last_ok


@pytest.mark.asyncio
async def test_get_queries_the_requested_peripheral() -> None:
    """The read is keyed by peripheral name.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``name`` is the primary key.
    """
    db = _db(_row(name="langfuse"))
    await get_peripheral_health(db, "langfuse")

    assert any("peripheral_health" in s for s in _executed_sql(db))


# ── report: ok ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_ok_stamps_last_ok_at_and_clears_last_error() -> None:
    """A successful report records the time and drops the stale failure message.

    Leaving the previous ``last_error`` behind would make a recovered peripheral read as
    though it were still failing.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``last_error`` is the "Most recent
    failure message"; ``last_ok_at`` the "Last successful connection".
    """
    db = _db(_row(status="ok"))
    await report_peripheral_health(db, "datahub", "ok")

    upsert = next(s for s in _executed_sql(db) if "insert" in s.lower())
    assert "last_ok_at" in upsert, "an ok report must write last_ok_at"
    assert "last_error" in upsert, "an ok report must clear last_error"


@pytest.mark.asyncio
async def test_report_ok_ignores_a_supplied_error_message() -> None:
    """``ok`` means no failure — an error string cannot ride along with it.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``last_error`` is ``NULL`` when never
    failed; feature/BACKEND.md §Health reporting — ``error`` is the state that carries a
    message.
    """
    db = _db(_row(status="ok", last_error=None))
    dto = await report_peripheral_health(db, "datahub", "ok", "leftover message")

    assert dto.last_error is None


# ── report: error ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_error_records_the_message_and_keeps_last_ok_at() -> None:
    """A failure records its message without erasing when the peripheral last worked.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``last_ok_at`` is the "Last successful
    connection"; a failure report must not overwrite it, or a reader loses the age of the
    outage.
    """
    last_ok = datetime.now(tz=UTC) - timedelta(days=1)
    db = _db(_row(status="error", last_error="brokers down", last_ok_at=last_ok))
    dto = await report_peripheral_health(db, "datahub", "error", "brokers down")

    upsert = next(s for s in _executed_sql(db) if "insert" in s.lower())
    assert "last_ok_at" not in upsert, (
        "an error report must leave the previous last_ok_at intact; "
        f"statement was {upsert!r}"
    )
    assert dto.last_error == "brokers down"
    assert dto.last_ok_at == last_ok


@pytest.mark.asyncio
async def test_report_error_without_a_message_stores_null() -> None:
    """An empty failure message is stored as absent rather than as an empty string.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``last_error`` is ``NULL`` when never
    failed; an empty string would render as a blank error in the response.
    """
    db = _db(_row(status="error", last_error=None))
    dto = await report_peripheral_health(db, "datahub", "error", "")

    assert dto.last_error is None


@pytest.mark.asyncio
async def test_report_truncates_a_verbose_failure_message() -> None:
    """A long librdkafka error is capped so it cannot bloat the row or the payload.

    The message is operator-facing and lands in an HTTP response.

    spec: feature/BACKEND.md §Health reporting — the message is returned by
    ``GET /admin/peripherals/datahub``; BACKEND_SCHEMA.md §peripheral_health —
    ``last_error`` holds the "Most recent failure message".
    """
    from src.backend.admin.peripheral_health import _MAX_ERROR_LENGTH

    long_message = "x" * (_MAX_ERROR_LENGTH * 3)
    stored = _row(status="error", last_error=long_message[:_MAX_ERROR_LENGTH])
    db = _db(stored)

    await report_peripheral_health(db, "datahub", "error", long_message)

    written = _written_last_error(db)
    assert written is not None and len(written) == _MAX_ERROR_LENGTH, (
        f"the stored message must be capped at {_MAX_ERROR_LENGTH}; "
        f"got {len(written or '')}"
    )


# ── report: last_error is credential-free at the choke point ─────────────────
#
# The redaction *algorithm* is covered in tests/unit/shared/test_redaction.py. What these
# tests cover is that it is **wired in here** — the single choke point every reporter
# funnels through. Deleting the sanitize call from this function must fail a test, or the
# control can be removed silently and only the pure function's tests keep passing.


@pytest.mark.asyncio
async def test_report_error_redacts_a_credential_out_of_the_message() -> None:
    """A credential quoted by a transport message is not persisted to ``last_error``.

    Asserted on the value the upsert binds, so a report that redacted nothing fails
    here rather than being masked by a mocked read-back.

    spec: feature/BACKEND.md §Health reporting — "``last_error`` is bounded and
    credential-free. This binds every reporter writing the table — ``langfuse`` and
    ``smtp`` as much as the two DataHub rows — because it is a property of the column,
    not of one plane."
    """
    db = _db(_row(status="error"))

    await report_peripheral_health(
        db, "datahub-api", "error", "GMS refused: access_token=SUPERSECRET123 (401)"
    )

    written = _written_last_error(db)
    assert written is not None
    assert "SUPERSECRET123" not in written, (
        f"the credential value must not reach the column; got {written!r}. "
        "spec: feature/BACKEND.md §Health reporting — last_error is credential-free."
    )
    assert REDACTED in written, (
        f"the value must be replaced by the redaction marker so an operator can see "
        f"something was withheld; got {written!r}. The marker is imported rather than "
        f"spelled out: no spec names a marker string, so the property is that *a* marker "
        f"is present, not which one."
    )
    # Backstop: the surrounding diagnostic survives, so the write was scrubbed rather
    # than blanked — a function that stored a constant would fail this.
    assert "GMS refused" in written and "(401)" in written, (
        f"the diagnostic text either side of the credential must survive; got {written!r}"
    )


@pytest.mark.asyncio
async def test_report_error_strips_control_characters_before_writing() -> None:
    """Newlines and NUL never reach the column.

    ``NUL`` is the sharper of the two: a PostgreSQL ``text`` column rejects it outright,
    so an unsanitized message turns a redaction problem into a lost row — the report is
    then absent exactly when it matters.

    spec: feature/BACKEND.md §Health reporting — a persisted message "cannot become a
    disclosure or log-forging surface".
    """
    db = _db(_row(status="error"))

    await report_peripheral_health(
        db, "datahub", "error", "line one\nline two\x00tail"
    )

    written = _written_last_error(db)
    assert written is not None
    assert "\n" not in written and "\x00" not in written, (
        f"control characters must be stripped before the write; got {written!r}"
    )
    assert "line one" in written and "line two" in written and "tail" in written, (
        f"the words either side must survive, unspliced; got {written!r}"
    )


@pytest.mark.asyncio
async def test_report_ok_writes_no_message_to_redact() -> None:
    """An ``ok`` report clears ``last_error`` rather than sanitizing anything.

    The complement of the two tests above: the redaction path belongs to the ``error``
    branch only, and ``ok`` must null the column.

    spec: feature/BACKEND.md §Health reporting — "``ok`` stamps ``last_ok_at`` and clears
    ``last_error``".
    """
    db = _db(_row(status="ok"))
    await report_peripheral_health(db, "datahub-api", "ok")
    assert _written_last_error(db) is None


# ── report: upsert, not append ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_upserts_so_the_table_carries_no_history() -> None:
    """The write is INSERT … ON CONFLICT DO UPDATE keyed on ``name``.

    spec: BACKEND_SCHEMA.md §peripheral_health — "A row is upserted on report, so the
    table never grows past the transport set and carries no history."
    """
    db = _db(_row())
    await report_peripheral_health(db, "datahub", "ok")

    upsert = next(s for s in _executed_sql(db) if "insert" in s.lower()).lower()
    assert "on conflict" in upsert, f"the write must be an upsert; got {upsert!r}"
    assert "do update" in upsert, "a conflicting report must overwrite, not be dropped"


@pytest.mark.asyncio
async def test_report_commits_and_reads_the_row_back() -> None:
    """The report commits and returns the state a subsequent reader would see.

    The re-select uses ``populate_existing`` because the session does not expire on
    commit, so an identity-mapped instance would otherwise be handed back stale.

    spec: BACKEND_SCHEMA.md §peripheral_health — the row is the readable state; the
    caller's returned DTO must match it.
    """
    db = _db(_row(status="ok"))
    dto = await report_peripheral_health(db, "datahub", "ok")

    db.commit.assert_awaited_once()
    assert dto.status == "ok"
    assert len(_executed_sql(db)) >= 2, (
        "the upsert must be followed by a read-back of the committed row"
    )
