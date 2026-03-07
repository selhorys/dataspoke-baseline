"""DataHub client wrapper with retry and circuit breaker."""

import asyncio
import logging
import time
from typing import Any, TypeVar

from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

from src.shared.config import (
    CIRCUIT_BREAKER_RESET_MS,
    CIRCUIT_BREAKER_THRESHOLD,
    RETRY_BACKOFF_BASE_MS,
    RETRY_MAX_ATTEMPTS,
)
from src.shared.exceptions import DataHubUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _CircuitBreaker:
    """Simple counter-based circuit breaker."""

    def __init__(self, threshold: int, reset_ms: int) -> None:
        self._threshold = threshold
        self._reset_ms = reset_ms
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        elapsed_ms = (time.monotonic() - self._opened_at) * 1000
        return elapsed_ms < self._reset_ms

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            if self._failure_count >= self._threshold:
                self._opened_at = time.monotonic()

    async def check(self) -> None:
        if self.is_open:
            raise DataHubUnavailableError("Circuit breaker is open")


class DataHubClient:
    """Unified DataHub read/write client with retry and circuit breaker."""

    def __init__(self, gms_url: str, token: str) -> None:
        self._gms_url = gms_url
        self._token = token
        self._graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=token or None))
        self._emitter = DatahubRestEmitter(gms_server=gms_url, token=token or None)
        self._breaker = _CircuitBreaker(
            threshold=CIRCUIT_BREAKER_THRESHOLD,
            reset_ms=CIRCUIT_BREAKER_RESET_MS,
        )

    async def _call_with_retry(self, func, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Run a sync SDK function in a thread with retry + circuit breaker."""
        await self._breaker.check()

        last_exc: Exception | None = None
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                result = await asyncio.to_thread(func, *args, **kwargs)
                await self._breaker.record_success()
                return result
            except Exception as exc:
                last_exc = exc
                await self._breaker.record_failure()
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    delay_s = (RETRY_BACKOFF_BASE_MS / 1000) * (2**attempt)
                    logger.warning(
                        "DataHub call failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        RETRY_MAX_ATTEMPTS,
                        delay_s,
                        exc,
                    )
                    await asyncio.sleep(delay_s)

        raise DataHubUnavailableError(
            f"DataHub call failed after {RETRY_MAX_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    # ── Read ──────────────────────────────────────────────────────────────

    async def get_aspect(self, urn: str, aspect_class: type[T]) -> T | None:
        """Read a single aspect from DataHub. Returns None if not found."""
        result = await self._call_with_retry(
            self._graph.get_aspect, entity_urn=urn, aspect_type=aspect_class
        )
        return result

    async def get_timeseries(self, urn: str, aspect_class: type[T], limit: int = 30) -> list[T]:
        """Read timeseries aspects for an entity."""
        result = await self._call_with_retry(
            self._graph.get_aspects_for_entity,
            entity_urn=urn,
            aspects=[aspect_class.ASPECT_NAME],
            aspect_types=[aspect_class],
        )
        if result is None:
            return []
        aspects = result.get(aspect_class.ASPECT_NAME, [])
        return aspects[:limit] if isinstance(aspects, list) else [aspects]

    async def get_downstream_lineage(self, urn: str) -> list[str]:
        """Get downstream entity URNs from lineage graph."""
        related = await self._call_with_retry(
            self._graph.get_related_entities,
            entity_urn=urn,
            relationship_types=["DownstreamOf"],
            direction="INCOMING",
        )
        return [r.urn for r in (related or [])]

    async def enumerate_datasets(self, platform: str | None = None) -> list[str]:
        """List all dataset URNs, optionally filtered by platform."""
        filter_str = f"platform:{platform}" if platform else None
        urns = await self._call_with_retry(
            self._graph.get_urns_by_filter,
            entity_type="dataset",
            platform=filter_str,
        )
        return list(urns or [])

    # ── Write ─────────────────────────────────────────────────────────────

    async def emit_aspect(self, urn: str, aspect: Any) -> None:
        """Emit (write) an aspect to DataHub."""
        from datahub.emitter.mcp import MetadataChangeProposalWrapper

        mcp = MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect)
        await self._call_with_retry(self._emitter.emit, mcp)

    # ── Health ────────────────────────────────────────────────────────────

    async def check_connectivity(self) -> bool:
        """Check if DataHub GMS is reachable."""
        try:
            await self._call_with_retry(self._graph.test_connection)
            return True
        except DataHubUnavailableError:
            return False
