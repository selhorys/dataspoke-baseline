"""CLI entry point for manual dummy-data management.

Usage:
    uv run python -m tests.integration.util --reset-all     # Full reset: PG + Kafka + DataHub + Kestra
    uv run python -m tests.integration.util --pg             # PostgreSQL only
    uv run python -m tests.integration.util --kafka          # Kafka only
    uv run python -m tests.integration.util --datahub        # DataHub only
    uv run python -m tests.integration.util --kestra         # Kestra only (delete flows + kill executions)
    uv run python -m tests.integration.util --qdrant         # Qdrant only (delete all collections)
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    from tests.integration.util import datahub, kafka, postgres

    args = set(sys.argv[1:])

    if not args or "--reset-all" in args:
        print("[INFO] Resetting all dummy data (PostgreSQL + Kafka + DataHub + Kestra)...")
        asyncio.run(postgres.reset_all())
        kafka.reset_all()
        asyncio.run(datahub.reset_and_ingest())
        from tests.integration.util import kestra, qdrant
        deleted = asyncio.run(kestra.reset_all())
        print(f"  Deleted {deleted} Kestra flows (startup flows re-registered).")
        qdrant_deleted = asyncio.run(qdrant.reset_all())
        print(f"  Deleted {qdrant_deleted} Qdrant collections.")
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

    if "--kestra" in args:
        print("[INFO] Resetting Kestra (delete flows + kill executions + re-register)...")
        from tests.integration.util import kestra
        deleted = asyncio.run(kestra.reset_all())
        print(f"  Deleted {deleted} Kestra flows (startup flows re-registered).")

    if "--qdrant" in args:
        print("[INFO] Resetting Qdrant (delete all collections)...")
        from tests.integration.util import qdrant
        deleted = asyncio.run(qdrant.reset_all())
        print(f"  Deleted {deleted} Qdrant collections.")

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
