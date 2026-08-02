"""CLI entry point for manual dummy-data management.

Usage:
    uv run python -m tests.integration.util --help
        print this usage text and exit

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import URL

_UC4_STATE_FILE = "/tmp/dataspoke_uc4_state.json"

# Every flag main() dispatches on. An argument outside this set is rejected rather
# than ignored, so a typo cannot pass for a completed run. Keep in sync with the
# module docstring, which is the usage text --help prints.
_RECOGNIZED_FLAGS = frozenset(
    {
        "--reset-all",
        "--reset-seed",
        "--pg",
        "--kafka",
        "--datahub",
        "--datahub-seed",
        "--reset-dataspoke-db",
        "--datahub-sync",
        "--emit-passive-kafka-ops",
        "--langfuse",
        "--uc4-seed",
        "--uc4-restore",
    }
)


def _dataspoke_db_url() -> URL:
    """Build the DataSpoke operational-DB URL from the ``DATASPOKE_TEST_POSTGRES_*`` block.

    The credentials are carried as ``URL`` fields rather than interpolated into a DSN
    string, matching ``src/shared/db/session.py::_build_url``. An ``@`` in the password
    truncates an interpolated DSN — the tail becomes the host — and a ``%`` decodes into a
    different password entirely; held as fields there is no round-trip to get wrong.
    ``str()`` of the result masks the password, so it cannot reach a traceback.

    spec: feature/BACKEND.md §Shared Services (PostgreSQL row) — 'Credentials are carried
        as ``sqlalchemy.URL`` fields rather than interpolated into a DSN string, so
        ``DATASPOKE_POSTGRES_USER`` / ``DATASPOKE_POSTGRES_PASSWORD`` reach the driver
        verbatim from this connection layer whatever characters they contain, and the
        URL's string form masks the password.' Same invariant, one layer over: this helper
        is the reset utility's connection layer and reads the ``DATASPOKE_TEST_*`` block.
    spec: TESTING.md §Integration Lifecycle & Isolation — 'Reset helpers … read all
        credentials from the environment (the ``DATASPOKE_TEST_*`` block in
        ``helm-charts/.env.dev``); no credential is hardcoded in a helper.'

    Covered by ``tests/unit/integration_util/test_main_db_url.py``.
    """
    from tests.integration.util.db_url import build_postgres_url

    return build_postgres_url(
        host=os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost"),
        port=os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201"),
        user=os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke"),
        password=os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", ""),
        db=os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke"),
    )


def main() -> None:
    raw_args = sys.argv[1:]

    # Resolved before any util import so usage and rejection stay available
    # without a reachable cluster.
    if "--help" in raw_args or "-h" in raw_args:
        print(__doc__)
        sys.exit(0)

    unknown = [arg for arg in raw_args if arg not in _RECOGNIZED_FLAGS]
    if unknown:
        print(f"[ERROR] Unrecognized argument(s): {' '.join(unknown)}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    from tests.integration.util import datahub, dataspoke_db, kafka, langfuse, postgres

    args = set(raw_args)
    # Names the actions that actually ran, so the closing line distinguishes a
    # real run from one that did nothing.
    performed: list[str] = []

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
        performed.append("reset-all")
    elif "--reset-seed" in args:
        _reset_seed()
        performed.append("reset-seed")
    elif not args:
        _reset_all()
        performed.append("reset-all")

    if "--uc4-seed" in args:
        asyncio.run(_uc4_seed())
        performed.append("uc4-seed")

    if "--uc4-restore" in args:
        asyncio.run(_uc4_restore())
        performed.append("uc4-restore")

    if "--pg" in args:
        print("[INFO] Resetting PostgreSQL dummy data (with seed SQL)...")
        asyncio.run(postgres.reset_all())
        performed.append("pg")

    if "--kafka" in args:
        print("[INFO] Resetting Kafka dummy data (with seed messages)...")
        kafka.reset_all()
        performed.append("kafka")

    if "--datahub" in args:
        print("[INFO] Hard-deleting DataHub datasets (no ingest)...")
        datahub.reset_only()
        performed.append("datahub")

    if "--datahub-seed" in args:
        print("[INFO] Hard-deleting DataHub datasets then re-ingesting...")
        asyncio.run(datahub.seed())
        performed.append("datahub-seed")

    if "--reset-dataspoke-db" in args:
        print("[INFO] Resetting DataSpoke operational DB...")
        asyncio.run(dataspoke_db.reset_all())
        performed.append("reset-dataspoke-db")

    if "--datahub-sync" in args:
        print("[INFO] Running ingestion datahub-sync (reconcile dataset_registry)...")
        asyncio.run(_datahub_sync())
        performed.append("datahub-sync")

    if "--emit-passive-kafka-ops" in args:
        print("[INFO] Emitting one fresh Operation on the orders Kafka topic...")
        emitted_ms = asyncio.run(datahub.emit_fresh_kafka_operation())
        # Parseable marker line so callers (E2E Step 5) can grep the emit timestamp and
        # match the resulting fresh passive_observation event.
        print(f"EMITTED_OCCURRED_AT_MS={emitted_ms}")
        performed.append("emit-passive-kafka-ops")

    if "--langfuse" in args:
        print("[INFO] Clearing Langfuse traces in the dataspoke project...")
        asyncio.run(langfuse.reset_project())
        performed.append("langfuse")

    print(f"[INFO] Done ({', '.join(performed)}).")


async def _uc4_seed() -> None:
    """Seed UC4 LLM context and write state to /tmp/dataspoke_uc4_state.json."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tests.integration.util.datahub import _gms_url, get_datahub_token
    from tests.integration.util.metagen import seed_uc4_context

    dh_token = get_datahub_token()

    engine = create_async_engine(_dataspoke_db_url())
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

    engine = create_async_engine(_dataspoke_db_url())
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

    engine = create_async_engine(_dataspoke_db_url())
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    datahub = DataHubClient(_gms_url, dh_token)
    try:
        async with async_session_factory() as session:
            service = IngestionService(datahub=datahub, db=session)
            summary = await service.sync()
    finally:
        await engine.dispose()

    # sources_zero_coverage / sources_pattern_degraded are the sweep's two defect
    # signals (spec/feature/BACKEND.md §Sync + mapping sweep §Sweep summary): every
    # other counter can read as a healthy sweep while a source maps nothing or keeps
    # an unreconciled mapping set, so an operator running a manual sweep sees them here.
    print(
        "datahub-sync done: "
        f"registry_inserted={summary['registry_inserted']} "
        f"registry_marked_true={summary['registry_marked_true']} "
        f"registry_marked_false={summary['registry_marked_false']} "
        f"sources_synced={summary['sources_synced']} "
        f"datasets_mapped={summary['datasets_mapped']} "
        f"sources_zero_coverage={summary['sources_zero_coverage']} "
        f"sources_pattern_degraded={summary['sources_pattern_degraded']}"
    )


if __name__ == "__main__":
    main()
