"""Kestra REST API client — httpx wrapper for flow management and execution."""

import asyncio
import logging

import httpx

from src.workflows.kestra.errors import (
    KestraExecutionFailedError,
    KestraTimeoutError,
    parse_execution_error,
)
from src.workflows.kestra.models import ExecutionResponse, ExecutionStatus

logger = logging.getLogger(__name__)


class KestraClient:
    """Async client for Kestra REST API."""

    def __init__(
        self,
        base_url: str,
        namespace: str = "dataspoke",
        username: str = "",
        password: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        auth = httpx.BasicAuth(username, password) if username else None
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0, auth=auth)

    async def close(self) -> None:
        await self._client.aclose()

    # ── Flow management ──────────────────────────────────────────────

    async def create_or_update_flow(self, flow_yaml: str) -> dict:
        """Create or update a flow from YAML source.

        Tries PUT (update) first; falls back to POST (create) on 404.
        """
        import yaml

        flow_data = yaml.safe_load(flow_yaml)
        ns = flow_data.get("namespace", self.namespace)
        flow_id = flow_data["id"]

        # Try update first
        resp = await self._client.put(
            f"/api/v1/flows/{ns}/{flow_id}",
            content=flow_yaml,
            headers={"Content-Type": "application/x-yaml"},
        )
        if resp.status_code == 404:
            # Flow doesn't exist yet — create it
            resp = await self._client.post(
                "/api/v1/flows",
                content=flow_yaml,
                headers={"Content-Type": "application/x-yaml"},
            )
        resp.raise_for_status()
        return resp.json()

    async def get_flow(self, flow_id: str) -> dict | None:
        """Get a flow by ID, returns None if not found."""
        resp = await self._client.get(
            f"/api/v1/flows/{self.namespace}/{flow_id}"
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ── Execution management ─────────────────────────────────────────

    async def trigger_execution(
        self, flow_id: str, inputs: dict | None = None, labels: dict | None = None
    ) -> ExecutionResponse:
        """Trigger a new execution of a flow."""
        # Kestra requires multipart/form-data for execution inputs
        files: dict = {}
        if inputs:
            for key, value in inputs.items():
                files[key] = (None, str(value))

        resp = await self._client.post(
            f"/api/v1/executions/{self.namespace}/{flow_id}",
            files=files if files else None,
        )
        resp.raise_for_status()
        body = resp.json()

        # Attach labels if provided
        if labels and body.get("id"):
            await self._set_labels(body["id"], labels)

        return ExecutionResponse(**body)

    async def _set_labels(self, execution_id: str, labels: dict) -> None:
        """Set labels on an execution for deduplication queries."""
        label_list = [{"key": k, "value": v} for k, v in labels.items()]
        try:
            resp = await self._client.put(
                f"/api/v1/executions/{execution_id}/labels",
                json=label_list,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning("Failed to set labels on execution %s", execution_id)

    async def get_execution(self, execution_id: str) -> ExecutionResponse:
        """Get execution status by ID."""
        resp = await self._client.get(f"/api/v1/executions/{execution_id}")
        resp.raise_for_status()
        return ExecutionResponse(**resp.json())

    async def wait_for_execution(
        self,
        execution_id: str,
        *,
        flow_id: str = "",
        timeout_seconds: float = 300,
        poll_interval: float = 5.0,
    ) -> ExecutionResponse:
        """Poll until execution reaches a terminal state."""
        interval = max(poll_interval, 5.0)
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                raise KestraTimeoutError(flow_id, execution_id, timeout_seconds)
            execution = await self.get_execution(execution_id)
            if execution.is_terminal:
                if execution.status == ExecutionStatus.FAILED:
                    detail = parse_execution_error(execution.model_dump())
                    raise KestraExecutionFailedError(flow_id, execution_id, detail)
                return execution
            await asyncio.sleep(interval)

    async def find_running_executions(
        self, flow_id: str, label_key: str | None = None, label_value: str | None = None
    ) -> list[ExecutionResponse]:
        """Find running executions for a flow, optionally filtered by label."""
        params: dict = {
            "namespace": self.namespace,
            "flowId": flow_id,
            "state": "RUNNING",
            "size": 10,
        }
        if label_key and label_value:
            params["labels"] = f"{label_key}:{label_value}"

        resp = await self._client.get("/api/v1/executions/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results", [])
        return [ExecutionResponse(**r) for r in results]

    async def kill_execution(self, execution_id: str) -> None:
        """Kill a running execution. No-op if already terminal or not found."""
        resp = await self._client.post(f"/api/v1/executions/{execution_id}/kill")
        if resp.status_code in (404, 409):
            return  # Not found or already in terminal state
        resp.raise_for_status()

    async def delete_execution(
        self, execution_id: str, *, delete_logs: bool = True
    ) -> None:
        """Delete an execution and optionally its logs and storage."""
        resp = await self._client.delete(
            f"/api/v1/executions/{execution_id}",
            params={
                "deleteLogs": str(delete_logs).lower(),
                "deleteMetrics": str(delete_logs).lower(),
                "deleteStorage": str(delete_logs).lower(),
            },
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def find_executions(
        self,
        flow_id: str | None = None,
        state: str | None = None,
        label_key: str | None = None,
        label_value: str | None = None,
        size: int = 100,
    ) -> list[ExecutionResponse]:
        """Search executions with flexible filters."""
        params: dict = {"namespace": self.namespace, "size": size}
        if flow_id:
            params["flowId"] = flow_id
        if state:
            params["state"] = state
        if label_key and label_value:
            params["labels"] = f"{label_key}:{label_value}"

        resp = await self._client.get("/api/v1/executions/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results", [])
        return [ExecutionResponse(**r) for r in results]

    async def list_flows(self, prefix: str | None = None) -> list[dict]:
        """List flows in the namespace, optionally filtered by ID prefix.

        Uses the search endpoint which is lighter than fetching full flow
        sources via GET /api/v1/flows/{namespace}.
        """
        params: dict = {
            "namespace": self.namespace,
            "size": 100,
        }
        if prefix:
            params["q"] = prefix
        resp = await self._client.get("/api/v1/flows/search", params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        body = resp.json()
        flows = body.get("results", [])
        if prefix:
            flows = [f for f in flows if f.get("id", "").startswith(prefix)]
        return flows

    async def delete_flow(self, flow_id: str) -> None:
        """Delete a flow. No-op if not found."""
        resp = await self._client.delete(
            f"/api/v1/flows/{self.namespace}/{flow_id}"
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    # ── Convenience helpers ──────────────────────────────────────────

    async def trigger_and_wait(
        self,
        flow_id: str,
        inputs: dict | None = None,
        labels: dict | None = None,
        timeout_seconds: float = 300,
    ) -> ExecutionResponse:
        """Trigger an execution and wait for it to complete."""
        execution = await self.trigger_execution(flow_id, inputs=inputs, labels=labels)
        return await self.wait_for_execution(
            execution.id, flow_id=flow_id, timeout_seconds=timeout_seconds
        )

    async def check_no_duplicate(
        self, flow_id: str, label_key: str, label_value: str, error_code: str
    ) -> None:
        """Raise ConflictError if a running execution exists for the given label."""
        from src.shared.exceptions import ConflictError

        running = await self.find_running_executions(flow_id, label_key, label_value)
        if running:
            raise ConflictError(
                error_code, f"A {flow_id} execution is already running for {label_value}"
            )
