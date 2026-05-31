"""Source-specific metadata extraction and DataHub emission.

The extractor registry maps ``recipe.source.type`` → async extractor function.
This release ships a **postgres extractor only**. The Kafka ACTIVE extraction
path is removed (Kafka is reachable as PASSIVE only).

Adding an extractor for a new ``source.type`` is the fork-and-extend path:
register an async function that accepts ``(datahub, source_id, recipe,
dry_run, run_id)`` and returns a set of emitted dataset URNs.

Spec: spec/feature/BACKEND.md §Custom Extractor Authoring Contract
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]
from datahub.emitter.mcp_builder import DatabaseKey, SchemaKey, gen_containers  # type: ignore
from datahub.metadata.schema_classes import (  # type: ignore
    ArrayTypeClass,
    BooleanTypeClass,
    BrowsePathEntryClass,
    BrowsePathsV2Class,
    BytesTypeClass,
    ContainerClass,
    DatasetPropertiesClass,
    DateTypeClass,
    MapTypeClass,
    NullTypeClass,
    NumberTypeClass,
    OtherSchemaClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StatusClass,
    StringTypeClass,
    SystemMetadataClass,
    TimeTypeClass,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


# ── Result model ──────────────────────────────────────────────────────────────


class IngestionResult(BaseModel):
    """Result of a single extractor invocation."""

    entities_ingested: int
    emitted_urns: list[str]
    errors: list[str]
    warnings: list[str]


# ── PostgreSQL type map ───────────────────────────────────────────────────────

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


def _parse_env_from_config(config: dict[str, Any]) -> str:
    """Extract the DataHub env string from recipe config, defaulting to 'DEV'."""
    return config.get("env", "DEV")


def _make_dataset_urn(platform: str, name: str, env: str) -> str:
    """Build a dataset URN using the SDK builder for correctness."""
    from datahub.emitter.mce_builder import make_dataset_urn  # type: ignore
    return make_dataset_urn(platform=platform, name=name, env=env)


# ── PostgreSQL extractor ─────────────────────────────────────────────────────


async def _extract_postgres(
    datahub: DataHubClient,
    source_id: str,
    recipe: dict[str, Any],
    dry_run: bool,
    run_id: str,
) -> IngestionResult:
    """Connect to PostgreSQL via resolved recipe.source.config; emit metadata.

    Config keys consumed:
      - ``host_port`` (required): ``"<host>:<port>"`` or ``"<host>"`` (default port 5432)
      - ``database`` (required): target database name
      - ``username`` (required): DB user
      - ``password``: plaintext password (resolved from ``${name__key}`` before call)
      - ``schema_pattern``: ``{allow: [...], deny: [...]}`` (optional)
      - ``env``: DataHub ``FabricType`` value; defaults to ``"DEV"``

    Aspects emitted per discovered table (non-dry-run):
      StatusClass, ContainerClass (schema), BrowsePathsV2Class,
      DatasetPropertiesClass, SchemaMetadataClass

    Also emits the database and schema container hierarchy
    (DatabaseKey / SchemaKey) for Browse v2 parity with DataHub's managed PG source.

    Returns:
        IngestionResult with ``emitted_urns`` listing every dataset URN emitted.
    """
    config = recipe.get("source", {}).get("config", {})
    host_port: str = config.get("host_port", "localhost:5432")
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 5432
    else:
        host = host_port
        port = 5432

    database: str = config.get("database", "")
    username: str = config.get("username", "")
    password: str = config.get("password", "")
    env: str = _parse_env_from_config(config)

    # Build schema allow/deny predicate from schema_pattern.
    schema_allow: list[str] = [".*"]
    schema_deny: list[str] = []
    if "schema_pattern" in config:
        sp = config["schema_pattern"]
        if isinstance(sp, dict):
            schema_allow = sp.get("allow", [".*"])
            schema_deny = sp.get("deny", [])

    # Use AllowDenyPattern for schema filtering.
    try:
        from datahub.configuration.common import AllowDenyPattern  # type: ignore
        schema_filter = AllowDenyPattern(allow=schema_allow, deny=schema_deny)
    except ImportError:
        schema_filter = None  # type: ignore[assignment]

    # Connect to PostgreSQL.
    try:
        conn = await asyncpg.connect(
            host=host, port=port, user=username, password=password, database=database,
        )
    except Exception as exc:
        return IngestionResult(
            entities_ingested=0,
            emitted_urns=[],
            errors=[f"PostgreSQL connection failed: {exc}"],
            warnings=[],
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
            WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
        )
    finally:
        await conn.close()

    if not rows:
        return IngestionResult(
            entities_ingested=0,
            emitted_urns=[],
            errors=[],
            warnings=["No columns found in database"],
        )

    # Group by (schema, table).
    tables: dict[tuple[str, str], list[asyncpg.Record]] = {}
    for row in rows:
        schema = row["table_schema"]
        table = row["table_name"]
        if schema_filter is not None and not schema_filter.allowed(schema):
            continue
        tables.setdefault((schema, table), []).append(row)

    if not tables:
        return IngestionResult(
            entities_ingested=0,
            emitted_urns=[],
            errors=[],
            warnings=["No tables matched schema_pattern filter"],
        )

    # pipelineName stamps the source_id so the sync sweep can observe it.
    sysmeta = SystemMetadataClass(
        runId=f"dataspoke-{source_id}-{run_id}",
        pipelineName=source_id,
        lastObserved=int(time.time() * 1000),
    )

    db_key = DatabaseKey(
        database=database,
        platform="postgres",
        instance=None,
        env=env,
        backcompat_env_as_instance=True,
    )

    # Emit database container once (non-dry-run).
    if not dry_run:
        for wu in gen_containers(container_key=db_key, name=database, sub_types=["Database"]):
            mcp = wu.metadata
            if hasattr(mcp, "entityUrn") and hasattr(mcp, "aspect") and mcp.entityUrn and mcp.aspect:
                try:
                    await datahub.emit_aspect(mcp.entityUrn, mcp.aspect, system_metadata=sysmeta)
                except Exception as exc:
                    logger.warning("Failed to emit database container: %s", exc)

    emitted_urns: list[str] = []
    errors: list[str] = []
    schemas_seen: set[str] = set()

    for (schema, table), columns in tables.items():
        schema_key = SchemaKey(
            database=database,
            schema=schema,
            platform="postgres",
            instance=None,
            env=env,
            backcompat_env_as_instance=True,
        )

        # Emit schema container once per schema (non-dry-run).
        if not dry_run and schema not in schemas_seen:
            schemas_seen.add(schema)
            for wu in gen_containers(
                container_key=schema_key,
                name=schema,
                sub_types=["Schema"],
                parent_container_key=db_key,
            ):
                mcp = wu.metadata
                if hasattr(mcp, "entityUrn") and hasattr(mcp, "aspect") and mcp.entityUrn and mcp.aspect:
                    try:
                        await datahub.emit_aspect(mcp.entityUrn, mcp.aspect, system_metadata=sysmeta)
                    except Exception as exc:
                        logger.warning("Failed to emit schema container for '%s': %s", schema, exc)

        dataset_name = f"{database}.{schema}.{table}"
        dataset_urn = _make_dataset_urn("postgres", dataset_name, env)

        fields = [
            SchemaFieldClass(
                fieldPath=col["column_name"],
                nativeDataType=col["data_type"],
                type=SchemaFieldDataTypeClass(
                    type=_PG_TO_DATAHUB_TYPE.get(col["data_type"], StringTypeClass()),
                ),
                nullable=col["is_nullable"] == "YES",
                description=col.get("column_comment"),
            )
            for col in columns
        ]

        table_comment = columns[0].get("table_comment") if columns else None
        description = table_comment or f"Ingested by DataSpoke: {dataset_name}"

        if not dry_run:
            schema_container_urn = schema_key.as_urn()
            db_container_urn = db_key.as_urn()
            try:
                await datahub.emit_aspect(
                    dataset_urn, StatusClass(removed=False), system_metadata=sysmeta
                )
                await datahub.emit_aspect(
                    dataset_urn,
                    ContainerClass(container=schema_container_urn),
                    system_metadata=sysmeta,
                )
                await datahub.emit_aspect(
                    dataset_urn,
                    BrowsePathsV2Class(
                        path=[
                            BrowsePathEntryClass(id=db_container_urn, urn=db_container_urn),
                            BrowsePathEntryClass(id=schema_container_urn, urn=schema_container_urn),
                        ]
                    ),
                    system_metadata=sysmeta,
                )
                await datahub.emit_aspect(
                    dataset_urn,
                    DatasetPropertiesClass(
                        name=f"{schema}.{table}",
                        qualifiedName=dataset_name,
                        description=description,
                        customProperties={
                            "source": "dataspoke-ingestion",
                            "source_id": source_id,
                            "database": database,
                            "schema": schema,
                        },
                    ),
                    system_metadata=sysmeta,
                )
                await datahub.emit_aspect(
                    dataset_urn,
                    SchemaMetadataClass(
                        schemaName=f"{schema}.{table}",
                        platform="urn:li:dataPlatform:postgres",
                        version=0,
                        hash="",
                        platformSchema=OtherSchemaClass(rawSchema=""),
                        fields=fields,
                    ),
                    system_metadata=sysmeta,
                )
            except Exception as exc:
                errors.append(f"Failed to emit aspects for '{dataset_name}': {exc}")
                continue

        emitted_urns.append(dataset_urn)

    return IngestionResult(
        entities_ingested=len(emitted_urns),
        emitted_urns=emitted_urns,
        errors=errors,
        warnings=[],
    )


# ── Extractor registry ────────────────────────────────────────────────────────
#
# Maps recipe.source.type → async extractor coroutine function.
# Signature: async (datahub, source_id, recipe, dry_run, run_id) -> IngestionResult
#
# Only postgres is registered in this release. To add support for another
# source type, implement an extractor function and register it here.

_EXTRACTOR_REGISTRY: dict[
    str,
    Any,  # Callable[[DataHubClient, str, dict, bool, str], Coroutine[Any, Any, IngestionResult]]
] = {
    "postgres": _extract_postgres,
}


async def run_extractor(
    datahub: DataHubClient,
    source_id: str,
    recipe: dict[str, Any],
    dry_run: bool,
    run_id: str,
) -> IngestionResult:
    """Dispatch to the registered extractor for ``recipe.source.type``.

    Args:
        datahub:   DataHub client (pre-configured).
        source_id: The ``ingestion_source.id`` UUID string — stamped as
                   ``systemMetadata.pipelineName`` for observed-mapping.
        recipe:    The RESOLVED recipe dict (plaintext credentials in-memory).
        dry_run:   When True, discover schema but do not emit any aspects.
        run_id:    A fresh UUID string per run for DPI URN derivation.

    Returns:
        IngestionResult with emitted_urns populated (empty on dry_run or error).
    """
    source_type = recipe.get("source", {}).get("type", "")
    extractor = _EXTRACTOR_REGISTRY.get(source_type)

    if extractor is None:
        return IngestionResult(
            entities_ingested=0,
            emitted_urns=[],
            errors=[
                f"No ACTIVE_CUSTOM_MANAGED extractor registered for source.type='{source_type}'. "
                "Fork and extend extractors.py to add support."
            ],
            warnings=[],
        )

    logger.info(
        "run_extractor",
        extra={
            "source_type": source_type,
            "source_id": source_id,
            "dry_run": dry_run,
            "run_id": run_id,
        },
    )
    return await extractor(datahub, source_id, recipe, dry_run, run_id)
