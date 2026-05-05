"""Unit tests — freshness source discriminator (Group A5).

Spec sources:
- spec/feature/BACKEND.md §Validation Service "Source discriminator" table
  * datahub_operation (default) → read OperationClass.lastUpdatedTimestamp
  * datahub_profile → read DatasetProfileClass.timestampMillis
  * query → SELECT MAX(<last_modified_field>) via execute_sql
- src/backend/validation/timeseries.py _IDENTIFIER_RE for identifier validation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.rules import evaluate_rule
from tests.unit.backend.conftest import make_mock_operation, make_mock_profile

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"

# ── Default source: datahub_operation ─────────────────────────────────────────


async def test_freshness_default_source_queries_operation_class(datahub):
    """BACKEND.md §Source discriminator: freshness default source=datahub_operation reads OperationClass."""
    import time

    recent_ts_ms = int(time.time() * 1000) - 3600 * 1000  # 1 hour ago
    mock_op = make_mock_operation(recent_ts_ms)

    datahub.get_timeseries = AsyncMock(return_value=[mock_op])

    rule = {"rule_id": "r_fresh", "type": "freshness", "lookback_interval": "24h"}
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    # Must have called get_timeseries
    datahub.get_timeseries.assert_awaited_once()
    call_args = datahub.get_timeseries.call_args
    # The second positional arg is the aspect class
    from datahub.metadata.schema_classes import OperationClass
    assert call_args[0][1] == OperationClass, (
        "Default source must query OperationClass, not DatasetProfileClass"
    )
    assert result.assertion_result == "SUCCESS"


async def test_freshness_explicit_datahub_operation_queries_operation_class(datahub):
    """BACKEND.md: explicit source=datahub_operation reads OperationClass."""
    import time

    recent_ts_ms = int(time.time() * 1000) - 1800 * 1000  # 30 min ago
    mock_op = make_mock_operation(recent_ts_ms)

    datahub.get_timeseries = AsyncMock(return_value=[mock_op])

    rule = {
        "rule_id": "r_fresh",
        "type": "freshness",
        "source": "datahub_operation",
        "lookback_interval": "24h",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    from datahub.metadata.schema_classes import OperationClass
    assert datahub.get_timeseries.call_args[0][1] == OperationClass
    assert result.assertion_result == "SUCCESS"


# ── source: datahub_profile ───────────────────────────────────────────────────


async def test_freshness_datahub_profile_source_reads_dataset_profile_class(datahub):
    """BACKEND.md §Source discriminator: source=datahub_profile reads DatasetProfileClass.timestampMillis."""
    import time

    recent_ts_ms = int(time.time() * 1000) - 3600 * 1000  # 1 hour ago
    mock_profile = make_mock_profile(recent_ts_ms)

    datahub.get_timeseries = AsyncMock(return_value=[mock_profile])

    rule = {
        "rule_id": "r_fresh_profile",
        "type": "freshness",
        "source": "datahub_profile",
        "lookback_interval": "24h",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    from datahub.metadata.schema_classes import DatasetProfileClass
    assert datahub.get_timeseries.call_args[0][1] == DatasetProfileClass
    assert result.assertion_result == "SUCCESS"


# ── source: query — validation errors ─────────────────────────────────────────


async def test_freshness_query_source_requires_last_modified_field(datahub, db):
    """BACKEND.md §Source discriminator: source=query with missing last_modified_field → ERROR invalid_rule."""
    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        # no last_modified_field
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)
    # execute_sql must NOT have been called
    datahub.get_timeseries.assert_not_awaited() if hasattr(datahub, "get_timeseries") else None


async def test_freshness_query_source_rejects_sql_injection_identifier(datahub, db):
    """Security: last_modified_field with SQL metacharacters → ERROR with safe message (no injection)."""
    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "updated_at; DROP TABLE orders",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)
    # Safe message must not contain the injection payload itself
    for issue in result.issues:
        assert "DROP" not in issue.get("msg", "")


async def test_freshness_query_source_rejects_newline_in_identifier(datahub, db):
    """Security: newline in last_modified_field fails _IDENTIFIER_RE (\\A/\\Z anchored) → ERROR."""
    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "foo\n",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)


async def test_freshness_query_source_rejects_over_length_identifier(datahub, db):
    """Security: identifier > 63 chars exceeds PostgreSQL NAMEDATALEN-1 → ERROR."""
    # 64 chars (one over the 63-char cap)
    long_field = "a" * 64
    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": long_field,
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)


async def test_freshness_query_source_rejects_unicode_identifier(datahub, db):
    """Security: non-ASCII unicode in identifier → ERROR (regex requires [A-Za-z0-9_])."""
    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "café",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)


# ── source: query — SQL construction ─────────────────────────────────────────


async def test_freshness_query_builds_select_max_from_quoted_table(datahub, db):
    """BACKEND.md §Source discriminator: source=query builds SELECT MAX(field) FROM "schema"."table"."""
    import time

    recent_ts_ms = int(time.time() * 1000) - 3600 * 1000

    async def mock_resolve(db_session, dataset_urn, rule):
        return ("postgres", {"host": "h", "port": 5432}, {"schema_name": "public", "table": "orders"}, None)

    async def capture_execute_sql(platform, locator, identifier, auth, sql):
        # Verify the SQL contains the correct FROM clause with quoted identifiers
        assert 'FROM "public"."orders"' in sql, f"Expected quoted table ref in SQL, got: {sql}"
        assert "updated_at" in sql, "SQL must reference the last_modified_field"
        return [{"last_ts": datetime_from_ms(recent_ts_ms)}]

    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "updated_at",
    }

    # freshness.py imports resolve_source_config and execute_sql from timeseries
    # via a local import inside _evaluate_query; patch at the timeseries module level
    with (
        patch("src.backend.validation.timeseries.resolve_source_config", new=AsyncMock(side_effect=mock_resolve)),
        patch("src.backend.validation.timeseries.execute_sql", new=AsyncMock(side_effect=capture_execute_sql)),
    ):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "SUCCESS"


async def test_freshness_query_includes_filter_in_where_clause(datahub, db):
    """BACKEND.md §Source discriminator: source=query with filter adds WHERE clause verbatim."""
    import time

    recent_ts_ms = int(time.time() * 1000) - 3600 * 1000
    captured_sql = []

    async def mock_resolve(db_session, dataset_urn, rule):
        return ("postgres", {"host": "h", "port": 5432}, {"schema_name": "orders", "table": "raw_events"}, None)

    async def capture_execute_sql(platform, locator, identifier, auth, sql):
        captured_sql.append(sql)
        return [{"last_ts": datetime_from_ms(recent_ts_ms)}]

    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "updated_at",
        "filter": "tenant_id = 'imazon'",
    }

    with (
        patch("src.backend.validation.timeseries.resolve_source_config", new=AsyncMock(side_effect=mock_resolve)),
        patch("src.backend.validation.timeseries.execute_sql", new=AsyncMock(side_effect=capture_execute_sql)),
    ):
        await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert len(captured_sql) == 1
    # Filter is concatenated verbatim per spec (same trust model as custom/sql_timeseries).
    assert "tenant_id = 'imazon'" in captured_sql[0], "Filter must appear verbatim in WHERE clause"
    assert "WHERE" in captured_sql[0]


async def test_freshness_query_invalid_table_identifier_errors(datahub, db):
    """Security: invalid schema/table in IngestionConfig → quote_table_ref raises ValueError → ERROR."""
    async def mock_resolve(db_session, dataset_urn, rule):
        # Return an identifier with a space in schema_name
        return ("postgres", {"host": "h", "port": 5432}, {"schema_name": "bad schema!", "table": "orders"}, None)

    rule = {
        "rule_id": "r_fresh_q",
        "type": "freshness",
        "source": "query",
        "lookback_interval": "24h",
        "last_modified_field": "updated_at",
    }

    with patch("src.backend.validation.timeseries.resolve_source_config", new=AsyncMock(side_effect=mock_resolve)):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)


# ── Unknown source → ERROR ────────────────────────────────────────────────────


async def test_freshness_unknown_source_errors(datahub, db):
    """BACKEND.md §Source discriminator: unknown source value → ERROR invalid_rule."""
    rule = {
        "rule_id": "r_fresh_unknown",
        "type": "freshness",
        "source": "bogus",
        "lookback_interval": "24h",
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)


# ── Helper ────────────────────────────────────────────────────────────────────


def datetime_from_ms(ms: int):
    """Return a datetime from epoch milliseconds."""
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
