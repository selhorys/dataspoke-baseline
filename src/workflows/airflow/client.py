"""Airflow 3.x Stable REST API client — async httpx wrapper for DAG run management.

Airflow 3.x replaced HTTP Basic auth with JWT: ``POST /auth/token`` with
``{"username", "password"}`` returns a short-lived bearer token. This client
logs in lazily on first request, stamps ``Authorization: Bearer <jwt>`` on the
shared httpx client's headers, and re-logs-in once on 401.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.workflows.airflow.errors import AirflowExecutionFailedError, AirflowTimeoutError
from src.workflows.airflow.models import DagRunResponse, DagRunState, XcomEntry

logger = logging.getLogger(__name__)


class AirflowClient:
    """Async client for Airflow 3.x Stable REST API (JWT-authenticated)."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_lock: asyncio.Lock | None = None  # lazy — bound to the running loop on first use
        self._client: httpx.AsyncClient | None = None  # lazy — avoids stale anyio connection events
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared httpx client, (re-)creating it when the event loop has changed.

        httpx's connection pool holds anyio SocketStream objects that bind asyncio
        Events to the loop they were created in. If the client is re-used across
        event loops (e.g. module-scoped fixture → function-scoped test) those Events
        raise "bound to a different event loop". Creating a fresh client whenever the
        running loop changes avoids this.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        # Recreate when no client exists, OR when we know which loop the existing
        # client was bound to and that loop has changed. If _client_loop is None,
        # the client was set externally (test fixture) — trust it as-is.
        should_recreate = self._client is None or (
            self._client_loop is not None and self._client_loop is not loop
        )
        if should_recreate:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)
            self._client_loop = loop
            # Re-apply token header if we already have one.
            if self._token:
                self._client.headers["Authorization"] = f"Bearer {self._token}"
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except RuntimeError:
                # Suppress "Event loop is closed" when the client was created
                # in a different (already-closed) event loop during teardown.
                pass
        self._client = None
        self._client_loop = None

    # ── Auth ─────────────────────────────────────────────────────────────

    def _get_lock(self) -> asyncio.Lock:
        """Return the token lock, creating it lazily in the current event loop.

        asyncio.Lock() binds to the running loop at creation time. Creating it
        lazily here (rather than in __init__) avoids the "bound to a different
        event loop" RuntimeError when the client instance is created in one loop
        (e.g. a module-scoped fixture) and used in another (a function-scoped test).
        """
        if self._token_lock is None:
            self._token_lock = asyncio.Lock()
        return self._token_lock

    async def _ensure_token(self) -> None:
        """Log in if we don't yet have a JWT; stamp the shared headers."""
        if self._token:
            return
        async with self._get_lock():
            if self._token:
                return
            client = self._get_client()
            resp = await client.post(
                "/auth/token",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token") or data.get("token")
            if not token:
                raise RuntimeError(
                    f"Airflow /auth/token response missing access_token: {data!r}"
                )
            self._token = token
            client.headers["Authorization"] = f"Bearer {token}"

    async def _invalidate_token(self) -> None:
        async with self._get_lock():
            self._token = None
            if self._client is not None:
                self._client.headers.pop("Authorization", None)

    async def _authed_call(self, call):
        """Run an async httpx call with JWT; refresh and retry once on 401."""
        await self._ensure_token()
        resp = await call()
        if resp.status_code == 401:
            await self._invalidate_token()
            await self._ensure_token()
            resp = await call()
        return resp

    # ── DAG run management ───────────────────────────────────────────────

    async def trigger_dag_run(
        self, dag_id: str, conf: dict | None = None
    ) -> DagRunResponse:
        """Trigger a new DAG run.

        POST /api/v2/dags/{dag_id}/dagRuns with JSON body {"conf": conf,
        "logical_date": now}. Airflow 3.x requires logical_date in the body;
        we default to the current UTC timestamp when the caller doesn't
        provide one via conf.
        """
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        body: dict[str, Any] = {"conf": conf or {}, "logical_date": now_iso}
        resp = await self._authed_call(
            lambda: self._get_client().post(f"/api/v2/dags/{dag_id}/dagRuns", json=body)
        )
        resp.raise_for_status()
        return DagRunResponse(**resp.json())

    async def get_dag_run(self, dag_id: str, dag_run_id: str) -> DagRunResponse:
        """Get a DAG run by ID."""
        resp = await self._authed_call(
            lambda: self._get_client().get(f"/api/v2/dags/{dag_id}/dagRuns/{dag_run_id}")
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
        resp = await self._authed_call(
            lambda: self._get_client().get(
                f"/api/v2/dags/{dag_id}/dagRuns",
                params={"state": "running", "limit": 25},
            )
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        body = resp.json()
        runs = body.get("dag_runs", [])
        return [DagRunResponse(**r) for r in runs]

    async def find_active_dag_runs(self, dag_id: str) -> list[DagRunResponse]:
        """Find all active DAG runs (running or queued) for the given dag_id."""
        active: list[DagRunResponse] = []
        for state in ("running", "queued"):
            resp = await self._authed_call(
                lambda s=state: self._get_client().get(
                    f"/api/v2/dags/{dag_id}/dagRuns",
                    params={"state": s, "limit": 25},
                )
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            runs = resp.json().get("dag_runs", [])
            active.extend(DagRunResponse(**r) for r in runs)
        return active

    async def kill_dag_run(self, dag_id: str, dag_run_id: str) -> None:
        """Mark a running DAG run as failed. No-op if already terminal or not found."""
        resp = await self._authed_call(
            lambda: self._get_client().patch(
                f"/api/v2/dags/{dag_id}/dagRuns/{dag_run_id}",
                json={"state": "failed"},
            )
        )
        if resp.status_code in (404, 409):
            return
        resp.raise_for_status()

    async def delete_dag_run(self, dag_id: str, dag_run_id: str) -> None:
        """Delete a DAG run. No-op if not found."""
        resp = await self._authed_call(
            lambda: self._get_client().delete(f"/api/v2/dags/{dag_id}/dagRuns/{dag_run_id}")
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def fetch_task_xcom(
        self,
        dag_id: str,
        dag_run_id: str,
        task_id: str,
        key: str = "return_value",
    ) -> Any:
        """Fetch a single XCom value produced by a task instance.

        Returns the deserialized ``value`` field from the XCom entry, or
        ``None`` when the entry does not exist (Airflow returns 404 before
        the task pushes a value, or when the task never pushed).

        Airflow REST: GET /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/{task_id}/xcomEntries/{key}
        """
        path = (
            f"/api/v2/dags/{dag_id}/dagRuns/{dag_run_id}"
            f"/taskInstances/{task_id}/xcomEntries/{key}"
        )
        resp = await self._authed_call(lambda: self._get_client().get(path))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        entry = XcomEntry(**resp.json())
        return entry.parsed_value

    async def list_dags(self, prefix: str | None = None) -> list[dict]:
        """List DAGs, optionally filtered by ID pattern prefix.

        Uses GET /api/v2/dags with optional dag_id_pattern param.
        """
        params: dict = {"limit": 100}
        if prefix:
            params["dag_id_pattern"] = prefix
        resp = await self._authed_call(
            lambda: self._get_client().get("/api/v2/dags", params=params)
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        body = resp.json()
        dags = body.get("dags", [])
        if prefix:
            dags = [d for d in dags if d.get("dag_id", "").startswith(prefix)]
        return dags
