"""Unit tests for PgVectorManager (vector/client.py).

Tests spec-mandated public surface: ensure_collection, upsert, search, delete.
- _validate_collection raises ValueError for unsupported collection names.
- search raises NotImplementedError for unsupported filter keys.
- upsert is a no-op for an empty hits list (no DB call).
- delete is a no-op for an empty ids list (no DB call).
- check_connectivity returns True on successful DB ping, False on failure.

spec: feature/BACKEND.md §Shared Services (Vector row) — PgVectorManager wraps pgvector;
      collection name must match EMBEDDING_COLLECTION (whitelist guard).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared.config import EMBEDDING_COLLECTION
from src.shared.vector.client import PgVectorManager, VectorHit


def _make_manager() -> PgVectorManager:
    """Return a PgVectorManager with a mocked session_factory."""
    mock_factory = MagicMock()
    return PgVectorManager(session_factory=mock_factory)


# ── _validate_collection ──────────────────────────────────────────────────────


def test_validate_collection_raises_for_unknown_name() -> None:
    """PgVectorManager must reject collection names outside EMBEDDING_COLLECTION.

    spec: feature/BACKEND.md §Shared Services (Vector row) — collection whitelist prevents
          SQL injection via table-name interpolation.
    """
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Unsupported collection"):
        mgr._validate_collection("evil_table; DROP TABLE users --")


def test_validate_collection_accepts_embedding_collection() -> None:
    """PgVectorManager must accept EMBEDDING_COLLECTION without raising.

    spec: feature/BACKEND.md §Shared Services (Vector row) — EMBEDDING_COLLECTION is the
          only valid collection name.
    """
    mgr = _make_manager()
    # Must not raise
    mgr._validate_collection(EMBEDDING_COLLECTION)


# ── search: filter validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_raises_for_unsupported_filter_key() -> None:
    """PgVectorManager.search raises NotImplementedError for unsupported filter keys.

    spec: feature/BACKEND.md §Shared Services (Vector row) — only 'platform' and 'has_pii'
          are supported filter keys; unknown keys raise loudly.
    """
    # Need a real session that returns empty rows when execute is called
    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.fetchall.return_value = []
    mock_session.execute = AsyncMock(return_value=execute_result)

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mgr = PgVectorManager(session_factory=mock_factory)

    with pytest.raises(NotImplementedError):
        await mgr.search(
            collection=EMBEDDING_COLLECTION,
            vector=[0.0] * 5,
            filters={"unsupported_key": "value"},
        )


# ── upsert: no-op on empty list ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_no_op_for_empty_hits() -> None:
    """PgVectorManager.upsert does not raise when hits is empty.

    spec: feature/BACKEND.md §Shared Services (Vector row) — empty upsert must not fail.
    """
    mock_factory = MagicMock()
    mgr = PgVectorManager(session_factory=mock_factory)

    # Must not raise
    await mgr.upsert(collection=EMBEDDING_COLLECTION, hits=[])


# ── delete: no-op on empty list ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_no_op_for_empty_ids() -> None:
    """PgVectorManager.delete does not raise when ids is empty.

    spec: feature/BACKEND.md §Shared Services (Vector row) — empty delete must not fail.
    """
    mock_factory = MagicMock()
    mgr = PgVectorManager(session_factory=mock_factory)

    # Must not raise
    await mgr.delete(collection=EMBEDDING_COLLECTION, ids=[])


# ── check_connectivity ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_connectivity_returns_true_on_success() -> None:
    """PgVectorManager.check_connectivity returns True when DB is reachable.

    spec: feature/BACKEND.md §Shared Services (Vector row) — connectivity check for health.
    """
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mgr = PgVectorManager(session_factory=mock_factory)
    result = await mgr.check_connectivity()
    assert result is True


@pytest.mark.asyncio
async def test_check_connectivity_returns_false_on_exception() -> None:
    """PgVectorManager.check_connectivity returns False when DB is unreachable.

    spec: feature/BACKEND.md §Shared Services (Vector row) — graceful False on failure.
    """
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(side_effect=Exception("db down"))
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mgr = PgVectorManager(session_factory=mock_factory)
    result = await mgr.check_connectivity()
    assert result is False


# ── VectorHit dataclass ───────────────────────────────────────────────────────


def test_vector_hit_has_required_fields() -> None:
    """VectorHit must carry dataset_urn, score, payload, and embedding.

    spec: feature/BACKEND.md §Shared Services (Vector row) — VectorHit shape.
    """
    hit = VectorHit(dataset_urn="urn:li:dataset:test", score=0.95)
    assert hit.dataset_urn == "urn:li:dataset:test"
    assert hit.score == 0.95
    assert hit.payload == {}
    assert hit.embedding == []
