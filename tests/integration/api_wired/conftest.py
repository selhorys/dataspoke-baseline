"""API-wired integration test fixtures.

Extends the root ``tests/integration/conftest.py`` (inherited automatically
by pytest).  Provides fixtures specific to REST-based testing so that spot
and story tests get a ready-to-use auth header dict without boilerplate.
"""

from unittest.mock import AsyncMock

import pytest

from tests.integration.conftest import _auth_headers


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """JWT auth headers for API-wired test requests."""
    return _auth_headers()


# ── Mock factories for activity dependencies ─────────────────────────


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


# ── Inline Kestra mock ──────────────────────────────────────────────


class InlineKestraClient:
    """Mock Kestra client that executes workflow activities inline.

    Instead of triggering a real Kestra flow (which would HTTP-callback to
    the API server), this client calls the backend service methods directly.
    This lets API-wired integration tests exercise the full route → service
    path without requiring a running HTTP server reachable from Kestra.
    """

    def __init__(self, *, datahub=None, db=None, llm=None, qdrant=None, cache=None, notification=None):
        self._datahub = datahub
        self._db = db
        self._llm = llm
        self._qdrant = qdrant
        self._cache = cache
        self._notification = notification

    async def check_no_duplicate(self, *args, **kwargs):
        pass

    async def find_running_executions(self, *args, **kwargs):
        return []

    async def trigger_and_wait(self, flow_id, inputs=None, **kwargs):
        from src.workflows.kestra.models import ExecutionResponse

        inputs = inputs or {}
        result = await self._run_flow(flow_id, inputs)
        return ExecutionResponse(
            id=f"test-{flow_id}",
            namespace="dataspoke",
            flowId=flow_id,
            state={"current": "SUCCESS"},
            outputs=result,
        )

    async def trigger_execution(self, flow_id, inputs=None, **kwargs):
        return await self.trigger_and_wait(flow_id, inputs=inputs)

    async def _run_flow(self, flow_id, inputs):
        if flow_id == "generation":
            return await self._run_generation(inputs)
        if flow_id == "ingestion":
            return await self._run_ingestion(inputs)
        if flow_id == "validation":
            return await self._run_validation(inputs)
        if flow_id == "metrics":
            return await self._run_metrics(inputs)
        return {}

    def _resolve(self, name):
        """Return provided dep or fall back to make_*() factory."""
        val = getattr(self, f"_{name}", None)
        if val is not None:
            return val
        from src.workflows import _common
        factory = getattr(_common, f"make_{name}", None)
        if factory:
            return factory()
        return None

    async def _run_generation(self, inputs):
        from src.backend.generation.service import GenerationService

        service = GenerationService(
            datahub=self._resolve("datahub"),
            db=self._db,
            llm=self._resolve("llm"),
            qdrant=self._resolve("qdrant"),
        )
        result = await service.generate(inputs["dataset_urn"])
        return {"run_id": result.run_id, "status": result.status, "detail": result.detail}

    async def _run_ingestion(self, inputs):
        import uuid

        from src.backend.ingestion.service import IngestionService

        service = IngestionService(
            datahub=self._resolve("datahub"),
            db=self._db,
            llm=self._resolve("llm"),
        )
        run_id = inputs.get("run_id", str(uuid.uuid4()))
        dry_run = inputs.get("dry_run", "false") == "true"
        extract_result = await service.extract_metadata(inputs["dataset_urn"], run_id)
        if dry_run:
            detail = {
                "dry_run": True,
                "metadata_extracted": extract_result.get("sources_processed", 0),
            }
            event_result = await service.record_ingestion_event(
                inputs["dataset_urn"], run_id, "success", detail
            )
            return event_result
        emit_result = await service.emit_metadata_to_datahub(
            inputs["dataset_urn"], extract_result
        )
        event_result = await service.record_ingestion_event(
            inputs["dataset_urn"], run_id, "success", emit_result
        )
        return event_result

    async def _run_validation(self, inputs):
        from src.backend.validation.service import ValidationService

        service = ValidationService(
            datahub=self._resolve("datahub"),
            db=self._db,
            cache=self._resolve("cache") or mock_cache(),
            llm=self._resolve("llm"),
            qdrant=self._resolve("qdrant") or mock_qdrant(),
        )
        dry_run = inputs.get("dry_run", "false") == "true"
        config_id = inputs.get("config_id") or None
        result = await service.run(inputs["dataset_urn"], config_id=config_id, dry_run=dry_run)
        return {"run_id": result.run_id, "status": result.status, "detail": result.detail}

    async def _run_metrics(self, inputs):
        from src.backend.metrics.service import MetricsService

        service = MetricsService(
            datahub=self._resolve("datahub"),
            db=self._db,
            cache=self._resolve("cache") or mock_cache(),
            notification=self._resolve("notification"),
        )
        dry_run = inputs.get("dry_run", "false") == "true"
        result = await service.run(inputs["metric_id"], dry_run=dry_run)
        return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
