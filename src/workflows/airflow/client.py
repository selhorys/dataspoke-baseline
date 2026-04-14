"""Airflow Stable REST API client — async httpx wrapper for DAG run management."""

import asyncio
import logging

import httpx

from src.workflows.airflow.errors import AirflowExecutionFailedError, AirflowTimeoutError
from src.workflows.airflow.models import DagRunResponse, DagRunState

logger = logging.getLogger(__name__)


class AirflowClient:
    """Async client for Airflow Stable REST API."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        auth = httpx.BasicAuth(username, password) if username else None
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0, auth=auth)

    async def close(self) -> None:
        await self._client.aclose()

    # ── DAG run management ───────────────────────────────────────────────

    async def trigger_dag_run(
        self, dag_id: str, conf: dict | None = None
    ) -> DagRunResponse:
        """Trigger a new DAG run.

        POST /api/v1/dags/{dag_id}/dagRuns with JSON body {"conf": conf}.
        """
        body: dict = {"conf": conf or {}}
        resp = await self._client.post(
            f"/api/v1/dags/{dag_id}/dagRuns",
            json=body,
        )
        resp.raise_for_status()
        return DagRunResponse(**resp.json())

    async def get_dag_run(self, dag_id: str, dag_run_id: str) -> DagRunResponse:
        """Get a DAG run by ID."""
        resp = await self._client.get(
            f"/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}"
        )
        resp.raise_for_status()
        return DagRunResponse(**resp.json())

    async def wait_for_dag_run(
        self,
        dag_id: str,
        dag_run_id: str,
        *,
        timeout_seconds: float = 300,
        poll_interval: float = 5.0,
    ) -> DagRunResponse:
        """Poll until the DAG run reaches a terminal state."""
        interval = max(poll_interval, 5.0)
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                raise AirflowTimeoutError(dag_id, dag_run_id, timeout_seconds)
            dag_run = await self.get_dag_run(dag_id, dag_run_id)
            if dag_run.is_terminal:
                if dag_run.state == DagRunState.failed:
                    raise AirflowExecutionFailedError(dag_id, dag_run_id)
                return dag_run
            await asyncio.sleep(interval)

    async def trigger_and_wait(
        self,
        dag_id: str,
        conf: dict | None = None,
        timeout_seconds: float = 300,
    ) -> DagRunResponse:
        """Trigger a DAG run and wait for it to complete."""
        dag_run = await self.trigger_dag_run(dag_id, conf=conf)
        return await self.wait_for_dag_run(
            dag_id, dag_run.dag_run_id, timeout_seconds=timeout_seconds
        )

    async def check_no_duplicate(
        self, dag_id: str, conf_key: str, conf_value: str, error_code: str
    ) -> None:
        """Raise ConflictError if a running DAG run exists with the given conf value."""
        from src.shared.exceptions import ConflictError

        running = await self.find_running_dag_runs(dag_id)
        for dag_run in running:
            if dag_run.conf.get(conf_key) == conf_value:
                raise ConflictError(
                    error_code,
                    f"A {dag_id} DAG run is already running for {conf_value}",
                )

    async def find_running_dag_runs(self, dag_id: str) -> list[DagRunResponse]:
        """Find all running DAG runs for the given dag_id."""
        resp = await self._client.get(
            f"/api/v1/dags/{dag_id}/dagRuns",
            params={"state": "running", "limit": 25},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        body = resp.json()
        runs = body.get("dag_runs", [])
        return [DagRunResponse(**r) for r in runs]

    async def kill_dag_run(self, dag_id: str, dag_run_id: str) -> None:
        """Mark a running DAG run as failed. No-op if already terminal or not found."""
        resp = await self._client.patch(
            f"/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}",
            json={"state": "failed"},
        )
        if resp.status_code in (404, 409):
            return
        resp.raise_for_status()

    async def delete_dag_run(self, dag_id: str, dag_run_id: str) -> None:
        """Delete a DAG run. No-op if not found."""
        resp = await self._client.delete(
            f"/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}"
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def list_dags(self, prefix: str | None = None) -> list[dict]:
        """List DAGs, optionally filtered by ID pattern prefix.

        Uses GET /api/v1/dags with optional dag_id_pattern param.
        """
        params: dict = {"limit": 100}
        if prefix:
            params["dag_id_pattern"] = prefix
        resp = await self._client.get("/api/v1/dags", params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        body = resp.json()
        dags = body.get("dags", [])
        if prefix:
            dags = [d for d in dags if d.get("dag_id", "").startswith(prefix)]
        return dags
