"""Unit tests for src/shared/db/registry.py helpers.

Tests cover:
- mark_registered: flip False → True, no-op on True → True, missing row warn-and-return
- mark_unregistered: flip True → False, no-op on False → False, missing row warn-and-return
- sync_with_datahub: full sweep and scoped modes, empty list early-return, duplicate URN dedup,
  no-commit contract, missing-row not_found counting
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.shared.db.registry import mark_registered, mark_unregistered, sync_with_datahub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_row(*, urn: str, registered: bool) -> MagicMock:
    """Create a mock DatasetRegistry row."""
    row = MagicMock()
    row.dataset_urn = urn
    row.datahub_registered = registered
    row.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return row


def _scalar_result(row: object | None) -> MagicMock:
    """Build a mock db.execute() return value for scalar_one_or_none()."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


def _scalars_result(rows: list) -> MagicMock:
    """Build a mock db.execute() return value for scalars().all()."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


# ---------------------------------------------------------------------------
# mark_registered
# ---------------------------------------------------------------------------


async def test_mark_registered_flips_false_to_true(db: AsyncMock):
    """Row with datahub_registered=False is updated to True; updated_at is bumped."""
    row = _make_registry_row(urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=False)
    before_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_registered(db, row.dataset_urn)

    assert row.datahub_registered is True
    assert row.updated_at > before_updated_at
    db.add.assert_called_once_with(row)


async def test_mark_registered_noop_when_already_true(db: AsyncMock):
    """Row already True → no attribute mutation, no db.add call."""
    row = _make_registry_row(urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=True)
    original_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_registered(db, row.dataset_urn)

    assert row.datahub_registered is True
    assert row.updated_at == original_updated_at
    db.add.assert_not_called()


async def test_mark_registered_missing_row_warns_and_returns(db: AsyncMock, caplog):
    """Missing row emits dataset_registry_row_missing_after_run warning; no exception."""
    db.execute = AsyncMock(return_value=_scalar_result(None))

    import logging
    with caplog.at_level(logging.WARNING, logger="src.shared.db.registry"):
        await mark_registered(db, "urn:li:dataset:(urn:li:dataPlatform:postgres,db.ghost,PROD)")

    assert "dataset_registry_row_missing_after_run" in caplog.text
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# mark_unregistered
# ---------------------------------------------------------------------------


async def test_mark_unregistered_flips_true_to_false(db: AsyncMock):
    """Row with datahub_registered=True is updated to False; updated_at is bumped."""
    row = _make_registry_row(urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=True)
    before_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_unregistered(db, row.dataset_urn)

    assert row.datahub_registered is False
    assert row.updated_at > before_updated_at
    db.add.assert_called_once_with(row)


async def test_mark_unregistered_noop_when_already_false(db: AsyncMock):
    """Row already False → no attribute mutation, no db.add call."""
    row = _make_registry_row(urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=False)
    original_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_unregistered(db, row.dataset_urn)

    assert row.datahub_registered is False
    assert row.updated_at == original_updated_at
    db.add.assert_not_called()


async def test_mark_unregistered_missing_row_warns_and_returns(db: AsyncMock, caplog):
    """Missing row emits dataset_registry_row_missing_after_run warning; no exception."""
    db.execute = AsyncMock(return_value=_scalar_result(None))

    import logging
    with caplog.at_level(logging.WARNING, logger="src.shared.db.registry"):
        await mark_unregistered(db, "urn:li:dataset:(urn:li:dataPlatform:postgres,db.ghost,PROD)")

    assert "dataset_registry_row_missing_after_run" in caplog.text
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# sync_with_datahub — full sweep
# ---------------------------------------------------------------------------

# Four-row fixture:
#   A (registered=False, in DataHub)  → should flip to True
#   B (registered=True,  in DataHub)  → unchanged
#   C (registered=False, not in DataHub) → unchanged
#   D (registered=True,  not in DataHub) → should flip to False

_URN_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
_URN_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
_URN_C = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,PROD)"
_URN_D = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.d,PROD)"


async def test_sync_full_sweep_correct_final_states(db: AsyncMock):
    """Full sweep: 4 rows, 2 in DataHub. Asserts final row states and return counts."""
    row_a = _make_registry_row(urn=_URN_A, registered=False)
    row_b = _make_registry_row(urn=_URN_B, registered=True)
    row_c = _make_registry_row(urn=_URN_C, registered=False)
    row_d = _make_registry_row(urn=_URN_D, registered=True)

    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=[_URN_A, _URN_B])

    # db.execute call sequence:
    #   0: full SELECT (scalars().all())
    #   1: mark_registered(db, A) → scalar_one_or_none → row_a
    #   2: mark_unregistered(db, D) → scalar_one_or_none → row_d
    db.execute = AsyncMock(side_effect=[
        _scalars_result([row_a, row_b, row_c, row_d]),  # full SELECT
        _scalar_result(row_a),  # mark_registered lookup for A
        _scalar_result(row_d),  # mark_unregistered lookup for D
    ])

    result = await sync_with_datahub(db, datahub, dataset_urns=None)

    assert result == {
        "checked": 4,
        "flipped_true": 1,
        "flipped_false": 1,
        "unchanged": 2,
        "not_found": 0,
    }
    assert row_a.datahub_registered is True
    assert row_b.datahub_registered is True   # unchanged
    assert row_c.datahub_registered is False  # unchanged
    assert row_d.datahub_registered is False


async def test_sync_full_sweep_no_commit(db: AsyncMock):
    """sync_with_datahub does NOT commit — caller is responsible."""
    row_a = _make_registry_row(urn=_URN_A, registered=False)

    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=[_URN_A])

    db.execute = AsyncMock(side_effect=[
        _scalars_result([row_a]),
        _scalar_result(row_a),
    ])

    await sync_with_datahub(db, datahub, dataset_urns=None)

    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# sync_with_datahub — scoped (dataset_urns provided)
# ---------------------------------------------------------------------------


async def test_sync_scoped_only_touches_listed_urns(db: AsyncMock):
    """Scoped call: only A and D are in dataset_urns; B and C are not queried."""
    row_a = _make_registry_row(urn=_URN_A, registered=False)
    row_d = _make_registry_row(urn=_URN_D, registered=True)

    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=[_URN_A, _URN_B])

    # Scoped SELECT returns only rows for A and D
    # mark_registered(A), mark_unregistered(D)
    db.execute = AsyncMock(side_effect=[
        _scalars_result([row_a, row_d]),  # WHERE IN (A, D)
        _scalar_result(row_a),            # mark_registered A
        _scalar_result(row_d),            # mark_unregistered D
    ])

    result = await sync_with_datahub(db, datahub, dataset_urns=[_URN_A, _URN_D])

    assert result == {
        "checked": 2,
        "flipped_true": 1,
        "flipped_false": 1,
        "unchanged": 0,
        "not_found": 0,
    }
    assert row_a.datahub_registered is True
    assert row_d.datahub_registered is False


async def test_sync_scoped_ghost_urn_returns_not_found(db: AsyncMock):
    """URN in dataset_urns but absent from registry is counted as not_found (no INSERT)."""
    ghost_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,ghost,PROD)"

    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=[ghost_urn])

    # Scoped SELECT finds no rows for ghost_urn
    db.execute = AsyncMock(return_value=_scalars_result([]))

    result = await sync_with_datahub(db, datahub, dataset_urns=[ghost_urn])

    assert result["not_found"] == 1
    assert result["checked"] == 0
    assert result["flipped_true"] == 0
    db.add.assert_not_called()


async def test_sync_scoped_duplicate_urns_counted_once(db: AsyncMock):
    """Duplicate URNs in dataset_urns are deduplicated for not_found count."""
    ghost_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,ghost,PROD)"

    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    db.execute = AsyncMock(return_value=_scalars_result([]))

    result = await sync_with_datahub(db, datahub, dataset_urns=[ghost_urn, ghost_urn])

    # The deduplicated set has one unique URN missing from registry
    assert result["not_found"] == 1


# ---------------------------------------------------------------------------
# sync_with_datahub — empty list early-return
# ---------------------------------------------------------------------------


async def test_sync_empty_list_returns_zero_counts_and_skips_datahub(db: AsyncMock):
    """dataset_urns=[] → all counts zero; enumerate_datasets is NOT called."""
    datahub = AsyncMock()
    datahub.enumerate_datasets = AsyncMock()

    result = await sync_with_datahub(db, datahub, dataset_urns=[])

    assert result == {
        "checked": 0,
        "flipped_true": 0,
        "flipped_false": 0,
        "unchanged": 0,
        "not_found": 0,
    }
    datahub.enumerate_datasets.assert_not_awaited()
    db.execute.assert_not_awaited()
