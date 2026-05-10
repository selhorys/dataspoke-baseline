"""Unit tests for the PostgreSQL ingestion extractor (mocked asyncpg)."""

from unittest.mock import AsyncMock, patch

import pytest
from datahub.metadata.schema_classes import (
    BooleanTypeClass,
    DateTypeClass,
    NumberTypeClass,
    SchemaFieldDataTypeClass,
    StringTypeClass,
)

from src.backend.ingestion.extractors import run_datahub_ingestion
from src.backend.ingestion.secret_resolver import (
    SecretRefNotFound,
    SecretResolverUnavailable,
)

_AUTH = {"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}}


def _make_pg_row(
    data_type: str,
    column_comment: str | None = None,
    table_comment: str | None = None,
) -> dict:
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


async def _run_pg_and_get_schema(mock_rows: list[dict]) -> object:
    """Helper: run postgres extractor (dry_run=False) and return the SchemaMetadataClass argument."""
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
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"
    from datahub.metadata.schema_classes import SchemaMetadataClass

    for call in datahub.emit_aspect.call_args_list:
        if isinstance(call.args[1], SchemaMetadataClass):
            return call.args[1]
    raise AssertionError("SchemaMetadataClass was never emitted")


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
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.errors == [], f"Unexpected errors: {result.errors}"
    props_call = next(
        (
            call
            for call in datahub.emit_aspect.call_args_list
            if isinstance(call.args[1], DatasetPropertiesClass)
        ),
        None,
    )
    assert props_call is not None, "DatasetPropertiesClass was never emitted"
    return props_call.args[1].description


# ── Discovery + emission ──────────────────────────────────────────────────────


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
            run_id="test-run-id",
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
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.entities_ingested == 0
    assert len(result.errors) == 1
    assert "connection failed" in result.errors[0].lower()


# ── Secret resolver error paths ───────────────────────────────────────────────


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
            run_id="test-run-id",
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
            run_id="test-run-id",
            dry_run=False,
        )

    assert result.entities_ingested == 0
    assert len(result.errors) >= 1
    mock_asyncpg.connect.assert_not_called()


# ── Typed PDL union: schema field types ───────────────────────────────────────


@pytest.mark.parametrize(
    "data_type,expected_inner_class",
    [
        ("integer", NumberTypeClass),
        ("bigint", NumberTypeClass),
        ("numeric", NumberTypeClass),
        ("character varying", StringTypeClass),
        ("text", StringTypeClass),
        ("date", DateTypeClass),
        ("boolean", BooleanTypeClass),
    ],
)
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
    schema = await _run_pg_and_get_schema(rows)

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
    schema = await _run_pg_and_get_schema(rows)

    field = schema.fields[0]
    assert isinstance(field.type, SchemaFieldDataTypeClass), (
        "Unknown type must still produce SchemaFieldDataTypeClass wrapper"
    )
    assert isinstance(field.type.type, StringTypeClass), (
        f"Unknown type must fall back to StringTypeClass; "
        f"got {type(field.type.type).__name__}"
    )


# ── Comment ingestion ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "column_comment,expected_description",
    [
        (
            "ISBN-13 identifier for the book. Combined with edition_id forms the natural key.",
            "ISBN-13 identifier for the book. Combined with edition_id forms the natural key.",
        ),
        (None, None),
    ],
)
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
    schema = await _run_pg_and_get_schema(rows)

    field = schema.fields[0]
    assert field.description == expected_description, (
        f"For column_comment={column_comment!r}, expected field.description={expected_description!r}; "
        f"got {field.description!r}"
    )
    if expected_description is None:
        assert field.description is not False and field.description != "None", (
            "None column_comment must produce None description, not the string 'None'"
        )


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

    assert description is not None
    assert description.strip() != ""
    assert "title_master" in description or "example_db" in description, (
        f"Fallback description must reference the dataset to be informative; got {description!r}"
    )


# ── systemMetadata runId emission ─────────────────────────────────────────────


async def test_postgres_extractor_emits_systemmetadata_with_dataspoke_runid() -> None:
    """Every dataset-aspect emit from the PostgreSQL extractor carries a
    SystemMetadataClass with runId='dataspoke-postgres-<run_id>'.

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
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "table_schema": "catalog",
            "table_name": "title_master",
            "column_name": "isbn",
            "data_type": "character varying",
            "ordinal_position": 1,
            "is_nullable": "NO",
            "column_comment": None,
            "table_comment": None,
        }
    ]

    run_id = "test-run-id"
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
            auth={"username": "u", "secret_ref": {"name": "dataspoke-source-cred-test", "key": "password"}},
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
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

    expected_run_id = f"dataspoke-postgres-{run_id}"
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
