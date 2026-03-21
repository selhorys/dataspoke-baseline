"""Kestra test utilities for integration tests.

Provides helpers to ensure a clean Kestra state before and after test runs:
- Kill stale running executions
- Delete test executions
- Register/verify flows
- ActivityServer: run a real HTTP server for Kestra activity callbacks
"""

import asyncio
import logging
import os
import threading
import time
from unittest.mock import AsyncMock, patch

from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.models import ExecutionStatus
from src.workflows.kestra.registry import register_all_flows

logger = logging.getLogger(__name__)

# Flow IDs defined in src/workflows/flows/*.yaml
ALL_FLOW_IDS = frozenset([
    "ingestion",
    "validation",
    "generation",
    "embedding-sync",
    "metrics",
    "ontology-rebuild",
    "sla-monitor",
])


async def kill_running_executions(
    client: KestraClient,
    flow_id: str | None = None,
    *,
    wait_seconds: float = 15.0,
    poll_interval: float = 1.0,
) -> int:
    """Kill all running executions for a flow (or all flows) and wait for termination.

    Returns the number of executions killed.
    """
    flow_ids = [flow_id] if flow_id else list(ALL_FLOW_IDS)
    killed = 0

    for fid in flow_ids:
        running = await client.find_running_executions(fid)
        for execution in running:
            try:
                await client.kill_execution(execution.id)
                killed += 1
                logger.info("Killed execution %s (flow=%s)", execution.id, fid)
            except Exception:
                logger.warning(
                    "Failed to kill execution %s (flow=%s)",
                    execution.id,
                    fid,
                    exc_info=True,
                )

    if killed > 0:
        # Wait for killed executions to reach terminal state
        elapsed = 0.0
        while elapsed < wait_seconds:
            still_running = 0
            for fid in flow_ids:
                still_running += len(await client.find_running_executions(fid))
            if still_running == 0:
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    return killed


async def cleanup_test_executions(
    client: KestraClient,
    flow_id: str | None = None,
    *,
    label_prefix: str = "test-",
) -> int:
    """Find and delete test executions (identified by label prefix).

    First kills running test executions, then deletes all matching.
    Returns the number of executions deleted.
    """
    flow_ids = [flow_id] if flow_id else list(ALL_FLOW_IDS)
    deleted = 0

    for fid in flow_ids:
        executions = await client.find_executions(flow_id=fid, size=100)
        for execution in executions:
            # Check if this is a test execution via labels
            labels = execution.model_dump().get("labels") or []
            is_test = any(
                isinstance(label, dict)
                and str(label.get("value", "")).startswith(label_prefix)
                for label in labels
            )
            if not is_test:
                # Also check workflow_id label specifically
                wf_labels = [
                    label
                    for label in labels
                    if isinstance(label, dict) and label.get("key") == "workflow_id"
                ]
                is_test = any(
                    str(label.get("value", "")).startswith(label_prefix)
                    for label in wf_labels
                )

            if is_test:
                if not execution.is_terminal:
                    try:
                        await client.kill_execution(execution.id)
                        # Brief wait for kill to propagate
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                try:
                    await client.delete_execution(execution.id)
                    deleted += 1
                except Exception:
                    logger.warning(
                        "Failed to delete execution %s", execution.id, exc_info=True
                    )

    return deleted


async def cleanup_flows(client: KestraClient) -> int:
    """Delete all DataSpoke flows from the test namespace.

    Returns the number of flows deleted.
    """
    deleted = 0
    for flow_id in ALL_FLOW_IDS:
        try:
            flow = await client.get_flow(flow_id)
            if flow is not None:
                await client.delete_flow(flow_id)
                deleted += 1
                logger.info("Deleted flow %s", flow_id)
        except Exception:
            logger.warning("Failed to delete flow %s", flow_id, exc_info=True)
    return deleted


async def ensure_flows_registered(client: KestraClient) -> int:
    """Register all DataSpoke flows and return the count."""
    return await register_all_flows(client)


async def verify_flows_registered(client: KestraClient) -> list[str]:
    """Check which flows are registered and return their IDs."""
    registered = []
    for flow_id in ALL_FLOW_IDS:
        flow = await client.get_flow(flow_id)
        if flow is not None:
            registered.append(flow_id)
    return registered


async def wait_for_execution_terminal(
    client: KestraClient,
    execution_id: str,
    *,
    timeout_seconds: float = 60,
    poll_interval: float = 1.0,
) -> ExecutionStatus:
    """Wait for an execution to reach any terminal state without raising on failure.

    Unlike KestraClient.wait_for_execution, this does NOT raise on FAILED status.
    Returns the terminal ExecutionStatus.
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        execution = await client.get_execution(execution_id)
        if execution.is_terminal:
            return execution.status
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return ExecutionStatus.RUNNING  # Still running after timeout


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


def _default_mock_qdrant() -> AsyncMock:
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
    """Run a real HTTP server for Kestra activity callbacks.

    Starts a uvicorn server on a configurable port and patches the
    ``make_*`` factories in the activities module so that services
    without real infrastructure (LLM, Qdrant, cache, notification) use
    test mocks.  DataHub and DB use real dev-env connections.

    The ``callback_url`` property returns the URL that Kestra (inside
    K8s) can use to reach this server.  On Docker Desktop Mac/Windows,
    ``host.docker.internal`` resolves to the host machine.

    Usage::

        async with ActivityServer() as server:
            # server.callback_url → "http://host.docker.internal:8765"
            # server.mock_llm, server.mock_qdrant, etc. are accessible
            server.mock_llm.complete_json.return_value = {"key": "val"}
            ...
    """

    def __init__(
        self,
        *,
        port: int = 8765,
        callback_host: str | None = None,
    ):
        self.port = port
        self.callback_host = callback_host or os.environ.get(
            "DATASPOKE_TEST_CALLBACK_HOST", "host.docker.internal"
        )

        # Exposed mocks — tests can reconfigure return values
        self.mock_llm = _default_mock_llm()
        self.mock_qdrant = _default_mock_qdrant()
        self.mock_cache = _default_mock_cache()
        self.mock_notification = _default_mock_notification()

        self._server = None
        self._thread: threading.Thread | None = None
        self._patches: list = []

    @property
    def callback_url(self) -> str:
        """URL that Kestra (in K8s) uses to reach this server."""
        return f"http://{self.callback_host}:{self.port}"

    async def start(self) -> None:
        import uvicorn

        from src.api.main import app
        from src.shared.settings import settings as _settings

        # Directly mutate the cached settings object so routers
        # pick up the test server URL (env var alone won't work
        # because the Settings singleton is already constructed).
        self._original_callback_url = _settings.kestra_callback_base_url
        _settings.kestra_callback_base_url = self.callback_url
        os.environ["DATASPOKE_KESTRA_CALLBACK_BASE_URL"] = (
            self.callback_url
        )

        # Ensure DataHub token is available for activity endpoints.
        # The test conftest uses a session-token fallback that the
        # activities' make_datahub() doesn't have.
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
                f"{_ACTIVITIES_MODULE}.make_qdrant",
                lambda: self.mock_qdrant,
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

        self._thread = threading.Thread(
            target=_run_server, daemon=True
        )
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

        _settings.kestra_callback_base_url = self._original_callback_url
        _settings.datahub_token = self._original_datahub_token
        os.environ.pop("DATASPOKE_KESTRA_CALLBACK_BASE_URL", None)
        logger.info("ActivityServer stopped")

    async def __aenter__(self) -> "ActivityServer":
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()
