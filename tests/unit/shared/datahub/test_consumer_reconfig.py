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
4. ``run_consumer()`` outer loop — closes the old client before constructing the new one.

No Kafka broker, database, or Kubernetes API is contacted.

Spec traceability:
- spec/feature/BACKEND.md §Kafka connection — "The consumer reads its whole connection
  from ``peripheral_config.datahub`` — brokers plus the security tuple … and re-reads
  it every few seconds while polling. A change to any element ends the inner poll loop,
  closes the client, and rebuilds it; an unconfigured peripheral parks the process in a
  retry sleep rather than crash-looping."
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


# ── 4. run_consumer outer loop — rebuild ordering ────────────────────────────


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
async def test_outer_loop_sleeps_instead_of_exiting_when_unconfigured(monkeypatch) -> None:
    """An unconfigured peripheral parks the process rather than crash-looping.

    No Consumer is constructed while the connection reads ``None``.

    spec: feature/BACKEND.md §Kafka connection — "an unconfigured peripheral parks the
    process in a retry sleep rather than crash-looping".
    """
    import src.shared.datahub.consumer as consumer_mod

    monkeypatch.setattr(consumer_mod, "_UNCONFIGURED_SLEEP_S", 0.0)

    reads = 0

    async def _read():
        nonlocal reads
        reads += 1
        if reads >= 3:
            raise _StopOuterLoop
        return None

    ctor = MagicMock(side_effect=AssertionError("no client may be built while unconfigured"))

    with (
        patch.object(consumer_mod, "read_kafka_connection", side_effect=_read),
        patch.object(consumer_mod, "Consumer", ctor),
        patch.object(consumer_mod, "build_router", return_value=MagicMock()),
        patch.object(consumer_mod, "_create_airflow_client", return_value=None),
    ):
        with pytest.raises(_StopOuterLoop):
            await consumer_mod.run_consumer()

    # Backstop: the loop actually iterated (rather than exiting on the first read).
    assert reads == 3
    ctor.assert_not_called()
