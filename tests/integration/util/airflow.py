"""Airflow test utilities for integration tests.

Provides helpers to ensure a clean Airflow state before and after test runs:
- Kill stale running DAG runs
- Delete test DAG runs
- AirflowClient factory from env vars
- ActivityServer: run a real HTTP server for Airflow activity callbacks
"""

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.workflows.airflow.client import AirflowClient

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load dev_env/.env into os.environ without overwriting existing vars."""
    start = Path(__file__).resolve().parents[3]
    for candidate in (start, *start.parents):
        env_path = candidate / "dev_env" / ".env"
        if env_path.is_file():
            break
    else:
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _make_client() -> AirflowClient:
    """Create an AirflowClient from environment variables."""
    return AirflowClient(
        base_url=os.environ.get("DATASPOKE_AIRFLOW_URL", "http://localhost:8080"),
        username=os.environ.get("DATASPOKE_AIRFLOW_USER", ""),
        password=os.environ.get("DATASPOKE_AIRFLOW_PASSWORD", ""),
    )


# DAG IDs that DataSpoke registers in Airflow
ALL_DAG_IDS = frozenset([
    "ingestion",
    "generation",
    "embedding-sync",
    "metrics",
    "ontology-rebuild",
])


async def kill_running_dag_runs(
    client: AirflowClient,
    dag_id: str | None = None,
    *,
    wait_seconds: float = 15.0,
    poll_interval: float = 1.0,
) -> int:
    """Kill all running DAG runs for a DAG (or all known DAGs) and wait for termination.

    Returns the number of DAG runs killed.
    """
    dag_ids = [dag_id] if dag_id else list(ALL_DAG_IDS)
    killed = 0

    for did in dag_ids:
        running = await client.find_running_dag_runs(did)
        for dag_run in running:
            try:
                await client.kill_dag_run(did, dag_run.dag_run_id)
                killed += 1
                logger.info("Killed DAG run %s (dag_id=%s)", dag_run.dag_run_id, did)
            except Exception:
                logger.warning(
                    "Failed to kill DAG run %s (dag_id=%s)",
                    dag_run.dag_run_id,
                    did,
                    exc_info=True,
                )

    if killed > 0:
        # Wait for killed runs to reach terminal state
        elapsed = 0.0
        while elapsed < wait_seconds:
            still_running = 0
            for did in dag_ids:
                still_running += len(await client.find_running_dag_runs(did))
            if still_running == 0:
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    return killed


# ── Activity Server ──────────────────────────────────────────────────────────

# Patch targets: activity endpoints import make_* from _common at module level.
# We must patch at the import site (activities module) not the definition site.
_ACTIVITIES_MODULE = "src.api.routers.internal.activities"


def _default_mock_llm() -> AsyncMock:
    m = AsyncMock()
    m.complete = AsyncMock(return_value="test response")
    m.complete_json = AsyncMock(return_value={})
    m.embed = AsyncMock(return_value=[0.0] * 1536)
    return m


def _default_mock_vector() -> AsyncMock:
    m = AsyncMock()
    m.search = AsyncMock(return_value=[])
    return m


def _default_mock_cache() -> AsyncMock:
    m = AsyncMock()
    m.get = AsyncMock(return_value=None)
    m.set = AsyncMock()
    m.publish = AsyncMock()
    m.delete = AsyncMock()
    return m


def _default_mock_notification() -> AsyncMock:
    m = AsyncMock()
    m.send_sla_alert = AsyncMock()
    return m


class ActivityServer:
    """Run a real HTTP server for Airflow activity callbacks.

    Starts a uvicorn server on a configurable port and patches the
    ``make_*`` factories in the activities module so that services
    without real infrastructure (LLM, vector, cache, notification) use
    test mocks.  DataHub and DB use real dev-env connections.

    The ``callback_url`` property returns the URL that Airflow (inside
    K8s) can use to reach this server.  On Docker Desktop Mac/Windows,
    ``host.docker.internal`` resolves to the host machine.

    Usage::

        async with ActivityServer() as server:
            # server.callback_url → "http://host.docker.internal:8000"
            # server.mock_llm, server.mock_vector, etc. are accessible
            server.mock_llm.complete_json.return_value = {"key": "val"}
            ...
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        callback_host: str | None = None,
    ):
        self.port = port or int(os.environ.get("DATASPOKE_API_PORT", "8000"))
        self.callback_host = callback_host or os.environ.get(
            "DATASPOKE_TEST_CALLBACK_HOST", "host.docker.internal"
        )

        # Exposed mocks — tests can reconfigure return values
        self.mock_llm = _default_mock_llm()
        self.mock_vector = _default_mock_vector()
        self.mock_cache = _default_mock_cache()
        self.mock_notification = _default_mock_notification()

        self._server = None
        self._thread: threading.Thread | None = None
        self._patches: list = []

    @property
    def callback_url(self) -> str:
        """URL that Airflow (in K8s) uses to reach this server."""
        return f"http://{self.callback_host}:{self.port}"

    async def start(self) -> None:
        import uvicorn

        from src.api.main import app
        from src.shared.settings import settings as _settings

        # Directly mutate the cached settings object so routers
        # pick up the test server URL (env var alone won't work
        # because the Settings singleton is already constructed).
        self._original_callback_url = _settings.airflow_callback_base_url
        _settings.airflow_callback_base_url = self.callback_url
        os.environ["DATASPOKE_AIRFLOW_CALLBACK_BASE_URL"] = self.callback_url

        # Reduce concurrent ingestion to 1 during tests to avoid
        # overwhelming the dev-env Airflow instance.
        self._original_ingestion_concurrent = _settings.airflow_ingestion_concurrent
        _settings.airflow_ingestion_concurrent = 1

        # Ensure DataHub token is available for activity endpoints.
        self._original_datahub_token = _settings.datahub_token
        if not _settings.datahub_token:
            from tests.integration.conftest import _resolve_datahub_token

            token = _resolve_datahub_token()
            if token:
                _settings.datahub_token = token

        # Patch make_* factories in the activities module
        self._patches = [
            patch(
                f"{_ACTIVITIES_MODULE}.make_llm",
                lambda: self.mock_llm,
            ),
            patch(
                f"{_ACTIVITIES_MODULE}.make_vector",
                lambda: self.mock_vector,
            ),
            patch(
                f"{_ACTIVITIES_MODULE}.make_cache",
                lambda: self.mock_cache,
            ),
            patch(
                f"{_ACTIVITIES_MODULE}.make_notification",
                lambda: self.mock_notification,
            ),
        ]
        for p in self._patches:
            p.start()

        config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Run uvicorn in a separate thread with its own event loop
        # so it doesn't deadlock with the test's event loop.
        def _run_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._server.serve())
            loop.close()

        self._thread = threading.Thread(target=_run_server, daemon=True)
        self._thread.start()

        # Wait for server to start (polling from main thread)
        for _ in range(100):
            if self._server.started:
                break
            time.sleep(0.1)

        logger.info(
            "ActivityServer started on port %d (callback=%s)",
            self.port,
            self.callback_url,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)
        for p in self._patches:
            p.stop()
        self._patches.clear()

        # Restore original settings
        from src.shared.settings import settings as _settings

        _settings.airflow_callback_base_url = self._original_callback_url
        _settings.datahub_token = self._original_datahub_token
        _settings.airflow_ingestion_concurrent = self._original_ingestion_concurrent
        os.environ.pop("DATASPOKE_AIRFLOW_CALLBACK_BASE_URL", None)
        logger.info("ActivityServer stopped")

    async def __aenter__(self) -> "ActivityServer":
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()
