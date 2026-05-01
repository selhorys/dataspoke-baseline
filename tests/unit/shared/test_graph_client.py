"""Tests for src/shared/graph/client.py — AgeGraph security and correctness.

Verifies contracts in spec/feature/BACKEND.md §Shared Services (Graph/Apache AGE row)
and spec/feature/BACKEND_SCHEMA.md §Graph: parameterized bind params prevent SQL/Cypher
injection, SET LOCAL search_path is scoped to the transaction, AGE errors are wrapped as
DataSpokeError to prevent information leakage, and traverse() enforces max_hops bounds."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.shared.exceptions import DataSpokeError
from src.shared.graph.client import AgeGraph, _assert_slug


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_session_factory(session: AsyncMock) -> MagicMock:
    """Return an async context-manager-compatible session factory mock."""
    factory = MagicMock()
    # Support `async with self._session_factory() as session`
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_session() -> AsyncMock:
    session = AsyncMock()
    # Support `async with session.begin()` — begin() must return a sync context manager mock
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=cm)
    session.execute = AsyncMock(return_value=MagicMock())
    return session


# ── _assert_slug ──────────────────────────────────────────────────────────────


def test_materialize_triple_rejects_malformed_slug_id() -> None:
    """`_assert_slug` raises ValueError for ids with uppercase or special chars."""
    with pytest.raises(ValueError, match="not a valid slug"):
        _assert_slug("Bad Id!", "test_label")


def test_assert_slug_rejects_special_chars() -> None:
    """IDs with spaces or uppercase chars are rejected."""
    with pytest.raises(ValueError):
        _assert_slug("Bad Id!", "test_label")

    with pytest.raises(ValueError):
        _assert_slug("", "test_label")


def test_assert_slug_accepts_valid_slugs() -> None:
    """Valid slug IDs pass _assert_slug without exception."""
    _assert_slug("book", "label")
    _assert_slug("has-edition", "label")
    _assert_slug("node_123", "label")
    _assert_slug("a-b-c-1", "label")


# ── materialize_triple ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_materialize_triple_uses_bind_params() -> None:
    """materialize_triple must pass cypher and params as named bind params, not interpolated."""
    session = _make_session()
    factory = _make_session_factory(session)

    age = AgeGraph(session_factory=factory)
    await age.materialize_triple(
        subject_id="book",
        edge_id="has-edition",
        object_id="edition",
        edge_label="has edition",
    )

    # Verify session.execute was called at least once after setup
    calls = session.execute.call_args_list
    # The last substantive call (after LOAD age and SET search_path) carries the SQL + params
    # Find the call that passes a dict with 'cypher' and 'params' keys
    found = False
    for c in calls:
        args, kwargs = c
        if len(args) >= 2 and isinstance(args[1], dict):
            if "cypher" in args[1] and "params" in args[1]:
                found = True
                # Ensure SQL text uses :cypher and :params placeholders
                sql_text = str(args[0])
                assert ":cypher" in sql_text
                assert ":params" in sql_text
                # Must NOT have inline JSON literal without parameter binding
                assert '::agtype' not in str(args[1])  # params dict values aren't cast inline
    assert found, "No execute call with 'cypher' and 'params' bind params found"


@pytest.mark.asyncio
async def test_set_local_search_path_used() -> None:
    """_setup_age must use SET LOCAL search_path (not plain SET search_path)."""
    session = _make_session()
    factory = _make_session_factory(session)

    age = AgeGraph(session_factory=factory)
    await age.materialize_triple(
        subject_id="book",
        edge_id="has-edition",
        object_id="edition",
        edge_label="has edition",
    )

    # Inspect all SQL strings passed to execute
    all_sql = " | ".join(str(c.args[0]) for c in session.execute.call_args_list)
    assert "SET LOCAL search_path" in all_sql


@pytest.mark.asyncio
async def test_age_failures_wrapped_as_dataspoke_error() -> None:
    """When the cypher execute raises, AgeGraph must wrap as DataSpokeError('AGE_ERROR...')."""
    session = _make_session()
    # LOAD age + SET LOCAL search_path succeed; cypher execute raises
    raw_err = RuntimeError("pg: syntax error in cypher query at position 42")
    # The session.begin() context manager itself can raise to trigger the except block
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)

    async def _aexit_raise(exc_type, exc_val, exc_tb):
        return False

    cm.__aexit__ = AsyncMock(side_effect=_aexit_raise)
    session.begin = MagicMock(return_value=cm)
    session.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(), raw_err])
    factory = _make_session_factory(session)

    age = AgeGraph(session_factory=factory)
    with pytest.raises(DataSpokeError) as exc_info:
        await age.materialize_triple(
            subject_id="book",
            edge_id="has-edition",
            object_id="edition",
            edge_label="has edition",
        )

    err = exc_info.value
    msg = str(err)
    assert "AGE_ERROR" in msg
    # Raw exception text must NOT appear in the wrapped message (info-leak protection)
    assert "syntax error" not in msg
    assert "position 42" not in msg


# ── delete_triple ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_triple_uses_bind_params() -> None:
    """delete_triple passes cypher + params as named bind params."""
    session = _make_session()
    factory = _make_session_factory(session)

    age = AgeGraph(session_factory=factory)
    await age.delete_triple(
        subject_id="book",
        edge_id="has-edition",
        object_id="edition",
    )

    calls = session.execute.call_args_list
    found = False
    for c in calls:
        args, kwargs = c
        if len(args) >= 2 and isinstance(args[1], dict):
            if "cypher" in args[1] and "params" in args[1]:
                found = True
                sql_text = str(args[0])
                assert ":cypher" in sql_text
                assert ":params" in sql_text
    assert found, "No execute call with bind params found in delete_triple"


# ── traverse ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traverse_rejects_non_int_max_hops() -> None:
    """traverse() must reject non-int max_hops with ValueError."""
    session = _make_session()
    factory = _make_session_factory(session)
    age = AgeGraph(session_factory=factory)

    with pytest.raises(ValueError, match="max_hops must be an int"):
        await age.traverse("book", max_hops=1.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_hops must be an int"):
        await age.traverse("book", max_hops="3")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_traverse_rejects_max_hops_below_1_above_10() -> None:
    """traverse() must reject max_hops outside 1..10 with ValueError."""
    session = _make_session()
    factory = _make_session_factory(session)
    age = AgeGraph(session_factory=factory)

    with pytest.raises(ValueError, match="between 1 and 10"):
        await age.traverse("book", max_hops=0)

    with pytest.raises(ValueError, match="between 1 and 10"):
        await age.traverse("book", max_hops=11)


@pytest.mark.asyncio
async def test_traverse_returns_tuples() -> None:
    """traverse() strips AGE-style quoted strings and returns (subj, edge, obj) tuples."""
    session = _make_session()

    # AGE returns agtype scalar strings as '"value"' — simulate that
    fake_row1 = ('"book"', '"has-edition"', '"edition"')
    fake_row2 = ('"edition"', '"belongs-to"', '"publisher"')

    result_mock = MagicMock()
    result_mock.fetchall.return_value = [fake_row1, fake_row2]

    # execute: LOAD 'age', SET LOCAL search_path, cypher SELECT
    session.execute = AsyncMock(side_effect=[MagicMock(), MagicMock(), result_mock])
    factory = _make_session_factory(session)

    age = AgeGraph(session_factory=factory)
    triples = await age.traverse("book", max_hops=2)

    assert len(triples) == 2
    assert triples[0] == ("book", "has-edition", "edition")
    assert triples[1] == ("edition", "belongs-to", "publisher")
