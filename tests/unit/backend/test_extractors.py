"""Unit tests for ingestion extractors (mocked infrastructure)."""

from unittest.mock import AsyncMock, patch

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
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
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

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
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

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
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

    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
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
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
    with (
        patch("src.backend.ingestion.extractors.asyncpg") as mock_asyncpg,
        patch(
            "src.backend.ingestion.extractors.resolve_secret_ref",
            side_effect=SecretRefNotFound("secret 'dataspoke-conf-test' does not exist"),
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
    _auth = {"username": "u", "secret_ref": {"name": "dataspoke-conf-test", "key": "password"}}
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
