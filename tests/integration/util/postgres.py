"""PostgreSQL dummy-data reset utilities for integration tests.

Connects directly to example-postgres via asyncpg and re-seeds from the SQL
fixture files under fixtures/sql/.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_SCHEMAS: frozenset[str] = frozenset(
    {
        "catalog",
        "orders",
        "customers",
        "reviews",
        "publishers",
        "shipping",
        "inventory",
        "marketing",
        "products",
        "content",
        "storefront",
    }
)

# Schemas that share a single seed file (09_ebooknow.sql).  Resetting any one
# of them must reset all three to keep referential integrity intact.
_COLOCATED_SCHEMAS: frozenset[str] = frozenset({"products", "content", "storefront"})

# Map each schema to the ordered list of SQL filenames required to recreate it.
# Every schema needs 00_schemas.sql (creates all schema namespaces) plus its
# own data file.
SCHEMA_TO_SQL_FILES: dict[str, list[str]] = {
    "catalog": ["00_schemas.sql", "01_catalog.sql"],
    "orders": ["00_schemas.sql", "02_orders.sql"],
    "customers": ["00_schemas.sql", "03_customers.sql"],
    "reviews": ["00_schemas.sql", "04_reviews.sql"],
    "publishers": ["00_schemas.sql", "05_publishers.sql"],
    "shipping": ["00_schemas.sql", "06_shipping.sql"],
    "inventory": ["00_schemas.sql", "07_inventory.sql"],
    "marketing": ["00_schemas.sql", "08_marketing.sql"],
    # products / content / storefront all live in 09_ebooknow.sql
    "products": ["00_schemas.sql", "09_ebooknow.sql"],
    "content": ["00_schemas.sql", "09_ebooknow.sql"],
    "storefront": ["00_schemas.sql", "09_ebooknow.sql"],
}

_FIXTURES_DIR: Path = Path(__file__).parent / "fixtures" / "sql"

# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load dev_env/.env into os.environ without overwriting existing vars.

    Walks up from the project root (three levels above this file) to handle
    git worktrees where dev_env/.env may live in the main worktree.
    """
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "dev_env" / ".env"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

_pg_host = "localhost"
_pg_port = int(os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT", "9102"))
_pg_user = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_pg_password = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "ExampleDev2024!")
_pg_db = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _expand_colocated_schemas(schemas: frozenset[str] | set[str]) -> frozenset[str]:
    """If any colocated schema is requested, expand to include all three."""
    expanded = set(schemas)
    if expanded & _COLOCATED_SCHEMAS:
        expanded |= _COLOCATED_SCHEMAS
    return frozenset(expanded)


async def _get_connection() -> asyncpg.Connection:
    """Create a connection to example-postgres."""
    return await asyncpg.connect(
        host=_pg_host,
        port=_pg_port,
        user=_pg_user,
        password=_pg_password,
        database=_pg_db,
    )


async def _execute_sql_file(conn: asyncpg.Connection, filename: str) -> None:
    """Execute a SQL seed file against the given connection."""
    sql = (_FIXTURES_DIR / filename).read_text()
    await conn.execute(sql)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reset_all() -> None:
    """Drop all custom schemas CASCADE, then re-execute all SQL seed files in order."""
    conn = await _get_connection()
    try:
        for schema in ALL_SCHEMAS:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

        for sql_file in sorted(_FIXTURES_DIR.glob("*.sql")):
            await _execute_sql_file(conn, sql_file.name)
    finally:
        await conn.close()


async def reset_schemas(schemas: frozenset[str] | set[str]) -> None:
    """Drop and re-seed specific schemas.

    Auto-expands colocated schemas (products / content / storefront) so that
    09_ebooknow.sql is always executed as a unit.
    """
    effective = _expand_colocated_schemas(schemas)

    # Collect the ordered, deduplicated set of SQL files needed.
    sql_files_needed: list[str] = ["00_schemas.sql"]
    seen: set[str] = {"00_schemas.sql"}
    for schema in sorted(effective):
        for f in SCHEMA_TO_SQL_FILES.get(schema, []):
            if f not in seen:
                sql_files_needed.append(f)
                seen.add(f)

    conn = await _get_connection()
    try:
        for schema in effective:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

        for filename in sql_files_needed:
            await _execute_sql_file(conn, filename)
    finally:
        await conn.close()


async def reset_tables(schema: str, tables: list[str]) -> None:
    """TRUNCATE specific tables within a schema and re-seed from the schema's SQL file.

    Uses TRUNCATE … RESTART IDENTITY CASCADE so sequences reset along with
    foreign-key dependents.  The entire schema seed file is then re-executed so
    all rows are restored.
    """
    effective_schemas = _expand_colocated_schemas({schema})

    conn = await _get_connection()
    try:
        for table in tables:
            await conn.execute(f"TRUNCATE {schema}.{table} RESTART IDENTITY CASCADE")

        # Re-seed the schema(s) from their SQL file(s), skipping 00_schemas.sql
        # (schema namespaces already exist).
        seed_files: list[str] = []
        seen: set[str] = set()
        for s in sorted(effective_schemas):
            for f in SCHEMA_TO_SQL_FILES.get(s, []):
                if f != "00_schemas.sql" and f not in seen:
                    seed_files.append(f)
                    seen.add(f)

        for filename in seed_files:
            await _execute_sql_file(conn, filename)
    finally:
        await conn.close()
