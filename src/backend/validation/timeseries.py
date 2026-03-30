"""SQL-based timeseries engine stub.

The full implementation that executes SQL against PostgreSQL, Trino,
Snowflake, etc. is deferred to the second pass.
"""

from typing import Any


class TimeseriesEngineNotImplemented(NotImplementedError):
    """Raised when the SQL timeseries engine is invoked before implementation."""


async def execute_timeseries_sql(
    source_config: dict[str, Any],
    sql: str,
    partition: dict[str, Any],
) -> dict[str, Any]:
    """Execute a timeseries SQL query against the dataset's source.

    Not yet implemented — raises TimeseriesEngineNotImplemented.
    """
    raise TimeseriesEngineNotImplemented("SQL timeseries engine not yet implemented")
