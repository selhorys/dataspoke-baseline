"""Spot tests — the ``datahub-api`` peripheral_health row the sync sweep writes.

DataSpoke reaches DataHub over two independent transports, each with its own
``peripheral_health`` row: ``datahub`` (the Kafka event stream, written by the event
consumer) and ``datahub-api`` (the GMS metadata API, written by the hourly sync +
mapping sweep). ``GET /admin/peripherals/datahub`` serves the first as ``health`` and
the second as ``api_health``.

**These tests assert the row, not the response shape.** A test that only checked that
the endpoint returns an ``api_health`` object would pass against a database where every
write silently fails — which is not hypothetical: before this cluster's schema was
rebuilt the ``ck_peripheral_health_name`` CHECK did not admit ``'datahub-api'``, so the
sweep completed, the upsert raised, the reporter swallowed it, and the response served a
truthful-looking ``unknown``. Every assertion below therefore reads the row back through
a session of its own.

Spot is the only layer that can: the row is a real ``peripheral_health`` upsert, the
independence requirement is about a *second database session*, and the failing-sweep
paths need a DataHub client that raises on demand — none of which the api-wired pipeline
reaches, and none of which a unit-tier fake can prove.

Spec: spec/feature/BACKEND.md §Health reporting — the two-row table, the reporters, and
    '``GET /admin/peripherals/datahub`` returns the first as ``health`` and the second as
    ``api_health``'; '**Two rows, not one.** … A single shared row would let the consumer
    and the sweep overwrite each other's verdict'.
Spec: spec/feature/BACKEND.md §Sync + mapping sweep §Health side effect — '``ok`` on
    completion, ``error`` carrying the message on failure — which is then re-raised';
    'The ``error`` branch catches broadly — any failure that escapes the sweep, not only
    ``DataHubUnavailableError``'; 'The ``error`` report is committed independently of the
    sweep's transaction.'
Spec: spec/feature/BACKEND_SCHEMA.md §peripheral_health.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import DataHubUnavailableError
from src.shared.redaction import REDACTED
from tests.integration.util import dataspoke_db

# No dummy-data constants: the sweep runs against a stubbed DataHub client, so no real
# Imazon dataset is consulted.
# spec: TESTING.md §Per-Module Dummy-Data Reset — modules with no constants are no-ops.

_PERIPHERALS_DH = "/api/v1/admin/peripherals/datahub"


class _StubDataHubForHealth:
    """Minimal DataHub stub for the sweep, optionally raising from one surface.

    ``fail_with`` is raised from ``enumerate_datasets`` — step 2 of the sweep, after the
    source enumeration has already committed, so the failure escapes ``_run_sweep`` the
    way a mid-sweep GMS fault does. Carries no ``sanitize`` attribute, mirroring a
    deployment where the reported exception never crossed ``DataHubClient``'s boundary
    scrub (the ``401``/``403`` fail-fast path).
    """

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return []

    async def enumerate_datasets(self) -> list[str]:
        if self.fail_with is not None:
            raise self.fail_with
        return []

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str | None]:
        return {u: None for u in urns}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return []

    async def get_last_ingested(self, count: int = 1000) -> dict[str, int]:
        """An estate with nothing observable, so the sub-pass books nothing here.

        Defined rather than omitted even though this module's sources list is empty: step
        4's ``lastIngested`` sub-pass re-raises ``AttributeError`` from the client out of
        the whole sweep (spec: feature/BACKEND.md §Best-Effort Operations — "a duck-typed
        test double missing the method passes green with the sub-pass never executing"), so
        a double without it would flip the very ``datahub-api`` health row this module
        asserts on the moment any test here books a source.
        """
        return {}


async def _read_health(engine: AsyncEngine, name: str) -> dict[str, Any] | None:
    """Read one ``peripheral_health`` row through a session of its own.

    A fresh session per read is the point: it is what proves the reporter's write was
    committed rather than merely pending on somebody else's transaction.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT name, status, last_error, last_ok_at, updated_at "
                "FROM dataspoke.peripheral_health WHERE name = :name"
            ),
            {"name": name},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None


async def _write_health(async_engine: AsyncEngine, row: dict[str, Any] | None, name: str) -> None:
    """Replace the ``peripheral_health`` row for *name* with *row* (or delete it)."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("DELETE FROM dataspoke.peripheral_health WHERE name = :name"),
            {"name": name},
        )
        if row is not None:
            await session.execute(
                text(
                    "INSERT INTO dataspoke.peripheral_health "
                    "(name, status, last_error, last_ok_at, updated_at) "
                    "VALUES (:name, :status, :last_error, :last_ok_at, :updated_at)"
                ),
                row,
            )
        await session.commit()


@pytest_asyncio.fixture
async def peripheral_health_baseline(async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """Snapshot both DataHub health rows, seed the ``datahub`` row, restore both verifiably.

    Yields the seeded ``datahub`` row so a test can assert the sweep left it untouched.

    Two rows, two reasons:

    - ``datahub-api`` is **cluster-wide singleton state that is a by-product of any
      sweep**, not state this module owns. The scheduled `datahub_sync` DAG writes it, and
      so does every other spot module that drives ``sync()`` in-process. The snapshot is
      therefore deliberately module-local and its ``before`` value is *ordering-dependent*
      — a stable "absent" is not something this fixture can assume or restore the estate
      to. What it guarantees is that this module hands the row back exactly as it found
      it.
    - ``datahub`` belongs to the event consumer, and the "two rows, not one" assertion
      needs it to hold a **known, non-empty** value: on a stock install no consumer is
      deployed, the row is absent, and comparing an absent row before and after proves
      nothing. So it is seeded to a distinctive state for the duration and restored after.

    **The ``datahub`` restore is verified by marker, not by byte-equality, and that
    asymmetry is deliberate.** When a consumer *is* deployed — as in this dev cluster — it
    owns that row and re-reports its latched fault on a backoff timer, so it can rewrite the
    row between the restore write below and the read-back two round trips later. A snapshot
    comparison would then fail for a write this fixture did not make. What the fixture can
    and does assert is the corruption it could actually cause: its own uniquely-marked seed
    must not be what the estate is left holding. ``datahub-api`` keeps the strict comparison
    — its only writers are sweeps, and none runs during teardown.

    spec: TESTING.md §Integration Lifecycle & Isolation — 'Snapshot → mutate → verified
        restore. … The restore is **asserted**, not assumed'; and 'Bind event assertions by
        identity, never by count … concurrent runs on the shared cluster invalidate the
        window and the assertion flakes' — the same hazard, applied to a shared row.
    spec: feature/BACKEND.md §Health reporting — '**Both reporters are opt-in, so
        ``unknown`` is the ordinary reading on a stock install**'.
    """
    api_before = await _read_health(async_engine, "datahub-api")
    kafka_before = await _read_health(async_engine, "datahub")

    stamped = datetime.now(tz=UTC)
    seed_marker = f"kafka-plane-baseline-{uuid.uuid4().hex}"
    seeded_kafka: dict[str, Any] = {
        "name": "datahub",
        "status": "error",
        # A state the event consumer itself produces, with a marker that identifies it as
        # this fixture's rather than a live consumer's.
        "last_error": f"{seed_marker} brokers are down",
        "last_ok_at": stamped,
        "updated_at": stamped,
    }
    await _write_health(async_engine, seeded_kafka, "datahub")
    try:
        yield seeded_kafka
    finally:
        await _write_health(async_engine, api_before, "datahub-api")
        await _write_health(async_engine, kafka_before, "datahub")
        api_after = await _read_health(async_engine, "datahub-api")
        kafka_after = await _read_health(async_engine, "datahub")
        assert api_after == api_before, (
            "the datahub-api health row must be restored to its pre-test value; "
            f"snapshot={api_before!r}, restored={api_after!r}. "
            "spec: TESTING.md §Integration Lifecycle & Isolation."
        )
        # Backstop: the marker really is unique to this fixture, so the absence check below
        # is not trivially satisfied by a string nothing ever wrote.
        assert seed_marker in seeded_kafka["last_error"], (
            "the seeded row must carry this fixture's marker, or the leak check is vacuous."
        )
        assert seed_marker not in ((kafka_after or {}).get("last_error") or ""), (
            "this fixture's seeded datahub row must not be left behind for later tests; "
            f"snapshot={kafka_before!r}, read back={kafka_after!r}. Byte-equality against "
            "the snapshot is deliberately not asserted here — a deployed event consumer "
            "owns this row and may rewrite it during teardown. See the fixture docstring. "
            "spec: TESTING.md §Integration Lifecycle & Isolation."
        )


@pytest.mark.asyncio
async def test_completed_sweep_writes_the_datahub_api_row_as_ok(
    async_session: AsyncSession,
    async_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    peripheral_health_baseline: dict[str, Any],
) -> None:
    """A completed sweep leaves ``datahub-api`` at ``status='ok'`` with a fresh ``last_ok_at``,
    and does not put that verdict on the ``datahub`` row.

    The row is read back through a session of its own, so a write that silently failed
    (a CHECK constraint that does not admit the name, say) fails this test instead of
    being masked by a response that serves ``unknown``. ``last_ok_at`` is asserted
    against a timestamp captured before the sweep — relative, not wall-clock.

    **Two rows, not one — and why this is not a snapshot comparison.** The ``datahub`` row
    is seeded to a known ``error`` state by the fixture so the invariant is non-vacuous: were
    the two planes sharing one row, the sweep's ``ok`` would visibly overwrite that seed and
    an operator would read the sweep's verdict as Kafka health. But the invariant is "the
    sweep writes the metadata-API plane and **not** the event-stream plane", and that is
    falsified only by the sweep's verdict *appearing* on ``datahub`` — not by some other
    writer legitimately updating it. Byte-equality against the seed (``updated_at`` included)
    asserts the second, stronger thing, and it is wrong here: this row has a live owner. When
    the event consumer is deployed it re-reports its latched fault on a backoff timer, so its
    ``updated_at`` and ``last_error`` move for reasons that have nothing to do with the sweep,
    and the comparison fails on a correct implementation. **Do not reinstate it.**
    spec: TESTING.md §Integration Lifecycle & Isolation — bind assertions on shared state by
    identity, never by a window a concurrent writer can invalidate.

    What is asserted instead is identity-bound two ways. The row must not carry the sweep's
    ``ok`` verdict (``status='ok'`` with a cleared ``last_error`` and a ``last_ok_at`` from
    this run), and its ``last_ok_at`` must differ from the one the sweep just stamped on
    ``datahub-api`` — a sweep makes one report, so that stamp can appear on exactly one row.
    Both are backstopped by the assertions above, which prove the sweep's ``ok`` did land on
    ``datahub-api``; without that, "the verdict is absent here" could just mean no sweep ran.

    The same rows are then confirmed over REST as ``api_health`` / ``health``, so the row and
    the field an operator reads are pinned together.

    spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — '``ok`` on
        completion'.
    spec: feature/BACKEND.md §Health reporting — '``datahub-api`` | Metadata API (GMS
        REST / GraphQL) | the hourly sync + mapping sweep | ``ok`` on a completed sweep';
        the endpoint 'returns the first as ``health`` and the second as ``api_health``';
        '**Two rows, not one.** … A single shared row would let the consumer and the sweep
        overwrite each other's verdict'.
    spec: TESTING.md §Assertion Discipline — 'Mutation tests verify a concrete side
        effect … the DB row'.
    """
    await dataspoke_db.reset_ingestion_sources()
    service = IngestionService(
        datahub=_StubDataHubForHealth(),  # type: ignore[arg-type]
        db=async_session,
    )
    try:
        before = datetime.now(tz=UTC)
        await service.sync()

        row = await _read_health(async_engine, "datahub-api")
        assert row is not None, (
            "the sweep must write a 'datahub-api' peripheral_health row; none exists. "
            "An absent row reads as 'unknown', indistinguishable from 'no reporter "
            "deployed'. spec: feature/BACKEND.md §Health reporting."
        )
        assert row["status"] == "ok", (
            f"a completed sweep must report status='ok'; got {row['status']!r} "
            f"(last_error={row['last_error']!r}). "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
        assert row["last_error"] is None, (
            f"'ok' clears last_error; got {row['last_error']!r}. "
            "spec: feature/BACKEND.md §Health reporting."
        )
        assert row["last_ok_at"] is not None and row["last_ok_at"] >= before, (
            f"'ok' must stamp a last_ok_at from this sweep, not an older one; got "
            f"{row['last_ok_at']!r} against a pre-sweep instant of {before!r}. "
            "spec: feature/BACKEND.md §Health reporting — the row carries last_ok_at."
        )

        # Two rows, not one: the sweep's 'ok' verdict must not appear on the Kafka plane's
        # row. Identity-bound, not a snapshot comparison — see the docstring.
        kafka_row = await _read_health(async_engine, "datahub")
        assert kafka_row is not None, (
            "the sweep must not delete the 'datahub' row; the fixture seeded one and it is "
            "gone. spec: feature/BACKEND.md §Health reporting — 'Two rows, not one.'"
        )
        # Backstop: the seed is a state the sweep's 'ok' would visibly overwrite, so the
        # check below has something to detect.
        assert peripheral_health_baseline["status"] == "error", (
            "the fixture must seed a non-'ok' datahub row, or a sweep that wrongly wrote "
            "there would be indistinguishable from one that did not."
        )
        carries_the_sweeps_verdict = (
            kafka_row["status"] == "ok"
            and kafka_row["last_error"] is None
            and kafka_row["last_ok_at"] is not None
            and kafka_row["last_ok_at"] >= before
        )
        assert not carries_the_sweeps_verdict, (
            "the sweep's 'ok' verdict must not land on the 'datahub' row — that row is the "
            f"event consumer's report of the Kafka plane. Read back {kafka_row!r} against a "
            f"pre-sweep instant of {before!r}. "
            "spec: feature/BACKEND.md §Health reporting — 'Two rows, not one.'"
        )
        assert kafka_row["last_ok_at"] != row["last_ok_at"], (
            "a sweep makes one report, so the last_ok_at it just stamped on 'datahub-api' "
            f"({row['last_ok_at']!r}) can appear on exactly one row; the 'datahub' row "
            f"carries the same value. spec: feature/BACKEND.md §Health reporting."
        )

        # Dual confirmation: the fields an operator reads render those same two rows.
        resp = await api_client.get(_PERIPHERALS_DH, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["api_health"]["status"] == "ok", (
            f"GET {_PERIPHERALS_DH} must serve the datahub-api row as api_health; got "
            f"{body['api_health']!r}. spec: feature/BACKEND.md §Health reporting."
        )
        assert body["api_health"]["last_error"] is None
        assert body["health"] != body["api_health"], (
            "'health' and 'api_health' must render two distinct rows; the endpoint served "
            f"the same reading for both ({body['health']!r}). That is the response-boundary "
            "form of 'Two rows, not one' — asserted as distinctness rather than against the "
            "seeded value, because the datahub row has a live owner that may have "
            "re-reported since. spec: feature/BACKEND.md §Health reporting."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "make_exc"),
    [
        pytest.param(
            "retry-exhausted transport fault",
            lambda sentinel: DataHubUnavailableError(f"GMS unreachable after retries {sentinel}"),
            id="datahub-unavailable",
        ),
        pytest.param(
            # A rotated or revoked PAT: DataHubClient._with_retry re-raises 401/403 raw,
            # so it never becomes DataHubUnavailableError. A narrow `except
            # DataHubUnavailableError` would leave api_health reading 'ok' through a dead
            # credential — the fault an operator most needs to see.
            "raw 401 from a revoked PAT",
            lambda sentinel: RuntimeError(
                f"401 Client Error: Unauthorized for url: http://gms/openapi/v3 {sentinel}"
            ),
            id="raw-401-not-a-dataspoke-error",
        ),
    ],
)
async def test_a_sweep_that_raises_flips_datahub_api_to_error_and_the_row_survives(
    label: str,
    make_exc,
    async_session: AsyncSession,
    async_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    peripheral_health_baseline: dict[str, Any],  # noqa: ARG001 — seeds/restores both rows
) -> None:
    """Any exception escaping the sweep flips ``datahub-api`` to ``error``, and the report
    outlives the re-raise.

    Both failure shapes are driven because the broad catch is the load-bearing part of
    the design: ``DataHubUnavailableError`` covers retry-exhausted transport faults and
    an open circuit, while an authentication failure takes the client's fail-fast path
    and escapes as a raw SDK exception. Keying the report on the DataSpoke error type
    would leave the row serving ``ok`` through a revoked credential.

    Sequencing makes each leg discriminating rather than incidental: a **successful**
    sweep first drives the row to ``ok``, so the ``error`` below is an observed
    transition and not the value the row already held.

    Survival is asserted by rolling back the sweep's own session and then reading the
    row through a fresh one: a report written inside the sweep's transaction would be
    gone.

    Non-conflation is asserted twice over, and both legs key on the same per-run sentinel.
    It must appear in the ``datahub-api`` row (identity, so the reading is bound to *this*
    sweep) and it must **not** appear on the ``datahub`` row — which is exact, because only
    this run produced that string. Were the two planes sharing one row, the sweep's verdict
    would be exactly what an operator read as Kafka health, sentinel and all.

    The ``datahub`` row is deliberately **not** compared byte-for-byte against the fixture's
    seed. When the event consumer is deployed it owns that row and re-reports its latched
    fault on a backoff timer, so its ``updated_at`` and ``last_error`` move for reasons
    unrelated to the sweep and a snapshot comparison fails on a correct implementation. **Do
    not reinstate it** — the sentinel is both stricter about what it detects and immune to
    the other writer. spec: TESTING.md §Integration Lifecycle & Isolation.

    spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — '``error``
        carrying the message on failure — which is then re-raised, so the activity
        endpoint still answers with a retryable failure'; 'The ``error`` branch catches
        broadly — any failure that escapes the sweep, not only
        ``DataHubUnavailableError``'; 'The ``error`` report is committed independently of
        the sweep's transaction.'
    spec: feature/BACKEND.md §Health reporting — '**Two rows, not one.** The planes use
        separate transports and credentials and fail independently'.
    """
    sentinel = f"spot-sweep-health-{uuid.uuid4().hex}"
    await dataspoke_db.reset_ingestion_sources()
    try:
        # Drive the row to 'ok' first, so the flip below is a transition.
        healthy = IngestionService(
            datahub=_StubDataHubForHealth(),  # type: ignore[arg-type]
            db=async_session,
        )
        await healthy.sync()
        assert (await _read_health(async_engine, "datahub-api"))["status"] == "ok", (  # type: ignore[index]
            "Backstop: the row must start this test at 'ok', or the 'error' reading below "
            "could be a row that was never written at all."
        )

        failing = IngestionService(
            datahub=_StubDataHubForHealth(fail_with=make_exc(sentinel)),  # type: ignore[arg-type]
            db=async_session,
        )
        with pytest.raises(Exception) as raised:  # noqa: B017, PT011 — the type is the point
            await failing.sync()
        assert sentinel in str(raised.value), (
            f"{label}: the sweep must re-raise the original failure, not swallow it; got "
            f"{raised.value!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )

        # Roll back the sweep's own session: a report written inside its transaction
        # would disappear here.
        await async_session.rollback()

        row = await _read_health(async_engine, "datahub-api")
        assert row is not None and row["status"] == "error", (
            f"{label}: a failure escaping the sweep must flip datahub-api to 'error'; got "
            f"{row!r}. spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
        assert row["last_error"] and sentinel in row["last_error"], (
            f"{label}: the row must carry this failure's message, so the reading is bound "
            f"to this sweep rather than to an earlier one; got {row['last_error']!r}."
        )
        assert row["last_ok_at"] is not None, (
            f"{label}: 'error' leaves the previous last_ok_at intact so a reader can see "
            "how long ago the peripheral last worked. "
            "spec: feature/BACKEND.md §Health reporting."
        )

        # Two rows, not one: this sweep's failure must not appear on the Kafka plane's row.
        # Bound by the per-run sentinel, which only this run produced — see the docstring.
        kafka_row = await _read_health(async_engine, "datahub")
        assert kafka_row is not None, (
            f"{label}: the sweep must not delete the 'datahub' row; the fixture seeded one "
            "and it is gone. spec: feature/BACKEND.md §Health reporting."
        )
        # Backstop: the sentinel provably exists in the estate (asserted on datahub-api
        # above), so its absence here is a filtered result rather than a missing string.
        assert sentinel not in (kafka_row["last_error"] or ""), (
            f"{label}: the sweep's failure message must not land on the 'datahub' row — that "
            f"row is the event consumer's report of the Kafka plane. Read back "
            f"{kafka_row!r}. spec: feature/BACKEND.md §Health reporting — "
            "'Two rows, not one.'"
        )
        assert kafka_row["last_error"] != row["last_error"], (
            f"{label}: the two rows must carry two different reports; both read "
            f"{row['last_error']!r}. spec: feature/BACKEND.md §Health reporting."
        )

        # Dual confirmation over REST, on both fields at once.
        resp = await api_client.get(_PERIPHERALS_DH, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["api_health"]["status"] == "error", (
            f"{label}: api_health must render the failed datahub-api row; got "
            f"{body['api_health']!r}. spec: feature/BACKEND.md §Health reporting."
        )
        assert sentinel in (body["api_health"]["last_error"] or "")
        assert sentinel not in (body["health"]["last_error"] or ""), (
            f"{label}: 'health' must render the Kafka plane's own report, not this sweep's "
            f"verdict; got {body['health']!r}. "
            "spec: feature/BACKEND.md §Health reporting — 'Two rows, not one.'"
        )
        assert body["health"] != body["api_health"], (
            f"{label}: 'health' and 'api_health' must render two distinct rows; the endpoint "
            f"served the same reading for both ({body['health']!r}). "
            "spec: feature/BACKEND.md §Health reporting — 'Two rows, not one.'"
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_a_credential_in_the_sweep_failure_is_redacted_in_the_row(
    async_session: AsyncSession,
    async_engine: AsyncEngine,
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    peripheral_health_baseline: dict[str, Any],  # noqa: ARG001 — snapshot/restore fixture
) -> None:
    """A credential quoted by the sweep's failure never reaches the persisted row.

    The other error tests deliberately use a non-credential correlation marker, because
    they need the sentinel to *survive* in order to bind the reading to their own sweep.
    That means neither of them would notice the redaction being removed. This one closes
    that gap end to end: the message is credential-shaped, it travels the full production
    path (``_describe_failure`` → ``report_peripheral_health``), and the row is read back
    from the DB and from the admin API.

    The plain ``marker`` is what keeps the assertion bound to this sweep — without it,
    "the secret is absent" would also be true of a row this test never wrote.

    spec: feature/BACKEND.md §Health reporting — "``last_error`` is bounded and
        credential-free. This binds every reporter writing the table … no credentials, no
        stack traces, and a length bound, so a persisted message cannot become a
        disclosure or log-forging surface."
    """
    marker = f"sweep-corr-{uuid.uuid4().hex}"
    secret = f"pat-{uuid.uuid4().hex}"
    await dataspoke_db.reset_ingestion_sources()
    try:
        failing = IngestionService(
            datahub=_StubDataHubForHealth(
                fail_with=RuntimeError(
                    f"401 Unauthorized for sweep {marker}: access_token={secret} rejected"
                )
            ),  # type: ignore[arg-type]
            db=async_session,
        )
        with pytest.raises(Exception):  # noqa: B017 — the message is the point, not the type
            await failing.sync()
        await async_session.rollback()

        row = await _read_health(async_engine, "datahub-api")
        assert row is not None and row["last_error"], (
            "Backstop: the failure must have produced a last_error, or the absence "
            f"assertion below proves nothing; got {row!r}."
        )
        assert marker in row["last_error"], (
            "Backstop: the row must be this sweep's report, bound by its correlation "
            f"marker; got {row['last_error']!r}."
        )
        assert secret not in row["last_error"], (
            f"the credential must not be persisted; got {row['last_error']!r}. "
            "spec: feature/BACKEND.md §Health reporting — last_error is credential-free."
        )
        assert REDACTED in row["last_error"], (
            f"the credential must be replaced by the redaction marker, so an operator can "
            f"see something was withheld; got {row['last_error']!r}. The marker is "
            f"imported from src/shared/redaction.py rather than spelled out: no spec "
            f"names a marker string, so the property is that *a* marker is present."
        )
        assert "access_token" in row["last_error"], (
            f"the credential's *name* survives — that is what tells the operator which "
            f"credential was rejected; got {row['last_error']!r}."
        )

        # The admin API serves the redacted value, not a differently-rendered original.
        resp = await api_client.get(_PERIPHERALS_DH, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        served = resp.json()["api_health"]["last_error"] or ""
        assert marker in served and secret not in served and REDACTED in served, (
            f"the admin response must serve the redacted message; got {served!r}. "
            "spec: feature/BACKEND.md §Health reporting."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_the_error_report_survives_a_sweep_session_left_in_a_failed_transaction(
    async_session: AsyncSession,
    async_engine: AsyncEngine,
    peripheral_health_baseline: dict[str, Any],  # noqa: ARG001 — snapshot/restore fixture
) -> None:
    """The report lands even when the sweep's own session can no longer execute SQL.

    This is the case that makes the reporter's session of its own structural rather than
    stylistic. The failure injected here is a *database* error raised mid-sweep, which
    leaves the sweep's session in an aborted transaction: any further statement on it
    raises. A reporter sharing that session would fail, and because reporting is
    best-effort the failure would be swallowed — leaving ``api_health`` pinned at the
    last ``ok`` exactly when it is wrong.

    The sweep's session and the reporter's are two sessions on the **same engine**, and
    that is what makes this discriminating: sharing an engine is not sharing a
    transaction, so a report on a fresh session commits while the sweep's connection is
    still poisoned. **Which** engine the reporter picks is observable here too, on a
    host-driven run: a module-level factory is bound at import time to the app-runtime
    ``DATASPOKE_POSTGRES_*`` values, which a developer machine reaching the cluster
    through a forwarded port does not have, so a reporter that regressed to one would
    write no row at all and this assertion would fail. Do not reinstate a patch of that
    factory here — it is what makes this test discriminate. Engine identity is
    additionally pinned at the unit tier
    (``tests/unit/backend/ingestion/test_service.py::TestSyncReportsApiHealth``), which is
    the tier that can still discriminate it in-cluster, where both addresses resolve to
    the same database.

    A successful sweep drives the row to ``ok`` first, so ``error`` here is an observed
    transition.

    spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — 'The ``error``
        report is committed independently of the sweep's transaction. Written inside it,
        the re-raise rolls the report back and leaves ``api_health`` pinned to the last
        ``ok`` exactly when it is wrong.'
    spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect — 'The accepted
        trade-off: a non-GMS failure escaping the sweep (a database error, say) also
        flips the row.'
    """

    class _PoisonsTheSweepSession(_StubDataHubForHealth):
        """Runs an invalid statement on the sweep's session, then lets the error escape."""

        def __init__(self, session: AsyncSession) -> None:
            super().__init__()
            self._session = session

        async def enumerate_datasets(self) -> list[str]:
            await self._session.execute(text("SELECT 1 / 0"))
            raise AssertionError("unreachable: the division must raise")

    await dataspoke_db.reset_ingestion_sources()
    try:
        healthy = IngestionService(
            datahub=_StubDataHubForHealth(),  # type: ignore[arg-type]
            db=async_session,
        )
        await healthy.sync()
        assert (await _read_health(async_engine, "datahub-api"))["status"] == "ok", (  # type: ignore[index]
            "Backstop: the row must start at 'ok', or the 'error' reading below could be "
            "a row that was never written."
        )

        poisoned = IngestionService(
            datahub=_PoisonsTheSweepSession(async_session),  # type: ignore[arg-type]
            db=async_session,
        )
        with pytest.raises(Exception):  # noqa: B017 — any DB error; the driver names it
            await poisoned.sync()

        await async_session.rollback()
        row = await _read_health(async_engine, "datahub-api")
        assert row is not None and row["status"] == "error", (
            "the error report must be written on a session of its own, so it survives a "
            f"sweep session left in an aborted transaction; got {row!r}. "
            "spec: feature/BACKEND.md §Sync + mapping sweep §Health side effect."
        )
    finally:
        await async_session.rollback()
        await dataspoke_db.reset_ingestion_sources()
