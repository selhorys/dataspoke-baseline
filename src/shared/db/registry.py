"""Dataset registry — the local mirror of the DataHub dataset estate.

Tracks whether a dataset URN is known to DataHub, plus the attribute columns
every ``dataset_filter`` is evaluated against.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.datahub.client import DataHubClient
from src.shared.datahub.urn import DATASET_URN_RE
from src.shared.db.models import DatasetRegistry
from src.shared.exceptions import PreconditionFailedError

logger = logging.getLogger(__name__)

#: Rows per attribute-upsert round trip. Bounded so one estate-wide sweep does
#: not build a single statement with an unbounded parameter count.
_ATTRIBUTE_CHUNK = 500


async def ensure_dataset_registered(
    db: AsyncSession,
    datahub: DataHubClient,
    dataset_urn: str,
    require_in_datahub: bool = False,
) -> bool:
    """Look up or create a dataset_registry row. Returns datahub_registered.

    1. SELECT from dataset_registry
    2. If no row: check DataHub via get_aspect(DatasetPropertiesClass),
       INSERT row with result (handles concurrent INSERT race)
    3. If require_in_datahub and datahub_registered is False: raise PreconditionFailedError
    4. Return datahub_registered

    Does NOT commit — the caller's commit covers both the registry
    insert and the subsequent config upsert atomically.
    """
    result = await db.execute(
        select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()

    if row is not None:
        registered = row.datahub_registered
    else:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        aspect = await datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        registered = aspect is not None

        new_row = DatasetRegistry(
            dataset_urn=dataset_urn,
            datahub_registered=registered,
        )
        db.add(new_row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
            )
            row = result.scalar_one_or_none()
            registered = row.datahub_registered if row is not None else False

    if require_in_datahub and not registered:
        raise PreconditionFailedError(
            "DATASET_NOT_IN_DATAHUB",
            f"Dataset '{dataset_urn}' is not registered in DataHub",
        )

    return registered


async def mark_registered(db: AsyncSession, dataset_urn: str) -> None:
    """Set datahub_registered=True for an existing registry row.

    If the row is missing, logs a warning and returns without error.
    If already True, no-ops.
    Does NOT commit — caller commits.
    """
    result = await db.execute(
        select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()
    if row is None:
        logger.warning(
            "dataset_registry_row_missing_after_run",
            extra={"dataset_urn": dataset_urn},
        )
        return
    if row.datahub_registered:
        return
    row.datahub_registered = True
    row.updated_at = datetime.now(tz=UTC)
    db.add(row)


async def mark_unregistered(db: AsyncSession, dataset_urn: str) -> None:
    """Set datahub_registered=False for an existing registry row.

    If the row is missing, logs a warning and returns without error.
    If already False, no-ops.
    Does NOT commit — caller commits.
    """
    result = await db.execute(
        select(DatasetRegistry).where(DatasetRegistry.dataset_urn == dataset_urn)
    )
    row = result.scalar_one_or_none()
    if row is None:
        logger.warning(
            "dataset_registry_row_missing_after_run",
            extra={"dataset_urn": dataset_urn},
        )
        return
    if not row.datahub_registered:
        return
    row.datahub_registered = False
    row.updated_at = datetime.now(tz=UTC)
    db.add(row)


async def reconcile_registry(
    db: AsyncSession,
    enumerated_urns: set[str],
) -> dict[str, int]:
    """Make dataset_registry a full stateful mirror of the provided URN set.

    Given a pre-fetched set of DataHub dataset URNs:
    - INSERT rows for URNs present in the set but absent from registry
      (datahub_registered=True).
    - Set datahub_registered=True for existing rows that are in the set
      and currently False.
    - Set datahub_registered=False for existing rows that are NOT in the
      set (soft-flag; no hard-delete).
    - Leave rows that are already correct unchanged.

    Does NOT commit — the caller commits as part of its step-isolated
    transaction so one step's failure does not roll back the others.

    Returns:
        dict with keys: inserted, marked_true, marked_false, unchanged
    """
    now = datetime.now(tz=UTC)

    # Load ALL existing registry rows in one query.
    all_result = await db.execute(select(DatasetRegistry))
    existing_rows: dict[str, DatasetRegistry] = {
        r.dataset_urn: r for r in all_result.scalars().all()
    }
    existing_urns = set(existing_rows.keys())

    inserted = 0
    marked_true = 0
    marked_false = 0
    unchanged = 0

    # Step A: process each enumerated URN.
    for urn in enumerated_urns:
        row = existing_rows.get(urn)
        if row is None:
            # Not in registry — insert as True.
            new_row = DatasetRegistry(
                dataset_urn=urn,
                datahub_registered=True,
            )
            db.add(new_row)
            inserted += 1
        elif not row.datahub_registered:
            # Exists but currently False — flip to True.
            row.datahub_registered = True
            row.updated_at = now
            db.add(row)
            marked_true += 1
        else:
            # Already True — no change.
            unchanged += 1

    # Step B: soft-flag registry rows absent from the enumerated set.
    # Guard: an empty enumeration is treated as "no signal" rather than
    # "everything is gone" — a transient empty-but-successful DataHub search
    # (e.g. during the ES index-lag window) must not mass-deregister a
    # non-empty registry. The deregister pass is skipped on empty input; it
    # self-corrects on the next sweep once enumeration returns results.
    if enumerated_urns:
        for urn in existing_urns - enumerated_urns:
            row = existing_rows[urn]
            if row.datahub_registered:
                row.datahub_registered = False
                row.updated_at = now
                db.add(row)
                marked_false += 1

    return {
        "inserted": inserted,
        "marked_true": marked_true,
        "marked_false": marked_false,
        "unchanged": unchanged,
    }


@dataclass(frozen=True)
class DatasetAttributes:
    """One dataset's filter attributes, ready to mirror into ``dataset_registry``.

    ``origin`` and ``platform_urn`` are parsed out of the URN by the caller; the
    two association lists and ``is_primary`` come from the estate-wide attribute
    read. ``is_primary`` is a plain bool, never null — the read resolves absent
    or unreadable sibling information to ``True``.
    """

    dataset_urn: str
    origin: str | None
    platform_urn: str | None
    tag_urns: list[str]
    glossary_term_urns: list[str]
    is_primary: bool


async def upsert_dataset_attributes(
    db: AsyncSession,
    records: Sequence[DatasetAttributes],
    *,
    synced_at: datetime | None = None,
) -> int:
    """Mirror dataset attributes into ``dataset_registry``. Returns rows refreshed.

    **Upsert per dataset — never delete-then-insert, never blank.** Only the URNs
    in *records* are touched; a dataset the attribute read did not return keeps
    its stored attributes and its old ``attrs_synced_at``. That rule is
    load-bearing rather than defensive: a half-completed read that emptied
    ``tag_urns`` would silently narrow the scope of every UC3, UC4 and UC5
    ``dataset_filter`` in the system — a wrong answer that looks like a correct
    one — whereas retaining stale attributes is bounded, visible through
    ``attrs_synced_at``, and self-correcting on the next sweep.

    **This step is not a registration authority.** ``reconcile_registry`` is —
    it reads the estate through the SDK's enumeration, and this step reads it
    through a separate paged ``scrollAcrossEntities``. The two page
    independently, so a URN only this read returned (page-cap disagreement,
    eventual consistency, or a non-dataset placeholder such as a ``Restricted``
    entity under a scoped PAT) must not enter the registered set: it would land
    in the scope of every empty ``dataset_filter`` across UC3, UC4 and UC5 with
    ``origin`` and ``platform_urn`` both NULL — the one shape no scalar predicate
    can exclude — without ever passing reconcile's enumeration. So an unseen URN
    is inserted with ``datahub_registered`` left at its column default (false)
    and waits for the reconcile step to register it, and on conflict only the
    attribute columns are written. Records whose URN is not a well-formed
    ``urn:li:dataset:(…)`` are skipped outright and counted as not refreshed.

    Does NOT commit — the caller commits as part of its step-isolated
    transaction.
    """
    records = [record for record in records if DATASET_URN_RE.match(record.dataset_urn)]
    if not records:
        return 0

    now = synced_at or datetime.now(tz=UTC)
    stmt = pg_insert(DatasetRegistry)
    stmt = stmt.on_conflict_do_update(
        index_elements=["dataset_urn"],
        set_={
            "origin": stmt.excluded.origin,
            "platform_urn": stmt.excluded.platform_urn,
            "tag_urns": stmt.excluded.tag_urns,
            "glossary_term_urns": stmt.excluded.glossary_term_urns,
            "is_primary": stmt.excluded.is_primary,
            "attrs_synced_at": stmt.excluded.attrs_synced_at,
            "updated_at": now,
        },
    )

    payload = [
        {
            "dataset_urn": record.dataset_urn,
            # Explicitly unregistered on insert, and absent from the conflict
            # SET above, so this step can neither register a URN reconcile has
            # not seen nor deregister one it has.
            "datahub_registered": False,
            "origin": record.origin,
            "platform_urn": record.platform_urn,
            "tag_urns": list(record.tag_urns),
            "glossary_term_urns": list(record.glossary_term_urns),
            "is_primary": record.is_primary,
            "attrs_synced_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for record in records
    ]

    for start in range(0, len(payload), _ATTRIBUTE_CHUNK):
        await db.execute(stmt, payload[start : start + _ATTRIBUTE_CHUNK])

    return len(payload)


async def sync_with_datahub(
    db: AsyncSession,
    datahub: DataHubClient,
    dataset_urns: list[str] | None = None,
) -> dict[str, int]:
    """Bidirectional reconciliation of dataset_registry against DataHub.

    Calls datahub.enumerate_datasets() once to obtain the current DataHub URN set.
    Then queries registry rows — all rows when dataset_urns is None, otherwise only
    rows whose dataset_urn appears in the provided list.

    For each selected row:
    - urn in DataHub and datahub_registered=False → mark registered (flip true)
    - urn not in DataHub and datahub_registered=True → mark unregistered (flip false)
    - otherwise → unchanged

    URNs passed in dataset_urns that have no registry row are counted as not_found.
    No new rows are inserted — the registry stays scoped to config-referenced URNs.

    Does NOT commit — caller commits.

    Returns:
        dict with keys: checked, flipped_true, flipped_false, unchanged, not_found
    """
    if dataset_urns is not None and not dataset_urns:
        return {"checked": 0, "flipped_true": 0, "flipped_false": 0, "unchanged": 0, "not_found": 0}

    datahub_urn_set = set(await datahub.enumerate_datasets())

    if dataset_urns is None:
        result = await db.execute(select(DatasetRegistry))
        rows = list(result.scalars().all())
        not_found = 0
    else:
        result = await db.execute(
            select(DatasetRegistry).where(
                DatasetRegistry.dataset_urn.in_(dataset_urns)
            )
        )
        rows = list(result.scalars().all())
        registry_urns = {row.dataset_urn for row in rows}
        not_found = len({urn for urn in dataset_urns if urn not in registry_urns})

    checked = len(rows)
    flipped_true = 0
    flipped_false = 0
    unchanged = 0

    for row in rows:
        in_datahub = row.dataset_urn in datahub_urn_set
        if in_datahub and not row.datahub_registered:
            flipped_true += 1
            await mark_registered(db, row.dataset_urn)
        elif not in_datahub and row.datahub_registered:
            flipped_false += 1
            await mark_unregistered(db, row.dataset_urn)
        else:
            unchanged += 1

    return {
        "checked": checked,
        "flipped_true": flipped_true,
        "flipped_false": flipped_false,
        "unchanged": unchanged,
        "not_found": not_found,
    }
