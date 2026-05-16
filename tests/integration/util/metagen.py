"""Raw-SQL seed and cleanup helpers for Metadata Generation integration tests.

Provides four public helpers used by spot and api-wired tests that need to
pre-populate metagen state without going through the REST API (bypassing LLM
and run-pipeline concerns not under test):

  seed_metagen_item        — insert a metagen_items row
  seed_metagen_candidate   — insert a metagen_candidates row (ensures parent item)
  seed_metagen_event       — insert a dataspoke.events row for metagen events
  delete_metagen_state_for_urn — cascade-delete all metagen rows for a dataset URN

spec: spec/TESTING.md §Spot vs Api-Wired Integration Tests — raw-SQL seeding
is the correct approach when the concern under test is review/query behavior,
not the run pipeline that would normally produce the data.
"""

import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def seed_metagen_item(
    session: AsyncSession,
    *,
    dataset_urn: str,
    item_id: str,
    kind: str = "dataset.description",
    field_path: str | None = None,
) -> None:
    """Insert a metagen_items row (composite PK: dataset_urn, item_id).

    Uses ON CONFLICT DO NOTHING so callers can safely call this multiple
    times for the same (dataset_urn, item_id) pair.

    spec: src/shared/db/models.py — MetagenItem composite PK (dataset_urn, item_id)
    spec: BACKEND.md §UC4 — item kind in {dataset.description, column.description}
    """
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind, field_path)"
            " VALUES (:urn, :item_id, :kind, :fp)"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"urn": dataset_urn, "item_id": item_id, "kind": kind, "fp": field_path},
    )
    await session.commit()


async def seed_metagen_candidate(
    session: AsyncSession,
    *,
    dataset_urn: str,
    item_id: str,
    value: str,
    status: str = "llm_approved",
    confidence: float = 0.85,
    created_at: datetime | None = None,
) -> str:
    """Insert a metagen_candidates row; ensures parent item row exists first.

    Returns the new candidate_id as a str (UUID hex).

    spec: src/shared/db/models.py — MetagenCandidate PK candidate_id UUID;
      FK (dataset_urn, item_id) -> metagen_items;
      partial unique index: UNIQUE (dataset_urn, item_id) WHERE status='approved'
    spec: BACKEND.md §UC4 — candidate status in {llm_approved, approved, rejected}
    """
    # Ensure parent item row exists.
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind)"
            " VALUES (:urn, :item_id, 'dataset.description')"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"urn": dataset_urn, "item_id": item_id},
    )

    candidate_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ts = created_at or datetime.now(tz=UTC)

    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_candidates"
            " (candidate_id, dataset_urn, item_id, run_id, value,"
            "  confidence_score, status, evidence, created_at)"
            " VALUES (:candidate_id, :urn, :item_id, :run_id, :value,"
            "         :confidence, :status, '{}'::jsonb, :created_at)"
        ),
        {
            "candidate_id": candidate_id,
            "urn": dataset_urn,
            "item_id": item_id,
            "run_id": run_id,
            "value": value,
            "confidence": confidence,
            "status": status,
            "created_at": ts,
        },
    )
    await session.commit()
    return str(candidate_id)


async def seed_metagen_event(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    detail: dict,
    occurred_at: datetime,
) -> str:
    """Insert a row into dataspoke.events.  Returns the event id as str.

    spec: src/shared/db/models.py — Event table schema
    spec: src/shared/events.py — event_type constants (METAGEN.* prefix)
    """
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dataspoke.events"
            " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
            " VALUES (:id, :etype, :eid, :evtype, 'success',"
            "         CAST(:detail AS jsonb), :occurred_at)"
        ),
        {
            "id": event_id,
            "etype": entity_type,
            "eid": entity_id,
            "evtype": event_type,
            "detail": json.dumps(detail),
            "occurred_at": occurred_at,
        },
    )
    await session.commit()
    return str(event_id)


async def delete_metagen_state_for_urn(
    session: AsyncSession,
    dataset_urn: str,
) -> None:
    """Cascade-delete all metagen rows for dataset_urn.

    Deletion order (FK chain): embeddings -> candidates -> items -> events.
    Each step wrapped in suppress(Exception) so a single failure does not
    abort later cleanup steps.

    spec: src/shared/db/models.py L267 —
      metagen_candidate_embeddings.candidate_id FK -> metagen_candidates.candidate_id
    spec: TESTING.md §Integration Testing — teardown must not leak state
    """
    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.metagen_candidate_embeddings"
                " WHERE candidate_id IN ("
                "   SELECT candidate_id FROM dataspoke.metagen_candidates"
                "   WHERE dataset_urn = :urn"
                " )"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.metagen_candidates WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.metagen_items WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await session.commit()

    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_type = 'dataset' AND entity_id = :urn"
                "   AND event_type LIKE 'METAGEN.%'"
            ),
            {"urn": dataset_urn},
        )
        await session.commit()
