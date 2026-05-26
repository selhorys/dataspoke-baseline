"""DataHub client wrapper with retry and circuit breaker."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import SystemMetadataClass

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
_DOC_HEALTH_BATCH_SIZE = 100


@dataclass(frozen=True)
class DocumentationAspects:
    """Four documentation aspects for a single dataset, collapsed from DataHub.

    table_description:           DatasetProperties.description (base)
    editable_table_description:  EditableDatasetProperties.description (overlay)
    field_descriptions:          All schemaMetadata fields — fieldPath → description (empty str when absent)
    editable_field_descriptions: EditableSchemaMetadata fields that have a description set — fieldPath → description

    When ``field_descriptions`` is empty the dataset has no schema metadata; the
    measurer treats this as "no documentable columns → score 0.0".
    """

    table_description: str | None
    editable_table_description: str | None
    field_descriptions: dict[str, str] = field(default_factory=dict)
    editable_field_descriptions: dict[str, str] = field(default_factory=dict)


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
                    types: [DATASET],
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
                    types: [DATASET],
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

    async def get_dataset_documentation_aspects(
        self,
        urns: list[str],
    ) -> dict[str, "DocumentationAspects"]:
        """Batch-fetch the four documentation aspects for a list of dataset URNs.

        Issues one GraphQL call per page of up to 100 URNs, replacing the four
        per-URN REST calls used by single-aspect reads. The returned dict always
        contains an entry for every requested URN; URNs not found in DataHub
        receive an all-empty DocumentationAspects so the caller is branch-free.

        DataHub docs: https://docs.datahub.com/docs/graphql/queries#entities
        """
        _QUERY = """
        query DocAspects($urns: [String!]!) {
            entities(urns: $urns) {
                urn
                ... on Dataset {
                    properties { description }
                    editableProperties { description }
                    schemaMetadata { fields { fieldPath description } }
                    editableSchemaMetadata {
                        editableSchemaFieldInfo { fieldPath description }
                    }
                }
            }
        }
        """
        _EMPTY = DocumentationAspects(
            table_description=None,
            editable_table_description=None,
            field_descriptions={},
            editable_field_descriptions={},
        )

        result: dict[str, DocumentationAspects] = {urn: _EMPTY for urn in urns}

        for i in range(0, len(urns), _DOC_HEALTH_BATCH_SIZE):
            page = urns[i : i + _DOC_HEALTH_BATCH_SIZE]
            raw = await self._with_retry(
                self._graph.execute_graphql, _QUERY, variables={"urns": page}
            )

            for entity in (raw or {}).get("entities", []) or []:
                urn = entity.get("urn")
                if not urn:
                    continue

                table_description: str | None = (
                    (entity.get("properties") or {}).get("description") or None
                )
                editable_table_description: str | None = (
                    (entity.get("editableProperties") or {}).get("description") or None
                )

                schema_metadata = entity.get("schemaMetadata")
                # Include all schema fields (even undescribed) so the measurer can
                # identify missing column descriptions.
                field_descriptions: dict[str, str] = {
                    f["fieldPath"]: f.get("description") or ""
                    for f in (schema_metadata or {}).get("fields", []) or []
                    if f.get("fieldPath")
                }
                # Editable overlay: only fields that actually have a description set.
                editable_field_descriptions: dict[str, str] = {
                    f["fieldPath"]: f["description"]
                    for f in (entity.get("editableSchemaMetadata") or {}).get(
                        "editableSchemaFieldInfo", []
                    ) or []
                    if f.get("description")
                }

                result[urn] = DocumentationAspects(
                    table_description=table_description,
                    editable_table_description=editable_table_description,
                    field_descriptions=field_descriptions,
                    editable_field_descriptions=editable_field_descriptions,
                )

        return result

    async def get_schema_version_list(self, urn: str) -> list[dict[str, Any]]:
        """Return schema version list via the Timeline GraphQL API.

        Each entry has keys: semanticVersion (str), semanticVersionTimestamp (int ms).
        """
        query = """
        query getSchemaVersionList($input: GetSchemaVersionListInput!) {
            getSchemaVersionList(input: $input) {
                semanticVersionList {
                    semanticVersion
                    semanticVersionTimestamp
                }
            }
        }
        """
        try:
            result = await self._with_retry(
                self._graph.execute_graphql, query, variables={"input": {"datasetUrn": urn}}
            )
        except Exception:
            return []
        inner = (result or {}).get("getSchemaVersionList") or {}
        version_list = inner.get("semanticVersionList") or []
        return [v for v in version_list if v.get("semanticVersion")]

    async def enumerate_datasets(
        self,
        platform: str | None = None,
        tags: list[str] | None = None,
        glossary_terms: list[str] | None = None,
        origin: str | None = None,
    ) -> list[str]:
        """Return all dataset URNs matching the given filters.

        Tag / glossary-term / platform filters are OR-ed; ``origin`` is AND-ed
        with each OR clause. When ``origin`` is provided and there are no other
        OR-clause dimensions, a single AND clause with just origin is emitted.
        """
        origin_clause: dict | None = (
            {"field": "origin", "value": origin} if origin else None
        )

        or_groups: list[dict] = []
        if platform:
            and_clauses: list[dict] = [
                {"field": "platform", "value": f"urn:li:dataPlatform:{platform}"}
            ]
            if origin_clause:
                and_clauses.append(origin_clause)
            or_groups.append({"and": and_clauses})
        for tag in (tags or []):
            and_clauses = [{"field": "tags", "value": tag}]
            if origin_clause:
                and_clauses.append(origin_clause)
            or_groups.append({"and": and_clauses})
        for term in (glossary_terms or []):
            and_clauses = [{"field": "glossaryTerms", "value": term}]
            if origin_clause:
                and_clauses.append(origin_clause)
            or_groups.append({"and": and_clauses})

        # When no OR-dimension filters are given but origin is set, emit a
        # single AND group so DataHub scopes the enumeration to that origin.
        if not or_groups and origin_clause:
            or_groups.append({"and": [origin_clause]})

        def _fetch() -> list[str]:
            result = self._graph.get_urns_by_filter(
                entity_types=["dataset"],
                extra_or_filters=or_groups if or_groups else None,
            )
            return list(result) if result else []

        return await self._with_retry(_fetch)

    def origin_from_dataset_urn(self, urn: str) -> str | None:
        """Parse the origin (third segment) from a dataset URN.

        Dataset URN format: ``urn:li:dataset:(<platform>,<name>,<origin>)``.
        The platform itself may be a nested URN containing commas, so we split
        on the last two commas inside the outer parentheses.

        Returns the origin string, or ``None`` when the URN is malformed.
        """
        if not urn.startswith("urn:li:dataset:(") or not urn.endswith(")"):
            return None
        inner = urn[len("urn:li:dataset:("):-1]
        # Find the last two commas to extract the three segments
        last_comma = inner.rfind(",")
        if last_comma == -1:
            return None
        second_last_comma = inner.rfind(",", 0, last_comma)
        if second_last_comma == -1:
            return None
        origin = inner[last_comma + 1:].strip()
        return origin if origin else None

    async def emit_aspect(
        self,
        urn: str,
        aspect: Any,
        system_metadata: SystemMetadataClass | None = None,
    ) -> None:
        mcp = MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=aspect,
            systemMetadata=system_metadata,
        )
        await self._with_retry(self._emitter.emit_mcp, mcp)

    async def emit_assertion(
        self,
        assertion_urn: str,
        aspect: Any,
        system_metadata: SystemMetadataClass | None = None,
    ) -> None:
        """Emit an aspect to an assertion entity."""
        mcp = MetadataChangeProposalWrapper(
            entityUrn=assertion_urn,
            aspect=aspect,
            systemMetadata=system_metadata,
        )
        await self._with_retry(self._emitter.emit_mcp, mcp)

    async def get_assertion_info(self, assertion_urn: str) -> Any | None:
        """Check if an assertion definition exists in DataHub."""
        from datahub.metadata.schema_classes import AssertionInfoClass

        return await self.get_aspect(assertion_urn, AssertionInfoClass)

    async def emit_mcp(self, mcp: MetadataChangeProposalWrapper) -> None:
        """Emit a single MCP through the REST emitter with retry."""
        await self._with_retry(self._emitter.emit_mcp, mcp)

    async def execute_graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query/mutation with retry."""
        return await self._with_retry(
            self._graph.execute_graphql, query, variables=variables or {}
        )

    async def hard_delete_entity(self, urn: str) -> None:
        """Hard-delete a DataHub entity and all its references with retry."""
        await self._with_retry(self._graph.hard_delete_entity, urn)

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
