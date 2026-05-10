"""Unit tests for the Kafka ingestion extractor (mocked consumer)."""

from unittest.mock import AsyncMock, patch

from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    BooleanTypeClass,
    NumberTypeClass,
    SchemaFieldDataTypeClass,
    StringTypeClass,
)

from src.backend.ingestion.extractors import run_datahub_ingestion


# ── Discovery + emission ──────────────────────────────────────────────────────


async def test_kafka_dry_run_discovers_but_does_not_emit():
    datahub = AsyncMock()
    sample_messages = [
        {"order_id": "ORD-001", "amount": 42.5, "shipped": True},
        {"order_id": "ORD-002", "amount": 10.0, "shipped": False, "note": "rush"},
    ]

    with patch(
        "src.backend.ingestion.extractors._poll_kafka_messages",
        return_value=sample_messages,
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "orders", "cluster": "test"},
            auth=None,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:kafka,test.orders,PROD)",
            run_id="test-run-id",
            dry_run=True,
        )

    assert result.entities_ingested == 1
    assert result.errors == []
    datahub.emit_aspect.assert_not_called()


async def test_kafka_run_emits_three_aspects():
    # spec: BACKEND.md §Ingestion Service — "Aspects emitted (non-dry-run, per discovered
    # dataset): StatusClass(removed=False), DatasetPropertiesClass, SchemaMetadataClass"
    # spec: BACKEND.md L182-L184
    datahub = AsyncMock()
    sample_messages = [{"key": "value"}]

    with patch(
        "src.backend.ingestion.extractors._poll_kafka_messages",
        return_value=sample_messages,
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "orders"},
            auth=None,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:kafka,test.orders,PROD)",
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.entities_ingested == 1
    assert result.errors == []

    # spec: BACKEND.md §Ingestion Service — aspects emitted per discovered dataset (L182-L184)
    emitted_aspect_types = {
        type(call.args[1]).__name__ for call in datahub.emit_aspect.call_args_list
    }
    assert emitted_aspect_types == {"StatusClass", "DatasetPropertiesClass", "SchemaMetadataClass"}, (
        f"Expected exactly {{StatusClass, DatasetPropertiesClass, SchemaMetadataClass}}, "
        f"got {emitted_aspect_types}"
    )


# ── Typed PDL union: Kafka schema field types ─────────────────────────────────


async def test_kafka_extractor_emits_typed_schema_field_types() -> None:
    """Kafka JSON field types map to typed PDL union instances, not bare strings.

    int → NumberTypeClass, str → StringTypeClass, bool → BooleanTypeClass,
    list → ArrayTypeClass. The field.type must be a SchemaFieldDataTypeClass
    wrapping the typed instance, not a raw string or RecordType.

    spec: BACKEND.md §Ingestion Service — Kafka schema discovery.
    spec: DATAHUB_INTEGRATION.md §schemaMetadata — fields[].type must be typed.
    """
    sample_messages = [
        {
            "page_count": 342,                # int → NumberTypeClass
            "title": "The Silent Cipher",     # str → StringTypeClass
            "is_active": True,                # bool → BooleanTypeClass
            "tags": ["fiction", "thriller"],  # list → ArrayTypeClass
        }
    ]

    datahub = AsyncMock()
    with patch(
        "src.backend.ingestion.extractors._poll_kafka_messages",
        return_value=sample_messages,
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "imazon.orders.events", "cluster": "example_kafka"},
            auth=None,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)",
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"

    from datahub.metadata.schema_classes import SchemaMetadataClass

    schema = next(
        call.args[1]
        for call in datahub.emit_aspect.call_args_list
        if isinstance(call.args[1], SchemaMetadataClass)
    )

    fields_by_name = {f.fieldPath: f for f in schema.fields}

    expected = {
        "page_count": NumberTypeClass,
        "title": StringTypeClass,
        "is_active": BooleanTypeClass,
        "tags": ArrayTypeClass,
    }
    for field_name, expected_class in expected.items():
        assert field_name in fields_by_name, (
            f"Expected field {field_name!r} in schema; got {list(fields_by_name.keys())}"
        )
        field = fields_by_name[field_name]
        assert isinstance(field.type, SchemaFieldDataTypeClass), (
            f"field '{field_name}'.type must be SchemaFieldDataTypeClass; "
            f"got {type(field.type).__name__}"
        )
        assert isinstance(field.type.type, expected_class), (
            f"field '{field_name}'.type.type must be {expected_class.__name__}; "
            f"got {type(field.type.type).__name__}"
        )


async def test_kafka_extractor_unknown_python_type_falls_back_to_string() -> None:
    """A value whose type.__name__ is not in _JSON_TO_DATAHUB_TYPE falls back to StringTypeClass.

    The Kafka extractor uses type(val).__name__ as the type key. Types outside the
    known set (str/int/float/bool/list/dict/NoneType) must not raise; StringTypeClass
    is the safe fallback — the same contract as the PostgreSQL unknown-type test.

    spec: BACKEND.md §Custom Ingestor Authoring Contract — extractors must not
    raise on unknown types; StringTypeClass is the safe fallback.
    """
    # bytes.__name__ == "bytes" — not in _JSON_TO_DATAHUB_TYPE
    sample_messages = [{"raw_payload": b"\x00\x01\x02"}]

    datahub = AsyncMock()
    with patch(
        "src.backend.ingestion.extractors._poll_kafka_messages",
        return_value=sample_messages,
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "imazon.orders.events", "cluster": "example_kafka"},
            auth=None,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)",
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"

    from datahub.metadata.schema_classes import SchemaMetadataClass

    schema = next(
        call.args[1]
        for call in datahub.emit_aspect.call_args_list
        if isinstance(call.args[1], SchemaMetadataClass)
    )

    assert len(schema.fields) == 1
    field = schema.fields[0]
    assert field.fieldPath == "raw_payload"
    assert isinstance(field.type, SchemaFieldDataTypeClass), (
        "Unknown type must still produce SchemaFieldDataTypeClass wrapper"
    )
    assert isinstance(field.type.type, StringTypeClass), (
        f"Unknown Kafka value type (bytes) must fall back to StringTypeClass; "
        f"got {type(field.type.type).__name__}"
    )


# ── systemMetadata runId emission ─────────────────────────────────────────────


async def test_kafka_extractor_emits_systemmetadata_with_dataspoke_runid() -> None:
    """Every dataset-aspect emit from the Kafka extractor carries a
    SystemMetadataClass with runId='dataspoke-kafka-<run_id>'.

    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "every aspect emission targeting a dataset URN within a custom ingestor
        run MUST carry a non-default systemMetadata"
    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "DataSpoke uses runId='dataspoke-{platform}-{run_id}'"
    spec: BACKEND.md §Custom Ingestor Authoring Contract — status, DatasetProperties,
        SchemaMetadata all pass system_metadata=sysmeta per run.
    """
    from datahub.metadata.schema_classes import SystemMetadataClass

    datahub = AsyncMock()
    run_id = "test-run-id"
    sample_messages = [{"order_id": "ORD-001", "amount": 42.5}]

    with patch(
        "src.backend.ingestion.extractors._poll_kafka_messages",
        return_value=sample_messages,
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "imazon.orders.events", "cluster": "example_kafka"},
            auth=None,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)",
            run_id=run_id,
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"

    emit_calls = datahub.emit_aspect.call_args_list
    assert len(emit_calls) >= 3, (
        f"Expected at least 3 emit calls (Status, DatasetProperties, SchemaMetadata); "
        f"got {len(emit_calls)}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract"
    )

    expected_run_id = f"dataspoke-kafka-{run_id}"
    for i, call in enumerate(emit_calls):
        sysmeta = call.kwargs.get("system_metadata")
        assert sysmeta is not None, (
            f"emit call #{i} must carry system_metadata kwarg; got None. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert isinstance(sysmeta, SystemMetadataClass), (
            f"emit call #{i} system_metadata must be SystemMetadataClass; "
            f"got {type(sysmeta).__name__!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert sysmeta.runId == expected_run_id, (
            f"emit call #{i} system_metadata.runId must be {expected_run_id!r}; "
            f"got {sysmeta.runId!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement — "
            "runId='dataspoke-{platform}-{run_id}'"
        )
        assert isinstance(sysmeta.lastObserved, int), (
            f"emit call #{i} system_metadata.lastObserved must be int (epoch-ms); "
            f"got {type(sysmeta.lastObserved).__name__!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert sysmeta.lastObserved > 0, (
            f"emit call #{i} system_metadata.lastObserved must be > 0; "
            f"got {sysmeta.lastObserved!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
