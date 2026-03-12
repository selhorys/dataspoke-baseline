"""DataHub client wrapper with retry and circuit breaker."""

import asyncio
import time
from typing import Any, TypeVar

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

from src.shared.config import (
    CIRCUIT_BREAKER_RESET_MS,
    CIRCUIT_BREAKER_THRESHOLD,
    RETRY_BACKOFF_BASE_MS,
    RETRY_MAX_ATTEMPTS,
)
from src.shared.exceptions import DataHubUnavailableError

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_FAIL_FAST_STATUS_CODES = {401, 403}


class DataHubClient:
    """Thin wrapper around DataHub SDK with retry and circuit breaker.

    The acryl-datahub SDK is synchronous; all calls are wrapped with
    asyncio.to_thread() to avoid blocking the event loop.
    """

    def __init__(self, gms_url: str, token: str) -> None:
        effective_token = token if token else None
        config = DatahubClientConfig(server=gms_url, token=effective_token)
        self._graph = DataHubGraph(config)
        self._emitter = DatahubRestEmitter(gms_server=gms_url, token=effective_token)
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0

    def _check_circuit(self) -> None:
        if self._consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
            return
        now = time.monotonic()
        if now < self._circuit_open_until:
            raise DataHubUnavailableError("circuit breaker open")

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.monotonic() + CIRCUIT_BREAKER_RESET_MS / 1000

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    async def _with_retry(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        self._check_circuit()
        last_exc: Exception | None = None
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                result = await asyncio.to_thread(func, *args, **kwargs)
                self._record_success()
                return result
            except Exception as exc:
                status_code = _extract_status_code(exc)
                if status_code in _FAIL_FAST_STATUS_CODES:
                    raise
                if isinstance(exc, ConnectionError) or status_code in _RETRYABLE_STATUS_CODES:
                    last_exc = exc
                    self._record_failure()
                    if attempt < RETRY_MAX_ATTEMPTS - 1:
                        wait_ms = RETRY_BACKOFF_BASE_MS * (2**attempt)
                        await asyncio.sleep(wait_ms / 1000)
                    continue
                raise
        self._record_failure()
        raise DataHubUnavailableError(str(last_exc))

    async def get_aspect(self, urn: str, aspect_class: type[T]) -> T | None:
        try:
            return await self._with_retry(self._graph.get_aspect, urn, aspect_class)
        except DataHubUnavailableError:
            raise
        except Exception as exc:
            status_code = _extract_status_code(exc)
            if status_code in _FAIL_FAST_STATUS_CODES:
                raise
            return None

    async def get_timeseries(
        self,
        urn: str,
        aspect_class: type[T],
        limit: int = 30,
        filter: dict[str, Any] | None = None,
    ) -> list[T]:
        result = await self._with_retry(
            self._graph.get_timeseries_values,
            urn,
            aspect_class,
            filter=filter or {},
            limit=limit,
        )
        return list(result) if result else []

    async def get_downstream_lineage(self, urn: str) -> list[str]:
        query = """
        query searchAcrossLineage($urn: String!) {
            searchAcrossLineage(
                input: {
                    urn: $urn,
                    direction: DOWNSTREAM,
                    count: 1000
                }
            ) {
                searchResults {
                    entity { urn }
                }
            }
        }
        """
        result = await self._with_retry(self._graph.execute_graphql, query, variables={"urn": urn})
        search_results = (result or {}).get("searchAcrossLineage", {}).get("searchResults", [])
        return [r["entity"]["urn"] for r in search_results]

    async def get_upstream_lineage(self, urn: str) -> list[str]:
        query = """
        query searchAcrossLineage($urn: String!) {
            searchAcrossLineage(
                input: {
                    urn: $urn,
                    direction: UPSTREAM,
                    count: 1000
                }
            ) {
                searchResults {
                    entity { urn }
                }
            }
        }
        """
        result = await self._with_retry(self._graph.execute_graphql, query, variables={"urn": urn})
        search_results = (result or {}).get("searchAcrossLineage", {}).get("searchResults", [])
        return [r["entity"]["urn"] for r in search_results]

    async def enumerate_datasets(self, platform: str | None = None) -> list[str]:
        extra_filters = []
        if platform:
            extra_filters.append({"field": "platform", "value": f"urn:li:dataPlatform:{platform}"})

        def _fetch() -> list[str]:
            result = self._graph.get_urns_by_filter(
                entity_types=["dataset"],
                extra_or_filters=extra_filters if extra_filters else None,
            )
            return list(result) if result else []

        return await self._with_retry(_fetch)

    async def emit_aspect(self, urn: str, aspect: Any) -> None:
        mcp = MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect)
        await self._with_retry(self._emitter.emit_mcp, mcp)

    async def check_connectivity(self) -> bool:
        try:
            await asyncio.to_thread(self._graph.test_connection)
            return True
        except Exception:
            return False


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "response_status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None
