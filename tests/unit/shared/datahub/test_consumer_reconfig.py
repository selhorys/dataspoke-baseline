"""Unit tests for the DataHub event consumer's DB-plane connection handling.

The consumer reads its whole Kafka connection — brokers plus the security tuple —
from ``peripheral_config`` and re-reads it every few poll iterations. Any change to
that tuple ends the inner loop so the outer loop can close the client and rebuild it.

Concerns covered here:

1. ``read_kafka_connection()`` — maps the DataHub peripheral DTO onto a
   ``KafkaConnection``; returns ``None`` when unconfigured; bypasses the process
   cache before reading.
2. Change detection — the WHOLE tuple is compared, not just the brokers. Each of the
   six fields independently triggers a rebuild, including the password-version counter
   that stands in for a Secret-only rotation.
3. ``_run_inner_loop()`` — returns on a changed tuple, keeps polling on an unchanged
   one.
4. ``run_consumer()`` outer loop — closes the old client before constructing the new one;
   parks rather than exits when the peripheral is unconfigured; and survives a
   ``peripheral_config`` read that fails outright.

No Kafka broker, database, or Kubernetes API is contacted.

Spec traceability:
- spec/feature/BACKEND.md §Kafka connection — "The consumer reads its whole connection
  from ``peripheral_config.datahub`` — brokers plus the security tuple … and re-reads
  it every few seconds while polling. A change to any element ends the inner poll loop,
  closes the client, and rebuilds it. An unconfigured peripheral parks the process in a
  retry sleep rather than crash-looping, recording no fault".
- spec/feature/BACKEND.md §Kafka connection — "a ``peripheral_config`` read that fails
  outright — the database unreachable, or its schema not yet migrated — keeps the
  process alive on the same retry sleep and reports the fault on the ``datahub``
  ``peripheral_health`` row on a best-effort basis."
- spec/feature/BACKEND.md §Kafka connection — "``kafka_sasl_password_version`` exists
  because a rotated password is invisible in the DB row … which turns a rotation into
  an ordinary DB-plane change the poll loop already detects."
- spec/API.md §DataHub Kafka security — the stored tuple's field set.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.admin.peripheral_service import DatahubConfigDTO
from src.shared.datahub.consumer import KafkaConnection, read_kafka_connection

# ── Helpers ──────────────────────────────────────────────────────────────────


def _session_ctx(db: object) -> AsyncMock:
    """An async context manager yielding *db*, standing in for ``SessionLocal()``."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _patch_config_read(dto: object | None, invalidate=None):
    """Patch the lazily imported peripheral-config surface used by read_kafka_connection."""
    return (
        patch("src.shared.db.session.SessionLocal", return_value=_session_ctx(AsyncMock())),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            AsyncMock(return_value=dto),
        ),
        patch(
            "src.backend.admin.peripheral_service.invalidate_peripheral_config_cache",
            invalidate if invalidate is not None else MagicMock(),
        ),
    )


_SCRAM_DTO = DatahubConfigDTO(
    gms_url="http://gms:8080",
    kafka_brokers="kafka-host:9092",
    kafka_security_protocol="SASL_SSL",
    kafka_sasl_mechanism="SCRAM-SHA-512",
    kafka_sasl_username="dataspoke",
    kafka_aws_region="",
    kafka_sasl_password_version=3,
)


# ── 1. read_kafka_connection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_kafka_connection_maps_the_whole_security_tuple() -> None:
    """Every stored Kafka field lands on the returned ``KafkaConnection``.

    The consumer's connection is the whole tuple, not just the broker list.

    spec: feature/BACKEND.md §Kafka connection — "The consumer reads its whole
    connection from ``peripheral_config.datahub`` — brokers plus the security tuple";
    spec/API.md §DataHub Kafka security — the field set.
    """
    session, get_config, invalidate = _patch_config_read(_SCRAM_DTO)
    with session, get_config, invalidate:
        conn = await read_kafka_connection()

    assert conn == KafkaConnection(
        brokers="kafka-host:9092",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username="dataspoke",
        aws_region="",
        sasl_password_version=3,
    )


@pytest.mark.asyncio
async def test_read_kafka_connection_defaults_an_absent_protocol_to_plaintext() -> None:
    """A row with no ``kafka_security_protocol`` resolves to an unsecured connection.

    This is what keeps a DataHub row written before the Kafka tuple existed working.

    spec: API.md §DataHub Kafka security — ``PLAINTEXT`` (default); "All of it is
    optional".
    """
    dto = DatahubConfigDTO(
        gms_url="http://gms:8080", kafka_brokers="kafka:9092", kafka_security_protocol=""
    )
    session, get_config, invalidate = _patch_config_read(dto)
    with session, get_config, invalidate:
        conn = await read_kafka_connection()

    assert conn is not None
    assert conn.security_protocol == "PLAINTEXT"
    assert conn.sasl_mechanism == ""


@pytest.mark.asyncio
async def test_read_kafka_connection_returns_none_when_peripheral_row_absent() -> None:
    """No DataHub peripheral row → ``None``, so the outer loop sleeps and retries.

    spec: feature/BACKEND.md §Kafka connection — "an unconfigured peripheral parks the
    process in a retry sleep rather than crash-looping".
    """
    session, get_config, invalidate = _patch_config_read(None)
    with session, get_config, invalidate:
        assert await read_kafka_connection() is None


@pytest.mark.asyncio
async def test_read_kafka_connection_returns_none_when_brokers_empty() -> None:
    """A row with no broker address is as unusable as no row at all.

    spec: feature/BACKEND.md §Kafka connection — the connection is read from the
    peripheral row; without ``bootstrap.servers`` there is nothing to dial.
    """
    dto = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="")
    session, get_config, invalidate = _patch_config_read(dto)
    with session, get_config, invalidate:
        assert await read_kafka_connection() is None


@pytest.mark.asyncio
async def test_read_kafka_connection_invalidates_the_cache_before_reading() -> None:
    """The process config cache is bypassed so a change is seen within the check interval.

    The 30-second peripheral-config cache would otherwise mask a change the 5-second
    reconfig check is supposed to catch.

    spec: feature/BACKEND.md §Kafka connection — the consumer "re-reads it every few
    seconds while polling"; src/shared/datahub/consumer.py read_kafka_connection.
    """
    call_order: list[str] = []

    def _invalidate(name: str) -> None:
        call_order.append(f"invalidate:{name}")

    async def _get_config(db, name):
        call_order.append("read")
        return _SCRAM_DTO

    with (
        patch("src.shared.db.session.SessionLocal", return_value=_session_ctx(AsyncMock())),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            side_effect=_get_config,
        ),
        patch(
            "src.backend.admin.peripheral_service.invalidate_peripheral_config_cache",
            side_effect=_invalidate,
        ),
    ):
        await read_kafka_connection()

    assert call_order == ["invalidate:datahub", "read"], (
        f"the cache must be evicted for 'datahub' before the read; got {call_order}"
    )


# ── 2. Change detection spans the whole tuple ────────────────────────────────


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("brokers", "kafka-new:9092"),
        ("security_protocol", "SASL_PLAINTEXT"),
        ("sasl_mechanism", "SCRAM-SHA-256"),
        ("sasl_username", "rotated-user"),
        ("aws_region", "us-east-1"),
        ("sasl_password_version", 4),
    ],
)
def test_every_tuple_field_participates_in_change_detection(field: str, new_value) -> None:
    """A change to ANY element of the connection makes the tuple unequal.

    Equality of ``KafkaConnection`` is the poll loop's rebuild predicate, so a field
    excluded from it would be a setting the consumer silently never adopts. The
    password-version counter is included precisely because a Secret-only rotation is
    otherwise invisible in the DB row.

    spec: feature/BACKEND.md §Kafka connection — "A change to any element ends the inner
    poll loop, closes the client, and rebuilds it"; "``kafka_sasl_password_version``
    exists because a rotated password is invisible in the DB row … which turns a
    rotation into an ordinary DB-plane change the poll loop already detects".
    """
    base = KafkaConnection(
        brokers="kafka:9092",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username="dataspoke",
        aws_region="",
        sasl_password_version=3,
    )
    changed = dataclasses.replace(base, **{field: new_value})

    assert changed != base, f"a change to {field!r} must be detected as a connection change"
    # Backstop: an identical copy compares equal, so the inequality above is the field
    # and not an identity comparison.
    assert dataclasses.replace(base) == base


# ── 3. _run_inner_loop ───────────────────────────────────────────────────────


def _quiet_health() -> MagicMock:
    """A HealthReporter stand-in whose ``report`` is an awaitable no-op (no DB)."""
    health = MagicMock()
    health.report = AsyncMock()
    return health


@pytest.mark.asyncio
async def test_inner_loop_returns_when_the_connection_tuple_changes() -> None:
    """A changed tuple ends the inner loop so the outer loop can rebuild.

    Only the SASL mechanism differs here — a broker-only comparison would miss it.

    spec: feature/BACKEND.md §Kafka connection — "A change to any element ends the inner
    poll loop".
    """
    from src.shared.datahub.consumer import KafkaFaultState, _run_inner_loop

    current = KafkaConnection(brokers="kafka:9092", security_protocol="PLAINTEXT")
    rotated = KafkaConnection(
        brokers="kafka:9092", security_protocol="SASL_SSL", sasl_mechanism="PLAIN",
        sasl_username="svc",
    )

    consumer = MagicMock()
    consumer.poll = MagicMock(return_value=None)
    reads = 0

    async def _read():
        nonlocal reads
        reads += 1
        return rotated

    with patch("src.shared.datahub.consumer.read_kafka_connection", side_effect=_read):
        await _run_inner_loop(
            consumer, MagicMock(), current, KafkaFaultState(), _quiet_health()
        )

    assert reads >= 1, "the inner loop must re-read the connection before returning"


@pytest.mark.asyncio
async def test_inner_loop_keeps_polling_while_the_tuple_is_unchanged() -> None:
    """An unchanged tuple does not end the loop — the client is not rebuilt needlessly.

    Two reconfig checks are forced: the first returns the identical tuple (loop must
    continue), the second a different one (loop must exit).

    spec: feature/BACKEND.md §Kafka connection — the rebuild is triggered by a *change*.
    """
    from src.shared.datahub.consumer import (
        _RECONFIG_CHECK_INTERVAL,
        KafkaFaultState,
        _run_inner_loop,
    )

    current = KafkaConnection(brokers="kafka:9092", security_protocol="PLAINTEXT")
    changed = KafkaConnection(brokers="kafka-new:9092", security_protocol="PLAINTEXT")

    polls = 0
    reads = 0

    consumer = MagicMock()
    consumer.poll = MagicMock(return_value=None)

    async def _to_thread(fn, *args):
        nonlocal polls
        polls += 1
        return fn(*args)

    async def _read():
        nonlocal reads
        reads += 1
        # An equal-but-distinct instance: equality, not identity, is the predicate.
        return dataclasses.replace(current) if reads <= 1 else changed

    with (
        patch("src.shared.datahub.consumer.read_kafka_connection", side_effect=_read),
        patch("asyncio.to_thread", side_effect=_to_thread),
    ):
        await _run_inner_loop(
            consumer, MagicMock(), current, KafkaFaultState(), _quiet_health()
        )

    assert reads == 2, (
        f"the loop must survive the first unchanged check and exit on the second; "
        f"got {reads} read(s)"
    )
    assert polls >= 2 * _RECONFIG_CHECK_INTERVAL, (
        f"two reconfig checks imply at least {2 * _RECONFIG_CHECK_INTERVAL} polls; got {polls}"
    )


# ── 4. run_consumer outer loop — rebuild ordering and fault survival ─────────


class _StopOuterLoop(BaseException):
    """Sentinel that escapes ``run_consumer``'s ``except Exception`` handlers."""


@pytest.mark.asyncio
async def test_outer_loop_closes_the_old_client_before_building_the_new_one(
    monkeypatch,
) -> None:
    """On a connection change the old Consumer is closed before the new one is built.

    Overlapping clients would double-join the ``dataspoke-consumers`` group and trigger
    a rebalance against a connection that is being torn down anyway.

    spec: feature/BACKEND.md §Kafka connection — "A change to any element ends the inner
    poll loop, closes the client, and rebuilds it."
    """
    import src.shared.datahub.consumer as consumer_mod

    monkeypatch.setattr(consumer_mod, "_UNCONFIGURED_SLEEP_S", 0.001)
    monkeypatch.setattr(consumer_mod, "_FAULT_RETRY_SLEEP_S", 0.001)

    call_log: list[str] = []
    ctor_count = 0

    class _FakeConsumer:
        def __init__(self, config: dict) -> None:
            nonlocal ctor_count
            ctor_count += 1
            call_log.append(f"Consumer({config['bootstrap.servers']})")
            if ctor_count >= 2:
                raise _StopOuterLoop

        def subscribe(self, topics, on_assign=None):
            pass

        def close(self):
            call_log.append("close")

    old = KafkaConnection(brokers="kafka-old:9092", security_protocol="PLAINTEXT")
    new = KafkaConnection(brokers="kafka-new:9092", security_protocol="PLAINTEXT")
    reads = 0

    async def _read():
        nonlocal reads
        reads += 1
        return old if reads == 1 else new

    async def _inner(consumer, router, current_conn, faults, health):
        call_log.append(f"inner_loop({current_conn.brokers})")

    with (
        patch.object(consumer_mod, "read_kafka_connection", side_effect=_read),
        patch.object(consumer_mod, "_run_inner_loop", side_effect=_inner),
        patch.object(consumer_mod, "Consumer", side_effect=_FakeConsumer),
        patch.object(consumer_mod, "build_router", return_value=MagicMock()),
        patch.object(consumer_mod, "_create_airflow_client", return_value=None),
        patch.object(consumer_mod.HealthReporter, "report", AsyncMock()),
    ):
        with pytest.raises(_StopOuterLoop):
            await consumer_mod.run_consumer()

    assert call_log == [
        "Consumer(kafka-old:9092)",
        "inner_loop(kafka-old:9092)",
        "close",
        "Consumer(kafka-new:9092)",
    ], f"unexpected rebuild ordering: {call_log!r}"


@pytest.mark.asyncio
async def test_outer_loop_sleeps_instead_of_exiting_when_unconfigured() -> None:
    """An unconfigured peripheral parks the process, and records no fault while it waits.

    Three things are asserted, because the spec sentence has three clauses: no Consumer
    is constructed while the connection reads ``None``, the loop sleeps on
    ``_UNCONFIGURED_SLEEP_S``, and nothing is written to the ``datahub``
    ``peripheral_health`` row.

    The negative health assertion is what makes this branch distinguishable from the
    fault branch below: without it, a handler that reported ``error`` for a peripheral
    the operator simply has not configured yet would pass here *and* satisfy the fault
    test's ``report.assert_any_call``, so neither test would be pinning which branch ran.

    ``asyncio.sleep`` is mocked rather than the constant shortened, so the awaited value
    is the shipped one — a park that forgets to sleep would hot-loop against the
    database.

    spec: feature/BACKEND.md §Kafka connection — "An unconfigured peripheral parks the
    process in a retry sleep rather than crash-looping, recording no fault".
    """
    import src.shared.datahub.consumer as consumer_mod

    reads = 0

    async def _read():
        nonlocal reads
        reads += 1
        if reads >= 3:
            raise _StopOuterLoop
        return None

    ctor = MagicMock(side_effect=AssertionError("no client may be built while unconfigured"))
    report = AsyncMock()
    sleep = AsyncMock()

    with (
        patch.object(consumer_mod, "read_kafka_connection", side_effect=_read),
        patch.object(consumer_mod, "Consumer", ctor),
        patch.object(consumer_mod, "build_router", return_value=MagicMock()),
        patch.object(consumer_mod, "_create_airflow_client", return_value=None),
        patch.object(consumer_mod.HealthReporter, "report", report),
        patch.object(consumer_mod.asyncio, "sleep", sleep),
    ):
        with pytest.raises(_StopOuterLoop):
            await consumer_mod.run_consumer()

    # Backstop: the loop actually iterated (rather than exiting on the first read).
    assert reads == 3
    ctor.assert_not_called()
    sleep.assert_awaited_with(consumer_mod._UNCONFIGURED_SLEEP_S)
    assert report.await_count == 0, (
        "an unconfigured peripheral is not a fault — nothing may be written to the "
        f"datahub peripheral_health row; got {report.await_args_list!r}. "
        "spec: feature/BACKEND.md §Kafka connection — 'recording no fault'."
    )


@pytest.mark.asyncio
async def test_outer_loop_survives_a_config_read_that_raises() -> None:
    """A ``peripheral_config`` read that raises is retried, not fatal.

    Regression for issue #117: the bootstrap ``read_kafka_connection()`` was the one
    segment of the outer loop with no ``try``. On a fresh install the event-consumer
    starts before the API's Alembic init container has created
    ``dataspoke.peripheral_config``, so the read raised ``UndefinedTableError``, escaped
    ``asyncio.run``, and the pod exited 1 — a crash-loop that resolved only by luck of
    restart timing.

    The proof of recovery is that the *second* read is reached and its connection is
    carried all the way to the ``Consumer`` constructor, which is where the sentinel is
    raised. On the pre-fix code the test dies at the first read and never gets there.

    The health assertion is what stops a fix that keeps the process alive while leaving
    the health row reading ``ok`` — a consumer dead in the water with nothing to show
    for it. Because the spec calls that write **best-effort**, the assertion is on the
    *attempt* (``HealthReporter.report`` was called with the fault), not on its
    persistence: the row lives in the same database that just failed, so it may well not
    land. Its negative counterpart is in the unconfigured test above, which asserts no
    report at all — together they pin *which* branch ran.

    The sleep assertions are the third leg: "keeps the process alive on the same retry
    sleep" is two claims, that a sleep happens at all (without it the recovery is a hot
    loop hammering the database that just failed) and that it is the same one the
    unconfigured branch parks on. ``asyncio.sleep`` is mocked rather than the constant
    shortened, so the awaited value is the shipped 10s and not a test-injected 1ms.

    The raised error is a plain ``RuntimeError`` carrying the production message text.
    The invariant is "any ``Exception``", not "this exception class" — constructing a
    real ``asyncpg.exceptions.UndefinedTableError`` requires driver internals and would
    pin the test to a dependency detail the spec does not name.

    spec: feature/BACKEND.md §Kafka connection — "a ``peripheral_config`` read that fails
    outright — the database unreachable, or its schema not yet migrated — keeps the
    process alive on the same retry sleep and reports the fault on the ``datahub``
    ``peripheral_health`` row on a best-effort basis."
    """
    import src.shared.datahub.consumer as consumer_mod

    failure = 'relation "peripheral_config" does not exist'
    reads = 0

    async def _read():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError(failure)
        return KafkaConnection(brokers="kafka:9092", security_protocol="PLAINTEXT")

    def _ctor(_config: dict) -> None:
        # Reaching the constructor is the evidence: the loop recovered from the failed
        # read and got as far as building a client from the connection it then read.
        raise _StopOuterLoop

    report = AsyncMock()
    sleep = AsyncMock()

    with (
        patch.object(consumer_mod, "read_kafka_connection", side_effect=_read),
        patch.object(consumer_mod, "Consumer", side_effect=_ctor),
        patch.object(consumer_mod, "build_router", return_value=MagicMock()),
        patch.object(consumer_mod, "_create_airflow_client", return_value=None),
        patch.object(consumer_mod.HealthReporter, "report", report),
        patch.object(consumer_mod.asyncio, "sleep", sleep),
    ):
        with pytest.raises(_StopOuterLoop):
            await consumer_mod.run_consumer()

    assert reads == 2, (
        f"the failed read must be retried and the retry must reach the client build; "
        f"got {reads} read(s)"
    )
    report.assert_any_call("error", failure)
    sleep.assert_awaited_once_with(consumer_mod._FAULT_RETRY_SLEEP_S)
    assert consumer_mod._FAULT_RETRY_SLEEP_S == consumer_mod._UNCONFIGURED_SLEEP_S, (
        "the fault retry must park on the *same* sleep as the unconfigured branch; got "
        f"{consumer_mod._FAULT_RETRY_SLEEP_S} vs {consumer_mod._UNCONFIGURED_SLEEP_S}. "
        "spec: feature/BACKEND.md §Kafka connection — 'keeps the process alive on the "
        "same retry sleep'."
    )


@pytest.mark.asyncio
async def test_a_base_exception_from_the_config_read_still_escapes() -> None:
    """Guard, not a regression: ``BaseException`` from the read is not swallowed.

    This passes on the pre-fix code too — its job is to fail if the handler added above
    is ever widened to ``except BaseException``, which would swallow the
    ``CancelledError`` a pod shutdown delivers and leave the consumer spinning through
    its retry sleep until the kubelet SIGKILLs it.

    spec: feature/BACKEND.md §Kafka connection — the retry covers a ``peripheral_config``
    read "that fails outright", i.e. an error of the operation; process cancellation is
    not one.
    """
    import src.shared.datahub.consumer as consumer_mod

    async def _read():
        raise _StopOuterLoop

    # A widened handler would swallow the sentinel and retry forever. Failing the
    # sleep turns that into an immediate, named failure rather than a hung run —
    # the suite carries no timeout, and a test that never terminates certifies
    # nothing.
    swallowed = AsyncMock(
        side_effect=AssertionError("the bootstrap read handler swallowed a BaseException")
    )

    with (
        patch.object(consumer_mod, "read_kafka_connection", side_effect=_read),
        patch.object(consumer_mod, "build_router", return_value=MagicMock()),
        patch.object(consumer_mod, "_create_airflow_client", return_value=None),
        patch.object(consumer_mod.HealthReporter, "report", AsyncMock()),
        patch.object(consumer_mod.asyncio, "sleep", swallowed),
    ):
        with pytest.raises(_StopOuterLoop):
            await consumer_mod.run_consumer()
