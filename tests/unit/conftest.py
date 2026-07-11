"""Shared fixtures for all DataSpoke unit tests.

Provides common infrastructure mock fixtures used across api/, backend/,
shared/, and workflows/ test suites, plus the query-routing fake-session helper
that dispatches mocked ``db.execute`` results by inspecting the SQL each query
compiles to (rather than a brittle call-ordered ``side_effect=[...]`` list).
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

# ── Query-routing fake session ───────────────────────────────────────────────
#
# Rule (spec/TESTING.md §Unit Testing → Mocking rules): do NOT drive a mocked
# ``db.execute(...)`` with a positional ``side_effect=[...]`` list ordered by call
# sequence. Route results by the SQL/statement the query compiles to instead, so a
# reordered / added / short-circuited query cannot silently shift every downstream
# result. This is the shared, generalized form of the copyable exemplar in
# ``tests/unit/backend/ingestion/test_service.py`` (``_wire_fake_session``).

_UNSET = object()

Matcher = str | Callable[[str], bool]


def compiled_sql(stmt: object) -> str:
    """Compile a SQLAlchemy statement to lowercased PostgreSQL SQL text.

    Bound parameters are inlined (``literal_binds``) when possible so matchers can
    route on the *entity identity* a query carries (e.g. a specific ``dataset_urn``),
    not just the table/columns. Falls back to a param-less compile, then to
    ``str(stmt)`` for objects that do not compile (e.g. already-text statements).
    Lowercasing lets matchers use case-insensitive substrings.

    Caveat: only key matchers on string-literal identity (URNs, names, status
    strings) or table/column names — those inline reliably. A matcher keyed on a
    non-inline-able bound value (UUID, JSONB, array) may fall through to the
    param-less compile, dropping the key from the SQL and silently misrouting.
    """
    dialect = postgresql.dialect()
    try:
        return str(
            stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True})  # type: ignore[attr-defined]
        ).lower()
    except Exception:
        pass
    try:
        return str(stmt.compile(dialect=dialect)).lower()  # type: ignore[attr-defined]
    except Exception:
        return str(stmt).lower()


def route_db_execute(
    db: AsyncMock,
    routes: list[tuple[Matcher, Any]],
    *,
    default: Any = _UNSET,
) -> None:
    """Wire ``db.execute`` to return results by inspecting each statement's SQL.

    ``routes`` is an ordered list of ``(matcher, result)``:

    - ``matcher`` is either a substring (tested case-insensitively against the
      compiled SQL) or a predicate ``(sql: str) -> bool``. The FIRST matching route
      supplies the result. Route order is only a tie-break for overlapping matchers,
      never a call-sequence encoding.
    - ``result`` is returned as-is, UNLESS it is a ``list`` — then the list is a
      per-route queue consumed front-to-back on successive matches (the last element
      sticks once exhausted). Use this for the one legitimate ordered case: the SAME
      query re-run within one operation returning an evolving result (e.g. an upsert's
      pre-insert miss then post-insert hit), scoped to that query signature rather
      than to the global call order.
    - a ``result`` (or queue element) that is a ``BaseException`` instance is *raised*
      instead of returned — models a query that errors (e.g. a cypher call the code
      wraps), keyed to the statement rather than to call position.

    ``default`` is returned when no route matches; omit it to make an unrouted query
    raise ``AssertionError`` loudly (surfacing an unexpected extra query).
    """
    queues = [(m, list(r) if isinstance(r, list) else r) for m, r in routes]

    async def _execute(stmt: object, *args: object, **kwargs: object) -> Any:
        sql = compiled_sql(stmt)
        for matcher, result in queues:
            hit = matcher(sql) if callable(matcher) else matcher.lower() in sql
            if hit:
                if isinstance(result, list):
                    value = result.pop(0) if len(result) > 1 else result[0]
                else:
                    value = result
                if isinstance(value, BaseException):
                    raise value
                return value
        if default is _UNSET:
            raise AssertionError(
                f"route_db_execute: no route matched the executed SQL:\n{sql}"
            )
        return default

    db.execute = AsyncMock(side_effect=_execute)


@pytest.fixture
def datahub():
    """Mock DataHub client — no real GMS connection."""
    return AsyncMock()


@pytest.fixture
def db():
    """Mock async DB session — no real PostgreSQL connection.

    `spec=AsyncSession` keeps sync methods (add, delete, merge) as sync
    MagicMocks so `db.add(x)` doesn't return an un-awaited coroutine.
    """
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def cache():
    """Mock Redis client — no real Redis connection."""
    return AsyncMock()


@pytest.fixture
def llm():
    """Mock LLM client — no real LLM API calls."""
    return AsyncMock()


@pytest.fixture
def vector():
    """Mock PgVectorManager — no real pgvector DB connection."""
    return AsyncMock()
