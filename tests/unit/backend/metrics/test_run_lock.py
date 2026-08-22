"""Run-lock lifecycle tests for MetricsService.run — acquire, hold, release.

A metric measurement is serialised per metric by the Redis lock
``metrics:running:{metric_id}``; a leaked lock is a permanent ``409 METRIC_RUNNING``.
These tests prove the lock is released in BOTH outcomes (success and mid-run failure)
and that a rejected duplicate run never clears the holder's lock (CAS release).

Spec:
  spec/feature/BACKEND.md §Concurrency Guards — Redis SET NX / *_RUNNING error codes;
    "If a duplicate is detected, the API returns 409 Conflict with the appropriate
    *_RUNNING error code (METAGEN_RUNNING, METRIC_RUNNING, ONTOGEN_RUNNING, …)."
  spec/feature/BACKEND.md §Error Model — ConflictError → 409, METRIC_RUNNING.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.metrics.service import MetricRunResult, MetricsService
from src.shared.exceptions import ConflictError

_METRIC_ID = "orders-freshness"
_LOCK_KEY = f"metrics:running:{_METRIC_ID}"


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


def _make_result() -> MetricRunResult:
    return MetricRunResult(run_id="run-1", status="success", detail={})


def _make_service(cache: FakeLockCache) -> MetricsService:
    return MetricsService(
        datahub=AsyncMock(),
        db=AsyncMock(spec=AsyncSession),
        cache=cache,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_run_releases_lock_on_success() -> None:
    """A successful run releases the per-metric lock so the next run can acquire it.

    Backstop: a competing acquire during the run must fail — proving the lock was held.

    Spec: BACKEND.md §Concurrency Guards — metrics run serialised, 409 METRIC_RUNNING on
    duplicate.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)

    held_mid_run: list[bool] = []

    async def _inner(
        metric_id: str, dry_run: bool, scheduled_at: datetime | None = None
    ) -> MetricRunResult:
        held_mid_run.append(await cache.set_nx(_LOCK_KEY, "intruder-token"))
        return _make_result()

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    result = await svc.run(_METRIC_ID)

    assert result.status == "success"
    assert held_mid_run == [False], "metric lock must be held while the run is in flight"
    assert await cache.set_nx(_LOCK_KEY, "next-run-token") is True


@pytest.mark.asyncio
async def test_run_releases_lock_on_midrun_failure() -> None:
    """A run that raises mid-way still releases the lock (finally-block release).

    Spec: BACKEND.md §Concurrency Guards — Redis SET NX guard; leaked lock would
    permanently 409 METRIC_RUNNING.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)

    held_mid_run: list[bool] = []

    async def _inner(
        metric_id: str, dry_run: bool, scheduled_at: datetime | None = None
    ) -> MetricRunResult:
        held_mid_run.append(await cache.set_nx(_LOCK_KEY, "intruder-token"))
        raise RuntimeError("measurement blew up mid-run")

    svc._run_inner = AsyncMock(side_effect=_inner)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="blew up"):
        await svc.run(_METRIC_ID)

    assert held_mid_run == [False], "metric lock must be held while the failing run runs"
    assert await cache.set_nx(_LOCK_KEY, "recovery-run-token") is True


@pytest.mark.asyncio
async def test_duplicate_run_rejected_and_preserves_holder_lock() -> None:
    """A duplicate run raises 409 METRIC_RUNNING and never releases the holder's lock.

    Spec: BACKEND.md §Concurrency Guards — 409 with METRIC_RUNNING on a duplicate run.
    """
    cache = FakeLockCache()
    svc = _make_service(cache)

    assert await cache.set_nx(_LOCK_KEY, "holder-token") is True

    svc._run_inner = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(_METRIC_ID)

    assert exc_info.value.error_code == "METRIC_RUNNING"
    svc._run_inner.assert_not_awaited()
    assert await cache.set_nx(_LOCK_KEY, "intruder-token") is False
