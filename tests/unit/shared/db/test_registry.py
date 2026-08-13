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


# ---------------------------------------------------------------------------
# upsert_dataset_attributes — the dataset_filter attribute mirror
# ---------------------------------------------------------------------------
#
# spec: BACKEND_SCHEMA.md §dataset_registry — "**Attribute sync**: the same sweep
#   refreshes the attribute columns, upserting per dataset and never
#   deleting-then-inserting — a dataset the attribute read missed keeps its prior
#   attributes, so a partial sweep cannot silently narrow every `dataset_filter` in
#   the system."


def _captured_upsert(db: AsyncMock) -> list[tuple[object, object]]:
    """Record every ``(statement, payload)`` the upsert issues."""
    calls: list[tuple[object, object]] = []

    async def _execute(stmt: object, payload: object = None, *args: object, **kwargs: object):
        calls.append((stmt, payload))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    return calls


async def test_upsert_dataset_attributes_writes_every_filter_column(db: AsyncMock):
    """Each record carries origin, platform_urn and both association arrays.

    spec: BACKEND_SCHEMA.md §dataset_registry — the five attribute columns
        (`origin`, `platform_urn`, `tag_urns`, `glossary_term_urns`,
        `attrs_synced_at`) are what `dataset_filter` is evaluated against.
    """
    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    calls = _captured_upsert(db)
    synced_at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    refreshed = await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=["urn:li:tag:pii"],
                glossary_term_urns=["urn:li:glossaryTerm:pii.gdpr"],
            )
        ],
        synced_at=synced_at,
    )

    assert refreshed == 1
    assert len(calls) == 1, f"one round trip for one record; got {len(calls)}"
    payload = calls[0][1]
    assert payload == [
        {
            "dataset_urn": _URN_NEW,
            "datahub_registered": False,
            "origin": "PROD",
            "platform_urn": "urn:li:dataPlatform:postgres",
            "tag_urns": ["urn:li:tag:pii"],
            "glossary_term_urns": ["urn:li:glossaryTerm:pii.gdpr"],
            "attrs_synced_at": synced_at,
            "created_at": synced_at,
            "updated_at": synced_at,
        }
    ]


async def test_upsert_dataset_attributes_only_touches_the_urns_it_was_given(db: AsyncMock):
    """No delete precedes the write — an unread dataset keeps its stored attributes.

    This is the rule the whole step hangs on: a delete-then-insert (or a blanking
    update over the estate) would empty `tag_urns` for every dataset a partial read
    missed, silently narrowing every UC3/UC4/UC5 filter instead of failing visibly.

    spec: BACKEND_SCHEMA.md §dataset_registry — "upserting per dataset and never
        deleting-then-inserting — a dataset the attribute read missed keeps its
        prior attributes";
    spec: DATAHUB_INTEGRATION.md §Dataset attribute sync — "a half-completed read
        that emptied `tag_urns` would silently narrow every filter in the system
        instead of failing visibly."
    """
    from sqlalchemy.dialects import postgresql

    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    calls = _captured_upsert(db)

    await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=[],
                glossary_term_urns=[],
            )
        ],
    )

    assert calls, "backstop: the upsert must have issued a statement"
    for stmt, payload in calls:
        sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
        assert "delete" not in sql, f"the attribute sync must never delete rows; got:\n{sql}"
        assert "on conflict" in sql, f"the write must be an upsert; got:\n{sql}"
        assert isinstance(payload, list), "rows are bound as an executemany payload"
        assert {row["dataset_urn"] for row in payload} == {_URN_NEW}


async def test_upsert_dataset_attributes_refreshes_the_attribute_columns_on_conflict(
    db: AsyncMock,
):
    """An existing row's attribute columns are overwritten with the read's values.

    An upsert that inserted but never refreshed (`DO NOTHING`, or a SET missing a
    column) would freeze every UC3/UC4/UC5 filter at its first-sweep scope with no
    observable symptom — `attrs_synced_at` would stand still while the sweep kept
    reporting success. Each refreshed column is therefore asserted by name.

    spec: BACKEND_SCHEMA.md §dataset_registry — `attrs_synced_at` is "When the four
        attribute columns above were last refreshed", and the sweep's attribute step
        "refreshes the attribute columns";
    spec: BACKEND.md §dataset_filter — filter staleness is diagnosable from
        "`attrs_synced_at` standing still".
    """
    from sqlalchemy.dialects import postgresql

    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    calls = _captured_upsert(db)

    await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=["urn:li:tag:pii"],
                glossary_term_urns=["urn:li:glossaryTerm:pii.gdpr"],
            )
        ],
    )

    assert calls, "backstop: the upsert must have issued a statement"
    stmt, _payload = calls[0]
    conflict_clause = (
        str(stmt.compile(dialect=postgresql.dialect())).lower().split("on conflict", 1)[1]
    )
    assert "do update set" in conflict_clause, (
        "a conflicting row must be updated, not skipped — DO NOTHING would freeze the "
        f"attributes at their first-sweep values. Got:\n{conflict_clause}"
    )
    for column in (
        "origin",
        "platform_urn",
        "tag_urns",
        "glossary_term_urns",
        "attrs_synced_at",
    ):
        assert f"{column} = excluded.{column}" in conflict_clause, (
            f"{column} must be refreshed from the read on conflict; got:\n{conflict_clause}"
        )


async def test_upsert_dataset_attributes_does_not_register_an_unseen_urn(db: AsyncMock):
    """The step is not a registration authority — `datahub_registered` stays false.

    The attribute read and `reconcile_registry`'s enumeration page independently, so
    a URN only this read returned must not enter the registered set: it would land in
    the scope of every empty `dataset_filter` without ever passing reconcile.

    spec: BACKEND_SCHEMA.md §dataset_registry — "**Creation / reconcile**: bulk, by
        the `datahub-sync-hourly` sweep" is reconcile's job; the attribute sync
        "refreshes the attribute columns".
    """
    from sqlalchemy.dialects import postgresql

    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    calls = _captured_upsert(db)

    await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=[],
                glossary_term_urns=[],
            )
        ],
    )

    stmt, payload = calls[0]
    assert payload[0]["datahub_registered"] is False
    conflict_clause = (
        str(stmt.compile(dialect=postgresql.dialect())).lower().split("on conflict", 1)[1]
    )
    assert "datahub_registered" not in conflict_clause, (
        "the conflict SET must not touch datahub_registered — this step can neither "
        "register nor deregister a dataset. spec: BACKEND_SCHEMA.md §dataset_registry."
    )


async def test_upsert_dataset_attributes_skips_a_malformed_urn(db: AsyncMock):
    """A record whose URN is not a dataset URN is skipped and not counted.

    spec: BACKEND_SCHEMA.md §dataset_registry — `dataset_urn` is the dataset URN PK;
        a non-dataset entity URN (e.g. a `Restricted` placeholder) is not a dataset.
    """
    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    calls = _captured_upsert(db)

    refreshed = await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn="urn:li:restricted:opaque",
                origin=None,
                platform_urn=None,
                tag_urns=[],
                glossary_term_urns=[],
            ),
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=[],
                glossary_term_urns=[],
            ),
        ],
    )

    assert refreshed == 1, "only the well-formed dataset URN is refreshed"
    assert [row["dataset_urn"] for row in calls[0][1]] == [_URN_NEW]


async def test_upsert_dataset_attributes_with_no_records_issues_no_statement(db: AsyncMock):
    """An empty read writes nothing at all — it must not blank the estate."""
    from src.shared.db.registry import upsert_dataset_attributes

    calls = _captured_upsert(db)

    assert await upsert_dataset_attributes(db, []) == 0
    assert calls == []


async def test_upsert_dataset_attributes_does_not_commit(db: AsyncMock):
    """The caller owns the step-isolated transaction, as with `reconcile_registry`."""
    from src.shared.db.registry import DatasetAttributes, upsert_dataset_attributes

    _captured_upsert(db)

    await upsert_dataset_attributes(
        db,
        [
            DatasetAttributes(
                dataset_urn=_URN_NEW,
                origin="PROD",
                platform_urn="urn:li:dataPlatform:postgres",
                tag_urns=[],
                glossary_term_urns=[],
            )
        ],
    )

    db.commit.assert_not_called()


async def test_upsert_dataset_attributes_chunks_a_large_estate(db: AsyncMock):
    """A large read is written in bounded batches, not one unbounded statement.

    Not a spec line — an implementation invariant of `_ATTRIBUTE_CHUNK` recorded here
    because the failure it prevents (PostgreSQL's 32,767-bind-parameter ceiling) is an
    opaque driver error at estate scale rather than a visible regression.
    """
    from src.shared.db.registry import (
        _ATTRIBUTE_CHUNK,
        DatasetAttributes,
        upsert_dataset_attributes,
    )

    calls = _captured_upsert(db)
    records = [
        DatasetAttributes(
            dataset_urn=f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)",
            origin="PROD",
            platform_urn="urn:li:dataPlatform:postgres",
            tag_urns=[],
            glossary_term_urns=[],
        )
        for i in range(_ATTRIBUTE_CHUNK + 1)
    ]

    refreshed = await upsert_dataset_attributes(db, records)

    assert refreshed == _ATTRIBUTE_CHUNK + 1
    assert len(calls) == 2, f"expected two batches; got {len(calls)}"
    assert [len(payload) for _, payload in calls] == [_ATTRIBUTE_CHUNK, 1]
