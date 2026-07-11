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

    uv run python -m tests.integration.util --datahub-sync
        Run the ingestion datahub-sync only (reconcile dataset_registry +
        ingestion sources against the current DataHub URN set). This is the same
        pipeline the hourly datahub-sync-hourly DAG runs; --reset-seed already
        calls it at the end, so use this standalone only to re-reconcile without
        a full reset.

    uv run python -m tests.integration.util --emit-passive-kafka-ops
        Emit one fresh Operation on the orders Kafka topic
        (imazon.orders.events) — the passive-observation signal for UC1-03.
        Prints EMITTED_OCCURRED_AT_MS=<int> so callers can match the resulting
        passive_observation event.

    uv run python -m tests.integration.util --langfuse
        Langfuse dataspoke project only (delete all traces)

    uv run python -m tests.integration.util --uc4-seed
        Seed UC4 LLM context (fulfillment doc + ontogen nodes + DataHub masking).
        Writes state to /tmp/dataspoke_uc4_state.json for use with --uc4-restore.
        Requires the customers/orders datasets already in DataHub; raises if
        absent. Combine with a reset to seed-then-mask in one call:
            uv run python -m tests.integration.util --reset-seed --uc4-seed
        (reset always runs first, then UC4 staging on top of it).

    uv run python -m tests.integration.util --uc4-restore
        Restore DataHub aspects and delete UC4 seed state created by --uc4-seed.
        Reads /tmp/dataspoke_uc4_state.json; idempotent if file is absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_UC4_STATE_FILE = "/tmp/dataspoke_uc4_state.json"


def main() -> None:
    from tests.integration.util import datahub, dataspoke_db, kafka, langfuse, postgres

    args = set(sys.argv[1:])

    if "--reset-all" in args and "--reset-seed" in args:
        print(
            "[ERROR] --reset-all and --reset-seed are mutually exclusive. Pick one.",
            file=sys.stderr,
        )
        sys.exit(1)

    if "--uc4-seed" in args and "--uc4-restore" in args:
        print(
            "[ERROR] --uc4-seed and --uc4-restore are mutually exclusive. Pick one.",
            file=sys.stderr,
        )
        sys.exit(1)

    async def _reset_all_async() -> None:
        # Empty baseline has no ingest step, so every leg is independent → run
        # them concurrently. Sync legs (kafka, datahub reset_only) go off-thread.
        await asyncio.gather(
            postgres.reset_all_empty(),
            asyncio.to_thread(kafka.reset_all_empty),
            asyncio.to_thread(datahub.reset_only),
            dataspoke_db.reset_all(),
            langfuse.reset_project(),
        )

    def _reset_all() -> None:
        print("[INFO] Resetting to empty state (PG + Kafka + DataHub + DataSpoke DB + Langfuse)...")
        asyncio.run(_reset_all_async())

    async def _reset_seed_async() -> None:
        # Phase 1 — mutually independent source/operational resets run concurrently.
        # Sync legs (kafka) go off-thread. datahub.seed() is NOT here: its PG-dataset
        # ingest reads the freshly-reset example-postgres, so it must follow Phase 1.
        await asyncio.gather(
            postgres.reset_all(),
            asyncio.to_thread(kafka.reset_all),
            dataspoke_db.reset_all(),
            langfuse.reset_project(),
        )
        # Phase 2 — DataHub reset+ingest, reading the Phase-1 PG rows.
        await datahub.seed()
        # Phase 3 — dataspoke_db.reset_all() (Phase 1) TRUNCATEs dataset_registry;
        # nothing else repopulates it. Run the real ingestion datahub-sync so the
        # registry mirrors the freshly-ingested DataHub estate — exactly what the
        # hourly datahub-sync-hourly DAG does in a deployment. It reads DataHub
        # (needs Phase 2) and writes the registry (needs Phase 1), so it runs last.
        # Without it, features that read the registry (UC4 metagen `uncovered`,
        # UC1 ingestion `unmanaged`) see an empty precondition under the seeded
        # baseline.
        await _datahub_sync()

    def _reset_seed() -> None:
        print("[INFO] Resetting and seeding (PG + Kafka + DataHub + DataSpoke DB + Langfuse)...")
        asyncio.run(_reset_seed_async())

    # Baseline reset/seed runs BEFORE UC4 staging. --uc4-seed masks the
    # customers/orders datasets that --reset-seed ingests into DataHub, so the
    # two compose as `--reset-seed --uc4-seed` and must run in that order. They
    # are not collapsed into a single fast-path: reset must finish first.
    if "--reset-all" in args:
        _reset_all()
    elif "--reset-seed" in args:
        _reset_seed()
    elif not args:
        _reset_all()

    if "--uc4-seed" in args:
        asyncio.run(_uc4_seed())

    if "--uc4-restore" in args:
        asyncio.run(_uc4_restore())

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

    if "--datahub-sync" in args:
        print("[INFO] Running ingestion datahub-sync (reconcile dataset_registry)...")
        asyncio.run(_datahub_sync())

    if "--emit-passive-kafka-ops" in args:
        print("[INFO] Emitting one fresh Operation on the orders Kafka topic...")
        emitted_ms = asyncio.run(datahub.emit_fresh_kafka_operation())
        # Parseable marker line so callers (E2E Step 5) can grep the emit timestamp and
        # match the resulting fresh passive_observation event.
        print(f"EMITTED_OCCURRED_AT_MS={emitted_ms}")

    if "--langfuse" in args:
        print("[INFO] Clearing Langfuse traces in the dataspoke project...")
        asyncio.run(langfuse.reset_project())

    print("[INFO] Done.")


async def _uc4_seed() -> None:
    """Seed UC4 LLM context and write state to /tmp/dataspoke_uc4_state.json."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tests.integration.util.datahub import _gms_url, get_datahub_token
    from tests.integration.util.metagen import seed_uc4_context

    dh_token = get_datahub_token()

    ds_host = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
    ds_port = os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")
    ds_user = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
    ds_password = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
    ds_db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")

    engine = create_async_engine(
        f"postgresql+asyncpg://{ds_user}:{ds_password}@{ds_host}:{ds_port}/{ds_db}"
    )
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_factory() as session:
        state = await seed_uc4_context(session, dh_token=dh_token, gms_url=_gms_url)

    await engine.dispose()

    with open(_UC4_STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)

    print(f"Seeded UC4 context. State file: {_UC4_STATE_FILE}")


async def _uc4_restore() -> None:
    """Restore UC4 context from /tmp/dataspoke_uc4_state.json."""
    if not os.path.exists(_UC4_STATE_FILE):
        print(f"State file {_UC4_STATE_FILE} not found — nothing to restore.")
        return

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tests.integration.util.datahub import _gms_url, get_datahub_token
    from tests.integration.util.metagen import restore_uc4_context

    with open(_UC4_STATE_FILE) as fh:
        state = json.load(fh)

    dh_token = get_datahub_token()

    ds_host = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
    ds_port = os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")
    ds_user = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
    ds_password = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
    ds_db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")

    engine = create_async_engine(
        f"postgresql+asyncpg://{ds_user}:{ds_password}@{ds_host}:{ds_port}/{ds_db}"
    )
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_factory() as session:
        await restore_uc4_context(session, state, dh_token=dh_token, gms_url=_gms_url)

    await engine.dispose()

    os.remove(_UC4_STATE_FILE)
    print("Restored UC4 context (or partial). State file deleted.")


async def _datahub_sync() -> None:
    """Run the real ingestion datahub-sync against the test DB + DataHub.

    Mirrors the production path: builds IngestionService exactly as
    /internal/activities/ingestion/sync does (DataHubClient(gms_url, token) +
    an AsyncSession), then calls IngestionService.sync(). That reconcile is the
    sole writer of dataset_registry, so running it after datahub.seed() +
    dataspoke_db.reset_all() leaves the registry mirroring the freshly-ingested
    DataHub URN set (datahub_registered=True for every ingested URN).

    The session/token are constructed the same way as _uc4_seed so the util has
    a single, consistent wiring for "talk to the test DB and DataHub".
    Idempotent: sync() upserts by URN, so repeated runs reconcile to the same
    state.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.backend.ingestion.service import IngestionService
    from src.shared.datahub.client import DataHubClient
    from tests.integration.util.datahub import _gms_url, get_datahub_token

    dh_token = get_datahub_token()

    ds_host = os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost")
    ds_port = os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")
    ds_user = os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke")
    ds_password = os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", "")
    ds_db = os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke")

    engine = create_async_engine(
        f"postgresql+asyncpg://{ds_user}:{ds_password}@{ds_host}:{ds_port}/{ds_db}"
    )
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    datahub = DataHubClient(_gms_url, dh_token)
    try:
        async with async_session_factory() as session:
            service = IngestionService(datahub=datahub, db=session)
            summary = await service.sync()
    finally:
        await engine.dispose()

    print(
        "datahub-sync done: "
        f"registry_inserted={summary['registry_inserted']} "
        f"registry_marked_true={summary['registry_marked_true']} "
        f"registry_marked_false={summary['registry_marked_false']} "
        f"sources_synced={summary['sources_synced']} "
        f"datasets_mapped={summary['datasets_mapped']}"
    )


if __name__ == "__main__":
    main()
