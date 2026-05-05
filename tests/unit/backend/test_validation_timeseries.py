"""Unit tests for the SQL-based timeseries engine (mocked asyncpg + DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.timeseries import (
    execute_sql,
    execute_timeseries_sql,
    resolve_source_config,
)
from src.shared.exceptions import EntityNotFoundError

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"


# ── resolve_source_config ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_source_config_ignores_rule_source_string(db):
    """Rule with source as a string discriminator always resolves via IngestionConfig."""
    ingestion_row = MagicMock()
    ingestion_row.platform = "postgres"
    ingestion_row.locator = {"host": "db.imazon.internal", "port": 5432}
    ingestion_row.identifier = {"database": "imazon"}
    ingestion_row.auth = None

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ingestion_row
    db.execute = AsyncMock(return_value=result_mock)

    rule = {"rule_id": "r1", "source": "query"}

    platform, locator, identifier, auth = await resolve_source_config(
        db, _DATASET_URN, rule
    )

    assert platform == "postgres"
    assert locator["host"] == "db.imazon.internal"
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_source_config_falls_back_to_ingestion_config(db):
    """Rule without source override queries DB for IngestionConfig."""
    ingestion_row = MagicMock()
    ingestion_row.platform = "postgres"
    ingestion_row.locator = {"host": "db.imazon.internal", "port": 5432}
    ingestion_row.identifier = {"database": "imazon"}
    ingestion_row.auth = {"username": "etl", "password": "pw"}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ingestion_row
    db.execute = AsyncMock(return_value=result_mock)

    rule = {"rule_id": "r1"}

    platform, locator, identifier, auth = await resolve_source_config(
        db, _DATASET_URN, rule
    )

    assert platform == "postgres"
    assert locator["host"] == "db.imazon.internal"
    assert identifier["database"] == "imazon"
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_source_config_raises_when_no_config_and_no_override(db):
    """No source override and no DB row → EntityNotFoundError."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    rule = {"rule_id": "r_missing"}

    with pytest.raises(EntityNotFoundError):
        await resolve_source_config(db, _DATASET_URN, rule)


@pytest.mark.asyncio
async def test_resolve_source_config_partial_source_override_missing_platform(db):
    """source dict present but missing platform falls back to DB lookup."""
    ingestion_row = MagicMock()
    ingestion_row.platform = "postgres"
    ingestion_row.locator = {"host": "db.host", "port": 5432}
    ingestion_row.identifier = {"database": "imazon"}
    ingestion_row.auth = None

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ingestion_row
    db.execute = AsyncMock(return_value=result_mock)

    # source dict present but no platform key
    rule = {"rule_id": "r1", "source": {"locator": {"host": "override"}}}

    platform, locator, _, _ = await resolve_source_config(db, _DATASET_URN, rule)

    # Must have fallen back to ingestion config
    assert platform == "postgres"
    assert locator["host"] == "db.host"


# ── execute_sql ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_sql_postgresql_calls_asyncpg_fetch():
    """PostgreSQL platform: asyncpg.connect is called and fetch returns rows."""
    mock_conn = AsyncMock()
    mock_row = {"row_count": 42}
    mock_conn.fetch = AsyncMock(return_value=[mock_row])

    with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
        rows = await execute_sql(
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "imazon"},
            auth={"username": "user", "password": "pass"},
            sql="SELECT COUNT(*) AS row_count FROM orders.order_items",
        )

    assert rows == [mock_row]
    mock_conn.fetch.assert_called_once_with(
        "SELECT COUNT(*) AS row_count FROM orders.order_items"
    )


@pytest.mark.asyncio
async def test_execute_sql_postgresql_platform_canonical_value():
    """Canonical lowercase platform value 'postgres' works correctly."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[{"v": 1}])

    with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
        rows = await execute_sql(
            platform="postgres",
            locator={"host": "h", "port": 5432},
            identifier={"database": "db"},
            auth=None,
            sql="SELECT 1 AS v",
        )

    assert rows == [{"v": 1}]


@pytest.mark.asyncio
async def test_execute_sql_connection_always_closed_on_success():
    """asyncpg connection is always closed after successful fetch."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
        await execute_sql(
            platform="postgres",
            locator={"host": "h", "port": 5432},
            identifier={"database": "db"},
            auth=None,
            sql="SELECT 1",
        )

    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_execute_sql_connection_closed_even_on_fetch_error():
    """asyncpg connection is closed in the finally block even when fetch raises."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=RuntimeError("connection reset"))

    with patch("asyncpg.connect", new=AsyncMock(return_value=mock_conn)):
        with pytest.raises(RuntimeError, match="connection reset"):
            await execute_sql(
                platform="postgres",
                locator={"host": "h", "port": 5432},
                identifier={"database": "db"},
                auth=None,
                sql="SELECT 1",
            )

    mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_execute_sql_unsupported_platform_raises():
    """Non-PostgreSQL platform raises NotImplementedError."""
    with pytest.raises(NotImplementedError, match="SQL execution not supported for bigquery"):
        await execute_sql(
            platform="bigquery",
            locator={},
            identifier={},
            auth=None,
            sql="SELECT 1",
        )


# ── execute_timeseries_sql ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_timeseries_sql_returns_latest_partition(db):
    """Full pipeline: resolves source and returns values from latest ordered row."""
    rows = [
        {"load_date": "2025-03-10", "row_count": 1000},
        {"load_date": "2025-03-11", "row_count": 1200},
        {"load_date": "2025-03-09", "row_count": 900},
    ]

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
                "order": ["load_date"],
                "partition": ["load_date"],
                "values": ["row_count"],
            },
            partition={},
        )

    # Latest partition (2025-03-11) should be selected
    assert result["partitions"] == {"load_date": "2025-03-11"}
    assert result["values"] == {"row_count": 1200}


@pytest.mark.asyncio
async def test_execute_timeseries_sql_with_explicit_partition_filter(db):
    """Explicit partition filters rows to matching load_date."""
    rows = [
        {"load_date": "2025-03-10", "row_count": 1000},
        {"load_date": "2025-03-11", "row_count": 1200},
    ]

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT load_date, COUNT(*) AS row_count FROM orders GROUP BY 1",
                "order": ["load_date"],
                "partition": ["load_date"],
                "values": ["row_count"],
            },
            partition={"load_date": "2025-03-10"},
        )

    assert result["partitions"] == {"load_date": "2025-03-10"}
    assert result["values"] == {"row_count": 1000}


@pytest.mark.asyncio
async def test_execute_timeseries_sql_empty_result_returns_empty_dicts(db):
    """Empty SQL result → {'partitions': {}, 'values': {}}."""
    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT load_date, row_count FROM summary",
                "order": ["load_date"],
                "partition": ["load_date"],
                "values": ["row_count"],
            },
            partition={},
        )

    assert result == {"partitions": {}, "values": {}}


@pytest.mark.asyncio
async def test_execute_timeseries_sql_extracts_only_declared_value_columns(db):
    """Only columns listed in rule['values'] appear in the result values."""
    rows = [
        {"load_date": "2025-03-11", "row_count": 500, "null_count": 5, "extra": "ignored"}
    ]

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT * FROM summary",
                "order": ["load_date"],
                "partition": ["load_date"],
                "values": ["row_count", "null_count"],
            },
            partition={},
        )

    assert set(result["values"].keys()) == {"row_count", "null_count"}
    assert "extra" not in result["values"]


@pytest.mark.asyncio
async def test_execute_sql_postgresql_connect_kwargs():
    """asyncpg.connect receives the correct host/port/user/password/database kwargs.

    Auth uses the persisted secret_ref shape; resolve_secret_ref is mocked to return
    the plaintext password so asyncpg receives the resolved credential.
    """
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    connect_mock = AsyncMock(return_value=mock_conn)

    auth = {
        "username": "etl",
        "secret_ref": {"name": "dataspoke-source-cred-imazon", "key": "password"},
    }

    with (
        patch("asyncpg.connect", new=connect_mock),
        patch(
            "src.backend.ingestion.secret_resolver.resolve_secret_ref",
            return_value="s3cret",
        ),
    ):
        await execute_sql(
            platform="postgres",
            locator={"host": "pg.imazon.internal", "port": 5433},
            identifier={"database": "imazon"},
            auth=auth,
            sql="SELECT 1",
        )

    connect_mock.assert_called_once_with(
        host="pg.imazon.internal",
        port=5433,
        user="etl",
        password="s3cret",
        database="imazon",
    )


@pytest.mark.asyncio
async def test_execute_sql_postgresql_auth_none_uses_empty_credentials():
    """When auth is None, asyncpg.connect receives empty user and password strings."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    connect_mock = AsyncMock(return_value=mock_conn)

    with patch("asyncpg.connect", new=connect_mock):
        await execute_sql(
            platform="postgres",
            locator={"host": "h", "port": 5432},
            identifier={"database": "db"},
            auth=None,
            sql="SELECT 1",
        )

    _, kwargs = connect_mock.call_args
    assert kwargs["user"] == ""
    assert kwargs["password"] == ""


@pytest.mark.asyncio
async def test_execute_timeseries_sql_no_order_columns_takes_first_row(db):
    """Without order columns, the first row is used as target partition."""
    rows = [
        {"load_date": "2025-03-09", "row_count": 900},
        {"load_date": "2025-03-11", "row_count": 1200},
    ]

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT load_date, row_count FROM summary",
                "partition": ["load_date"],
                "values": ["row_count"],
                # no "order" key
            },
            partition={},
        )

    # First row is used when no order is specified
    assert result["values"]["row_count"] == 900


@pytest.mark.asyncio
async def test_execute_timeseries_sql_unmatched_partition_falls_back_to_first_row(db):
    """Partition filter matches no row → falls back to rows[0] as the target."""
    rows = [
        {"load_date": "2025-03-09", "row_count": 900},
        {"load_date": "2025-03-10", "row_count": 1000},
    ]

    with (
        patch(
            "src.backend.validation.timeseries.resolve_source_config",
            new=AsyncMock(
                return_value=("postgres", {"host": "h", "port": 5432}, {"database": "d"}, None)
            ),
        ),
        patch(
            "src.backend.validation.timeseries.execute_sql",
            new=AsyncMock(return_value=rows),
        ),
    ):
        result = await execute_timeseries_sql(
            db=db,
            dataset_urn=_DATASET_URN,
            rule={
                "sql": "SELECT load_date, row_count FROM summary",
                "order": ["load_date"],
                "partition": ["load_date"],
                "values": ["row_count"],
            },
            # Partition key value that does not match any row
            partition={"load_date": "1999-01-01"},
        )

    # Falls back to rows[0] when the filter matches nothing
    assert result["values"]["row_count"] == 900
