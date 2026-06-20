"""Raw-SQL seed and cleanup helpers for Metadata Generation integration tests.

Provides public helpers used by spot and api-wired tests that need to
pre-populate metagen state without going through the REST API (bypassing LLM
and run-pipeline concerns not under test):

  seed_metagen_conf          — insert a metagen_config row (named conf) → returns UUID
  seed_metagen_boundary      — insert/replace a metagen_boundary row for a dataset URN
  seed_metagen_item          — insert a metagen_items row
  seed_metagen_candidate     — insert a metagen_candidates row (ensures parent item; conf_id)
  seed_metagen_event         — insert a dataspoke.events row for metagen events
  delete_metagen_conf        — delete a metagen_config row (FK-safe)
  delete_metagen_state_for_urn — cascade-delete all metagen rows for a dataset URN

  seed_approved_ontogen_node — insert an approved ontogen_nodes row
  seed_dataset_node_map      — insert a dataset_node_map row
  delete_ontogen_node        — delete an ontogen_nodes row (FK-safe)

  load_fulfillment_doc       — read the UC4 fulfillment document fixture from disk
  FULFILLMENT_DOC_PATH       — pathlib.Path to the markdown fixture file

  seed_uc4_context           — seed all UC4 LLM context + mask DataHub aspects
  restore_uc4_context        — undo everything seed_uc4_context did (idempotent)

  EU_PROFILES_URN            — URN for the customers.eu_profiles dataset
  ORDERS_EVENTS_URN          — URN for the imazon.orders.events dataset
  FULFILLMENT_TAG            — URN for the area:fulfillment tag

spec: spec/TESTING.md §Spot vs Api-Wired Integration Tests — raw-SQL seeding
is the correct approach when the concern under test is review/query behavior,
not the run pipeline that would normally produce the data.
"""

import json
import pathlib
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ── Module-level constants ─────────────────────────────────────────────────────

EU_PROFILES_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)"
)
ORDERS_EVENTS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
FULFILLMENT_TAG = "urn:li:tag:area:fulfillment"

FULFILLMENT_DOC_PATH: pathlib.Path = (
    pathlib.Path(__file__).parent / "fixtures" / "metagen" / "uc4_fulfillment_doc.md"
)


def load_fulfillment_doc() -> str:
    """Return the UC4 fulfillment document body as a string."""
    return FULFILLMENT_DOC_PATH.read_text()


# ── Metagen table helpers ──────────────────────────────────────────────────────


async def seed_metagen_conf(
    session: AsyncSession,
    *,
    name: str,
    is_enabled: bool = True,
    schedule_tier: str | None = None,
    dataset_filter: dict | None = None,  # type: ignore[type-arg]
    result_limit: int = 3,
    overwrite_pending: bool = True,
) -> str:
    """Insert a metagen_config (collection) row via raw SQL; return its UUID as str.

    The conf collection is keyed by `id UUID` with a UNIQUE `name`. Use this when a
    spot test needs a conf present in raw state (e.g. to attach conf_id to seeded
    candidates) without exercising the POST /spoke/metagen/conf route.

    spec: feature/BACKEND_SCHEMA.md §metagen_config — id UUID PK, name UNIQUE
    spec: feature/BACKEND.md §Metadata Generation Service — conf collection
    """
    conf_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_config"
            " (id, name, is_enabled, schedule_tier, dataset_filter,"
            "  result_limit, overwrite_pending)"
            " VALUES (:id, :name, :is_enabled, :tier, CAST(:flt AS jsonb),"
            "         :result_limit, :overwrite_pending)"
        ),
        {
            "id": conf_id,
            "name": name,
            "is_enabled": is_enabled,
            "tier": schedule_tier,
            "flt": json.dumps(dataset_filter or {}),
            "result_limit": result_limit,
            "overwrite_pending": overwrite_pending,
        },
    )
    await session.commit()
    return str(conf_id)


async def seed_metagen_boundary(
    session: AsyncSession,
    *,
    dataset_urn: str,
    is_enabled: bool = True,
    allowed: list[str] | None = None,
    owner: str | None = None,
) -> None:
    """Insert or replace a metagen_boundary row (PK dataset_urn).

    The per-dataset boundary opts a dataset into metagen and caps the writable
    element kinds. Shared across all confs.

    spec: feature/BACKEND_SCHEMA.md §metagen_boundary — dataset_urn PK, allowed TEXT[]
    """
    allowed_arr = allowed if allowed is not None else ["dataset.description", "column.description"]
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_boundary"
            " (dataset_urn, is_enabled, allowed, owner)"
            " VALUES (:urn, :is_enabled, CAST(:allowed AS text[]), :owner)"
            " ON CONFLICT (dataset_urn) DO UPDATE SET"
            "   is_enabled = EXCLUDED.is_enabled,"
            "   allowed = EXCLUDED.allowed,"
            "   owner = EXCLUDED.owner"
        ),
        {
            "urn": dataset_urn,
            "is_enabled": is_enabled,
            "allowed": "{" + ",".join(allowed_arr) + "}",
            "owner": owner,
        },
    )
    await session.commit()


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
    conf_id: str | None = None,
    item_kind: str = "dataset.description",
) -> str:
    """Insert a metagen_candidates row; ensures parent item row exists first.

    `conf_id` is the producing conf's UUID (nullable FK → metagen_config); pass the
    value returned by ``seed_metagen_conf`` to attach the candidate to a conf so the
    candidate response carries conf_id/conf_name. ``None`` leaves it orphaned
    (the post-conf-delete state).

    Returns the new candidate_id as a str (UUID hex).

    spec: feature/BACKEND_SCHEMA.md §metagen_candidates — PK candidate_id UUID;
      conf_id FK (nullable) → metagen_config; FK (dataset_urn, item_id) → metagen_items;
      partial unique index: UNIQUE (dataset_urn, item_id) WHERE status='approved'
    spec: BACKEND.md §UC4 — candidate status in {llm_approved, approved, rejected}
    """
    # Ensure parent item row exists (with the correct kind for column items).
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind)"
            " VALUES (:urn, :item_id, :kind)"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"urn": dataset_urn, "item_id": item_id, "kind": item_kind},
    )

    candidate_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ts = created_at or datetime.now(tz=UTC)

    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_candidates"
            " (candidate_id, conf_id, dataset_urn, item_id, run_id, value,"
            "  confidence_score, status, evidence, created_at)"
            " VALUES (:candidate_id, :conf_id, :urn, :item_id, :run_id, :value,"
            "         :confidence, :status, '{}'::jsonb, :created_at)"
        ),
        {
            "candidate_id": candidate_id,
            "conf_id": conf_id,
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
    detail: dict,  # type: ignore[type-arg]
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

    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.metagen_boundary WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await session.commit()


async def delete_metagen_conf(session: AsyncSession, conf_id: str) -> None:
    """Delete a metagen_config row by id (FK-safe).

    Detaches the conf from its candidates first (the ON DELETE SET NULL is enforced
    at the DB layer, but tests that seed orphan candidates may rely on either path);
    then deletes any per-conf run events and the conf row.

    spec: feature/BACKEND_SCHEMA.md §metagen_candidates — conf_id ON DELETE SET NULL
    spec: TESTING.md §Integration Testing — teardown must not leak state
    """
    with suppress(Exception):
        await session.execute(
            text(
                "UPDATE dataspoke.metagen_candidates SET conf_id = NULL"
                " WHERE conf_id = CAST(:id AS uuid)"
            ),
            {"id": conf_id},
        )
        await session.commit()
    with suppress(Exception):
        await session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_type = 'metagen' AND entity_id = :id"
            ),
            {"id": conf_id},
        )
        await session.commit()
    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.metagen_config WHERE id = CAST(:id AS uuid)"),
            {"id": conf_id},
        )
        await session.commit()


# ── Ontogen table helpers ──────────────────────────────────────────────────────


async def seed_approved_ontogen_node(
    session: AsyncSession,
    node_id: str,
    name: str,
) -> str:
    """Insert an approved ontogen_nodes row via raw SQL (UC3 → UC4 coupling setup).

    Returns the id actually stored — either the newly inserted id or the id of
    the pre-existing row with the same name (idempotent across re-runs).

    spec: BACKEND.md §UC4 — UC4 reads UC3-approved nodes via
    dataset_node_map.status='approved'.
    """
    row = await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_nodes"
            " (id, name, description, confidence_score, status)"
            " VALUES (:id, :name, :desc, :conf, 'approved')"
            " ON CONFLICT (name) DO UPDATE"
            "  SET description = EXCLUDED.description"
            " RETURNING id"
        ),
        {
            "id": node_id,
            "name": name,
            "desc": "Fulfillment domain ontology node",
            "conf": 0.90,
        },
    )
    await session.commit()
    return str(row.scalar_one())


async def seed_dataset_node_map(
    session: AsyncSession,
    *,
    dataset_urn: str,
    node_id: str,
    status: str = "approved",
) -> None:
    """Insert a dataset_node_map row via raw SQL.

    spec: BACKEND.md §UC4 — UC4 reads rows with status='approved'.
    spec: src/shared/db/models.py — DatasetNodeMap schema.
    """
    await session.execute(
        text(
            "INSERT INTO dataspoke.dataset_node_map"
            " (dataset_urn, node_id, confidence_score, status, is_primary)"
            " VALUES (:dataset_urn, :node_id, :conf, :status, false)"
            " ON CONFLICT (dataset_urn, node_id) DO UPDATE SET status = EXCLUDED.status"
        ),
        {
            "dataset_urn": dataset_urn,
            "node_id": node_id,
            "conf": 0.90,
            "status": status,
        },
    )
    await session.commit()


async def delete_ontogen_node(session: AsyncSession, node_id: str) -> None:
    """Delete an ontogen_nodes row. Removes dataset_node_map rows first (FK)."""
    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.dataset_node_map WHERE node_id = :node_id"),
            {"node_id": node_id},
        )
        await session.commit()
    with suppress(Exception):
        await session.execute(
            text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"),
            {"id": node_id},
        )
        await session.commit()


# ── High-level UC4 fixture orchestration ──────────────────────────────────────


async def seed_uc4_context(
    session: AsyncSession,
    *,
    dh_token: str,
    gms_url: str,
) -> dict:  # type: ignore[type-arg]
    """Seed UC4 LLM context and mask DataHub aspects.

    Returns a state dict suitable for passing to restore_uc4_context.

    Actions performed:
    - Seed the fulfillment document via seed_native_document.
    - Seed 5 approved ontogen nodes mapped to both EU_PROFILES_URN and
      ORDERS_EVENTS_URN via seed_dataset_node_map.
    - Snapshot eu_profiles DatasetProperties + SchemaMetadata and
      orders.events SchemaMetadata; capture original descriptions.
    - Mask: blank eu_profiles dataset description, None all eu_profiles field
      descriptions, None first 4 orders.events field descriptions.

    spec: USE_CASE_en.md §UC4 — LLM context seeding + DataHub masking
    spec: TESTING.md §Api-Wired Integration Tests — setup may use raw SQL
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        SchemaMetadataClass,
    )

    from tests.integration.util.datahub import seed_native_document

    # 1a. Seed fulfillment document.
    document_id = uuid.uuid4().hex[:16]
    document_urn = seed_native_document(
        document_id=document_id,
        title="Imazon Fulfillment Process Guide",
        body_markdown=load_fulfillment_doc(),
        related_dataset_urns=[EU_PROFILES_URN, ORDERS_EVENTS_URN],
        token=dh_token,
    )

    # 1b. Seed 5 approved ontogen nodes mapped to both datasets.
    suffix = uuid.uuid4().hex[:8]
    node_names = ["Order", "OrderLine", "Customer", "ShipmentEvent", "DeliveryStatus"]
    node_ids: list[str] = []
    for name in node_names:
        candidate_id = f"uc4-{name.lower()}-{suffix}"
        actual_id = await seed_approved_ontogen_node(session, candidate_id, name)
        node_ids.append(actual_id)
        for urn in (EU_PROFILES_URN, ORDERS_EVENTS_URN):
            await seed_dataset_node_map(session, dataset_urn=urn, node_id=actual_id)

    # 2. Snapshot DataHub aspects before masking, capture original descriptions.
    graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=dh_token))

    eu_props = graph.get_aspect(entity_urn=EU_PROFILES_URN, aspect_type=DatasetPropertiesClass)
    eu_schema = graph.get_aspect(entity_urn=EU_PROFILES_URN, aspect_type=SchemaMetadataClass)
    oe_schema = graph.get_aspect(entity_urn=ORDERS_EVENTS_URN, aspect_type=SchemaMetadataClass)

    # Fail loud when the masking targets are absent from DataHub. Masking is the
    # whole point of UC4 seeding — generating "missing" metadata to predict — so
    # a no-op mask (which silently produces an empty-state file and leaves the
    # datasets fully described) is never acceptable. The usual cause is running
    # --uc4-seed before --reset-seed has ingested customers/orders into DataHub.
    missing = [
        name
        for name, aspect in (
            (f"{EU_PROFILES_URN} datasetProperties", eu_props),
            (f"{EU_PROFILES_URN} schemaMetadata", eu_schema),
            (f"{ORDERS_EVENTS_URN} schemaMetadata", oe_schema),
        )
        if aspect is None
    ]
    if missing:
        raise RuntimeError(
            "UC4 seed cannot mask absent DataHub aspects: "
            + "; ".join(missing)
            + ". Run `--reset-seed` (which ingests the customers/orders schemas) "
            "and let it finish before `--uc4-seed`. These are separate, ordered "
            "commands — the CLI does not let you combine them in one invocation "
            "without reset running first."
        )

    eu_original_dataset_description: str | None = eu_props.description if eu_props else None

    eu_original_field_descs: dict[str, str | None] = {}
    if eu_schema is not None and hasattr(eu_schema, "fields"):
        for f in eu_schema.fields:
            eu_original_field_descs[f.fieldPath] = f.description

    oe_original_field_descs: dict[str, str | None] = {}
    masked_oe_field_paths: list[str] = []
    if oe_schema is not None and hasattr(oe_schema, "fields"):
        for f in oe_schema.fields:
            oe_original_field_descs[f.fieldPath] = f.description
        all_field_paths = [f.fieldPath for f in oe_schema.fields]
        masked_oe_field_paths = all_field_paths[:4]

    # Mask eu_profiles DatasetProperties: blank description, preserve other fields.
    if eu_props is not None:
        masked_eu_props = DatasetPropertiesClass(
            name=eu_props.name,
            qualifiedName=eu_props.qualifiedName,
            description="",
            customProperties=eu_props.customProperties,
        )
        graph.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=EU_PROFILES_URN, aspect=masked_eu_props)
        )

    # Mask eu_profiles SchemaMetadata: None all field descriptions in-place.
    if eu_schema is not None and hasattr(eu_schema, "fields"):
        for f in eu_schema.fields:
            f.description = None
        graph.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=EU_PROFILES_URN, aspect=eu_schema)
        )

    # Mask orders.events SchemaMetadata: None first 4 field descriptions in-place.
    if oe_schema is not None and hasattr(oe_schema, "fields"):
        for f in oe_schema.fields:
            if f.fieldPath in masked_oe_field_paths:
                f.description = None
        graph.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema)
        )

    return {
        "document_urn": document_urn,
        "node_ids": node_ids,
        "eu_original_dataset_description": eu_original_dataset_description,
        "eu_original_field_descs": eu_original_field_descs,
        "oe_original_field_descs": oe_original_field_descs,
        "masked_oe_field_paths": masked_oe_field_paths,
    }


async def restore_uc4_context(
    session: AsyncSession,
    state: dict,  # type: ignore[type-arg]
    *,
    dh_token: str,
    gms_url: str,
) -> None:
    """Undo everything seed_uc4_context did.

    Idempotent — each step is wrapped in suppress(Exception) so a single
    failure does not abort later steps.

    Actions performed (in order):
    - Restore eu_profiles DatasetProperties.description.
    - Restore eu_profiles SchemaMetadata field descriptions.
    - Restore orders.events SchemaMetadata field descriptions.
    - Emit EditableDatasetPropertiesClass(description=None) on eu_profiles.
    - Emit EditableSchemaMetadataClass(editableSchemaFieldInfo=[]) on both URNs.
    - Hard-delete the fulfillment document.
    - delete_metagen_state_for_urn for both URNs.
    - delete_ontogen_node for each node_id in state["node_ids"].

    spec: TESTING.md §Integration Testing — teardown must not leak state
    """
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        EditableDatasetPropertiesClass,
        EditableSchemaMetadataClass,
        SchemaMetadataClass,
    )

    from tests.integration.util.datahub import hard_delete_document

    graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=dh_token))

    # Restore eu_profiles DatasetProperties.description (re-fetch to avoid stale object).
    with suppress(Exception):
        eu_props = graph.get_aspect(entity_urn=EU_PROFILES_URN, aspect_type=DatasetPropertiesClass)
        if eu_props is not None:
            eu_props.description = state.get("eu_original_dataset_description")
            graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=EU_PROFILES_URN, aspect=eu_props)
            )

    # Restore eu_profiles SchemaMetadata field descriptions (re-fetch).
    with suppress(Exception):
        eu_schema = graph.get_aspect(entity_urn=EU_PROFILES_URN, aspect_type=SchemaMetadataClass)
        if eu_schema is not None and hasattr(eu_schema, "fields"):
            original_descs: dict[str, str | None] = state.get("eu_original_field_descs", {})
            for f in eu_schema.fields:
                f.description = original_descs.get(f.fieldPath)
            graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=EU_PROFILES_URN, aspect=eu_schema)
            )

    # Restore orders.events SchemaMetadata field descriptions (re-fetch).
    with suppress(Exception):
        oe_schema = graph.get_aspect(entity_urn=ORDERS_EVENTS_URN, aspect_type=SchemaMetadataClass)
        if oe_schema is not None and hasattr(oe_schema, "fields"):
            oe_original_descs: dict[str, str | None] = state.get("oe_original_field_descs", {})
            for f in oe_schema.fields:
                f.description = oe_original_descs.get(f.fieldPath)
            graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema)
            )

    # Clear approve-flow side effects: editable aspects.
    with suppress(Exception):
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=EU_PROFILES_URN,
                aspect=EditableDatasetPropertiesClass(description=None),
            )
        )
    with suppress(Exception):
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=EU_PROFILES_URN,
                aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
            )
        )
    with suppress(Exception):
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=ORDERS_EVENTS_URN,
                aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
            )
        )

    # Hard-delete fulfillment document.
    document_urn: str | None = state.get("document_urn")
    if document_urn:
        with suppress(Exception):
            hard_delete_document(document_urn=document_urn, token=dh_token)

    # Delete metagen state for both datasets.
    with suppress(Exception):
        await delete_metagen_state_for_urn(session, EU_PROFILES_URN)
    with suppress(Exception):
        await delete_metagen_state_for_urn(session, ORDERS_EVENTS_URN)

    # Delete ontogen nodes.
    for nid in state.get("node_ids", []):
        with suppress(Exception):
            await delete_ontogen_node(session, nid)
