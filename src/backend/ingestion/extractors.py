"""Source-specific metadata extraction and DataHub emission.

Connects to data sources (PostgreSQL, Kafka, …), discovers schema metadata,
and emits aspects (Status, DatasetProperties, SchemaMetadata) to DataHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import asyncpg
from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    BooleanTypeClass,
    BytesTypeClass,
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
    TimeTypeClass,
)
from pydantic import BaseModel

from src.backend.ingestion.secret_resolver import (
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
    resolve_secret_ref,
)
from src.shared.models.ingestion import Platform

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS: frozenset[str] = frozenset(p.value for p in Platform)

# ── Type mappings ─────────────────────────────────────────────────────────────

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

_JSON_TO_DATAHUB_TYPE: dict[str, object] = {
    "str": StringTypeClass(),
    "int": NumberTypeClass(),
    "float": NumberTypeClass(),
    "bool": BooleanTypeClass(),
    "list": ArrayTypeClass(),
    "dict": MapTypeClass(),
    "NoneType": NullTypeClass(),
}


class IngestionResult(BaseModel):
    """Result of a DataHub ingestion run."""

    entities_ingested: int
    errors: list[str]
    warnings: list[str]


def _resolve_auth_password(auth: dict[str, Any] | None) -> str | IngestionResult:
    """Return the plaintext password from auth, or an IngestionResult on resolution error.

    Handles the persisted reference shape ``{username, secret_ref: {name, key}}``.
    Plaintext passwords in the persisted dict are never used — the only valid source
    of a credential at run time is ``auth.secret_ref``.
    """
    if not auth:
        return ""

    if "password" in auth:
        logger.warning(
            "Persisted auth contains a plaintext 'password' field — ignoring. "
            "Only secret_ref is used at run time."
        )

    secret_ref = auth.get("secret_ref")
    if not secret_ref:
        return ""

    if not isinstance(secret_ref, dict):
        return IngestionResult(
            entities_ingested=0,
            errors=[f"Invalid secret_ref shape: {secret_ref!r}"],
            warnings=[],
        )

    name = secret_ref.get("name")
    key = secret_ref.get("key")
    if not name or not key:
        return IngestionResult(
            entities_ingested=0,
            errors=["secret_ref missing name or key"],
            warnings=[],
        )

    try:
        return resolve_secret_ref(f"k8s-secret/{name}/{key}")
    except (SecretRefMalformed, SecretRefNotFound, SecretResolverUnavailable) as exc:
        return IngestionResult(
            entities_ingested=0,
            errors=[f"Secret resolution failed: {exc}"],
            warnings=[],
        )


# ── PostgreSQL extractor ─────────────────────────────────────────────────────


async def _extract_postgresql(
    datahub: DataHubClient,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    dataset_urn: str,
    dry_run: bool,
    platform: str,
) -> IngestionResult:
    """Connect to PostgreSQL, discover columns, emit schema to DataHub."""
    host = locator["host"]
    port = locator["port"]
    database = identifier.get("database", "")
    schema_name = identifier.get("schema_name")
    table = identifier.get("table")
    username = auth.get("username", "") if auth else ""
    resolved = _resolve_auth_password(auth)
    if isinstance(resolved, IngestionResult):
        return resolved
    password = resolved

    try:
        conn = await asyncpg.connect(
            host=host, port=port, user=username, password=password, database=database,
        )
    except Exception as exc:
        return IngestionResult(
            entities_ingested=0,
            errors=[f"PostgreSQL connection failed: {exc}"],
            warnings=[],
        )

    try:
        # Build WHERE clause based on identifier granularity
        conditions = ["table_schema NOT IN ('pg_catalog', 'information_schema')"]
        params: list[Any] = []
        if schema_name:
            params.append(schema_name)
            conditions.append(f"table_schema = ${len(params)}")
        if table:
            params.append(table)
            conditions.append(f"table_name = ${len(params)}")

        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""
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
            WHERE {where}
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """,
            *params,
        )
    finally:
        await conn.close()

    if not rows:
        return IngestionResult(
            entities_ingested=0, errors=[], warnings=["No columns found for the given identifier"],
        )

    # Group columns by (schema, table)
    tables: dict[tuple[str, str], list[asyncpg.Record]] = {}
    for row in rows:
        key = (row["table_schema"], row["table_name"])
        tables.setdefault(key, []).append(row)

    entities_ingested = 0
    errors: list[str] = []

    for (s, t), columns in tables.items():
        # Build the URN for this specific table
        table_urn = dataset_urn  # For single-table configs, reuse the provided URN

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
        description = table_comment or f"Ingested by DataSpoke: {database}.{s}.{t}"

        if not dry_run:
            try:
                await datahub.emit_aspect(table_urn, StatusClass(removed=False))
                await datahub.emit_aspect(
                    table_urn,
                    DatasetPropertiesClass(
                        name=f"{s}.{t}",
                        qualifiedName=f"{database}.{s}.{t}",
                        description=description,
                        customProperties={
                            "source": "dataspoke-ingestion",
                            "database": database,
                            "schema": s,
                        },
                    ),
                )
                await datahub.emit_aspect(
                    table_urn,
                    SchemaMetadataClass(
                        schemaName=f"{s}.{t}",
                        platform=f"urn:li:dataPlatform:{platform}",
                        version=0,
                        hash="",
                        platformSchema=OtherSchemaClass(rawSchema=""),
                        fields=fields,
                    ),
                )
            except Exception as exc:
                errors.append(f"Failed to emit aspects for {s}.{t}: {exc}")
                continue

        entities_ingested += 1

    return IngestionResult(entities_ingested=entities_ingested, errors=errors, warnings=[])


# ── Kafka extractor ──────────────────────────────────────────────────────────


async def _extract_kafka(
    datahub: DataHubClient,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    dataset_urn: str,
    dry_run: bool,
    platform: str,
) -> IngestionResult:
    """Consume sample messages from a Kafka topic, infer schema, emit to DataHub."""
    bootstrap_servers = locator["bootstrap_servers"]
    topic = identifier["topic"]
    cluster = identifier.get("cluster", "")

    # Discover schema by polling messages
    field_types: dict[str, str] = {}
    errors: list[str] = []

    try:
        messages = await asyncio.to_thread(
            _poll_kafka_messages, bootstrap_servers, topic,
        )
        for msg in messages:
            for key, value in msg.items():
                if key not in field_types and value is not None:
                    field_types[key] = type(value).__name__
    except Exception as exc:
        return IngestionResult(
            entities_ingested=0,
            errors=[f"Kafka consumer failed: {exc}"],
            warnings=[],
        )

    if not field_types:
        return IngestionResult(
            entities_ingested=0, errors=[], warnings=["No messages found in topic or all values null"],
        )

    fields = [
        SchemaFieldClass(
            fieldPath=name,
            nativeDataType=py_type,
            type=SchemaFieldDataTypeClass(
                type=_JSON_TO_DATAHUB_TYPE.get(py_type, StringTypeClass()),
            ),
            nullable=True,
        )
        for name, py_type in field_types.items()
    ]

    if not dry_run:
        try:
            await datahub.emit_aspect(dataset_urn, StatusClass(removed=False))
            await datahub.emit_aspect(
                dataset_urn,
                DatasetPropertiesClass(
                    name=topic,
                    qualifiedName=f"{cluster}.{topic}" if cluster else topic,
                    description=f"Ingested by DataSpoke: Kafka topic {topic}",
                    customProperties={
                        "source": "dataspoke-ingestion",
                        "cluster": cluster,
                    },
                ),
            )
            await datahub.emit_aspect(
                dataset_urn,
                SchemaMetadataClass(
                    schemaName=topic,
                    platform=f"urn:li:dataPlatform:{platform}",
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=fields,
                ),
            )
        except Exception as exc:
            errors.append(f"Failed to emit aspects for topic {topic}: {exc}")

    entities_ingested = 0 if errors else 1
    return IngestionResult(entities_ingested=entities_ingested, errors=errors, warnings=[])


def _poll_kafka_messages(
    bootstrap_servers: str, topic: str, *, max_messages: int = 100, timeout_s: float = 15.0,
) -> list[dict[str, Any]]:
    """Synchronous helper: poll Kafka topic and return parsed JSON messages."""
    import time

    from confluent_kafka import Consumer, KafkaError

    consumer = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": f"dataspoke-ingestion-{topic}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([topic])

    messages: list[dict[str, Any]] = []
    try:
        deadline = time.monotonic() + timeout_s
        while len(messages) < max_messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = consumer.poll(timeout=min(remaining, 1.0))
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    break
                continue
            try:
                parsed = json.loads(msg.value().decode("utf-8"))
                if isinstance(parsed, dict):
                    messages.append(parsed)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    finally:
        consumer.close()

    return messages


# ── Dispatcher ───────────────────────────────────────────────────────────────


async def run_datahub_ingestion(
    datahub: DataHubClient,
    platform: str,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    dataset_urn: str,
    dry_run: bool = False,
) -> IngestionResult:
    """Extract metadata from a data source and emit aspects to DataHub.

    Dispatches to source-specific extractors based on platform.
    """
    logger.info(
        "run_datahub_ingestion",
        extra={"platform": platform, "dataset_urn": dataset_urn, "dry_run": dry_run},
    )

    if platform == Platform.POSTGRESQL.value:
        return await _extract_postgresql(datahub, locator, identifier, auth, dataset_urn, dry_run, platform=platform)

    if platform == Platform.KAFKA.value:
        return await _extract_kafka(datahub, locator, identifier, dataset_urn, dry_run, platform=platform)

    if platform in SUPPORTED_PLATFORMS:
        return IngestionResult(
            entities_ingested=0,
            errors=[],
            warnings=[f"Extraction for {platform} is not yet implemented"],
        )

    return IngestionResult(
        entities_ingested=0,
        errors=[f"Unsupported platform: {platform}"],
        warnings=[],
    )
