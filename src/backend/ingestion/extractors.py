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
    DatasetPropertiesClass,
    OtherSchemaClass,
    SchemaFieldClass,
    SchemaMetadataClass,
    StatusClass,
)
from pydantic import BaseModel

from src.shared.models.ingestion import SourceType

if TYPE_CHECKING:
    from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

SUPPORTED_SOURCE_TYPES: frozenset[str] = frozenset(st.value for st in SourceType)

# ── Type mappings ─────────────────────────────────────────────────────────────

_PG_TO_DATAHUB_TYPE: dict[str, str] = {
    "integer": "NUMBER",
    "bigint": "NUMBER",
    "smallint": "NUMBER",
    "numeric": "NUMBER",
    "real": "NUMBER",
    "double precision": "NUMBER",
    "boolean": "BOOLEAN",
    "text": "STRING",
    "character varying": "STRING",
    "character": "STRING",
    "varchar": "STRING",
    "char": "STRING",
    "date": "DATE",
    "timestamp with time zone": "TIME",
    "timestamp without time zone": "TIME",
    "time with time zone": "TIME",
    "time without time zone": "TIME",
    "jsonb": "STRING",
    "json": "STRING",
    "uuid": "STRING",
    "bytea": "BYTES",
    "ARRAY": "ARRAY",
}

_JSON_TO_DATAHUB_TYPE: dict[str, str] = {
    "str": "STRING",
    "int": "NUMBER",
    "float": "NUMBER",
    "bool": "BOOLEAN",
    "list": "ARRAY",
    "dict": "MAP",
    "NoneType": "NULL",
}


class IngestionResult(BaseModel):
    """Result of a DataHub ingestion run."""

    entities_ingested: int
    errors: list[str]
    warnings: list[str]


# ── PostgreSQL extractor ─────────────────────────────────────────────────────


async def _extract_postgresql(
    datahub: DataHubClient,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    dataset_urn: str,
    dry_run: bool,
) -> IngestionResult:
    """Connect to PostgreSQL, discover columns, emit schema to DataHub."""
    host = locator["host"]
    port = locator["port"]
    database = identifier.get("database", "")
    schema_name = identifier.get("schema_name")
    table = identifier.get("table")
    username = auth.get("username", "") if auth else ""
    # Use 'password' directly if provided, otherwise fall back to 'secret_ref'
    # (in production, secret_ref would be resolved via a secret manager)
    password = auth.get("password", auth.get("secret_ref", "")) if auth else ""

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
            SELECT table_schema, table_name, column_name, data_type,
                   ordinal_position, is_nullable
            FROM information_schema.columns
            WHERE {where}
            ORDER BY table_schema, table_name, ordinal_position
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
                type={"type": {"type": _PG_TO_DATAHUB_TYPE.get(col["data_type"], "STRING")}},
                nullable=col["is_nullable"] == "YES",
            )
            for col in columns
        ]

        if not dry_run:
            try:
                await datahub.emit_aspect(table_urn, StatusClass(removed=False))
                await datahub.emit_aspect(
                    table_urn,
                    DatasetPropertiesClass(
                        name=f"{s}.{t}",
                        qualifiedName=f"{database}.{s}.{t}",
                        description=f"Ingested by DataSpoke: {database}.{s}.{t}",
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
                        platform="urn:li:dataPlatform:postgres",
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
            type={"type": {"type": _JSON_TO_DATAHUB_TYPE.get(py_type, "STRING")}},
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
                    platform="urn:li:dataPlatform:kafka",
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
    bootstrap_servers: str, topic: str, *, max_messages: int = 100, timeout_s: float = 5.0,
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
    source_type: str,
    locator: dict[str, Any],
    identifier: dict[str, Any],
    auth: dict[str, Any] | None,
    dataset_urn: str,
    dry_run: bool = False,
) -> IngestionResult:
    """Extract metadata from a data source and emit aspects to DataHub.

    Dispatches to source-specific extractors based on source_type.
    """
    logger.info(
        "run_datahub_ingestion",
        extra={"source_type": source_type, "dataset_urn": dataset_urn, "dry_run": dry_run},
    )

    if source_type == SourceType.POSTGRESQL.value:
        return await _extract_postgresql(datahub, locator, identifier, auth, dataset_urn, dry_run)

    if source_type == SourceType.KAFKA.value:
        return await _extract_kafka(datahub, locator, identifier, dataset_urn, dry_run)

    if source_type in SUPPORTED_SOURCE_TYPES:
        return IngestionResult(
            entities_ingested=0,
            errors=[],
            warnings=[f"Extraction for {source_type} is not yet implemented"],
        )

    return IngestionResult(
        entities_ingested=0,
        errors=[f"Unsupported source_type: {source_type}"],
        warnings=[],
    )
