"""Unit tests for the DataHub consumer reconfig behavior.

Tests the peripheral-config-aware consumer restart logic in consumer.py:

1. _read_kafka_brokers():
   - Returns kafka_brokers string when peripheral is configured.
   - Returns None when peripheral row is absent (dto is None).
   - Returns None when dto.kafka_brokers is empty string.
   - Calls invalidate_peripheral_config_cache("datahub") before reading,
     so broker changes bypass the 30-second process-level cache.

2. _run_inner_loop():
   - Returns when broker address changes (triggering outer loop rebuild).
   - Does not return when broker address stays the same.

spec traceability:
- DATAHUB §Event Subscription (impl: consumer.py) — _read_kafka_brokers
  invalidates cache; outer loop rebuilds consumer on broker change.
- src/shared/datahub/consumer.py — _read_kafka_brokers, _run_inner_loop.
- spec/DATAHUB_INTEGRATION.md §Event Subscription — peripheral-config-backed broker address.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.datahub.consumer import _read_kafka_brokers

# ── _read_kafka_brokers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_kafka_brokers_returns_configured_value(monkeypatch) -> None:
    """_read_kafka_brokers returns the kafka_brokers string when peripheral is configured.

    spec: DATAHUB §Event Subscription (impl: consumer.py).
    """
    from src.backend.admin.peripheral_service import DatahubConfigDTO

    _fake_dto = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka-host:9092")

    mock_db = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    # SessionLocal is imported lazily inside _read_kafka_brokers — patch at the source module.
    with (
        patch(
            "src.shared.db.session.SessionLocal",
            return_value=mock_session_ctx,
        ),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            AsyncMock(return_value=_fake_dto),
        ),
        patch(
            "src.backend.admin.peripheral_service.invalidate_peripheral_config_cache"
        ) as mock_invalidate,
    ):
        result = await _read_kafka_brokers()

    assert result == "kafka-host:9092", (
        f"Expected 'kafka-host:9092' from peripheral config, got {result!r}. "
        "spec: DATAHUB §Event Subscription (impl: consumer.py)."
    )
    mock_invalidate.assert_called_once_with("datahub")


@pytest.mark.asyncio
async def test_read_kafka_brokers_returns_none_when_dto_is_none(monkeypatch) -> None:
    """_read_kafka_brokers returns None when peripheral row is absent.

    When the peripheral is unconfigured, the consumer outer loop should
    sleep and retry rather than attempt to connect to Kafka.

    spec: DATAHUB §Event Subscription (impl: consumer.py) —
    outer loop sleeps when peripheral unconfigured.
    """
    mock_db = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "src.shared.db.session.SessionLocal",
            return_value=mock_session_ctx,
        ),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            AsyncMock(return_value=None),
        ),
        patch("src.backend.admin.peripheral_service.invalidate_peripheral_config_cache"),
    ):
        result = await _read_kafka_brokers()

    assert result is None, (
        f"Expected None when peripheral is unconfigured, got {result!r}. "
        "spec: DATAHUB §Event Subscription (impl: consumer.py)."
    )


@pytest.mark.asyncio
async def test_read_kafka_brokers_returns_none_when_kafka_brokers_empty(monkeypatch) -> None:
    """_read_kafka_brokers returns None when dto.kafka_brokers is empty string.

    An empty string kafka_brokers means the row exists but has not been configured
    with a broker address — treated the same as unconfigured.

    spec: src/shared/datahub/consumer.py _read_kafka_brokers —
    return dto.kafka_brokers or None.
    """
    from src.backend.admin.peripheral_service import DatahubConfigDTO

    _dto_empty_brokers = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="")

    mock_db = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "src.shared.db.session.SessionLocal",
            return_value=mock_session_ctx,
        ),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            AsyncMock(return_value=_dto_empty_brokers),
        ),
        patch("src.backend.admin.peripheral_service.invalidate_peripheral_config_cache"),
    ):
        result = await _read_kafka_brokers()

    assert result is None, (
        f"Empty kafka_brokers should yield None from _read_kafka_brokers; got {result!r}."
    )


@pytest.mark.asyncio
async def test_read_kafka_brokers_invalidates_cache_before_reading(monkeypatch) -> None:
    """_read_kafka_brokers calls invalidate_peripheral_config_cache('datahub') first.

    The cache is invalidated BEFORE the DB read so that broker changes are visible
    within the 5-second reconfig check interval (not masked by the 30-second TTL).

    spec: DATAHUB §Event Subscription (impl: consumer.py) —
    invalidate cache before read so broker changes are visible promptly.
    spec: src/shared/datahub/consumer.py _read_kafka_brokers — invalidate then read.
    """
    from src.backend.admin.peripheral_service import DatahubConfigDTO

    _fake_dto = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
    call_order: list[str] = []

    mock_db = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    def _track_invalidate(name):
        call_order.append("invalidate")

    async def _track_get_config(db, name):
        call_order.append("get_config")
        return _fake_dto

    with (
        patch(
            "src.shared.db.session.SessionLocal",
            return_value=mock_session_ctx,
        ),
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            side_effect=_track_get_config,
        ),
        patch(
            "src.backend.admin.peripheral_service.invalidate_peripheral_config_cache",
            side_effect=_track_invalidate,
        ),
    ):
        await _read_kafka_brokers()

    assert call_order[0] == "invalidate", (
        "invalidate_peripheral_config_cache must be called before get_peripheral_config. "
        f"Got call order: {call_order}"
    )
    assert "get_config" in call_order


# ── _run_inner_loop broker-change detection ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_inner_loop_returns_when_broker_changes() -> None:
    """_run_inner_loop returns when _read_kafka_brokers detects a broker address change.

    When the new broker address differs from current_brokers, _run_inner_loop
    returns so the outer loop can rebuild the consumer.

    spec: DATAHUB §Event Subscription (impl: consumer.py) —
    consumer rebuilt when kafka_brokers changes.
    spec: src/shared/datahub/consumer.py _run_inner_loop — returns on broker change.
    """
    from src.shared.datahub.consumer import _run_inner_loop

    mock_consumer = MagicMock()
    mock_consumer.poll = MagicMock(return_value=None)  # no messages
    mock_router = MagicMock()

    call_count = 0

    async def _read_brokers_changed():
        nonlocal call_count
        call_count += 1
        # Return different broker on first reconfig check
        return "kafka-new:9092"

    with patch(
        "src.shared.datahub.consumer._read_kafka_brokers",
        side_effect=_read_brokers_changed,
    ):
        await _run_inner_loop(mock_consumer, mock_router, current_brokers="kafka-old:9092")

    # If we get here, _run_inner_loop returned (which is the desired behavior).
    assert call_count >= 1, "_read_kafka_brokers must be called during the inner loop."


@pytest.mark.asyncio
async def test_run_inner_loop_does_not_return_when_broker_unchanged() -> None:
    """_run_inner_loop does not return prematurely when broker address stays the same.

    When _read_kafka_brokers returns the same broker address, the inner loop
    must continue polling — not return on the first reconfig check.

    We verify this by running the inner loop for exactly _RECONFIG_CHECK_INTERVAL + 1
    polls with unchanged brokers, then changing the broker to cause a clean exit.
    The reconfig check must have run at least once (after _RECONFIG_CHECK_INTERVAL polls)
    and must NOT have caused an early return.

    spec: src/shared/datahub/consumer.py _run_inner_loop —
    only returns on broker change.
    """
    from src.shared.datahub.consumer import _RECONFIG_CHECK_INTERVAL, _run_inner_loop

    same_brokers = "kafka:9092"
    new_brokers = "kafka-new:9092"

    # Track poll calls and reconfig calls.
    poll_calls = 0
    reconfig_calls = 0

    mock_consumer = MagicMock()
    mock_router = MagicMock()

    async def _fake_to_thread(fn, *args):
        nonlocal poll_calls
        poll_calls += 1
        return fn(*args)  # call poll() directly

    mock_consumer.poll = MagicMock(return_value=None)  # always no messages

    async def _read_brokers():
        nonlocal reconfig_calls
        reconfig_calls += 1
        # Return same brokers for first check, then changed brokers to exit
        if reconfig_calls <= 1:
            return same_brokers
        return new_brokers  # trigger exit on 2nd reconfig check

    with (
        patch("src.shared.datahub.consumer._read_kafka_brokers", side_effect=_read_brokers),
        patch("asyncio.to_thread", side_effect=_fake_to_thread),
    ):
        # Run the inner loop; it must exit after the 2nd reconfig check detects broker change.
        await _run_inner_loop(mock_consumer, mock_router, current_brokers=same_brokers)

    # The inner loop ran _RECONFIG_CHECK_INTERVAL polls before the 1st reconfig check,
    # then continued because brokers were unchanged, then exited on the 2nd check.
    assert reconfig_calls >= 2, (
        f"Inner loop must not exit immediately when broker unchanged; "
        f"got {reconfig_calls} reconfig check(s). "
        "The loop should have continued past the first 'same broker' check."
    )
    # With 2 reconfig checks, at least 2 * _RECONFIG_CHECK_INTERVAL polls ran.
    assert poll_calls >= _RECONFIG_CHECK_INTERVAL, (
        f"Expected at least {_RECONFIG_CHECK_INTERVAL} polls; got {poll_calls}."
    )


# ── F3: run_consumer outer loop — rebuild ordering ────────────────────────────


@pytest.mark.asyncio
async def test_run_consumer_outer_loop_closes_old_consumer_before_building_new(
    monkeypatch,
) -> None:
    """run_consumer outer loop: closes old Consumer before constructing the new one.

    Verified ordering:
    1. Consumer({"bootstrap.servers": "kafka-old:9092", ...}) constructed.
    2. _run_inner_loop enters.
    3. old_consumer.close() called.
    4. Consumer({"bootstrap.servers": "kafka-new:9092", ...}) constructed.

    Technique: a shared ``call_log`` list that every mock appends to on call.
    After the second Consumer ctor, a sentinel StopIteration exits the outer loop.

    monkeypatching _UNCONFIGURED_SLEEP_S=0.001 keeps the test fast in case
    run_consumer ever sleeps.

    spec: DATAHUB §Event Subscription (impl: consumer.py) —
    consumer rebuilt when kafka_brokers changes; old consumer closed first.
    spec: src/shared/datahub/consumer.py run_consumer — outer loop rebuilds on
    broker change; consumer.close() in finally block.
    """
    import src.shared.datahub.consumer as _consumer_mod

    monkeypatch.setattr(_consumer_mod, "_UNCONFIGURED_SLEEP_S", 0.001)

    call_log: list[str] = []

    # Track Consumer() ctor calls; raise on the 3rd call to stop the outer loop.
    consumer_ctor_count = 0
    sentinel_exc = RuntimeError("_STOP_LOOP_SENTINEL_")

    class _FakeConsumer:
        def __init__(self, config: dict) -> None:
            nonlocal consumer_ctor_count
            consumer_ctor_count += 1
            broker = config.get("bootstrap.servers", "")
            call_log.append(f"Consumer({broker})")
            if consumer_ctor_count >= 2:
                # After second construction, raise to stop the outer loop cleanly.
                raise sentinel_exc

        def subscribe(self, topics):
            pass

        def close(self):
            call_log.append("close")

    # _read_kafka_brokers: first call returns "kafka-old:9092"; subsequent return "kafka-new:9092".
    read_call_count = 0

    async def _fake_read_kafka_brokers() -> str | None:
        nonlocal read_call_count
        read_call_count += 1
        if read_call_count == 1:
            return "kafka-old:9092"
        return "kafka-new:9092"

    # _run_inner_loop: first call records "inner_loop" then returns (simulating broker change).
    inner_loop_call_count = 0

    async def _fake_run_inner_loop(consumer, router, current_brokers: str) -> None:
        nonlocal inner_loop_call_count
        inner_loop_call_count += 1
        call_log.append(f"inner_loop({current_brokers})")
        # Return immediately (simulates broker-change detection).

    with (
        patch.object(_consumer_mod, "_read_kafka_brokers", side_effect=_fake_read_kafka_brokers),
        patch.object(_consumer_mod, "_run_inner_loop", side_effect=_fake_run_inner_loop),
        patch.object(_consumer_mod, "Consumer", side_effect=_FakeConsumer),
        patch.object(_consumer_mod, "build_router", return_value=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="_STOP_LOOP_SENTINEL_"):
            await _consumer_mod.run_consumer()

    # Verify the required ordering:
    # 1. First Consumer ctor with old brokers.
    # 2. inner_loop entered.
    # 3. close() called.
    # 4. Second Consumer ctor with new brokers (raises sentinel).

    assert "Consumer(kafka-old:9092)" in call_log, (
        f"First Consumer must be constructed with 'kafka-old:9092'. call_log={call_log!r}"
    )
    assert "inner_loop(kafka-old:9092)" in call_log, (
        f"_run_inner_loop must be entered with old brokers. call_log={call_log!r}"
    )
    assert "close" in call_log, (
        f"old_consumer.close() must be called after inner loop exits. call_log={call_log!r}"
    )
    assert "Consumer(kafka-new:9092)" in call_log, (
        f"Second Consumer must be constructed with 'kafka-new:9092'. call_log={call_log!r}"
    )

    # Strict ordering assertions using index positions.
    idx_old = call_log.index("Consumer(kafka-old:9092)")
    idx_inner = call_log.index("inner_loop(kafka-old:9092)")
    idx_close = call_log.index("close")
    idx_new = call_log.index("Consumer(kafka-new:9092)")

    assert idx_old < idx_inner, (
        f"Consumer(old) must be constructed before inner_loop enters. "
        f"Positions: Consumer(old)={idx_old}, inner_loop={idx_inner}. "
        "spec: DATAHUB §Event Subscription (impl: consumer.py)."
    )
    assert idx_inner < idx_close, (
        f"inner_loop must run before close(). "
        f"Positions: inner_loop={idx_inner}, close={idx_close}. "
        "spec: src/shared/datahub/consumer.py run_consumer — close in finally."
    )
    assert idx_close < idx_new, (
        f"close() must be called before the new Consumer is constructed. "
        f"Positions: close={idx_close}, Consumer(new)={idx_new}. "
        "spec: DATAHUB §Event Subscription (impl: consumer.py) — "
        "old consumer closed before new one is built."
    )
