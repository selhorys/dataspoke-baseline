"""Unit tests for src/shared/db/registry.py helpers.

Tests cover:
- mark_registered: flip False → True, no-op on True → True, missing row warn-and-return
- mark_unregistered: flip True → False, no-op on False → False, missing row warn-and-return
- sync_with_datahub: full sweep and scoped modes, empty list early-return, duplicate URN dedup,
  no-commit contract, missing-row not_found counting
- reconcile_registry: inserts new URNs (True), flips absent existing rows to False,
  keeps existing True rows, idempotent, no-commit contract
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.shared.db.registry import (
    mark_registered,
    mark_unregistered,
    reconcile_registry,
    sync_with_datahub,
)
from tests.unit.conftest import route_db_execute

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
    row = _make_registry_row(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=False
    )
    before_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_registered(db, row.dataset_urn)

    assert row.datahub_registered is True
    assert row.updated_at > before_updated_at
    db.add.assert_called_once_with(row)


async def test_mark_registered_noop_when_already_true(db: AsyncMock):
    """Row already True → no attribute mutation, no db.add call."""
    row = _make_registry_row(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=True
    )
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
    row = _make_registry_row(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=True
    )
    before_updated_at = row.updated_at

    db.execute = AsyncMock(return_value=_scalar_result(row))

    await mark_unregistered(db, row.dataset_urn)

    assert row.datahub_registered is False
    assert row.updated_at > before_updated_at
    db.add.assert_called_once_with(row)


async def test_mark_unregistered_noop_when_already_false(db: AsyncMock):
    """Row already False → no attribute mutation, no db.add call."""
    row = _make_registry_row(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,PROD)", registered=False
    )
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

    # Route by statement: the sweep SELECT (no per-urn equality) is the default; each
    # mark_* single-row lookup is keyed on its own dataset_urn equality, so the A and D
    # lookups return their own rows regardless of the order the sweep visits them in.
    route_db_execute(
        db,
        [
            (lambda s: "dataset_urn = " in s and _URN_A.lower() in s, _scalar_result(row_a)),
            (lambda s: "dataset_urn = " in s and _URN_D.lower() in s, _scalar_result(row_d)),
        ],
        default=_scalars_result([row_a, row_b, row_c, row_d]),
    )

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

    route_db_execute(
        db,
        [(lambda s: "dataset_urn = " in s and _URN_A.lower() in s, _scalar_result(row_a))],
        default=_scalars_result([row_a]),
    )

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
    route_db_execute(
        db,
        [
            (lambda s: "dataset_urn = " in s and _URN_A.lower() in s, _scalar_result(row_a)),
            (lambda s: "dataset_urn = " in s and _URN_D.lower() in s, _scalar_result(row_d)),
        ],
        default=_scalars_result([row_a, row_d]),  # scoped WHERE IN (A, D)
    )

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


# ---------------------------------------------------------------------------
# reconcile_registry
# ---------------------------------------------------------------------------
#
# Fixture layout for reconcile_registry tests:
#   URN_NEW   — in enumerated_urns, NOT in registry → INSERT (True)
#   URN_TRUE  — in enumerated_urns, in registry (True) → unchanged
#   URN_FALSE — in enumerated_urns, in registry (False) → flip True
#   URN_GONE  — NOT in enumerated_urns, in registry (True) → soft-flag False

_URN_NEW = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.new,PROD)"
_URN_TRUE = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.true,PROD)"
_URN_FALSE = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.false,PROD)"
_URN_GONE = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.gone,PROD)"


async def test_reconcile_registry_inserts_new_urns_as_registered(db: AsyncMock):
    """URNs in enumerated_urns not in registry are inserted with datahub_registered=True."""
    # No existing rows in registry.
    db.execute = AsyncMock(return_value=_scalars_result([]))

    added_rows: list = []
    db.add = MagicMock(side_effect=added_rows.append)

    result = await reconcile_registry(db, {_URN_NEW})

    assert result["inserted"] == 1
    assert result["marked_true"] == 0
    assert result["marked_false"] == 0
    assert result["unchanged"] == 0
    # db.add must have been called with the new row.
    assert db.add.call_count == 1
    new_row = added_rows[0]
    assert new_row.dataset_urn == _URN_NEW
    assert new_row.datahub_registered is True


async def test_reconcile_registry_flips_false_rows_to_true(db: AsyncMock):
    """Existing rows with datahub_registered=False are flipped to True."""
    row_false = _make_registry_row(urn=_URN_FALSE, registered=False)
    db.execute = AsyncMock(return_value=_scalars_result([row_false]))

    result = await reconcile_registry(db, {_URN_FALSE})

    assert result["inserted"] == 0
    assert result["marked_true"] == 1
    assert result["marked_false"] == 0
    assert result["unchanged"] == 0
    assert row_false.datahub_registered is True


async def test_reconcile_registry_keeps_existing_true_unchanged(db: AsyncMock):
    """Existing rows already True and in enumerated_urns are not touched."""
    row_true = _make_registry_row(urn=_URN_TRUE, registered=True)
    original_updated_at = row_true.updated_at
    db.execute = AsyncMock(return_value=_scalars_result([row_true]))

    added: list = []
    db.add = MagicMock(side_effect=added.append)

    result = await reconcile_registry(db, {_URN_TRUE})

    assert result["unchanged"] == 1
    assert result["inserted"] == 0
    assert result["marked_true"] == 0
    assert result["marked_false"] == 0
    assert row_true.datahub_registered is True
    assert row_true.updated_at == original_updated_at
    assert db.add.call_count == 0


async def test_reconcile_registry_soft_flags_absent_rows_to_false(db: AsyncMock):
    """Existing True rows whose URN is absent from a NON-empty enumeration are flipped False."""
    row_gone = _make_registry_row(urn=_URN_GONE, registered=True)
    db.execute = AsyncMock(return_value=_scalars_result([row_gone]))

    # Non-empty enumeration that does not include the gone row.
    result = await reconcile_registry(db, {_URN_NEW})

    assert result["marked_false"] == 1
    assert result["inserted"] == 1  # _URN_NEW inserted
    assert result["marked_true"] == 0
    assert row_gone.datahub_registered is False


async def test_reconcile_registry_empty_enumeration_does_not_deregister(db: AsyncMock):
    """An empty (but successful) enumeration is 'no signal' — must NOT mass-deregister.

    Guards the ES-index-lag window where DataHub search can return zero hits while
    datasets still exist; the deregister pass is skipped on empty input.
    """
    row_a = _make_registry_row(urn=_URN_TRUE, registered=True)
    row_b = _make_registry_row(urn=_URN_GONE, registered=True)
    db.execute = AsyncMock(return_value=_scalars_result([row_a, row_b]))

    result = await reconcile_registry(db, set())

    assert result["marked_false"] == 0
    assert row_a.datahub_registered is True
    assert row_b.datahub_registered is True


async def test_reconcile_registry_full_mixed_scenario(db: AsyncMock):
    """Full mixed scenario: insert new, flip False→True, keep True, soft-flag gone."""
    row_true = _make_registry_row(urn=_URN_TRUE, registered=True)
    row_false = _make_registry_row(urn=_URN_FALSE, registered=False)
    row_gone = _make_registry_row(urn=_URN_GONE, registered=True)

    # Registry has: TRUE, FALSE, GONE.  Enumerated: NEW, TRUE, FALSE (GONE is absent).
    db.execute = AsyncMock(return_value=_scalars_result([row_true, row_false, row_gone]))

    added: list = []
    db.add = MagicMock(side_effect=added.append)

    result = await reconcile_registry(db, {_URN_NEW, _URN_TRUE, _URN_FALSE})

    assert result["inserted"] == 1     # NEW was not in registry
    assert result["marked_true"] == 1  # FALSE flipped to True
    assert result["marked_false"] == 1 # GONE soft-flagged
    assert result["unchanged"] == 1    # TRUE already correct

    assert row_true.datahub_registered is True
    assert row_false.datahub_registered is True
    assert row_gone.datahub_registered is False


async def test_reconcile_registry_does_not_commit(db: AsyncMock):
    """reconcile_registry does NOT commit — caller is responsible."""
    db.execute = AsyncMock(return_value=_scalars_result([]))

    await reconcile_registry(db, set())

    db.commit.assert_not_awaited()


async def test_reconcile_registry_idempotent(db: AsyncMock):
    """Calling reconcile_registry twice with the same set produces the same outcome."""
    row_true = _make_registry_row(urn=_URN_TRUE, registered=True)

    # First call: existing True row, in enumerated set → unchanged.
    db.execute = AsyncMock(return_value=_scalars_result([row_true]))
    result1 = await reconcile_registry(db, {_URN_TRUE})

    # Second call: same state.
    db.execute = AsyncMock(return_value=_scalars_result([row_true]))
    result2 = await reconcile_registry(db, {_URN_TRUE})

    assert result1 == result2 == {
        "inserted": 0,
        "marked_true": 0,
        "marked_false": 0,
        "unchanged": 1,
    }
