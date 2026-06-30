"""DataHub dummy-data reset/ingest utilities for integration tests.

Registers example-postgres tables and example-kafka topics as DataHub
dataset entities.

Usage (as a module):
    uv run python -m tests.integration.util.datahub          # ingest
    uv run python -m tests.integration.util.datahub --reset  # delete + ingest
    uv run python -m tests.integration.util.datahub --reset-only  # delete only

Environment variables (loaded from helm-charts/.env.dev if present):
    DATASPOKE_TEST_DATAHUB_GMS_URL       (default: http://localhost:9004)
    DATASPOKE_TEST_DATAHUB_TOKEN         (required for DataHub-touching helpers)
    DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST                        (default: localhost)
    DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT                        (default: 9102)
    DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER      (default: postgres)
    DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD  (default: ExampleDev2024!)
    DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB        (default: example_db)
    DATASPOKE_DEV_DUMMY_DATA_KAFKA_INSTANCE     (default: example_kafka)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import asyncpg
import requests
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mcp_builder import DatabaseKey, SchemaKey, gen_containers
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    AssertionInfoClass,
    AuditStampClass,
    BooleanTypeClass,
    BrowsePathEntryClass,
    BrowsePathsV2Class,
    BytesTypeClass,
    ContainerClass,
    ContainerPropertiesClass,
    DataPlatformInstanceClass,
    DatasetFieldProfileClass,
    DatasetProfileClass,
    DatasetPropertiesClass,
    DateTypeClass,
    DocumentContentsClass,
    DocumentInfoClass,
    DocumentSourceClass,
    DocumentStatusClass,
    GlobalTagsClass,
    MapTypeClass,
    NullTypeClass,
    NumberTypeClass,
    OperationClass,
    OperationTypeClass,
    OtherSchemaClass,
    RelatedAssetClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StatusClass,
    StringTypeClass,
    TagAssociationClass,
    TimeTypeClass,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_PLATFORM = "postgres"
KAFKA_PLATFORM = "kafka"
ENV = "DEV"
PG_INSTANCE = "example_db"

# Business-area tags applied to seeded datasets. Catalog covers product master
# data and customer reviews; fulfillment covers customer profiles, daily
# fulfillment aggregates, carrier scans, and the order/shipping Kafka streams.
TAG_AREA_CATALOG = "urn:li:tag:area:catalog"
TAG_AREA_FULFILLMENT = "urn:li:tag:area:fulfillment"

_PG_DATASET_AREA_TAGS: dict[str, str] = {
    "catalog.title_master": TAG_AREA_CATALOG,
    "catalog.editions": TAG_AREA_CATALOG,
    "reviews.user_ratings": TAG_AREA_CATALOG,
    "customers.eu_profiles": TAG_AREA_FULFILLMENT,
    "orders.daily_fulfillment_summary": TAG_AREA_FULFILLMENT,
    "shipping.carrier_status": TAG_AREA_FULFILLMENT,
}

_KAFKA_TOPIC_AREA_TAGS: dict[str, str] = {
    "imazon.orders.events": TAG_AREA_FULFILLMENT,
    "imazon.shipping.updates": TAG_AREA_FULFILLMENT,
}

# Dataspoke operational DB — used to reconcile dataset_registry rows whose
# datahub_registered cache was frozen False before this ingest.
_DATASPOKE_PG_HOST = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
_DATASPOKE_PG_PORT = int(os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201"))
_DATASPOKE_PG_USER = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
_DATASPOKE_PG_PASSWORD = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
_DATASPOKE_PG_DB = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")


async def _mark_registry_registered(urns: list[str]) -> None:
    """Flip dataset_registry.datahub_registered=true for any existing rows in `urns`.

    Why: ensure_dataset_registered() only queries DataHub on the row's first
    insert and never refreshes. A row created before its dataset existed in
    DataHub stays frozen at false, blocking validation (require_in_datahub=True).
    """
    if not urns:
        return
    conn = await asyncpg.connect(
        host=_DATASPOKE_PG_HOST,
        port=_DATASPOKE_PG_PORT,
        user=_DATASPOKE_PG_USER,
        password=_DATASPOKE_PG_PASSWORD,
        database=_DATASPOKE_PG_DB,
    )
    try:
        await conn.execute(
            "UPDATE dataset_registry "
            "SET datahub_registered = TRUE, updated_at = NOW() "
            "WHERE dataset_urn = ANY($1::text[]) AND datahub_registered = FALSE",
            urns,
        )
    finally:
        await conn.close()


TARGET_SCHEMAS: frozenset[str] = frozenset(
    {
        "catalog",
        "orders",
        "customers",
        "reviews",
        "shipping",
    }
)

_PG_TO_DATAHUB_TYPE: dict[str, object] = {
    "integer": NumberTypeClass(),
    "bigint": NumberTypeClass(),
    "smallint": NumberTypeClass(),
    "numeric": NumberTypeClass(),
    "real": NumberTypeClass(),
    "double precision": NumberTypeClass(),
    "boolean": BooleanTypeClass(),
    "text": StringTypeClass(),
    "character varying": StringTypeClass(),
    "character": StringTypeClass(),
    "varchar": StringTypeClass(),
    "char": StringTypeClass(),
    "date": DateTypeClass(),
    "timestamp with time zone": TimeTypeClass(),
    "timestamp without time zone": TimeTypeClass(),
    "time with time zone": TimeTypeClass(),
    "time without time zone": TimeTypeClass(),
    "jsonb": StringTypeClass(),
    "json": StringTypeClass(),
    "uuid": StringTypeClass(),
    "bytea": BytesTypeClass(),
    "ARRAY": ArrayTypeClass(),
}

# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load helm-charts/.env.dev into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "helm-charts" / ".env.dev"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "http://localhost:9004")
_token_env = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

_pg_host = os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST", "localhost")
_pg_port = int(os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT", "9102"))
_pg_user = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")
_pg_password = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "ExampleDev2024!")
_pg_db = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB", "example_db")

_kafka_instance = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_KAFKA_INSTANCE", "example_kafka")

# ---------------------------------------------------------------------------
# Lazy token resolution — never called at module import time
# ---------------------------------------------------------------------------

_token: str | None = None


def _resolve_token() -> str | None:
    """Return the DataHub auth token from the environment, or None if unset."""
    return _token_env or None


def _get_token() -> str | None:
    """Return the cached token, resolving it lazily on first call."""
    global _token
    if _token is None:
        _token = _resolve_token()
    return _token


def get_datahub_token() -> str:
    """Return a valid DataHub token for integration-test helpers.

    Raises RuntimeError if no token could be obtained (DataHub unreachable).
    Use this to supply the ``token`` argument to ``seed_native_document`` /
    ``soft_delete_document`` / ``hard_delete_document`` inside test bodies
    that do not receive the ``datahub_client`` fixture directly.
    """
    tok = _get_token()
    if not tok:
        raise RuntimeError(
            "Cannot obtain a DataHub token. Set DATASPOKE_TEST_DATAHUB_TOKEN "
            "(populated from helm-charts/.env.dev by the install scripts)."
        )
    return tok


# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------


def _make_pg_urn(schema: str, table: str) -> str:
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:{PG_PLATFORM},{PG_INSTANCE}.{schema}.{table},{ENV})"
    )


def _make_kafka_urn(topic: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{KAFKA_PLATFORM},{_kafka_instance}.{topic},{ENV})"


# ---------------------------------------------------------------------------
# Document entity helpers (integration-test seeding and cleanup)
# ---------------------------------------------------------------------------

_DATASPOKE_ACTOR_URN = "urn:li:corpuser:dataspoke"


def seed_native_document(
    *,
    document_id: str,
    title: str,
    body_markdown: str,
    related_dataset_urns: list[str],
    token: str,
) -> str:
    """Emit a NATIVE document entity for integration-test seeding. Returns the URN.

    Spec: spec/DATAHUB_INTEGRATION.md §Document Aspects.
    Sets documentInfo with title, contents.text=body_markdown, relatedAssets,
    source=NATIVE, status=PUBLISHED, created=now, lastModified=now.

    The caller supplies a deterministic ``document_id`` (e.g. a uuid hex prefix)
    so that cleanup via ``soft_delete_document`` / ``hard_delete_document`` is
    unambiguous even when the test aborts mid-run.
    """
    urn = f"urn:li:document:{document_id}"
    now_ms = int(time.time() * 1000)
    audit = AuditStampClass(time=now_ms, actor=_DATASPOKE_ACTOR_URN)

    info = DocumentInfoClass(
        title=title,
        contents=DocumentContentsClass(text=body_markdown),
        relatedAssets=[RelatedAssetClass(asset=u) for u in related_dataset_urns],
        source=DocumentSourceClass(sourceType="NATIVE"),
        status=DocumentStatusClass(state="PUBLISHED"),
        created=audit,
        lastModified=audit,
    )
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))
    graph.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=info))
    return urn


def soft_delete_document(*, document_urn: str, token: str) -> None:
    """Emit StatusClass(removed=True) on the document URN. Idempotent.

    Spec: spec/DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke never
    hard-deletes documents; soft-delete uses Status.removed=true.
    """
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))
    graph.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=document_urn, aspect=StatusClass(removed=True))
    )


def hard_delete_document(*, document_urn: str, token: str) -> None:
    """Wipe the entity from DataHub via SDK hard-delete (test cleanup only).

    Use in test teardown only — spec/DATAHUB_INTEGRATION.md states DataSpoke
    never hard-deletes documents in production. Hard-delete is used here so
    that repeated test runs start from a clean baseline.
    """
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))
    graph.hard_delete_entity(document_urn)


# ---------------------------------------------------------------------------
# Discover tables + columns from example-postgres (async via asyncpg)
# ---------------------------------------------------------------------------


async def discover_catalog_tables() -> set[str]:
    """Return the set of catalog-schema URNs found in example-postgres.

    Used by integration tests that seed only the catalog schema (spec:
    project_datahub_resolvable_urns_catalog_only).  Raises AssertionError
    if the query returns no tables — an empty result means example-postgres
    is not seeded, which is an environment failure that must fail loudly
    rather than silently passing a set-equality.

    F1 fix: non-empty floor guard so vacuous equality on empty discovery
    cannot silently pass.
    """
    tables = await discover_tables(schemas=frozenset({"catalog"}))
    assert tables, (
        "discover_catalog_tables() returned no tables in the 'catalog' schema. "
        "The dummy-data postgres is not seeded — run "
        "'uv run python -m tests.integration.util --reset-seed' before "
        "executing integration tests."
    )
    return set(tables.keys())


async def discover_tables(
    schemas: frozenset[str] | None = None,
) -> dict[str, list[dict]]:  # type: ignore[type-arg]
    """Return {urn: [column_dicts]} by querying information_schema.

    Args:
        schemas: Set of schema names to discover.  Defaults to TARGET_SCHEMAS.
    """
    effective_schemas = schemas if schemas is not None else TARGET_SCHEMAS

    conn = await asyncpg.connect(
        host=_pg_host,
        port=_pg_port,
        user=_pg_user,
        password=_pg_password,
        database=_pg_db,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                   c.ordinal_position, c.is_nullable,
                   col_description(
                       format('%I.%I', c.table_schema, c.table_name)::regclass,
                       c.ordinal_position
                   ) AS column_comment,
                   obj_description(
                       format('%I.%I', c.table_schema, c.table_name)::regclass,
                       'pg_class'
                   ) AS table_comment
            FROM information_schema.columns c
            WHERE c.table_schema = ANY($1::text[])
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """,
            sorted(effective_schemas),
        )
    finally:
        await conn.close()

    datasets: dict[str, list[dict]] = {}  # type: ignore[type-arg]
    for row in rows:
        urn = _make_pg_urn(row["table_schema"], row["table_name"])
        datasets.setdefault(urn, []).append(
            {
                "schema": row["table_schema"],
                "table": row["table_name"],
                "name": row["column_name"],
                "native_type": row["data_type"],
                "ordinal": row["ordinal_position"],
                "nullable": row["is_nullable"] == "YES",
                "description": row["column_comment"],
                "table_description": row["table_comment"],
            }
        )
    return datasets


# ---------------------------------------------------------------------------
# Reset: soft-delete all datasets from the example_db platform instance
# ---------------------------------------------------------------------------


def reset_datasets() -> int:
    """Hard-delete all example_db and example_kafka datasets from DataHub.

    Also hard-deletes any DataProcessInstance entities that produced one of
    those datasets (UC1 ingestion runs), since DPIs are useless once the
    dataset they reference is gone.

    Hard-delete (vs. the prior soft-delete) wipes both versioned aspects
    (datasetProperties, schemaMetadata, status, etc.) and timeseries aspects
    (datasetProfile, datasetUsageStatistics, run events) in one pass — leaving
    no stale history for the next test run to trip over.

    Returns total count deleted (datasets + DPIs).
    """
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))

    dataset_urns: list[str] = []

    # PostgreSQL datasets
    pg_prefix = f"urn:li:dataset:(urn:li:dataPlatform:{PG_PLATFORM},{PG_INSTANCE}."
    for u in graph.get_urns_by_filter(entity_types=["dataset"], platform=PG_PLATFORM):
        if u.startswith(pg_prefix):
            dataset_urns.append(u)

    # Kafka datasets
    kafka_prefix = f"urn:li:dataset:(urn:li:dataPlatform:{KAFKA_PLATFORM},{_kafka_instance}."
    for u in graph.get_urns_by_filter(entity_types=["dataset"], platform=KAFKA_PLATFORM):
        if u.startswith(kafka_prefix):
            dataset_urns.append(u)

    if not dataset_urns:
        print("  No existing dummy-data datasets to delete.")
        return 0

    # Discover DPIs that produced any of these datasets, so they vanish
    # alongside the datasets they reference.
    dpi_urns: set[str] = set()
    for dataset_urn in dataset_urns:
        for rel in graph.get_related_entities(
            entity_urn=dataset_urn,
            relationship_types=["Produces"],
            direction=DataHubGraph.RelationshipDirection.INCOMING,
        ):
            if rel.urn.startswith("urn:li:dataProcessInstance:"):
                dpi_urns.add(rel.urn)

    for urn in dataset_urns:
        graph.hard_delete_entity(urn)
    for urn in dpi_urns:
        graph.hard_delete_entity(urn)

    if dpi_urns:
        print(f"  Hard-deleted {len(dataset_urns)} datasets and {len(dpi_urns)} DPIs.")
    else:
        print(f"  Hard-deleted {len(dataset_urns)} datasets.")
    return len(dataset_urns) + len(dpi_urns)


_DATASPOKE_ASSERTION_MARKER = "DATASPOKE_VALIDATION"


def _list_urns_incl_soft_deleted(entity_type: str) -> list[str]:
    """Return every URN of an entity type, including soft-deleted ones.

    `DataHubGraph.get_urns_by_filter` uses the search index, which by default
    excludes entities with `status.removed = true`. That means a soft-deleted
    DataSpoke assertion or container slips past the cleanup sweep and
    accumulates as an orphan in MySQL. This helper hits the OpenAPI search
    endpoint with `includeSoftDeleted=true` so cleanup actually finds it.
    """
    token = _get_token()
    urns: list[str] = []
    start = 0
    page_size = 100
    while True:
        resp = requests.post(
            f"{_gms_url}/entities?action=searchAcrossEntities",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-RestLi-Protocol-Version": "2.0.0",
            },
            json={
                "input": "*",
                "entities": [entity_type],
                "start": start,
                "count": page_size,
                "searchFlags": {"includeSoftDeleted": True},
            },
            timeout=30,
        )
        resp.raise_for_status()
        page = [e["entity"] for e in resp.json().get("value", {}).get("entities", [])]
        urns.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return urns


def reset_assertions() -> int:
    """Hard-delete DataSpoke-emitted assertion entities left over from prior runs.

    Identifies DataSpoke assertions via `assertionInfo.customAssertion.type ==
    "DATASPOKE_VALIDATION"` (set by `build_assertion_info` in
    src/backend/validation/assertions.py). Only assertions carrying that marker
    are touched; all others are left in place.

    Enumerates via the soft-deleted-aware helper so an assertion that was
    soft-deleted by a prior test's `finally` block doesn't slip past the sweep.
    """
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))

    urns: list[str] = []
    for urn in _list_urns_incl_soft_deleted("assertion"):
        info = graph.get_aspect(entity_urn=urn, aspect_type=AssertionInfoClass)
        if info is None:
            continue
        custom = getattr(info, "customAssertion", None)
        if custom is not None and getattr(custom, "type", None) == _DATASPOKE_ASSERTION_MARKER:
            urns.append(urn)

    if not urns:
        print("  Hard-deleted 0 assertions.")
        return 0

    for urn in urns:
        graph.hard_delete_entity(urn)

    print(f"  Hard-deleted {len(urns)} assertions.")
    return len(urns)


def reset_containers() -> int:
    """Hard-delete postgres/kafka containers from prior runs.

    Selection logic (any match triggers deletion):
    1. ``dataPlatformInstance.platform`` URN ends with ``:postgres`` or ``:kafka``
       — catches containers emitted by both managed ingestion and the fixture util.
    2. ``containerProperties.customProperties.platform`` equals ``postgres`` or
       ``kafka`` — legacy fallback for older residue that predates the platform
       instance aspect.
    3. If the ``dataPlatformInstance`` aspect is absent and neither legacy check
       matches, hard-delete anyway (dev-env is single-tenant; unknown containers
       are safe to wipe so they don't accumulate as orphans).
    """
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))

    _pg_suffix = f":{PG_PLATFORM}"
    _kafka_suffix = f":{KAFKA_PLATFORM}"

    deleted = 0
    for urn in _list_urns_incl_soft_deleted("container"):
        # Check dataPlatformInstance aspect first (present on containers emitted
        # by both managed ingestion and the updated fixture util).
        dpi = graph.get_aspect(entity_urn=urn, aspect_type=DataPlatformInstanceClass)
        if dpi is not None:
            platform_urn: str = getattr(dpi, "platform", "") or ""
            if platform_urn.endswith(_pg_suffix) or platform_urn.endswith(_kafka_suffix):
                graph.hard_delete_entity(urn)
                deleted += 1
                continue
            # dataPlatformInstance exists but is neither PG nor Kafka — skip.
            continue

        # dataPlatformInstance absent: check legacy customProperties marker.
        props = graph.get_aspect(entity_urn=urn, aspect_type=ContainerPropertiesClass)
        if props is not None:
            custom = props.customProperties or {}
            if custom.get("platform") in (PG_PLATFORM, KAFKA_PLATFORM):
                graph.hard_delete_entity(urn)
                deleted += 1
                continue

        # Neither aspect present — dev-env single-tenant fallback: wipe it.
        graph.hard_delete_entity(urn)
        deleted += 1

    print(f"  Hard-deleted {deleted} containers.")
    return deleted


def reset_glossary_terms() -> int:
    """Hard-delete every glossary term in dev-env DataHub.

    DataSpoke ontogen is the only glossary-term emitter, so a non-empty term
    set after reset-all is always residue from a prior run. This includes the
    body-less placeholder terms that ontogen emits via the `GlossaryTerms`
    association on a dataset (the term URN appears in the index even when no
    `GlossaryTermInfo` aspect was ever set).
    """
    deleted = 0
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))
    for urn in _list_urns_incl_soft_deleted("glossaryTerm"):
        graph.hard_delete_entity(urn)
        deleted += 1
    print(f"  Hard-deleted {deleted} glossary terms.")
    return deleted


def reset_documents() -> int:
    """Hard-delete every dataDocument entity in dev-env DataHub.

    The Imazon seed never emits documents — only datasets, containers, and
    glossary terms. Any urn:li:document:* present in dev-env is residue from
    prior spot/api-wired tests (metagen cross_data.md create-action approvals,
    UI manual "New Document" clicks, or seed_native_document() calls that
    aborted before their cleanup ran).
    """
    deleted = 0
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))
    for urn in _list_urns_incl_soft_deleted("document"):
        graph.hard_delete_entity(urn)
        deleted += 1
    print(f"  Hard-deleted {deleted} documents.")
    return deleted


def hard_delete_dataspoke_assertions_for_dataset(dataset_urn: str) -> int:
    """Hard-delete every DataSpoke-emitted assertion attached to one dataset URN.

    Used by the api-wired `purge_urns` conftest fixture so a per-test purge
    clears DataHub-side state (versioned aspects + timeseries run events) in
    addition to the operational DB.
    """
    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))

    related = graph.get_related_entities(
        entity_urn=dataset_urn,
        relationship_types=["Asserts"],
        direction=DataHubGraph.RelationshipDirection.INCOMING,
    )

    deleted = 0
    for rel in related:
        urn = rel.urn
        info = graph.get_aspect(entity_urn=urn, aspect_type=AssertionInfoClass)
        if info is None:
            continue
        custom = getattr(info, "customAssertion", None)
        if custom is None or getattr(custom, "type", None) != _DATASPOKE_ASSERTION_MARKER:
            continue
        graph.hard_delete_entity(urn)
        deleted += 1
    return deleted


def hard_delete_documents_for_dataset(dataset_urn: str) -> int:
    """Hard-delete every document entity whose relatedAssets references ``dataset_urn``.

    Used by spot/api-wired test teardown for flows (e.g. metagen cross_data.md
    create-action approval) where the spot test triggers a document emission
    but does not know the random URN. Filters by the ``IsRelatedTo`` relationship
    so unrelated documents are left untouched.
    """
    from datahub.metadata.schema_classes import DocumentInfoClass

    token = _get_token()
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=token))

    deleted = 0
    for urn in _list_urns_incl_soft_deleted("document"):
        info = graph.get_aspect(entity_urn=urn, aspect_type=DocumentInfoClass)
        if info is None:
            continue
        related = getattr(info, "relatedAssets", None) or []
        if any(getattr(r, "asset", None) == dataset_urn for r in related):
            graph.hard_delete_entity(urn)
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Search-index readiness gate (eventual-consistency, not timeout inflation)
# ---------------------------------------------------------------------------


def wait_until_datasets_searchable(
    expected_urns: set[str],
    platform: str,
    timeout: float = 120.0,
    interval: float = 3.0,
) -> None:
    """Block until ``expected_urns`` are visible through the search-backed read path.

    Eventual-consistency gate, NOT a timeout inflation to mask infra. The
    DataHub REST emitter writes aspects to GMS synchronously, but the
    Elasticsearch search index that ``get_urns_by_filter`` (and the backend
    sync sweep's ``enumerate_datasets``) read from lags that write by a few
    seconds. Without this gate, a freshly emitted dataset can be invisible to
    the very next search-backed read, so a sync sweep run immediately after
    seeding maps only the already-indexed subset.

    The poll exits as soon as every expected URN is searchable (normally a few
    seconds). ``timeout`` is only an upper bound before declaring a genuine
    seeding failure — on timeout this raises ``TimeoutError`` naming the URNs
    still missing, so a real failure is loud rather than silently passed.

    Scope: this gates the platform/name search index (``platform``-filtered),
    which is a proxy for the sweep's unscoped ``enumerate_datasets`` read — it is
    strictly narrower (a URN visible under its platform facet is visible
    unscoped), so it cannot pass before the sweep would see the URN. It does NOT
    cover the slower tag/glossary-term index, which lags longer (~2-3 min); a
    caller depending on tag-search readiness needs a separate, longer gate.

    spec: project_es_indexing_lag_after_reset_seed — ES indexing lags GMS emit.
    spec: TESTING.md §Per-Module Dummy-Data Reset — ingest post-condition is
          "emitted AND searchable" so the sync sweep sees the full universe.
    """
    if not expected_urns:
        return

    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=_get_token()))
    deadline = time.time() + timeout
    seen: set[str] = set()
    while True:
        seen = set(graph.get_urns_by_filter(entity_types=["dataset"], platform=platform))
        if expected_urns <= seen:
            return
        if time.time() >= deadline:
            missing = sorted(expected_urns - seen)
            raise TimeoutError(
                f"Datasets not searchable on platform {platform!r} after {timeout:.0f}s. "
                f"Still missing {len(missing)} URN(s): {missing}. "
                "GMS emitted them but the ES search index never caught up — "
                "this is a genuine seeding/infra failure, not a poll-too-short."
            )
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Ingest: emit DatasetProperties + SchemaMetadata for each table
# ---------------------------------------------------------------------------


def _build_schema_fields(columns: list[dict]) -> list[SchemaFieldClass]:  # type: ignore[type-arg]
    fields = []
    for col in columns:
        fields.append(
            SchemaFieldClass(
                fieldPath=col["name"],
                nativeDataType=col["native_type"],
                type=SchemaFieldDataTypeClass(
                    type=_PG_TO_DATAHUB_TYPE.get(col["native_type"], StringTypeClass()),
                ),
                nullable=col["nullable"],
                description=col.get("description") or None,
            )
        )
    return fields


async def _fetch_row_counts(
    schemas: frozenset[str],
) -> dict[tuple[str, str], int]:
    """Return {(schema, table): n_live_tup} from pg_stat_all_tables."""
    conn = await asyncpg.connect(
        host=_pg_host,
        port=_pg_port,
        user=_pg_user,
        password=_pg_password,
        database=_pg_db,
    )
    try:
        rows = await conn.fetch(
            "SELECT schemaname, relname, n_live_tup "
            "FROM pg_stat_all_tables "
            "WHERE schemaname = ANY($1::text[])",
            sorted(schemas),
        )
    finally:
        await conn.close()
    return {(r["schemaname"], r["relname"]): r["n_live_tup"] for r in rows}


def _quote_ident(ident: str) -> str:
    """Return a double-quoted PostgreSQL identifier, asserting safe characters."""
    assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", ident), (
        f"Identifier contains unsafe characters: {ident!r}"
    )
    return f'"{ident}"'


async def _fetch_null_counts(
    datasets: dict[str, list[dict]],  # type: ignore[type-arg]
) -> dict[tuple[str, str], dict[str, int]]:
    """Return {(schema, table): {column_name: null_count}} for all discovered tables.

    Issues one round-trip per table using COUNT(*) FILTER (WHERE col IS NULL).
    """
    conn = await asyncpg.connect(
        host=_pg_host,
        port=_pg_port,
        user=_pg_user,
        password=_pg_password,
        database=_pg_db,
    )
    result: dict[tuple[str, str], dict[str, int]] = {}
    try:
        for columns in datasets.values():
            if not columns:
                continue
            schema = columns[0]["schema"]
            table = columns[0]["table"]
            col_exprs = ", ".join(
                f"COUNT(*) FILTER (WHERE {_quote_ident(col['name'])} IS NULL) "
                f"AS {_quote_ident(col['name'])}"
                for col in columns
            )
            sql = f"SELECT {col_exprs} FROM {_quote_ident(schema)}.{_quote_ident(table)}"
            row = await conn.fetchrow(sql)
            result[(schema, table)] = {col["name"]: row[col["name"]] for col in columns}
    finally:
        await conn.close()
    return result


async def ingest_pg_datasets(schemas: frozenset[str] | None = None) -> int:
    """Discover tables and emit metadata to DataHub. Returns count ingested.

    Args:
        schemas: Optional subset of schemas to ingest.  Defaults to all
                 TARGET_SCHEMAS.
    """
    token = _get_token()
    datasets = await discover_tables(schemas=schemas)
    if not datasets:
        print("  No tables found in example-postgres. Run postgres.reset_all() first.")
        return 0

    effective_schemas = schemas if schemas is not None else TARGET_SCHEMAS
    row_counts = await _fetch_row_counts(effective_schemas)
    null_counts = await _fetch_null_counts(datasets)

    emitter = DatahubRestEmitter(gms_server=_gms_url, token=token)

    # Emit database container once (idempotent — DataHub merges by URN).
    db_key = DatabaseKey(
        database=PG_INSTANCE,
        platform=PG_PLATFORM,
        instance=None,
        env=ENV,
        backcompat_env_as_instance=True,
    )
    for wu in gen_containers(container_key=db_key, name=PG_INSTANCE, sub_types=["Database"]):
        mcp = wu.metadata
        if (
            hasattr(mcp, "entityUrn") and hasattr(mcp, "aspect")
            and mcp.entityUrn and mcp.aspect
        ):
            emitter.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=mcp.entityUrn, aspect=mcp.aspect)
            )

    # Emit schema containers (one per distinct schema, idempotent).
    schemas_seen: set[str] = set()

    for urn, columns in datasets.items():
        schema = columns[0]["schema"]
        table = columns[0]["table"]

        if schema not in schemas_seen:
            schemas_seen.add(schema)
            schema_key = SchemaKey(
                database=PG_INSTANCE,
                schema=schema,
                platform=PG_PLATFORM,
                instance=None,
                env=ENV,
                backcompat_env_as_instance=True,
            )
            for wu in gen_containers(
                container_key=schema_key,
                name=schema,
                sub_types=["Schema"],
                parent_container_key=db_key,
            ):
                mcp = wu.metadata
                if (
                    hasattr(mcp, "entityUrn") and hasattr(mcp, "aspect")
                    and mcp.entityUrn and mcp.aspect
                ):
                    emitter.emit_mcp(
                        MetadataChangeProposalWrapper(entityUrn=mcp.entityUrn, aspect=mcp.aspect)
                    )

        schema_key_for_dataset = SchemaKey(
            database=PG_INSTANCE,
            schema=schema,
            platform=PG_PLATFORM,
            instance=None,
            env=ENV,
            backcompat_env_as_instance=True,
        )

        # 1. Mark as not-deleted (undo any previous soft-delete)
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StatusClass(removed=False),
            )
        )

        # 2. Link dataset to its schema container
        _schema_container_urn = schema_key_for_dataset.as_urn()
        _db_container_urn = db_key.as_urn()
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=ContainerClass(container=_schema_container_urn),
            )
        )

        # 2b. Explicit BrowsePathsV2 with container URN refs — DataHub's server-side
        # generation from Container is unreliable across re-emits; upstream sources
        # also emit explicitly via auto_browse_path_v2.
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=BrowsePathsV2Class(
                    path=[
                        BrowsePathEntryClass(id=_db_container_urn, urn=_db_container_urn),
                        BrowsePathEntryClass(id=_schema_container_urn, urn=_schema_container_urn),
                    ]
                ),
            )
        )

        # 3. DatasetProperties
        table_description = columns[0].get("table_description") if columns else None
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetPropertiesClass(
                    name=f"{schema}.{table}",
                    qualifiedName=f"{PG_INSTANCE}.{schema}.{table}",
                    description=table_description or f"Imazon example table: {schema}.{table}",
                    customProperties={
                        "source": "dummy-data-ingest",
                        "schema": schema,
                        "database": PG_INSTANCE,
                    },
                ),
            )
        )

        # 4. SchemaMetadata
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=SchemaMetadataClass(
                    schemaName=f"{schema}.{table}",
                    platform=f"urn:li:dataPlatform:{PG_PLATFORM}",
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=_build_schema_fields(columns),
                ),
            )
        )

        # 5. Operation record (enables freshness validation checks)
        now_ms = int(time.time() * 1000)
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=OperationClass(
                    timestampMillis=now_ms,
                    lastUpdatedTimestamp=now_ms,
                    operationType=OperationTypeClass.INSERT,
                ),
            )
        )

        # 6. DatasetProfile (enables row-count and field-metric validation checks)
        row_count = row_counts.get((schema, table), 0)
        table_null_counts = null_counts.get((schema, table), {})
        field_profiles = [
            DatasetFieldProfileClass(
                fieldPath=col["name"],
                nullCount=table_null_counts.get(col["name"]),
                nullProportion=(
                    table_null_counts[col["name"]] / row_count
                    if row_count > 0 and col["name"] in table_null_counts
                    else None
                ),
            )
            for col in columns
        ]
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetProfileClass(
                    timestampMillis=now_ms,
                    rowCount=row_count,
                    columnCount=len(columns),
                    fieldProfiles=field_profiles,
                ),
            )
        )

        # 7. GlobalTags — business-area tag for cross-dataset filtering
        area_tag = _PG_DATASET_AREA_TAGS.get(f"{schema}.{table}")
        if area_tag is not None:
            emitter.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=urn,
                    aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=area_tag)]),
                )
            )

    await _mark_registry_registered(list(datasets.keys()))

    # Gate the post-condition on "emitted AND searchable" so callers (the
    # module fixture, --reset-seed) don't return before the backend sync
    # sweep's search-backed read can see these table datasets.
    # spec: project_es_indexing_lag_after_reset_seed
    wait_until_datasets_searchable(set(datasets.keys()), platform=PG_PLATFORM)

    print(
        f"  Ingested {len(datasets)} PG datasets "
        f"({sum(len(c) for c in datasets.values())} columns)."
    )
    return len(datasets)


# ---------------------------------------------------------------------------
# Discover Kafka topic schemas from JSONL fixtures
# ---------------------------------------------------------------------------

_JSON_TO_DATAHUB_TYPE: dict[str, object] = {
    "str": StringTypeClass(),
    "int": NumberTypeClass(),
    "float": NumberTypeClass(),
    "bool": BooleanTypeClass(),
    "list": ArrayTypeClass(),
    "dict": MapTypeClass(),
    "NoneType": NullTypeClass(),
}


def _load_kafka_meta(topic: str) -> dict:  # type: ignore[type-arg]
    """Load optional <topic>.meta.json beside JSONL fixtures.

    Returns the parsed dict if present, else an empty dict.
    The meta.json format is: {"description": str, "fields": {<name>: <description>}}.
    """
    meta_path = Path(__file__).parent / "fixtures" / "kafka" / f"{topic}.meta.json"
    if meta_path.is_file():
        return json.loads(meta_path.read_text())
    return {}


def _discover_kafka_topics() -> dict[str, tuple[list[dict], int, str | None]]:  # type: ignore[type-arg]
    """Return {urn: ([field_dicts], message_count, topic_description)} from JSONL fixtures.

    Unions all keys across all messages in each topic's JSONL file, inferring
    field types from the first non-null occurrence.  Message count is the
    number of non-empty lines in the JSONL file.  If a sibling ``<topic>.meta.json``
    exists, per-field descriptions and the topic-level description are merged in.
    """
    from tests.integration.util.kafka import ALL_TOPICS

    _kafka_fixtures_dir = Path(__file__).parent / "fixtures" / "kafka"
    datasets: dict[str, tuple[list[dict], int, str | None]] = {}  # type: ignore[type-arg]

    for topic, jsonl_file in ALL_TOPICS.items():
        urn = _make_kafka_urn(topic)
        field_types: dict[str, str] = {}
        fixture_path = _kafka_fixtures_dir / jsonl_file
        message_count = 0
        for line in fixture_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            message_count += 1
            msg = json.loads(line)
            for key, value in msg.items():
                if key not in field_types and value is not None:
                    field_types[key] = type(value).__name__

        meta = _load_kafka_meta(topic)
        topic_description: str | None = meta.get("description") or None
        field_descriptions: dict[str, str] = meta.get("fields", {})

        fields = []
        for ordinal, (key, py_type) in enumerate(field_types.items(), start=1):
            fields.append(
                {
                    "name": key,
                    "native_type": py_type,
                    "ordinal": ordinal,
                    "nullable": True,
                    "description": field_descriptions.get(key) or None,
                }
            )
        datasets[urn] = (fields, message_count, topic_description)

    return datasets


def _build_kafka_schema_fields(
    fields: list[dict],  # type: ignore[type-arg]
) -> list[SchemaFieldClass]:
    result = []
    for f in fields:
        result.append(
            SchemaFieldClass(
                fieldPath=f["name"],
                nativeDataType=f["native_type"],
                type=SchemaFieldDataTypeClass(
                    type=_JSON_TO_DATAHUB_TYPE.get(f["native_type"], StringTypeClass()),
                ),
                nullable=f["nullable"],
                description=f.get("description") or None,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Ingest: emit DatasetProperties + SchemaMetadata for each Kafka topic
# ---------------------------------------------------------------------------


async def ingest_kafka_datasets(topics: frozenset[str] | None = None) -> int:
    """Discover Kafka topics from JSONL fixtures and emit metadata to DataHub.

    Args:
        topics: Optional set of bare topic names (the part after ``<instance>.``)
                to ingest.  When provided, only matching topics are registered.
                Defaults to all discovered topics.

    Returns count of datasets ingested.
    """
    token = _get_token()
    all_datasets = _discover_kafka_topics()

    if topics is not None:
        datasets = {
            urn: payload
            for urn, payload in all_datasets.items()
            # URN format: urn:li:dataset:(urn:li:dataPlatform:kafka,{instance}.{topic},{ENV})
            if urn.split(",")[1].split(".", 1)[1] in topics
        }
    else:
        datasets = all_datasets

    if not datasets:
        print("  No Kafka topics found in fixtures.")
        return 0

    emitter = DatahubRestEmitter(gms_server=_gms_url, token=token)

    for urn, (fields, message_count, topic_description) in datasets.items():
        # URN format: urn:li:dataset:(urn:li:dataPlatform:kafka,{instance}.{topic},{ENV})
        topic = urn.split(",")[1].split(".", 1)[1]

        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StatusClass(removed=False),
            )
        )

        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetPropertiesClass(
                    name=topic,
                    qualifiedName=f"{_kafka_instance}.{topic}",
                    description=topic_description or f"Imazon example Kafka topic: {topic}",
                    customProperties={
                        "source": "dummy-data-ingest",
                        "cluster": _kafka_instance,
                    },
                ),
            )
        )

        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=SchemaMetadataClass(
                    schemaName=topic,
                    platform=f"urn:li:dataPlatform:{KAFKA_PLATFORM}",
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=_build_kafka_schema_fields(fields),
                ),
            )
        )

        now_ms = int(time.time() * 1000)
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=OperationClass(
                    timestampMillis=now_ms,
                    lastUpdatedTimestamp=now_ms,
                    operationType=OperationTypeClass.INSERT,
                ),
            )
        )

        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetProfileClass(
                    timestampMillis=now_ms,
                    rowCount=message_count,
                    columnCount=len(fields),
                ),
            )
        )

        area_tag = _KAFKA_TOPIC_AREA_TAGS.get(topic)
        if area_tag is not None:
            emitter.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=urn,
                    aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=area_tag)]),
                )
            )

    await _mark_registry_registered(list(datasets.keys()))

    # Gate the post-condition on "emitted AND searchable" — the sync-sweep
    # matcher enumerates topics via the same ES-backed search read, so an
    # unindexed topic would be silently dropped from the mapping.
    # spec: project_es_indexing_lag_after_reset_seed
    wait_until_datasets_searchable(set(datasets.keys()), platform=KAFKA_PLATFORM)

    print(
        f"  Ingested {len(datasets)} Kafka datasets "
        f"({sum(len(payload[0]) for payload in datasets.values())} fields)."
    )
    return len(datasets)


# ---------------------------------------------------------------------------
# Emit: Operation timeseries records (passive-observation source signal)
# ---------------------------------------------------------------------------

# Imazon Kafka dataset URN observed by the passive ingestion source (UC1-03).
ORDERS_KAFKA_URN = _make_kafka_urn("imazon.orders.events")


async def emit_operation(
    dataset_urn: str,
    last_updated_ts_ms: int,
    operation_type: str = "INSERT",
) -> int:
    """Emit one Operation timeseries record on ``dataset_urn``.

    ``timestampMillis`` and ``lastUpdatedTimestamp`` are both set to
    ``last_updated_ts_ms`` so the passive-observation sweep derives the event's
    ``occurred_at`` from a caller-controlled timestamp. ``operation_type`` is the
    string name of an ``OperationTypeClass`` member (default ``INSERT``).

    Returns ``last_updated_ts_ms`` (the timestamp emitted).
    """
    token = _get_token()
    emitter = DatahubRestEmitter(gms_server=_gms_url, token=token)
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=dataset_urn,
            aspect=OperationClass(
                timestampMillis=last_updated_ts_ms,
                lastUpdatedTimestamp=last_updated_ts_ms,
                operationType=getattr(OperationTypeClass, operation_type),
            ),
        )
    )
    print(f"  Emitted Operation({operation_type}@{last_updated_ts_ms}) on {dataset_urn}")
    return last_updated_ts_ms


async def emit_fresh_kafka_operation(dataset_urn: str = ORDERS_KAFKA_URN) -> int:
    """Emit ONE fresh INSERT Operation on an Imazon Kafka topic at the current time.

    Models the external ingestor appending to the topic. The passive-observation
    sweep observes this Operation on the mapped dataset and mirrors it as a fresh
    passive_observation event (occurred_at == the emitted millisecond). Returns the
    emitted ``now_ms`` so callers can match the resulting event.
    """
    now_ms = int(time.time() * 1000)
    await emit_operation(dataset_urn, now_ms)
    return now_ms


# ---------------------------------------------------------------------------
# Reset-only and seed helpers
# ---------------------------------------------------------------------------


def reset_only() -> int:
    """Hard-delete all Imazon-related DataHub state. No ingest.

    Post-condition: zero example_db / example_kafka datasets, zero DataSpoke
    assertions, zero stale containers / glossary terms attributable to Imazon.

    Returns:
        Total number of deleted entities.
    """
    deleted = reset_datasets()
    reset_assertions()
    reset_containers()
    reset_glossary_terms()
    reset_documents()
    return deleted


async def seed(
    schemas: frozenset[str] | None = None,
) -> int:
    """Reset DataHub then re-ingest all Imazon datasets and topics.

    Post-condition: full Imazon set present in DataHub with descriptions
    and typed columns.

    Args:
        schemas: Optional subset of PG schemas to ingest.
                 Defaults to all TARGET_SCHEMAS.

    Returns:
        Total number of deleted + ingested entities.
    """
    deleted = reset_only()
    ingested_pg = await ingest_pg_datasets(schemas=schemas)
    ingested_kafka = await ingest_kafka_datasets()
    return deleted + ingested_pg + ingested_kafka


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def async_main() -> None:
    reset_only_flag = "--reset-only" in sys.argv
    do_reset = "--reset" in sys.argv or reset_only_flag

    if do_reset:
        print("[INFO]  Resetting DataHub datasets...")
        reset_datasets()
        if reset_only_flag:
            print("[INFO]  Reset complete (--reset-only).")
            return

    print("[INFO]  Ingesting example-postgres tables into DataHub...")
    pg_count = await ingest_pg_datasets()
    print("[INFO]  Ingesting example-kafka topics into DataHub...")
    kafka_count = await ingest_kafka_datasets()
    print(f"[INFO]  Done. {pg_count + kafka_count} datasets registered in DataHub.")


if __name__ == "__main__":
    asyncio.run(async_main())
