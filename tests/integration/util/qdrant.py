"""Qdrant reset utilities for integration tests.

Connects to the dev-env Qdrant instance and deletes all collections.
"""

from __future__ import annotations

import os
from pathlib import Path

from qdrant_client import AsyncQdrantClient

# ---------------------------------------------------------------------------
# Environment / dotenv
# ---------------------------------------------------------------------------


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

_qdrant_host = os.environ.get("DATASPOKE_QDRANT_HOST", "localhost")
_qdrant_http_port = int(os.environ.get("DATASPOKE_QDRANT_HTTP_PORT", "9203"))
_qdrant_grpc_port = int(os.environ.get("DATASPOKE_QDRANT_GRPC_PORT", "9204"))
_qdrant_api_key = os.environ.get("DATASPOKE_QDRANT_API_KEY", "")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reset_all() -> int:
    """Delete all Qdrant collections. Returns the number deleted."""
    client = AsyncQdrantClient(
        host=_qdrant_host,
        port=_qdrant_http_port,
        grpc_port=_qdrant_grpc_port,
        api_key=_qdrant_api_key if _qdrant_api_key else None,
        prefer_grpc=True,
    )
    try:
        collections = await client.get_collections()
        names = [c.name for c in collections.collections]
        for name in names:
            await client.delete_collection(name)
        return len(names)
    finally:
        await client.close()
