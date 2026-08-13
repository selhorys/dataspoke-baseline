"""Run-lock lifecycle tests for MetagenService.run — acquire, hold, release.

A metagen run is serialised per conf by the Redis lock ``metagen:running:{conf_id}``;
a leaked lock is a permanent ``409 METAGEN_RUNNING`` for the next run. These tests
prove the lock is released in BOTH outcomes (success and mid-run failure) and that a
rejected duplicate run never clears the holder's lock (CAS release).

Spec:
  spec/feature/BACKEND.md §Metadata Generation Service — "Concurrency. Generation
    runs are serialised per conf by a Redis lock metagen:running:{conf_id}. A
    duplicate ... returns 409 METAGEN_RUNNING; distinct confs run concurrently."
  spec/feature/BACKEND.md §Concurrency Guards — Redis SET NX, key
    metagen:running:{conf_id}, TTL 1 hour.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metagen.service import MetagenConfDTO, MetagenService, RunResultDTO
from src.shared.exceptions import ConflictError


class FakeLockCache:
    """Minimal in-memory stand-in for the Redis lock abstraction.

    Implements only ``set_nx`` (SET NX — acquire) and ``delete_if_value`` (CAS
    release keyed on the token) so the test asserts the lock *contract* the service
    depends on, rather than a specific mock-call sequence.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set_nx(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete_if_value(self, key: str, value: str) -> bool:
        if self.store.get(key) == value:
            del self.store[key]
            return True
        return False


def _make_conf_dto(conf_id: str, *, is_enabled: bool = True) -> MetagenConfDTO:
    now = datetime.now(tz=UTC)
    return MetagenConfDTO(
        id=conf_id,
        name="catalog-docs",
        is_enabled=is_enabled,
        schedule_tier=None,
        dataset_filter="",
        result_limit=3,
        overwrite_pending=True,
        created_at=now,
        updated_at=now,
    )


def _make_run_dto(conf_id: str) -> RunResultDTO:
    return RunResultDTO(
        run_id=str(uuid.uuid4()),
        conf_id=conf_id,
        status="success",
        dry_run=False,
        unresolved_urns=[],
        counts={"items_considered": 0},
    )


def _make_service(cache: FakeLockCache) -> MetagenService:
    return MetagenService(
        datahub=AsyncMock(),
        db=AsyncMock(spec=AsyncSession),
        cache=cache,  # type: ignore[arg-type]
        llm=AsyncMock(),
        vector=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_run_releases_lock_on_success() -> None:
    """A successful run releases the per-conf lock so the next run can acquire it.

    Backstop: a competing acquire *during* the run must fail — proving the lock was
    actually held (the post-run acquire is not vacuously free).

    Spec: BACKEND.md §Metadata Generation Service — per-conf run serialisation via
    metagen:running:{conf_id}.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)
    conf_id = str(uuid.uuid4())
    lock_key = f"metagen:running:{conf_id}"

    svc.get_conf = AsyncMock(return_value=_make_conf_dto(conf_id))  # type: ignore[method-assign]

    held_mid_run: list[bool] = []

    async def _inner(**_kwargs: object) -> RunResultDTO:
        # While the run is in flight the conf lock must be held: a competing
        # acquire on the same key returns False.
        held_mid_run.append(await cache.set_nx(lock_key, "intruder-token"))
        return _make_run_dto(conf_id)

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    result = await svc.run(conf_id)

    assert result.status == "success"
    # Backstop: the lock was genuinely held during the run.
    assert held_mid_run == [False], "lock must be held while the run is in flight"
    # Spec contract: the lock is released after the run, so a fresh acquire succeeds.
    assert await cache.set_nx(lock_key, "next-run-token") is True


@pytest.mark.asyncio
async def test_run_releases_lock_on_midrun_failure() -> None:
    """A run that raises mid-way still releases the lock (finally-block release).

    Without the finally release, the raised error would leak the lock and every
    later run would 409 forever.

    Spec: BACKEND.md §Concurrency Guards — Redis SET NX guard metagen:running:{conf_id}.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)
    conf_id = str(uuid.uuid4())
    lock_key = f"metagen:running:{conf_id}"

    svc.get_conf = AsyncMock(return_value=_make_conf_dto(conf_id))  # type: ignore[method-assign]
    # Isolate the lock lifecycle from the failure-event side effect.
    svc._record_metagen_event = AsyncMock()  # type: ignore[method-assign]

    held_mid_run: list[bool] = []

    async def _inner(**_kwargs: object) -> RunResultDTO:
        held_mid_run.append(await cache.set_nx(lock_key, "intruder-token"))
        raise RuntimeError("debate blew up mid-run")

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="blew up"):
        await svc.run(conf_id)

    # Backstop: the lock was held at the moment the run raised.
    assert held_mid_run == [False], "lock must be held while the failing run is in flight"
    # Spec contract: even on failure the lock is released — the next run acquires cleanly.
    assert await cache.set_nx(lock_key, "recovery-run-token") is True


@pytest.mark.asyncio
async def test_duplicate_run_rejected_and_preserves_holder_lock() -> None:
    """A duplicate run raises 409 METAGEN_RUNNING and never releases the holder's lock.

    The 409 is raised before the try/finally is entered, so the CAS release cannot
    delete a token it does not own — the in-flight holder keeps its lock.

    Spec: BACKEND.md §Metadata Generation Service — "A duplicate ... returns 409
    METAGEN_RUNNING".
    """
    cache = FakeLockCache()
    svc = _make_service(cache)
    conf_id = str(uuid.uuid4())
    lock_key = f"metagen:running:{conf_id}"

    # Another worker already holds the conf lock.
    assert await cache.set_nx(lock_key, "holder-token") is True

    svc.get_conf = AsyncMock(return_value=_make_conf_dto(conf_id))  # type: ignore[method-assign]
    svc._run_inner = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(conf_id)

    assert exc_info.value.error_code == "METAGEN_RUNNING"
    # The rejected run must not have executed the pipeline...
    svc._run_inner.assert_not_awaited()
    # ...and must not have released the holder's lock (CAS by token).
    assert await cache.set_nx(lock_key, "intruder-token") is False
