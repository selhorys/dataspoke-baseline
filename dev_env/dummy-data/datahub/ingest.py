"""Register example-postgres tables as DataHub dataset entities.

Connects to the port-forwarded example-postgres to discover schemas, tables,
and columns, then emits DatasetProperties + SchemaMetadata aspects to DataHub
GMS via the REST emitter.

Usage:
    uv run python dev_env/dummy-data/datahub/ingest.py          # ingest
    uv run python dev_env/dummy-data/datahub/ingest.py --reset   # delete + ingest
    uv run python dev_env/dummy-data/datahub/ingest.py --reset-only  # delete only

Environment variables (loaded from dev_env/.env if present):
    DATASPOKE_DATAHUB_GMS_URL       (default: http://localhost:9004)
    DATASPOKE_DATAHUB_TOKEN         (default: empty — auto-fetched via frontend login)
    DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT (default: 9102)
    DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER     (default: postgres)
    DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD  (default: ExampleDev2024!)
    DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB        (default: example_db)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import asyncpg
import requests
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    OtherSchemaClass,
    SchemaFieldClass,
    SchemaMetadataClass,
    StatusClass,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PLATFORM = "postgres"
ENV = "DEV"
INSTANCE = "example_db"

# Schemas seeded by dummy-data-reset.sh (skip system schemas)
TARGET_SCHEMAS = frozenset(
    [
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
    ]
)

# Map information_schema.columns.data_type to DataHub SchemaFieldDataType
_PG_TO_DATAHUB_TYPE = {
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


def _load_dotenv() -> None:
    """Load dev_env/.env into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[2]
    for candidate in (start, *start.parents):
        env_path = candidate / ".env"
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

_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "http://localhost:9004")
_frontend_url = os.environ.get(
    "DATASPOKE_DATAHUB_FRONTEND_URL",
    f"http://localhost:{os.environ.get('DATASPOKE_DEV_KUBE_DATAHUB_PORT_FORWARD_UI_PORT', '9002')}",
)
_token_env = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")
_pg_host = "localhost"
_pg_port = int(os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT", "9102"))
_pg_user = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_pg_password = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "ExampleDev2024!")
_pg_db = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")


# ---------------------------------------------------------------------------
# DataHub auth: obtain a session token via frontend login if not provided
# ---------------------------------------------------------------------------


def _get_datahub_session_token() -> str:
    """Get a DataHub session token via frontend login for dev-env."""
    resp = requests.post(
        f"{_frontend_url}/logIn",
        json={"username": "datahub", "password": "datahub"},
        timeout=5,
    )
    resp.raise_for_status()
    cookie = resp.headers.get("Set-Cookie", "")
    if "PLAY_SESSION=" not in cookie:
        return ""
    play_session = cookie.split("PLAY_SESSION=")[1].split(";")[0]
    payload = play_session.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    data = json.loads(base64.b64decode(payload))
    return data.get("data", {}).get("token", "")


def _resolve_token() -> str | None:
    """Return auth token: env var first, then frontend login, then None."""
    if _token_env:
        return _token_env
    try:
        token = _get_datahub_session_token()
        if token:
            return token
    except Exception as exc:
        print(f"  [WARN] Could not obtain DataHub session token: {exc}")
    return None


_token = _resolve_token()


def _make_urn(schema: str, table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{INSTANCE}.{schema}.{table},{ENV})"


# ---------------------------------------------------------------------------
# Discover tables + columns from example-postgres (async via asyncpg)
# ---------------------------------------------------------------------------


async def discover_tables() -> dict[str, list[dict]]:
    """Return {urn: [column_dicts]} by querying information_schema."""
    conn = await asyncpg.connect(
        host=_pg_host,
        port=_pg_port,
        user=_pg_user,
        password=_pg_password,
        database=_pg_db,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT table_schema, table_name, column_name, data_type,
                   ordinal_position, is_nullable
            FROM information_schema.columns
            WHERE table_schema = ANY($1::text[])
            ORDER BY table_schema, table_name, ordinal_position
            """,
            sorted(TARGET_SCHEMAS),
        )
    finally:
        await conn.close()

    datasets: dict[str, list[dict]] = {}
    for row in rows:
        urn = _make_urn(row["table_schema"], row["table_name"])
        datasets.setdefault(urn, []).append(
            {
                "schema": row["table_schema"],
                "table": row["table_name"],
                "name": row["column_name"],
                "native_type": row["data_type"],
                "ordinal": row["ordinal_position"],
                "nullable": row["is_nullable"] == "YES",
            }
        )
    return datasets


# ---------------------------------------------------------------------------
# Reset: soft-delete all datasets from the example_db platform instance
# ---------------------------------------------------------------------------


def reset_datahub_datasets() -> int:
    """Soft-delete all example_db datasets from DataHub. Returns count deleted."""
    graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=_token))
    emitter = DatahubRestEmitter(gms_server=_gms_url, token=_token)

    urns = list(
        graph.get_urns_by_filter(
            entity_types=["dataset"],
            platform=PLATFORM,
        )
    )
    # Filter to only example_db datasets
    prefix = f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},{INSTANCE}."
    urns = [u for u in urns if u.startswith(prefix)]

    if not urns:
        print(f"  No existing {INSTANCE} datasets to delete.")
        return 0

    for urn in urns:
        mcp = MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=StatusClass(removed=True),
        )
        emitter.emit_mcp(mcp)

    print(f"  Soft-deleted {len(urns)} datasets.")
    return len(urns)


# ---------------------------------------------------------------------------
# Ingest: emit DatasetProperties + SchemaMetadata for each table
# ---------------------------------------------------------------------------


def _build_schema_fields(columns: list[dict]) -> list[SchemaFieldClass]:
    fields = []
    for col in columns:
        dh_type = _PG_TO_DATAHUB_TYPE.get(col["native_type"], "STRING")
        fields.append(
            SchemaFieldClass(
                fieldPath=col["name"],
                nativeDataType=col["native_type"],
                type={"type": {"type": dh_type}},
                nullable=col["nullable"],
            )
        )
    return fields


async def ingest_datasets() -> int:
    """Discover tables and emit metadata to DataHub. Returns count ingested."""
    datasets = await discover_tables()
    if not datasets:
        print("  No tables found in example-postgres. Run dummy-data-reset.sh first.")
        return 0

    emitter = DatahubRestEmitter(gms_server=_gms_url, token=_token)

    for urn, columns in datasets.items():
        schema = columns[0]["schema"]
        table = columns[0]["table"]

        # 1. Mark as not-deleted (undo any previous soft-delete)
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StatusClass(removed=False),
            )
        )

        # 2. DatasetProperties
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetPropertiesClass(
                    name=f"{schema}.{table}",
                    qualifiedName=f"{INSTANCE}.{schema}.{table}",
                    description=f"Imazon example table: {schema}.{table}",
                    customProperties={
                        "source": "dummy-data-ingest",
                        "schema": schema,
                        "database": INSTANCE,
                    },
                ),
            )
        )

        # 3. SchemaMetadata
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=SchemaMetadataClass(
                    schemaName=f"{schema}.{table}",
                    platform=f"urn:li:dataPlatform:{PLATFORM}",
                    version=0,
                    hash="",
                    platformSchema=OtherSchemaClass(rawSchema=""),
                    fields=_build_schema_fields(columns),
                ),
            )
        )

    print(
        f"  Ingested {len(datasets)} datasets ({sum(len(c) for c in datasets.values())} columns)."
    )
    return len(datasets)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def async_main() -> None:
    reset_only = "--reset-only" in sys.argv
    do_reset = "--reset" in sys.argv or reset_only

    if do_reset:
        print("[INFO]  Resetting DataHub datasets...")
        reset_datahub_datasets()
        if reset_only:
            print("[INFO]  Reset complete (--reset-only).")
            return

    print("[INFO]  Ingesting example-postgres tables into DataHub...")
    count = await ingest_datasets()
    print(f"[INFO]  Done. {count} datasets registered in DataHub.")


if __name__ == "__main__":
    asyncio.run(async_main())
