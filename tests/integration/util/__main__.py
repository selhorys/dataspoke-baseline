"""CLI entry point for manual dummy-data management.

Usage:
    uv run python -m tests.integration.util --reset-all          # PG + Kafka + DataHub + DataSpoke operational DB
    uv run python -m tests.integration.util --pg                 # PostgreSQL (source) only
    uv run python -m tests.integration.util --kafka              # Kafka only
    uv run python -m tests.integration.util --datahub            # DataHub only
    uv run python -m tests.integration.util --reset-dataspoke-db # DataSpoke operational DB only
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    from tests.integration.util import datahub, dataspoke_db, kafka, postgres

    args = set(sys.argv[1:])

    if not args or "--reset-all" in args:
        print("[INFO] Resetting all dummy data (PostgreSQL + Kafka + DataHub + DataSpoke operational DB)...")
        asyncio.run(postgres.reset_all())
        kafka.reset_all()
        asyncio.run(datahub.reset_and_ingest())
        asyncio.run(dataspoke_db.reset_all())
        print("[INFO] Done.")
        return

    if "--pg" in args:
        print("[INFO] Resetting PostgreSQL dummy data...")
        asyncio.run(postgres.reset_all())

    if "--kafka" in args:
        print("[INFO] Resetting Kafka dummy data...")
        kafka.reset_all()

    if "--datahub" in args:
        print("[INFO] Resetting DataHub datasets...")
        asyncio.run(datahub.reset_and_ingest())

    if "--reset-dataspoke-db" in args:
        print("[INFO] Resetting DataSpoke operational DB...")
        asyncio.run(dataspoke_db.reset_all())

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
