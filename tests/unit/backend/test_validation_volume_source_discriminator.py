"""Unit tests — volume source discriminator (Group A6).

Spec sources:
- spec/feature/BACKEND.md §Validation Service "Source discriminator" table
  * datahub_profile (default) → read DatasetProfileClass.rowCount
  * query → SELECT COUNT(*) [WHERE filter] on the source platform
  * datahub_operation is NOT valid for volume → ERROR
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.backend.validation.rules import evaluate_rule
from tests.unit.backend.conftest import make_mock_profile

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"


# ── Default source: datahub_profile ──────────────────────────────────────────


async def test_volume_default_source_reads_dataset_profile_class(datahub):
    """BACKEND.md §Source discriminator: volume default source=datahub_profile reads DatasetProfileClass."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    mock_profile = make_mock_profile(timestamp_ms=0, row_count=5000)
    datahub.get_timeseries = AsyncMock(return_value=[mock_profile])

    rule = {
        "rule_id": "r_vol",
        "type": "volume",
        "condition": {"type": "greater_than", "value": 100},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    datahub.get_timeseries.assert_awaited_once()
    # Must have queried DatasetProfileClass, not OperationClass
    assert datahub.get_timeseries.call_args[0][1] == DatasetProfileClass
    assert result.assertion_result == "SUCCESS"


async def test_volume_explicit_datahub_profile_reads_row_count(datahub):
    """BACKEND.md: explicit source=datahub_profile reads DatasetProfileClass.rowCount."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    mock_profile = make_mock_profile(timestamp_ms=0, row_count=200)
    datahub.get_timeseries = AsyncMock(return_value=[mock_profile])

    rule = {
        "rule_id": "r_vol_profile",
        "type": "volume",
        "source": "datahub_profile",
        "condition": {"type": "greater_than", "value": 100},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    assert datahub.get_timeseries.call_args[0][1] == DatasetProfileClass
    assert result.assertion_result == "SUCCESS"


# ── source: query — SQL construction ─────────────────────────────────────────


async def test_volume_query_builds_select_count_from_quoted_table(datahub, db):
    """BACKEND.md §Source discriminator: source=query builds SELECT COUNT(*) FROM "schema"."table"."""
    captured_sql = []

    async def mock_resolve(db_session, dataset_urn, rule):
        return ("postgres", {"host": "h", "port": 5432}, {"schema_name": "orders", "table": "daily_fulfillment_summary"}, None)

    async def capture_execute_sql(platform, locator, identifier, auth, sql):
        captured_sql.append(sql)
        return [{"row_count": 3000}]

    rule = {
        "rule_id": "r_vol_q",
        "type": "volume",
        "source": "query",
        "condition": {"type": "greater_than", "value": 100},
    }

    # volume.py imports resolve_source_config and execute_sql from timeseries
    # via a local import inside _evaluate_query; patch at the timeseries module level
    with (
        patch("src.backend.validation.timeseries.resolve_source_config", new=AsyncMock(side_effect=mock_resolve)),
        patch("src.backend.validation.timeseries.execute_sql", new=AsyncMock(side_effect=capture_execute_sql)),
    ):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert len(captured_sql) == 1
    sql = captured_sql[0]
    assert "COUNT(*)" in sql.upper() or "count(*)" in sql.lower()
    assert '"orders"."daily_fulfillment_summary"' in sql
    assert result.assertion_result == "SUCCESS"


async def test_volume_query_includes_filter_in_where_clause(datahub, db):
    """BACKEND.md §Source discriminator: source=query with filter adds WHERE clause verbatim."""
    captured_sql = []

    async def mock_resolve(db_session, dataset_urn, rule):
        return ("postgres", {"host": "h", "port": 5432}, {"schema_name": "orders", "table": "raw_events"}, None)

    async def capture_execute_sql(platform, locator, identifier, auth, sql):
        captured_sql.append(sql)
        return [{"row_count": 500}]

    rule = {
        "rule_id": "r_vol_q",
        "type": "volume",
        "source": "query",
        "condition": {"type": "greater_than", "value": 0},
        "filter": "tenant_id = 'imazon'",
    }

    with (
        patch("src.backend.validation.timeseries.resolve_source_config", new=AsyncMock(side_effect=mock_resolve)),
        patch("src.backend.validation.timeseries.execute_sql", new=AsyncMock(side_effect=capture_execute_sql)),
    ):
        await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert len(captured_sql) == 1
    assert "WHERE" in captured_sql[0]
    # Filter is concatenated verbatim per spec (same trust model as custom/sql_timeseries).
    assert "tenant_id = 'imazon'" in captured_sql[0]


# ── datahub_operation is NOT valid for volume ─────────────────────────────────


async def test_volume_datahub_operation_source_errors(datahub, db):
    """BACKEND.md §Source discriminator table: datahub_operation is only valid for freshness, not volume."""
    rule = {
        "rule_id": "r_vol_op",
        "type": "volume",
        "source": "datahub_operation",
        "condition": {"type": "greater_than", "value": 0},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues), (
        "datahub_operation is not a valid volume source — must produce invalid_rule error"
    )


# ── Unknown source → ERROR ────────────────────────────────────────────────────


async def test_volume_unknown_source_errors(datahub, db):
    """BACKEND.md §Source discriminator: unknown source value → ERROR invalid_rule."""
    rule = {
        "rule_id": "r_vol_unknown",
        "type": "volume",
        "source": "bogus_source",
        "condition": {"type": "greater_than", "value": 0},
    }
    result = await evaluate_rule(datahub, _DATASET_URN, rule, {}, db=db)

    assert result.assertion_result == "ERROR"
    assert any(issue.get("type") == "invalid_rule" for issue in result.issues)
