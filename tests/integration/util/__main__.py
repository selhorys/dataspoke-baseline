"""CLI entry point for manual dummy-data management.

Usage:
    uv run python -m tests.integration.util --reset-all     # Full reset: PG + Kafka + DataHub
    uv run python -m tests.integration.util --pg             # PostgreSQL only
    uv run python -m tests.integration.util --kafka          # Kafka only
    uv run python -m tests.integration.util --datahub        # DataHub only
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    from tests.integration.util import datahub, kafka, postgres

    args = set(sys.argv[1:])

    if not args or "--reset-all" in args:
        print("[INFO] Resetting all dummy data (PostgreSQL + Kafka + DataHub)...")
        asyncio.run(postgres.reset_all())
        kafka.reset_all()
        asyncio.run(datahub.reset_and_ingest())
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

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
