"""DataHub client wrapper with retry and circuit breaker."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

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
from src.shared.redaction import sanitize_error_message

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_FAIL_FAST_STATUS_CODES = {401, 403}
_DOC_HEALTH_BATCH_SIZE = 100
# Cursor-paged reads have no intrinsic bound (unlike the ``total``-bounded ones),
# and they run on the API pod's event loop. Stop after this many pages.
_SCROLL_MAX_PAGES = 100
# DataHub's reserved internal pipeline types. Their CLI wrappers (executorId
# "__datahub_cli_") are NOT tagged sourceType=SYSTEM, so the GraphQL filter
# alone misses them — deny by type here too.
_SYSTEM_SOURCE_TYPES: frozenset[str] = frozenset({"datahub-gc", "datahub-documents"})


@dataclass(frozen=True)
class DocumentationAspects:
    """Four documentation aspects for a single dataset, collapsed from DataHub.

    table_description:           DatasetProperties.description (base)
    editable_table_description:  EditableDatasetProperties.description (overlay)
    field_descriptions:          All schemaMetadata fields — fieldPath → description
                                 (empty str when absent)
    editable_field_descriptions: EditableSchemaMetadata fields that have a
                                 description set — fieldPath → description

    When ``field_descriptions`` is empty the dataset has no schema metadata; the
    measurer treats this as "no documentable columns → score 0.0".
    """

    table_description: str | None
    editable_table_description: str | None
    field_descriptions: dict[str, str] = field(default_factory=dict)
    editable_field_descriptions: dict[str, str] = field(default_factory=dict)


def _url_password(url: str) -> str | None:
    """Return the password component of *url*'s userinfo, if it has one."""
    try:
        return urlsplit(url).password
    except ValueError:
        return None


class DataHubClient:
    """Thin wrapper around DataHub SDK with retry and circuit breaker.

    The acryl-datahub SDK is synchronous; all calls are wrapped with
    asyncio.to_thread() to avoid blocking the event loop.

    A ``DataHubUnavailableError`` carries the transport's own ``str(exc)``, which
    can quote the failed request. That message reaches three sinks with three
    different reader populations — the ``peripheral_health.last_error`` column an
    Admin reads back, the internal activity's ``500`` body Airflow keeps in its
    task logs, and the API's own logs — so it is sanitized here, at the one
    boundary where the transport's string becomes a DataSpoke-owned message,
    rather than once per sink. This client is also the only place that holds the
    live credential, so it can scrub it by exact value instead of by pattern.

    The ``401``/``403`` fail-fast path re-raises the SDK's own exception rather
    than wrapping it, deliberately: an auth failure is a configuration fault, not
    unavailability, and callers distinguish the two by type. That exception
    therefore never passes through this boundary even though it is the failure
    most likely to quote the PAT, so :meth:`sanitize` is public — a caller that
    reports an arbitrary DataHub exception (``IngestionService.sync``) routes the
    text through it to get the same exact-value scrub.
    """

    def __init__(self, gms_url: str, token: str) -> None:
        effective_token = token if token else None
        config = DatahubClientConfig(server=gms_url, token=effective_token)
        self._graph = DataHubGraph(config)
        self._emitter = DatahubRestEmitter(gms_server=gms_url, token=effective_token)
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0
        # Exact values to scrub from any reported transport message: the PAT, and
        # the password half of the GMS URL's userinfo when one is configured
        # (gms_url accepts a userinfo component). The host is left intact — it is
        # not a secret and removing it would gut the message's diagnostic value.
        self._redact_values: tuple[str, ...] = tuple(
            v for v in (token, _url_password(gms_url)) if v
        )

    def sanitize(self, message: str) -> str:
        """Scrub this client's live credentials out of an operator-facing message."""
        return sanitize_error_message(message, secrets=self._redact_values) or ""

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
        raise DataHubUnavailableError(self.sanitize(str(last_exc)))

    async def get_aspect(
        self,
        urn: str,
        aspect_class: type[T],
        *,
        strict: bool = False,
    ) -> T | None:
        """Read *aspect_class* off *urn*. Returns None when the aspect is absent.

        Default behavior swallows non-fail-fast, non-retryable exceptions and
        returns None — callers that degrade gracefully (e.g. evidence/dataset
        readers) rely on this.

        With ``strict=True``, those exceptions surface as
        ``DataHubUnavailableError`` instead — used by audit-critical readers
        like ``read_role`` so a transient read failure is not mis-recorded as
        a legitimate "no aspect" observation.
        """
        try:
            return await self._with_retry(self._graph.get_aspect, urn, aspect_class)  # type: ignore[no-any-return]  # DataHub SDK get_aspect returns Any.
        except DataHubUnavailableError:
            raise
        except Exception as exc:
            status_code = _extract_status_code(exc)
            if status_code in _FAIL_FAST_STATUS_CODES:
                raise
            if strict:
                raise DataHubUnavailableError(self.sanitize(str(exc))) from exc
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

    async def enumerate_datasets(self) -> list[str]:
        """Return every dataset URN DataHub holds.

        The estate enumeration behind the sync sweep's ``dataset_registry``
        reconcile and the on-demand scoped reconcile. It takes no filter
        dimensions: ``dataset_filter`` scope is resolved DataSpoke-side by one
        SQL query over the registry mirror, never by a DataHub search
        (spec/DATAHUB_INTEGRATION.md §Dataset attribute sync).
        """

        def _fetch() -> list[str]:
            result = self._graph.get_urns_by_filter(entity_types=["dataset"])
            return list(result) if result else []

        return await self._with_retry(_fetch)  # type: ignore[no-any-return]  # DataHub SDK returns Any.

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

    async def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query/mutation with retry."""
        return await self._with_retry(  # type: ignore[no-any-return]  # GraphQL response is untyped JSON.
            self._graph.execute_graphql, query, variables=variables or {}
        )

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        """Return non-system (user-facing) DataHub-managed ingestion sources.

        Paginates listIngestionSources until all pages are consumed. System
        sources are excluded by two layers: a negated ``sourceType=SYSTEM``
        GraphQL filter, plus a deny-list on the reserved system source types
        (``datahub-gc``, ``datahub-documents``). The deny-list is required
        because DataHub auto-creates CLI wrappers for those pipelines that are
        NOT tagged ``sourceType=SYSTEM`` and would otherwise slip through. The
        result matches the set of sources shown in DataHub's Manage Data
        Sources view.

        Each returned dict contains:
          - urn (str): the dataHubIngestionSource URN
          - name (str): display name
          - type (str): source type (e.g. "postgres", "mysql")
          - schedule (dict | None): {"interval": str, "timezone": str | None}
          - recipe (str): the raw JSON recipe string as DataHub returned it
            (secrets may be raw plaintext — the caller is responsible for masking
            before persisting)
          - executor_id (str | None): the source's configured executorId, used
            to classify CLI/ad-hoc sources (``__datahub_cli_*``)

        Raises:
            DataHubUnavailableError: on transport failure after retries.
        """
        _QUERY = """
        query ListIngestionSources($input: ListIngestionSourcesInput!) {
            listIngestionSources(input: $input) {
                start
                count
                total
                ingestionSources {
                    urn
                    name
                    type
                    schedule {
                        interval
                        timezone
                    }
                    config {
                        recipe
                        executorId
                    }
                }
            }
        }
        """
        page_size = 100
        start = 0
        all_sources: list[dict[str, Any]] = []

        while True:
            raw = await self.execute_graphql(
                _QUERY,
                variables={
                    "input": {
                        "start": start,
                        "count": page_size,
                        "filters": [
                            {
                                "field": "sourceType",
                                "values": ["SYSTEM"],
                                "negated": True,
                            }
                        ],
                    }
                },
            )
            outer = (raw or {}).get("listIngestionSources") or {}
            sources_page: list[dict[str, Any]] = outer.get("ingestionSources") or []
            for s in sources_page:
                if s.get("type") in _SYSTEM_SOURCE_TYPES:
                    continue
                schedule_raw = s.get("schedule")
                all_sources.append(
                    {
                        "urn": s.get("urn") or "",
                        "name": s.get("name") or "",
                        "type": s.get("type") or "",
                        "schedule": (
                            {
                                "interval": schedule_raw.get("interval") or "",
                                "timezone": schedule_raw.get("timezone"),
                            }
                            if schedule_raw
                            else None
                        ),
                        "recipe": (s.get("config") or {}).get("recipe") or "",
                        "executor_id": (s.get("config") or {}).get("executorId"),
                    }
                )
            total: int = outer.get("total") or 0
            start += len(sources_page)
            if start >= total or not sources_page:
                break

        return all_sources

    async def list_execution_requests(
        self, ingestion_source_urn: str, count: int = 100
    ) -> list[dict[str, Any]]:
        """Return result-bearing execution requests for an ingestion source.

        Uses the IngestionSource.executions(start, count) field on the source
        entity (see ingestion.graphql: IngestionSource.executions).  Only
        requests whose ``result`` field is non-null are returned — i.e. the
        executor has started and reported a status.  This INCLUDES in-progress
        runs (RUNNING/ROLLING_BACK): the caller classifies the status into
        terminal / in-progress / non-outcome.  Truly not-started requests
        (no ``result``, DataHub "Pending…") are skipped here.

        Each returned dict contains:
          - urn (str): execution request URN
          - status (str): canonical DataHub status — e.g. SUCCESS / FAILURE /
            RUNNING / TIMEOUT / ABORTED / CANCELLED / ROLLING_BACK / …
          - startTimeMs (int | None): epoch ms when the executor began
            (absent/0 until the task starts)
          - durationMs (int | None): task duration in ms
          - requestedAt (int | None): epoch ms when the run was requested
            (always present on the Input; used as the fallback timestamp)

        Raises:
            DataHubUnavailableError: on transport failure after retries.
        """
        _QUERY = """
        query GetIngestionSourceExecutions($urn: String!, $start: Int!, $count: Int!) {
            ingestionSource(urn: $urn) {
                executions(start: $start, count: $count) {
                    total
                    executionRequests {
                        urn
                        input {
                            requestedAt
                        }
                        result {
                            status
                            startTimeMs
                            durationMs
                        }
                    }
                }
            }
        }
        """
        page_size = count
        start = 0
        all_requests: list[dict[str, Any]] = []

        while True:
            raw = await self.execute_graphql(
                _QUERY,
                variables={
                    "urn": ingestion_source_urn,
                    "start": start,
                    "count": page_size,
                },
            )
            source_node = (raw or {}).get("ingestionSource") or {}
            executions = source_node.get("executions") or {}
            requests_page: list[dict[str, Any]] = executions.get("executionRequests") or []
            total: int = executions.get("total") or 0

            for req in requests_page:
                result = req.get("result")
                if result is None:
                    # Not started yet (DataHub "Pending…") — skip.
                    continue
                input_node = req.get("input") or {}
                all_requests.append(
                    {
                        "urn": req.get("urn") or "",
                        "status": result.get("status") or "",
                        "startTimeMs": result.get("startTimeMs"),
                        "durationMs": result.get("durationMs"),
                        "requestedAt": input_node.get("requestedAt"),
                    }
                )

            start += len(requests_page)
            if start >= total or not requests_page:
                break

        return all_requests

    async def get_last_ingested(self, count: int = 1000) -> dict[str, int]:
        """Return ``{dataset_urn: lastIngested_ms}`` for the whole dataset estate.

        ``Dataset.lastIngested`` is DataHub's own answer to "when was this dataset
        last ingested", derived from each aspect's ``systemMetadata.runId``. One
        paged ``scrollAcrossEntities`` covers the estate, so the sweep reads it
        once rather than probing per dataset (spec/DATAHUB_INTEGRATION.md
        §Observed Ingestion Recency).

        Four properties of this read are load-bearing:

        - **The ``... on Dataset`` inline fragment is mandatory.** ``lastIngested``
          is declared on the concrete ``Dataset`` type, not on the ``Entity``
          interface that ``entity`` resolves to, so selecting it directly on
          ``entity`` fails the *whole query* as a GraphQL validation error rather
          than returning null for the field.
        - **Null, non-integer and non-positive values are omitted**, never carried
          as ``None``. ``lastIngested`` is null exactly when every aspect on the
          dataset carries DataHub's ``"no-run-id-provided"`` sentinel — nothing
          observable — and absence is what stops the caller booking an event at an
          instant DataHub never reported. ``bool`` is rejected explicitly: it is an
          ``int`` subclass and would otherwise pass the numeric check.
        - **``scrollId`` is transmitted only once non-empty**, so the first page
          sends no cursor rather than an explicit null.
        - **The cursor loop is capped** at ``_SCROLL_MAX_PAGES`` and also stops on
          an unchanged cursor, each with a warning. An unchanged cursor is
          otherwise undetectable and burns the whole page budget every sweep.
        - **Every element of the response is shape-checked**, so a malformed one is
          skipped rather than raising. The call site classifies an
          ``AttributeError``/``TypeError`` out of this client as a fault in
          DataSpoke's own call shape and re-raises it out of the sweep; a remote
          payload must never be able to reach that branch.

        Raises:
            DataHubUnavailableError: on transport failure after retries. Errors
            propagate, as in every sibling read; containment lives at the single
            call site (spec/feature/BACKEND.md §Best-Effort Operations).
        """
        _QUERY = """
        query ScrollDatasetLastIngested($input: ScrollAcrossEntitiesInput!) {
            scrollAcrossEntities(input: $input) {
                nextScrollId
                searchResults {
                    entity {
                        urn
                        ... on Dataset {
                            lastIngested
                        }
                    }
                }
            }
        }
        """
        result: dict[str, int] = {}
        scroll_id: str | None = None

        for _ in range(_SCROLL_MAX_PAGES):
            scroll_input: dict[str, Any] = {
                "types": ["DATASET"],
                "query": "*",
                "count": count,
            }
            if scroll_id:
                scroll_input["scrollId"] = scroll_id

            raw = await self.execute_graphql(_QUERY, variables={"input": scroll_input})
            page = raw.get("scrollAcrossEntities") if isinstance(raw, dict) else None
            if not isinstance(page, dict):
                logger.warning(
                    "datahub_last_ingested_payload_unreadable — GMS returned no "
                    "readable scrollAcrossEntities container after %d datasets; "
                    "stopping this read",
                    len(result),
                )
                break

            # Every element is shape-checked rather than duck-typed. The remote
            # payload is GMS's, not DataSpoke's: the single call site treats an
            # AttributeError/TypeError out of this client as a fault in DataSpoke's
            # own call shape and re-raises it out of the sweep, so a malformed
            # element here must degrade the signal instead of reaching that branch.
            hits = page.get("searchResults")
            for hit in hits if isinstance(hits, list) else []:
                if not isinstance(hit, dict):
                    continue
                entity = hit.get("entity")
                if not isinstance(entity, dict):
                    continue
                urn = entity.get("urn")
                last_ingested = entity.get("lastIngested")
                if not urn or not isinstance(urn, str):
                    continue
                if isinstance(last_ingested, bool) or not isinstance(last_ingested, int):
                    continue
                if last_ingested <= 0:
                    continue
                result[urn] = last_ingested

            next_scroll_id = page.get("nextScrollId")
            if not next_scroll_id or not isinstance(next_scroll_id, str):
                break
            if next_scroll_id == scroll_id:
                logger.warning(
                    "datahub_last_ingested_cursor_stalled — GMS returned an unchanged "
                    "nextScrollId after %d datasets; stopping this read",
                    len(result),
                )
                break
            scroll_id = next_scroll_id
        else:
            logger.warning(
                "datahub_last_ingested_page_cap — stopped at the %d-page ceiling with "
                "%d datasets read; the estate's remaining datasets are not observed "
                "this sweep",
                _SCROLL_MAX_PAGES,
                len(result),
            )

        return result

    async def get_dataset_attributes(
        self, count: int = 1000
    ) -> dict[str, tuple[list[str], list[str]]]:
        """Return ``{dataset_urn: (tag_urns, glossary_term_urns)}`` for the estate.

        The associations a ``dataset_filter`` reads live only in DataHub, so the
        sweep mirrors them into ``dataset_registry`` from one paged
        ``scrollAcrossEntities`` (spec/DATAHUB_INTEGRATION.md §Dataset attribute
        sync). ``origin`` and ``platform_urn`` are **not** read here — the dataset
        URN encodes both, and parsing them is exact and free.

        This read carries the same four hardening properties as
        :meth:`get_last_ingested`:

        - **The ``... on Dataset`` inline fragment is mandatory.** ``tags`` and
          ``glossaryTerms`` are declared on the concrete ``Dataset`` type, not on
          the ``Entity`` interface ``entity`` resolves to, so selecting them
          directly on ``entity`` fails the *whole query* as a GraphQL validation
          error rather than returning null.
        - **``scrollId`` is transmitted only once non-empty**, so the first page
          sends no cursor rather than an explicit null.
        - **The cursor loop is capped** at ``_SCROLL_MAX_PAGES`` and also stops on
          an unchanged cursor, each with a warning.
        - **Every element of the response is shape-checked** rather than
          duck-typed, so a malformed one is skipped instead of raising. The call
          site treats an ``AttributeError``/``TypeError`` out of this client as a
          fault in DataSpoke's own call shape; a remote payload must never be able
          to reach that branch.

        A dataset present in the estate but carrying neither association still
        yields an entry with two empty lists — that is a *read* answer ("no tags"),
        distinct from the absence of an entry ("not read this sweep"), which is
        what the caller's never-blank upsert rule keys on.

        Raises:
            DataHubUnavailableError: on transport failure after retries. Errors
            propagate, as in every sibling read; containment lives at the single
            call site (spec/feature/BACKEND.md §Best-Effort Operations).
        """
        _QUERY = """
        query ScrollDatasetAttributes($input: ScrollAcrossEntitiesInput!) {
            scrollAcrossEntities(input: $input) {
                nextScrollId
                searchResults {
                    entity {
                        urn
                        ... on Dataset {
                            tags { tags { tag { urn } } }
                            glossaryTerms { terms { term { urn } } }
                        }
                    }
                }
            }
        }
        """
        result: dict[str, tuple[list[str], list[str]]] = {}
        scroll_id: str | None = None

        for _ in range(_SCROLL_MAX_PAGES):
            scroll_input: dict[str, Any] = {
                "types": ["DATASET"],
                "query": "*",
                "count": count,
            }
            if scroll_id:
                scroll_input["scrollId"] = scroll_id

            raw = await self.execute_graphql(_QUERY, variables={"input": scroll_input})
            page = raw.get("scrollAcrossEntities") if isinstance(raw, dict) else None
            if not isinstance(page, dict):
                logger.warning(
                    "datahub_dataset_attributes_payload_unreadable — GMS returned no "
                    "readable scrollAcrossEntities container after %d datasets; "
                    "stopping this read",
                    len(result),
                )
                break

            hits = page.get("searchResults")
            for hit in hits if isinstance(hits, list) else []:
                if not isinstance(hit, dict):
                    continue
                entity = hit.get("entity")
                if not isinstance(entity, dict):
                    continue
                urn = entity.get("urn")
                if not urn or not isinstance(urn, str):
                    continue
                result[urn] = (
                    _association_urns(entity.get("tags"), "tags", "tag"),
                    _association_urns(entity.get("glossaryTerms"), "terms", "term"),
                )

            next_scroll_id = page.get("nextScrollId")
            if not next_scroll_id or not isinstance(next_scroll_id, str):
                break
            if next_scroll_id == scroll_id:
                logger.warning(
                    "datahub_dataset_attributes_cursor_stalled — GMS returned an "
                    "unchanged nextScrollId after %d datasets; stopping this read",
                    len(result),
                )
                break
            scroll_id = next_scroll_id
        else:
            logger.warning(
                "datahub_dataset_attributes_page_cap — stopped at the %d-page ceiling "
                "with %d datasets read; the estate's remaining datasets keep their "
                "stored attributes this sweep",
                _SCROLL_MAX_PAGES,
                len(result),
            )

        return result

    async def get_pipeline_names(
        self, dataset_urns: list[str]
    ) -> dict[str, str | None]:
        """Return the systemMetadata.pipelineName for each dataset URN.

        Reads all MCPs for the dataset via graph.get_entity_as_mcps() and
        returns the first non-null pipelineName found across any aspect.

        Returns a dict {dataset_urn: pipelineName | None} for every input URN.
        URNs that raise exceptions or have no pipelineName map to None.

        Raises:
            DataHubUnavailableError: only propagated when the circuit breaker
            fires.  Individual per-URN errors degrade to None (best-effort).
        """
        result: dict[str, str | None] = {urn: None for urn in dataset_urns}

        # TODO: batch via getEntities if the estate grows
        for urn in dataset_urns:
            try:
                mcps = await self._with_retry(self._graph.get_entity_as_mcps, urn)
                for mcp in mcps or []:
                    sys_meta = getattr(mcp, "systemMetadata", None)
                    if sys_meta is not None:
                        pipeline_name = getattr(sys_meta, "pipelineName", None)
                        if pipeline_name:
                            result[urn] = pipeline_name
                            break
            except DataHubUnavailableError:
                # Circuit breaker fired — re-raise immediately to let the caller
                # (sync sweep) abort rather than silently returning all Nones.
                raise
            except Exception:
                # Per-URN failure: log and continue with None.
                pass

        return result

    async def hard_delete_entity(self, urn: str) -> None:
        """Hard-delete a DataHub entity and all its references with retry."""
        await self._with_retry(self._graph.hard_delete_entity, urn)

    async def check_connectivity(self) -> bool:
        try:
            await asyncio.to_thread(self._graph.test_connection)
            return True
        except Exception:
            return False


def _association_urns(container: Any, list_key: str, entity_key: str) -> list[str]:
    """Read the URNs out of a DataHub association container, shape-checking each level.

    Both association aspects nest the same way — ``tags { tags { tag { urn } } }``
    and ``glossaryTerms { terms { term { urn } } }`` — and every level is remote,
    GMS-supplied payload, so each is checked rather than duck-typed. An
    unreadable level yields no URNs instead of raising.
    """
    if not isinstance(container, dict):
        return []
    associations = container.get(list_key)
    if not isinstance(associations, list):
        return []

    urns: list[str] = []
    seen: set[str] = set()
    for association in associations:
        if not isinstance(association, dict):
            continue
        entity = association.get(entity_key)
        if not isinstance(entity, dict):
            continue
        urn = entity.get("urn")
        if isinstance(urn, str) and urn and urn not in seen:
            seen.add(urn)
            urns.append(urn)
    return urns


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
