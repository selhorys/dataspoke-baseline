"""API-wired integration test fixtures.

Extends the root ``tests/integration/conftest.py`` (inherited automatically
by pytest).  Provides fixtures specific to REST-based testing so that spot
and story tests get a ready-to-use auth header dict without boilerplate.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from tests.integration.conftest import _auth_headers


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """JWT auth headers for API-wired test requests."""
    return _auth_headers()


# ── Mock factories for Temporal activity dependencies ─────────────────────────


def mock_qdrant() -> AsyncMock:
    """AsyncMock QdrantManager with ``search`` returning empty results."""
    m = AsyncMock()
    m.search = AsyncMock(return_value=[])
    return m


def mock_cache() -> AsyncMock:
    """AsyncMock RedisClient with standard get/set/publish/delete methods."""
    m = AsyncMock()
    m.get = AsyncMock(return_value=None)
    m.set = AsyncMock()
    m.publish = AsyncMock()
    m.delete = AsyncMock()
    return m


def mock_llm(**overrides) -> AsyncMock:
    """AsyncMock LLMClient with ``complete`` and ``complete_json`` methods.

    Pass keyword arguments to override ``complete_json``'s return value::

        mock_llm(complete_json_return={"field_descriptions": {...}})
    """
    m = AsyncMock()
    m.complete = AsyncMock(return_value="test response")
    m.complete_json = AsyncMock(return_value=overrides.get("complete_json_return", {}))
    return m


class _TestSessionWrapper:
    """Async context manager that yields a session without closing it.

    Activities use ``async with make_db_session() as db:``.  In tests the
    session lifecycle is managed by the ``async_session`` fixture, so this
    wrapper prevents the activity from closing it prematurely.
    """

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_args):
        pass


# ── Temporal worker helper ───────────────────────────────────────────────────


@asynccontextmanager
async def make_temporal_worker(
    temporal_client,
    datahub_client,
    *,
    db_session=None,
    workflow_module: str,
    workflow_cls,
    activity_fn,
    extra_patches: dict[str, object] | None = None,
) -> AsyncGenerator[Worker]:
    """Start a function-scoped in-process Temporal worker with mocked factories.

    Patches ``make_datahub``, ``make_llm``, and ``make_db_session`` in
    *workflow_module* so the activity uses the test's authenticated DataHub
    client, deterministic mocks, and the test-scoped DB session (avoiding
    stale connection-pool issues across pytest-asyncio event loops).

    Additional patches (e.g. ``make_qdrant``, ``make_cache``) can be
    supplied via *extra_patches*.
    """
    patches = {
        f"{workflow_module}.make_datahub": datahub_client,
        f"{workflow_module}.make_llm": mock_llm(),
    }
    if db_session is not None:
        patches[f"{workflow_module}.make_db_session"] = _TestSessionWrapper(db_session)
    if extra_patches:
        patches.update(extra_patches)

    stack = ExitStack()
    for target, return_value in patches.items():
        stack.enter_context(patch(target, return_value=return_value))

    with stack:
        worker = Worker(
            temporal_client,
            task_queue=TASK_QUEUE,
            workflows=[workflow_cls],
            activities=[activity_fn],
        )
        worker_task = asyncio.create_task(worker.run())
        try:
            yield worker
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
