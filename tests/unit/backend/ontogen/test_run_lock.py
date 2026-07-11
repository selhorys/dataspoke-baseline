"""Run-lock lifecycle tests for OntogenService.run — acquire, hold, release.

Ontogen inference is serialised by the singleton Redis lock
``ontogen:running:singleton``; a leaked lock is a permanent ``409 ONTOGEN_RUNNING``.
These tests prove the lock is released in BOTH outcomes (success and mid-run failure)
and that a rejected duplicate run never clears the holder's lock (CAS release).

Spec:
  spec/feature/BACKEND.md §Ontology Generation Service — "Concurrent inference runs
    return 409 ONTOGEN_RUNNING ... enforced by the Redis ontogen:running:singleton
    SET NX guard."
  spec/feature/BACKEND.md §Concurrency Guards — Redis SET NX, key
    ontogen:running:singleton, TTL 1 hour.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ontogen.service import OntogenRunSummary, OntogenService
from src.shared.exceptions import ConflictError

_LOCK_KEY = "ontogen:running:singleton"


class FakeLockCache:
    """Minimal in-memory stand-in for the Redis lock abstraction (SET NX + CAS release)."""

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


def _make_summary() -> OntogenRunSummary:
    return OntogenRunSummary(status="success", dry_run=False, unresolved_urns=[], counts={})


def _make_service(cache: FakeLockCache) -> OntogenService:
    return OntogenService(
        datahub=AsyncMock(),
        db=AsyncMock(spec=AsyncSession),
        cache=cache,  # type: ignore[arg-type]
        llm=AsyncMock(),
        vector=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_run_releases_lock_on_success() -> None:
    """A successful run releases the singleton lock so the next run can acquire it.

    Backstop: a competing acquire during the run must fail — proving the lock was held.

    Spec: BACKEND.md §Ontology Generation Service — ontogen:running:singleton SET NX guard.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)

    held_mid_run: list[bool] = []

    async def _inner(**_kwargs: object) -> OntogenRunSummary:
        held_mid_run.append(await cache.set_nx(_LOCK_KEY, "intruder-token"))
        return _make_summary()

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    result = await svc.run()

    assert result.status == "success"
    assert held_mid_run == [False], "singleton lock must be held while the run is in flight"
    assert await cache.set_nx(_LOCK_KEY, "next-run-token") is True


@pytest.mark.asyncio
async def test_run_releases_lock_on_midrun_failure() -> None:
    """A run that raises mid-way still releases the singleton lock (finally-block release).

    Spec: BACKEND.md §Concurrency Guards — Redis SET NX guard ontogen:running:singleton.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)
    # Isolate the lock lifecycle from the failure-event side effect.
    svc._record_ontogen_event = AsyncMock()  # type: ignore[method-assign]

    held_mid_run: list[bool] = []

    async def _inner(**_kwargs: object) -> OntogenRunSummary:
        held_mid_run.append(await cache.set_nx(_LOCK_KEY, "intruder-token"))
        raise RuntimeError("inference blew up mid-run")

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="blew up"):
        await svc.run()

    assert held_mid_run == [False], "singleton lock must be held while the failing run runs"
    assert await cache.set_nx(_LOCK_KEY, "recovery-run-token") is True


@pytest.mark.asyncio
async def test_duplicate_run_rejected_and_preserves_holder_lock() -> None:
    """A duplicate run raises 409 ONTOGEN_RUNNING and never releases the holder's lock.

    Spec: BACKEND.md §Ontology Generation Service — "Concurrent inference runs return
    409 ONTOGEN_RUNNING".
    """
    cache = FakeLockCache()
    svc = _make_service(cache)

    assert await cache.set_nx(_LOCK_KEY, "holder-token") is True

    svc._run_inner = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ConflictError) as exc_info:
        await svc.run()

    assert exc_info.value.error_code == "ONTOGEN_RUNNING"
    svc._run_inner.assert_not_awaited()
    assert await cache.set_nx(_LOCK_KEY, "intruder-token") is False
