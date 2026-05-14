"""CLI entry point for manual dummy-data management.

Usage:
    uv run python -m tests.integration.util
        same as --reset-all

    uv run python -m tests.integration.util --reset-all
        empty baseline: PG + Kafka + DataHub + DataSpoke operational DB, no seed

    uv run python -m tests.integration.util --reset-seed
        seeded baseline: PG + Kafka + DataHub + DataSpoke DB with Imazon data

    uv run python -m tests.integration.util --pg
        PostgreSQL (source) only, with seed SQL applied

    uv run python -m tests.integration.util --kafka
        Kafka only, with seed messages produced

    uv run python -m tests.integration.util --datahub
        DataHub hard-delete only (no ingest)

    uv run python -m tests.integration.util --datahub-seed
        DataHub hard-delete + ingest

    uv run python -m tests.integration.util --reset-dataspoke-db
        DataSpoke operational DB only

    uv run python -m tests.integration.util --langfuse
        Langfuse dataspoke project only (delete all traces)
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    from tests.integration.util import datahub, dataspoke_db, kafka, langfuse, postgres

    args = set(sys.argv[1:])

    if "--reset-all" in args and "--reset-seed" in args:
        print(
            "[ERROR] --reset-all and --reset-seed are mutually exclusive. Pick one.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args or "--reset-all" in args:
        print("[INFO] Resetting to empty state (PG + Kafka + DataHub + DataSpoke DB + Langfuse)...")
        asyncio.run(postgres.reset_all_empty())
        kafka.reset_all_empty()
        datahub.reset_only()
        asyncio.run(dataspoke_db.reset_all())
        asyncio.run(langfuse.reset_project())
        print("[INFO] Done.")
        return

    if "--reset-seed" in args:
        print("[INFO] Resetting and seeding (PG + Kafka + DataHub + DataSpoke DB + Langfuse)...")
        asyncio.run(postgres.reset_all())
        kafka.reset_all()
        asyncio.run(datahub.seed())
        asyncio.run(dataspoke_db.reset_all())
        asyncio.run(langfuse.reset_project())
        print("[INFO] Done.")
        return

    if "--pg" in args:
        print("[INFO] Resetting PostgreSQL dummy data (with seed SQL)...")
        asyncio.run(postgres.reset_all())

    if "--kafka" in args:
        print("[INFO] Resetting Kafka dummy data (with seed messages)...")
        kafka.reset_all()

    if "--datahub" in args:
        print("[INFO] Hard-deleting DataHub datasets (no ingest)...")
        datahub.reset_only()

    if "--datahub-seed" in args:
        print("[INFO] Hard-deleting DataHub datasets then re-ingesting...")
        asyncio.run(datahub.seed())

    if "--reset-dataspoke-db" in args:
        print("[INFO] Resetting DataSpoke operational DB...")
        asyncio.run(dataspoke_db.reset_all())

    if "--langfuse" in args:
        print("[INFO] Clearing Langfuse traces in the dataspoke project...")
        asyncio.run(langfuse.reset_project())

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
