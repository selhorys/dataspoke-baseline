"""Langfuse project-reset utility for integration tests.

Clears all traces in the dataspoke Langfuse project so test runs don't carry
forward observability state from prior sessions. Uses Langfuse's public REST
API (`DELETE /api/public/traces`) and skips silently when Langfuse credentials
are absent (environments without the observability subsystem installed).

Public entry point: `reset_project()` — discovers credentials from env, lists
all traces page-by-page, and bulk-deletes them.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from tests.integration.util.postgres import _load_dotenv

_load_dotenv()

_LF_HOST = os.environ.get("DATASPOKE_TEST_LANGFUSE_HOST", "")
_LF_PUBLIC_KEY = os.environ.get("DATASPOKE_TEST_LANGFUSE_PUBLIC_KEY", "")
_LF_SECRET_KEY = os.environ.get("DATASPOKE_TEST_LANGFUSE_SECRET_KEY", "")

# Langfuse caps bulk-delete payloads. 500 keeps us comfortably under any
# reasonable server-side limit while still being efficient on large projects.
_BULK_DELETE_BATCH = 500
_LIST_PAGE_SIZE = 100


def _credentials_present() -> bool:
    return bool(_LF_HOST and _LF_PUBLIC_KEY and _LF_SECRET_KEY)


async def reset_project() -> None:
    """Delete every trace in the configured Langfuse project.

    No-op when ``DATASPOKE_TEST_LANGFUSE_*`` env vars are unset — the observability
    subsystem is optional in some dev environments.
    """
    if not _credentials_present():
        print("  [SKIP] Langfuse credentials not configured")
        return

    auth = (_LF_PUBLIC_KEY, _LF_SECRET_KEY)
    async with httpx.AsyncClient(base_url=_LF_HOST, auth=auth, timeout=30.0) as client:
        trace_ids = await _list_all_trace_ids(client)
        if not trace_ids:
            print("  [INFO] Langfuse project already empty")
            return
        deleted = await _bulk_delete(client, trace_ids)
        print(f"  Deleted {deleted} Langfuse trace(s).")


async def _list_all_trace_ids(client: httpx.AsyncClient) -> list[str]:
    ids: list[str] = []
    page = 1
    while True:
        resp = await client.get(
            "/api/public/traces",
            params={"page": page, "limit": _LIST_PAGE_SIZE},
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        batch = body.get("data") or []
        ids.extend(t["id"] for t in batch if t.get("id"))
        meta = body.get("meta") or {}
        total_pages = meta.get("totalPages") or 0
        if page >= total_pages or not batch:
            return ids
        page += 1


async def _bulk_delete(client: httpx.AsyncClient, trace_ids: list[str]) -> int:
    deleted = 0
    for start in range(0, len(trace_ids), _BULK_DELETE_BATCH):
        chunk = trace_ids[start : start + _BULK_DELETE_BATCH]
        resp = await client.request(
            "DELETE",
            "/api/public/traces",
            json={"traceIds": chunk},
        )
        resp.raise_for_status()
        deleted += len(chunk)
    return deleted
