"""Unit tests for ingestion extractors (mocked infrastructure)."""

import pytest
from unittest.mock import AsyncMock, patch

from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    BooleanTypeClass,
    NumberTypeClass,
    SchemaFieldDataTypeClass,
    StringTypeClass,
    DateTypeClass,
)

from src.backend.ingestion.extractors import (
    SUPPORTED_PLATFORMS,
    run_datahub_ingestion,
)
from src.backend.ingestion.secret_resolver import SecretRefNotFound, SecretResolverUnavailable

# ── SUPPORTED_PLATFORMS ────────────────────────────────────────────────────


def test_supported_platforms_contains_expected():
    assert {"postgres", "mysql", "oracle", "bigquery", "snowflake", "kafka"}.issubset(
        SUPPORTED_PLATFORMS
    )


# ── run_datahub_ingestion — unsupported / not-yet-implemented ─────────────────


async def test_unsupported_source_returns_error():
    datahub = AsyncMock()
    result = await run_datahub_ingestion(
        datahub=datahub,
        platform="unknown_source",
        locator={},
        identifier={},
        auth=None,
        dataset_urn="urn:li:dataset:x",
        dry_run=False,
    )
    assert result.entities_ingested == 0
    assert len(result.errors) == 1
    assert "Unsupported platform" in result.errors[0]


async def test_not_yet_implemented_source_returns_warning():
    datahub = AsyncMock()
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    result = await run_datahub_ingestion(
        datahub=datahub,
        platform="mysql",
        locator={"host": "x", "port": 3306},
        identifier={"database": "db"},
        auth=_auth,
        dataset_urn="urn:li:dataset:x",
    )
    assert result.entities_ingested == 0
    assert result.errors == []
    assert any("not yet implemented" in w for w in result.warnings)


# ── PostgreSQL extractor (mocked asyncpg) ─────────────────────────────────────


async def test_postgresql_dry_run_discovers_but_does_not_emit():
    datahub = AsyncMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "id",
            "data_type": "integer",
            "ordinal_position": 1,
            "is_nullable": "NO",
        },
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "email",
            "data_type": "text",
            "ordinal_position": 2,
            "is_nullable": "YES",
        },
    ]

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch("src.backend.ingestion.extractors.resolve_secret_ref", return_value="p"),
    ):
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb", "schema_name": "public", "table": "users"},
            auth=_auth,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,testdb.public.users,PROD)",
            dry_run=True,
        )

    assert result.entities_ingested == 1
    assert result.errors == []
    datahub.emit_aspect.assert_not_called()


async def test_postgresql_run_emits_three_aspects():
    # spec: BACKEND.md §Ingestion Service — "Aspects emitted (non-dry-run, per discovered
    # dataset): StatusClass(removed=False), DatasetPropertiesClass, SchemaMetadataClass"
    # spec: BACKEND.md L182-L184
    datahub = AsyncMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "table_schema": "public",
            "table_name": "users",
            "column_name": "id",
            "data_type": "integer",
            "ordinal_position": 1,
            "is_nullable": "NO",
        },
    ]

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch("src.backend.ingestion.extractors.resolve_secret_ref", return_value="p"),
    ):
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb"},
            auth=_auth,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,testdb.public.users,PROD)",
            dry_run=False,
        )

    assert result.entities_ingested == 1
    assert result.errors == []

    # Assert exact set of emitted aspect class names rather than raw call count;
    # guards against silent order changes or extra/missing aspect emissions.
    # spec: BACKEND.md §Ingestion Service — aspects emitted per discovered dataset (L182-L184)
    emitted_aspect_types = {
        type(call.args[1]).__name__
        for call in datahub.emit_aspect.call_args_list
    }
    assert emitted_aspect_types == {"StatusClass", "DatasetPropertiesClass", "SchemaMetadataClass"}, (
        f"Expected exactly {{StatusClass, DatasetPropertiesClass, SchemaMetadataClass}}, "
        f"got {emitted_aspect_types}"
    )


async def test_postgresql_connection_failure_returns_error():
    datahub = AsyncMock()

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch("src.backend.ingestion.extractors.resolve_secret_ref", return_value="p"),
    ):
        mock_asyncpg.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "badhost", "port": 5432},
            identifier={"database": "testdb"},
            auth=_auth,
            dataset_urn="urn:li:dataset:x",
            dry_run=False,
        )

    assert result.entities_ingested == 0
    assert len(result.errors) == 1
    assert "connection failed" in result.errors[0].lower()


async def test_postgresql_resolver_not_found_returns_error_result():
    # spec: SECRET_RESOLUTION.md §Error taxonomy — SecretRefNotFound surfaces as
    # IngestionResult(errors=[...]) at run time (no 5xx).
    # spec: SECRET_RESOLUTION.md §Caller integration "At run time" step 5 —
    # "On any resolver exception: return IngestionResult(errors=[…])".
    datahub = AsyncMock()
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch(
            "src.backend.ingestion.extractors.resolve_secret_ref",
            side_effect=SecretRefNotFound("secret 'dataspoke-source-cred-test' does not exist"),
        ),
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb"},
            auth=_auth,
            dataset_urn="urn:li:dataset:x",
            dry_run=False,
        )

    assert result.entities_ingested == 0
    assert len(result.errors) >= 1
    mock_asyncpg.connect.assert_not_called()


async def test_postgresql_resolver_unavailable_returns_error_result():
    # spec: SECRET_RESOLUTION.md §Error taxonomy — SecretResolverUnavailable surfaces as
    # IngestionResult(errors=[...]) at run time (no 5xx).
    # spec: SECRET_RESOLUTION.md §Caller integration "At run time" step 5 —
    # "On any resolver exception: return IngestionResult(errors=[…])".
    datahub = AsyncMock()
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch(
            "src.backend.ingestion.extractors.resolve_secret_ref",
            side_effect=SecretResolverUnavailable("in-cluster config not available"),
        ),
    ):
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb"},
            auth=_auth,
            dataset_urn="urn:li:dataset:x",
            dry_run=False,
        )

    assert result.entities_ingested == 0
    assert len(result.errors) >= 1
    mock_asyncpg.connect.assert_not_called()


# ── Kafka extractor (mocked consumer) ────────────────────────────────────────


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
            dry_run=False,
        )

    assert result.entities_ingested == 1
    assert result.errors == []

    # Assert exact set of emitted aspect class names rather than raw call count.
    # spec: BACKEND.md §Ingestion Service — aspects emitted per discovered dataset (L182-L184)
    emitted_aspect_types = {
        type(call.args[1]).__name__
        for call in datahub.emit_aspect.call_args_list
    }
    assert emitted_aspect_types == {"StatusClass", "DatasetPropertiesClass", "SchemaMetadataClass"}, (
        f"Expected exactly {{StatusClass, DatasetPropertiesClass, SchemaMetadataClass}}, "
        f"got {emitted_aspect_types}"
    )


# ── Typed PDL union: PostgreSQL schema field types ────────────────────────────


_AUTH = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}


def _make_pg_row(data_type: str, column_comment: str | None = None, table_comment: str | None = None) -> dict:
    return {
        "table_schema": "catalog",
        "table_name": "title_master",
        "column_name": "col",
        "data_type": data_type,
        "ordinal_position": 1,
        "is_nullable": "NO",
        "column_comment": column_comment,
        "table_comment": table_comment,
    }


async def _run_pg_dry(mock_rows: list[dict]) -> object:
    """Helper: run postgres extractor dry_run=False and return the SchemaMetadataClass argument."""
    datahub = AsyncMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = mock_rows

    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch("src.backend.ingestion.extractors.resolve_secret_ref", return_value="p"),
    ):
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "example_db", "schema_name": "catalog", "table": "title_master"},
            auth=_AUTH,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"
    # Locate the SchemaMetadataClass call by class name
    from datahub.metadata.schema_classes import SchemaMetadataClass
    for call in datahub.emit_aspect.call_args_list:
        if isinstance(call.args[1], SchemaMetadataClass):
            return call.args[1]
    raise AssertionError("SchemaMetadataClass was never emitted")


@pytest.mark.parametrize("data_type,expected_inner_class", [
    ("integer", NumberTypeClass),
    ("bigint", NumberTypeClass),
    ("numeric", NumberTypeClass),
    ("character varying", StringTypeClass),
    ("text", StringTypeClass),
    ("date", DateTypeClass),
    ("boolean", BooleanTypeClass),
])
async def test_postgres_extractor_emits_typed_schema_field_types(
    data_type: str,
    expected_inner_class: type,
) -> None:
    """PostgreSQL column types map to typed PDL union instances, not bare strings.

    Fixes the bug where DataHub showed every column as 'Struct' because the
    extractor emitted raw string type identifiers instead of SchemaFieldDataTypeClass
    wrapping the correct typed class.

    spec: BACKEND.md §Custom Ingestor Authoring Contract — SchemaMetadata aspect emission.
    spec: DATAHUB_INTEGRATION.md §schemaMetadata — fields[].type must be a typed class.
    """
    rows = [_make_pg_row(data_type)]
    schema = await _run_pg_dry(rows)

    assert len(schema.fields) == 1
    field = schema.fields[0]

    assert isinstance(field.type, SchemaFieldDataTypeClass), (
        f"field.type must be SchemaFieldDataTypeClass; got {type(field.type).__name__}"
    )
    assert isinstance(field.type.type, expected_inner_class), (
        f"For data_type={data_type!r}, expected inner type {expected_inner_class.__name__}; "
        f"got {type(field.type.type).__name__}"
    )


async def test_postgres_extractor_unknown_type_falls_back_to_string() -> None:
    """An unrecognised PostgreSQL data_type falls back to StringTypeClass.

    spec: BACKEND.md §Custom Ingestor Authoring Contract — extractors must not
    raise on unknown types; StringTypeClass is the safe fallback.
    """
    rows = [_make_pg_row("some_obscure_type")]
    schema = await _run_pg_dry(rows)

    field = schema.fields[0]
    assert isinstance(field.type, SchemaFieldDataTypeClass), (
        "Unknown type must still produce SchemaFieldDataTypeClass wrapper"
    )
    assert isinstance(field.type.type, StringTypeClass), (
        f"Unknown type must fall back to StringTypeClass; "
        f"got {type(field.type.type).__name__}"
    )


@pytest.mark.parametrize("column_comment,expected_description", [
    ("ISBN-13 identifier for the book. Combined with edition_id forms the natural key.", "ISBN-13 identifier for the book. Combined with edition_id forms the natural key."),
    (None, None),
])
async def test_postgres_extractor_propagates_column_comments_to_field_description(
    column_comment: str | None,
    expected_description: str | None,
) -> None:
    """col_description() from PostgreSQL flows to SchemaFieldClass.description.

    When column_comment is set, the value is passed through verbatim.
    When column_comment is NULL (None), description must be None — not the empty
    string and not the literal 'None'.

    spec: BACKEND.md §Ingestion Service — PG comment ingestion.
    spec: DATAHUB_INTEGRATION.md §schemaMetadata — fields[].description.
    """
    rows = [_make_pg_row("integer", column_comment=column_comment)]
    schema = await _run_pg_dry(rows)

    field = schema.fields[0]
    assert field.description == expected_description, (
        f"For column_comment={column_comment!r}, expected field.description={expected_description!r}; "
        f"got {field.description!r}"
    )
    # Explicitly guard against 'None' string serialisation regression
    if expected_description is None:
        assert field.description is not False and field.description != "None", (
            "None column_comment must produce None description, not the string 'None'"
        )


async def _run_pg_and_get_dataset_properties_description(table_comment: str | None) -> str | None:
    """Helper: run postgres extractor and return the DatasetPropertiesClass.description value."""
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    datahub = AsyncMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [_make_pg_row("text", table_comment=table_comment)]

    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch("src.backend.ingestion.extractors.resolve_secret_ref", return_value="p"),
    ):
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)
        result = await run_datahub_ingestion(
            datahub=datahub,
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "example_db", "schema_name": "catalog", "table": "title_master"},
            auth=_AUTH,
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"
    props_call = next(
        (call for call in datahub.emit_aspect.call_args_list
         if isinstance(call.args[1], DatasetPropertiesClass)),
        None,
    )
    assert props_call is not None, "DatasetPropertiesClass was never emitted"
    return props_call.args[1].description


async def test_postgres_extractor_uses_table_comment_for_dataset_properties_description() -> None:
    """obj_description() from PostgreSQL flows to DatasetPropertiesClass.description.

    When table_comment is set, description equals the comment exactly.

    spec: BACKEND.md §Ingestion Service — PG comment ingestion.
    spec: DATAHUB_INTEGRATION.md §datasetProperties — description field.
    """
    seeded_comment = "Master record for each book title — one row per (ISBN, edition_id) combination."
    description = await _run_pg_and_get_dataset_properties_description(seeded_comment)

    assert description == seeded_comment, (
        f"When table_comment is set, description must match exactly; "
        f"expected {seeded_comment!r}, got {description!r}"
    )


async def test_postgres_extractor_emits_fallback_description_when_no_table_comment() -> None:
    """When obj_description() returns NULL, a non-empty fallback description is emitted.

    The spec requires a description to be emitted when obj_description() returns NULL.
    The exact placeholder text is impl-defined, but it must be non-empty and
    contextually informative (must reference the dataset).

    spec: BACKEND.md §Ingestion Service — a description MUST be emitted when
    obj_description() returns NULL.
    """
    description = await _run_pg_and_get_dataset_properties_description(None)

    # Spec (BACKEND.md §Ingestion Service): a description MUST be emitted when
    # obj_description() returns NULL. The exact placeholder text is impl-defined,
    # but it must be non-empty and contextually informative.
    assert description is not None
    assert description.strip() != ""
    assert "title_master" in description or "example_db" in description, (
        f"Fallback description must reference the dataset to be informative; got {description!r}"
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
            "page_count": 342,          # int → NumberTypeClass
            "title": "The Silent Cipher",  # str → StringTypeClass
            "is_active": True,          # bool → BooleanTypeClass
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
