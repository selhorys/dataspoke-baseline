"""Tests for src/shared/datahub/client.py — verifies the contracts in
spec/DATAHUB_INTEGRATION.md §SDK Patterns, §GraphQL Patterns, and §Error Handling &
Resilience. Covers retry logic, circuit breaker, aspect emission (MCP wrapper), and
downstream lineage query construction."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.config import RETRY_MAX_ATTEMPTS
from src.shared.exceptions import DataHubUnavailableError
from src.shared.redaction import REDACTED


@pytest.fixture
def mock_graph():
    with patch("src.shared.datahub.client.DataHubGraph") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_emitter():
    with patch("src.shared.datahub.client.DatahubRestEmitter") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def client(mock_graph, mock_emitter):
    from src.shared.datahub.client import DataHubClient

    return DataHubClient(gms_url="http://localhost:8080", token="test-token")


async def test_get_aspect_returns_value(client, mock_graph) -> None:
    aspect = MagicMock()
    mock_graph.get_aspect.return_value = aspect

    result = await client.get_aspect("urn:li:dataset:test", type(aspect))
    assert result is aspect


async def test_get_aspect_returns_none_on_404(client, mock_graph) -> None:
    exc = Exception("not found")
    exc.status_code = 404  # type: ignore[attr-defined]
    mock_graph.get_aspect.side_effect = exc

    result = await client.get_aspect("urn:li:dataset:missing", MagicMock)
    assert result is None


async def test_get_aspect_retries_on_connection_error(client, mock_graph) -> None:
    aspect = MagicMock()
    mock_graph.get_aspect.side_effect = [
        ConnectionError("refused"),
        ConnectionError("refused"),
        aspect,
    ]

    result = await client.get_aspect("urn:li:dataset:test", type(aspect))
    assert result is aspect
    assert mock_graph.get_aspect.call_count == 3


async def test_get_aspect_retries_on_5xx(client, mock_graph) -> None:
    exc = Exception("server error")
    exc.status_code = 500  # type: ignore[attr-defined]
    aspect = MagicMock()
    mock_graph.get_aspect.side_effect = [exc, exc, aspect]

    result = await client.get_aspect("urn:li:dataset:test", type(aspect))
    assert result is aspect
    assert mock_graph.get_aspect.call_count == 3


async def test_get_aspect_fails_fast_on_401(client, mock_graph) -> None:
    exc = Exception("unauthorized")
    exc.status_code = 401  # type: ignore[attr-defined]
    mock_graph.get_aspect.side_effect = exc

    with pytest.raises(Exception, match="unauthorized"):
        await client.get_aspect("urn:li:dataset:test", MagicMock)
    assert mock_graph.get_aspect.call_count == 1


async def test_get_aspect_strict_raises_on_unexpected_exception(client, mock_graph) -> None:
    """strict=True surfaces non-fail-fast, non-retryable exceptions as
    DataHubUnavailableError so audit-critical callers (read_role) can
    distinguish a read failure from a legitimate 'no aspect'."""
    exc = Exception("schema mismatch")  # no status_code → not fail-fast, not retryable
    mock_graph.get_aspect.side_effect = exc

    with pytest.raises(DataHubUnavailableError):
        await client.get_aspect("urn:li:dataset:test", MagicMock, strict=True)


async def test_get_aspect_strict_still_returns_value_on_success(client, mock_graph) -> None:
    aspect = MagicMock()
    mock_graph.get_aspect.return_value = aspect

    result = await client.get_aspect("urn:li:dataset:test", type(aspect), strict=True)
    assert result is aspect


async def test_get_aspect_default_swallows_unexpected_exception(client, mock_graph) -> None:
    """Default (strict=False) preserves graceful-degrade behavior for the
    15+ existing callers (dataset/service, ontogen/evidence, etc.)."""
    exc = Exception("schema mismatch")
    mock_graph.get_aspect.side_effect = exc

    result = await client.get_aspect("urn:li:dataset:test", MagicMock)
    assert result is None


async def test_circuit_breaker_opens_after_threshold(client, mock_graph) -> None:
    mock_graph.get_aspect.side_effect = ConnectionError("refused")

    # Each _with_retry call records RETRY_MAX_ATTEMPTS (3) failures.
    # After 2 calls = 6 failures > CIRCUIT_BREAKER_THRESHOLD (5), breaker opens.
    for _ in range(2):
        with pytest.raises(DataHubUnavailableError):
            await client.get_aspect("urn:li:dataset:test", MagicMock)

    # Now circuit should be open — should raise without calling graph
    mock_graph.get_aspect.reset_mock()
    with pytest.raises(DataHubUnavailableError):
        await client.get_aspect("urn:li:dataset:test", MagicMock)
    mock_graph.get_aspect.assert_not_called()


async def test_circuit_breaker_resets_after_timeout(client, mock_graph) -> None:
    mock_graph.get_aspect.side_effect = ConnectionError("refused")
    for _ in range(2):
        with pytest.raises(DataHubUnavailableError):
            await client.get_aspect("urn:li:dataset:test", MagicMock)

    # Advance time past circuit breaker reset period
    client._circuit_open_until = time.monotonic() - 1

    aspect = MagicMock()
    mock_graph.get_aspect.side_effect = None
    mock_graph.get_aspect.return_value = aspect

    result = await client.get_aspect("urn:li:dataset:test", type(aspect))
    assert result is aspect


async def test_circuit_breaker_closes_on_probe_success(client, mock_graph) -> None:
    mock_graph.get_aspect.side_effect = ConnectionError("refused")
    for _ in range(2):
        with pytest.raises(DataHubUnavailableError):
            await client.get_aspect("urn:li:dataset:test", MagicMock)

    client._circuit_open_until = time.monotonic() - 1
    aspect = MagicMock()
    mock_graph.get_aspect.side_effect = None
    mock_graph.get_aspect.return_value = aspect

    await client.get_aspect("urn:li:dataset:test", type(aspect))
    assert client._consecutive_failures == 0


async def test_get_timeseries_returns_list(client, mock_graph) -> None:
    profiles = [MagicMock(), MagicMock()]
    mock_graph.get_timeseries_values.return_value = profiles

    result = await client.get_timeseries("urn:li:dataset:test", MagicMock)
    assert result == profiles


async def test_get_downstream_lineage_graphql(client, mock_graph) -> None:
    """get_downstream_lineage uses GraphQL searchAcrossLineage with direction DOWNSTREAM.

    Verifies spec/DATAHUB_INTEGRATION.md §GraphQL Patterns §Downstream Lineage:
    'call graph.execute_graphql(...) with a searchAcrossLineage query
    (direction: DOWNSTREAM, types: [DATASET])'.
    REST API only exposes upstreamLineage — downstream traversal requires GraphQL.
    """
    mock_graph.execute_graphql.return_value = {
        "searchAcrossLineage": {
            "searchResults": [
                {"entity": {"urn": "urn:li:dataset:downstream1"}},
                {"entity": {"urn": "urn:li:dataset:downstream2"}},
            ]
        }
    }

    result = await client.get_downstream_lineage("urn:li:dataset:source")
    assert result == ["urn:li:dataset:downstream1", "urn:li:dataset:downstream2"]

    # Verify the GraphQL query body contains 'DOWNSTREAM' direction as required by spec
    call_args = mock_graph.execute_graphql.call_args
    query_str = call_args[0][0]
    assert "DOWNSTREAM" in query_str, "GraphQL query must specify direction: DOWNSTREAM"
    # spec/DATAHUB_INTEGRATION.md §Downstream Lineage: query must filter types: [DATASET]
    assert "DATASET" in query_str, (
        "GraphQL query must include types: [DATASET] per DATAHUB_INTEGRATION.md §Downstream Lineage"
    )
    # Verify the source URN is passed as a variable, not interpolated into the query string.
    # Don't pin the variable key name — assert the value appears somewhere in variables.
    variables = call_args[1].get("variables") or {}
    assert "urn:li:dataset:source" in variables.values(), (
        "Source URN must be passed as a GraphQL variable value"
    )


async def test_enumerate_datasets(client, mock_graph) -> None:
    urns = ["urn:li:dataset:a", "urn:li:dataset:b"]
    mock_graph.get_urns_by_filter.return_value = urns

    result = await client.enumerate_datasets()
    assert result == urns


async def test_enumerate_datasets_takes_no_filter_dimensions(client, mock_graph) -> None:
    """The estate enumeration is unfiltered — scope is not resolved by a DataHub search.

    Spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "**Filter evaluation is
    DataSpoke-side, not a DataHub search.**"; spec/API.md §`dataset_filter` grammar —
    "Resolution is a DataSpoke-side SQL query, not a DataHub search". A wrapper that still
    accepted filter dimensions would make a second, divergent path to scope.
    """
    import inspect as _inspect

    from src.shared.datahub.client import DataHubClient

    parameters = set(_inspect.signature(DataHubClient.enumerate_datasets).parameters) - {"self"}
    assert parameters == set(), (
        f"enumerate_datasets must take no filter dimensions; got {sorted(parameters)}"
    )

    mock_graph.get_urns_by_filter.return_value = []
    await client.enumerate_datasets()
    kwargs = mock_graph.get_urns_by_filter.call_args.kwargs
    assert kwargs.get("entity_types") == ["dataset"]
    assert not kwargs.get("extra_or_filters"), (
        f"the sweep enumerates the whole estate; got {kwargs.get('extra_or_filters')!r}"
    )


async def test_list_execution_requests_returns_result_bearing(client, mock_graph) -> None:
    """list_execution_requests returns every result-bearing execution — INCLUDING
    in-progress (RUNNING) ones — with status + requestedAt, and skips requests whose
    result is None (DataHub 'Pending…').

    Spec: spec/feature/BACKEND.md §Sync step 4 (Run events) — the client surfaces all
    result-bearing requests (it does not classify); the service maps status → event.
    A RUNNING execution must reach the service (carrying status + requestedAt) so it can
    be classified as in-progress; a request with no result aspect is skipped here.
    """
    mock_graph.execute_graphql.return_value = {
        "ingestionSource": {
            "executions": {
                "total": 3,
                "executionRequests": [
                    {
                        "urn": "urn:li:dataHubExecutionRequest:done",
                        "input": {"requestedAt": 1_699_999_000_000},
                        "result": {
                            "status": "SUCCESS",
                            "startTimeMs": 1_700_000_000_000,
                            "durationMs": 5000,
                        },
                    },
                    {
                        "urn": "urn:li:dataHubExecutionRequest:running",
                        "input": {"requestedAt": 1_699_999_500_000},
                        "result": {
                            "status": "RUNNING",
                            "startTimeMs": 1_700_000_100_000,
                            "durationMs": None,
                        },
                    },
                    {
                        # Pending — no result aspect yet → must be skipped.
                        "urn": "urn:li:dataHubExecutionRequest:pending",
                        "input": {"requestedAt": 1_699_999_900_000},
                        "result": None,
                    },
                ],
            }
        }
    }

    result = await client.list_execution_requests("urn:li:dataHubIngestionSource:s1")

    urns = [r["urn"] for r in result]
    assert urns == [
        "urn:li:dataHubExecutionRequest:done",
        "urn:li:dataHubExecutionRequest:running",
    ], "result-bearing requests are returned (incl. RUNNING); the pending one is skipped"

    running = next(r for r in result if r["urn"].endswith("running"))
    # The in-progress run carries its status so the service can classify it as in-progress,
    # and requestedAt so the timestamp fallback is available.
    assert running["status"] == "RUNNING"
    assert running["requestedAt"] == 1_699_999_500_000
    assert running["startTimeMs"] == 1_700_000_100_000

    done = next(r for r in result if r["urn"].endswith("done"))
    assert done["status"] == "SUCCESS"
    assert done["requestedAt"] == 1_699_999_000_000
    assert done["durationMs"] == 5000


async def test_list_execution_requests_skips_when_all_pending(client, mock_graph) -> None:
    """When no execution has a result aspect, list_execution_requests returns []."""
    mock_graph.execute_graphql.return_value = {
        "ingestionSource": {
            "executions": {
                "total": 1,
                "executionRequests": [
                    {
                        "urn": "urn:li:dataHubExecutionRequest:pending",
                        "input": {"requestedAt": 1_699_999_900_000},
                        "result": None,
                    }
                ],
            }
        }
    }

    result = await client.list_execution_requests("urn:li:dataHubIngestionSource:s1")
    assert result == []


async def test_emit_aspect_wraps_mcp(client, mock_emitter) -> None:
    """emit_aspect wraps the aspect in MetadataChangeProposalWrapper with correct fields.

    Verifies spec/DATAHUB_INTEGRATION.md §SDK Patterns Pattern C:
    'emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=AspectClass(...)))'
    The wrapper must produce UPSERT changeType and derive entityType from the URN scheme.
    """
    aspect = MagicMock()
    await client.emit_aspect("urn:li:dataset:(urn:li:dataPlatform:postgres,test,DEV)", aspect)
    mock_emitter.emit_mcp.assert_called_once()
    call_args = mock_emitter.emit_mcp.call_args
    mcp = call_args[0][0]
    assert mcp.entityUrn == "urn:li:dataset:(urn:li:dataPlatform:postgres,test,DEV)"
    # MCP wrapper must default to UPSERT semantics per DATAHUB_INTEGRATION.md §SDK Patterns
    assert mcp.changeType == "UPSERT"
    # entityType is derived from the URN scheme by MetadataChangeProposalWrapper
    assert mcp.entityType == "dataset"
    # The original aspect object must be preserved unchanged
    assert mcp.aspect is aspect


# ── Credential scrubbing on operator-facing messages ─────────────────────────
#
# The redaction *algorithm* is covered in tests/unit/shared/test_redaction.py. These
# tests cover the client's **wiring** of it: that the client registers its own live
# credentials as exact values (the strongest layer, and the only path that supplies
# `secrets=` anywhere in the product), and that both DataHubUnavailableError raise sites
# route their message through it.
#
# spec: DATAHUB_INTEGRATION.md §Resilience Conventions — a reported failure carries no
#   credentials.
# spec: feature/BACKEND.md §Health reporting — "``last_error`` is bounded and
#   credential-free"; the DataHubUnavailableError message is one of the strings that
#   reaches that column.

_PAT = "pat-abc123def456ghi"
_URL_PW = "tOpS3cretPass"


@pytest.fixture
def token_client(mock_graph, mock_emitter):
    """A client holding a distinctive PAT and a GMS URL carrying userinfo.

    Both are credentials only this client knows the value of, so only the exact-value
    layer can remove them: neither appears next to a credential-shaped name in the
    messages below, and neither sits inside a `scheme://…@host` URL there.
    """
    from src.shared.datahub.client import DataHubClient

    return DataHubClient(gms_url=f"http://dsuser:{_URL_PW}@gms:8080", token=_PAT)


def test_sanitize_scrubs_the_live_pat_by_exact_value(token_client) -> None:
    """The client's own token is removed from a message that merely quotes it.

    The token is not adjacent to any credential-shaped name here, so the pattern layer
    cannot catch it — passing only if the client registered the live value.
    """
    out = token_client.sanitize(f"GMS refused the request carrying {_PAT} at the edge")

    assert _PAT not in out, f"the live PAT must not survive; got {out!r}"
    # The marker is imported, not spelled out: no spec names a marker string, so the
    # property is that *a* marker is present rather than which one.
    assert REDACTED in out
    # Backstop: the diagnostic either side survives, so the scrub did not blank it.
    assert "GMS refused the request carrying" in out and "at the edge" in out


def test_sanitize_scrubs_the_gms_url_userinfo_password(token_client) -> None:
    """The password half of the GMS URL's userinfo is removed too.

    Quoted outside a URL, so the userinfo pattern cannot match it — this passes only if
    the client extracted the password from ``gms_url`` and registered it as an exact
    value.
    """
    out = token_client.sanitize(f"pg handshake rejected the supplied {_URL_PW} value")

    assert _URL_PW not in out, f"the GMS URL password must not survive; got {out!r}"
    assert REDACTED in out
    assert "pg handshake rejected the supplied" in out


def test_sanitize_scrubs_a_pat_split_by_a_transport_newline(token_client) -> None:
    """A PAT with a newline spliced into it is still matched.

    Transport messages wrap; the exact-value match is space-tolerant *after*
    normalization for exactly this reason. Asserted through the client so the wiring of
    ``secrets=`` is what is under test, not the matcher in isolation.
    """
    tampered = _PAT[:8] + "\n" + _PAT[8:]
    assert _PAT not in f"token {tampered}", (
        "Backstop: the fixture must not contain the untampered PAT."
    )

    out = token_client.sanitize(f"GMS refused token {tampered} here")

    assert _PAT not in out, f"normalization must not reassemble the PAT; got {out!r}"
    for half in (_PAT[:8], _PAT[8:]):
        assert half not in out, f"no half of the PAT may survive either; {half!r} in {out!r}"


def test_sanitize_leaves_a_credential_free_message_intact(token_client) -> None:
    """A message holding no credential is returned unchanged.

    The non-matching side of the filter: a client that blanked or truncated every
    message would destroy the only operator signal a transport failure carries.
    """
    message = (
        "Unable to fetch entity with key: "
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    )
    assert token_client.sanitize(message) == message


async def test_retry_exhaustion_error_message_is_sanitized(token_client, mock_graph) -> None:
    """The DataHubUnavailableError raised after retry exhaustion carries no credential.

    This is the message that reaches ``peripheral_health.last_error`` and the internal
    activity's 500 body, so the scrub has to happen at the raise site rather than at
    each sink.
    """
    mock_graph.get_aspect.side_effect = ConnectionError(
        f"connection refused while presenting {_PAT}"
    )

    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(DataHubUnavailableError) as exc:
        await token_client.get_aspect("urn:li:dataset:test", MagicMock)

    assert _PAT not in str(exc.value), (
        f"the raised message must be scrubbed; got {str(exc.value)!r}"
    )
    # Backstop: the transport's own diagnostic survives, so the message is still useful.
    assert "connection refused" in str(exc.value)


async def test_strict_read_error_message_is_sanitized(token_client, mock_graph) -> None:
    """The strict-mode DataHubUnavailableError is scrubbed at its own raise site.

    A second, independent raise site: a non-retryable exception never reaches
    ``_with_retry``'s final raise, so scrubbing there alone would leave this path
    leaking.
    """
    mock_graph.get_aspect.side_effect = Exception(f"schema mismatch, sent {_PAT}")

    with pytest.raises(DataHubUnavailableError) as exc:
        await token_client.get_aspect("urn:li:dataset:test", MagicMock, strict=True)

    assert _PAT not in str(exc.value), (
        f"the strict-mode message must be scrubbed; got {str(exc.value)!r}"
    )
    assert "schema mismatch" in str(exc.value)


# ── get_last_ingested: the estate-wide observed-recency read ──────────────────
#
# spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "DataSpoke reads it
#   estate-wide as **one paged ``scrollAcrossEntities`` per sweep** —
#   ``{dataset_urn: lastIngested_ms}`` for every dataset". The four constraints listed
#   there are covered one test each below.


def _scroll_page(hits: list[dict], next_scroll_id: str | None) -> dict:
    """One ``scrollAcrossEntities`` response envelope."""
    return {
        "scrollAcrossEntities": {
            "nextScrollId": next_scroll_id,
            "searchResults": hits,
        }
    }


def _dataset_hit(urn: str, last_ingested: object) -> dict:
    """One search hit whose entity carries ``urn`` and ``lastIngested``."""
    return {"entity": {"urn": urn, "lastIngested": last_ingested}}


async def test_get_last_ingested_selects_last_ingested_through_a_dataset_fragment(
    client, mock_graph
) -> None:
    """The query selects ``lastIngested`` inside an ``... on Dataset`` inline fragment.

    This is pinned as query *text* because the failure mode it guards has no other
    observable: ``lastIngested`` is declared on the concrete ``Dataset`` type rather than
    on the ``Entity`` interface ``entity`` resolves to, so selecting it directly on
    ``entity`` fails the whole query as a GraphQL validation error against a real GMS
    while a mocked graph would happily return the same rows. The two assertions are
    ordered: the fragment must be present, and ``lastIngested`` must sit inside it.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "**The ``... on
        Dataset`` inline fragment is mandatory.** … Selecting it directly on ``entity``
        fails the **whole query** as a GraphQL validation error — it does not return
        ``null`` for the field, so the failure is total rather than partial."
    """
    mock_graph.execute_graphql.return_value = _scroll_page([], None)

    await client.get_last_ingested()

    query = mock_graph.execute_graphql.call_args.args[0]
    assert "... on Dataset" in query, (
        f"the query must select through an '... on Dataset' inline fragment; got:\n{query}"
    )
    fragment_body = query.split("... on Dataset", 1)[1]
    assert "lastIngested" in fragment_body.split("}", 1)[0], (
        "lastIngested must be selected INSIDE the Dataset fragment, not on the Entity "
        f"interface; got:\n{query}"
    )


async def test_get_last_ingested_merges_pages_and_sends_the_cursor_only_once_set(
    client, mock_graph
) -> None:
    """Two pages merge into one mapping; the first request transmits no ``scrollId``.

    Both halves are contractual. Merging is what makes this one read per sweep rather
    than one per page, and omitting the cursor on the first request is what stops the
    first page transmitting an explicit null.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "one paged
        ``scrollAcrossEntities`` per sweep — ``{dataset_urn: lastIngested_ms}`` for every
        dataset"; "**``scrollId`` is transmitted only once non-empty**, so the first page
        sends no cursor rather than an explicit null."
    """
    first = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    second = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    mock_graph.execute_graphql.side_effect = [
        _scroll_page([_dataset_hit(first, 1_700_000_000_000)], "cursor-2"),
        _scroll_page([_dataset_hit(second, 1_700_000_600_000)], None),
    ]

    result = await client.get_last_ingested()

    assert result == {first: 1_700_000_000_000, second: 1_700_000_600_000}, (
        f"both pages must merge into one estate mapping; got {result!r}. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )
    calls = mock_graph.execute_graphql.call_args_list
    assert len(calls) == 2, f"exactly two requests for two pages; got {len(calls)}."
    assert "scrollId" not in calls[0].kwargs["variables"]["input"], (
        "the first request must carry no scrollId key at all. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )
    assert calls[1].kwargs["variables"]["input"]["scrollId"] == "cursor-2", (
        "the second request must carry the cursor the first page returned."
    )


@pytest.mark.parametrize(
    ("label", "last_ingested"),
    [
        ("null", None),
        ("zero", 0),
        ("negative", -1),
    ],
)
async def test_get_last_ingested_omits_null_and_non_positive_values(
    client, mock_graph, label: str, last_ingested: object
) -> None:
    """A null or non-positive ``lastIngested`` is absent from the mapping.

    Absent, never mapped to ``None``: absence is the caller's whole guard against booking
    an event at an instant DataHub never reported. Both sides are in the same page — a
    usable neighbour is always returned — so an implementation that dropped the page
    wholesale would fail on the neighbour rather than pass on the omission.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "**Null and
        non-positive values are omitted, not carried as ``None``.** ``lastIngested`` is
        ``null`` exactly when every aspect on the dataset carries the
        ``"no-run-id-provided"`` sentinel — nothing observable. Absence is the guard
        against booking an event at an instant DataHub never reported."
    """
    unusable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    usable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [_dataset_hit(unusable, last_ingested), _dataset_hit(usable, 1_700_000_000_000)],
        None,
    )

    result = await client.get_last_ingested()

    assert result == {usable: 1_700_000_000_000}, (
        f"{label}: a lastIngested of {last_ingested!r} must be omitted while its usable "
        f"neighbour is kept; got {result!r}. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )


@pytest.mark.parametrize(
    ("label", "last_ingested"),
    [
        ("boolean-true", True),
        ("boolean-false", False),
        ("string", "1700000000000"),
    ],
)
async def test_get_last_ingested_omits_a_value_that_is_no_epoch_millisecond(
    client, mock_graph, label: str, last_ingested: object
) -> None:
    """A value that is not an epoch-millisecond reading is omitted, not coerced.

    The read answers with epoch milliseconds. A ``bool`` and a numeric string are not
    readings DataHub took: coercing either fabricates an instant — ``True`` would map to
    one millisecond after the epoch, and a string would map to whatever ``int()`` made of
    it — and the caller books an ``INGESTION.COMPLETE`` at it. ``bool`` is the sharp case
    because it is an ``int`` subclass and passes a bare numeric check.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "``Dataset.lastIngested``
        (epoch ms) … DataSpoke reads it estate-wide … ``{dataset_urn: lastIngested_ms}``
        for every dataset"; "Absence is the guard against booking an event at an instant
        DataHub never reported."
    """
    unusable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    usable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [_dataset_hit(unusable, last_ingested), _dataset_hit(usable, 1_700_000_000_000)],
        None,
    )

    result = await client.get_last_ingested()

    assert result == {usable: 1_700_000_000_000}, (
        f"{label}: a lastIngested of {last_ingested!r} is no epoch-millisecond reading and "
        f"must be omitted while its usable neighbour is kept; got {result!r}. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )


@pytest.mark.parametrize(
    ("label", "hits"),
    [
        ("hit is not a mapping", ["not-a-hit"]),
        ("entity is not a mapping", [{"entity": "not-a-mapping"}]),
        ("entity missing", [{}]),
        ("urn missing", [{"entity": {"lastIngested": 1_700_000_000_000}}]),
        ("urn is not a string", [{"entity": {"urn": 42, "lastIngested": 1}}]),
    ],
)
async def test_get_last_ingested_skips_a_malformed_hit_without_raising(
    client, mock_graph, label: str, hits: list
) -> None:
    """A malformed search hit is skipped; the well-formed one in the same page survives.

    The shape check is load-bearing rather than defensive: the single call site treats an
    ``AttributeError``/``TypeError`` out of this client as a fault in DataSpoke's own call
    shape and **re-raises it out of the sweep**, so a malformed remote payload reaching
    that branch would turn one bad GMS row into a failed hourly sync for every source.

    spec: spec/feature/BACKEND.md §Best-Effort Operations — the interface-violation
        exemption holds "because the read is a fixed-shape traversal of a GraphQL response
        in which every element is shape-checked", so an ``AttributeError``/``TypeError``
        out of this client can only be a call-shape fault.
    """
    usable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [*hits, _dataset_hit(usable, 1_700_000_000_000)], None
    )

    result = await client.get_last_ingested()

    assert result == {usable: 1_700_000_000_000}, (
        f"{label}: a malformed hit must be skipped, not raise, and the well-formed hit in "
        f"the same page must still be read; got {result!r}. "
        "spec: feature/BACKEND.md §Best-Effort Operations."
    )


async def test_get_last_ingested_stops_on_an_unchanged_cursor(client, mock_graph) -> None:
    """An unchanged ``nextScrollId`` stops the loop after the repeat, keeping what it read.

    A GMS that returns the same cursor forever is capped by the page ceiling, but an
    unchanged cursor is otherwise undetectable and would burn the whole page budget on
    every sweep. The call count is what discriminates: the ceiling alone would let this
    run to 100 requests.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "the sweep stops at a
        fixed page ceiling **and also on an unchanged cursor** — each logged as a warning,
        since an unchanged cursor is otherwise undetectable and burns the whole page
        budget every sweep."
    """
    from src.shared.datahub.client import _SCROLL_MAX_PAGES

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [_dataset_hit(urn, 1_700_000_000_000)], "stuck"
    )

    result = await client.get_last_ingested()

    assert mock_graph.execute_graphql.call_count == 2, (
        f"the loop must stop on the repeated cursor rather than run to the "
        f"{_SCROLL_MAX_PAGES}-page ceiling; got "
        f"{mock_graph.execute_graphql.call_count} requests. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )
    assert result == {urn: 1_700_000_000_000}, (
        "what was read before the stall is still returned; the guard bounds the read, it "
        "does not discard it."
    )


async def test_get_last_ingested_stops_at_the_page_ceiling(client, mock_graph) -> None:
    """An endless cursor stream stops at ``_SCROLL_MAX_PAGES`` requests.

    Unlike the ``total``-bounded sibling reads a cursor loop has no intrinsic bound, and
    this one runs on the API pod's event loop. Every cursor differs, so the unchanged-
    cursor guard cannot be what stops it — only the ceiling can.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "**The cursor loop is
        capped.** Unlike the ``total``-bounded reads, ``nextScrollId`` paging has no
        intrinsic bound and this runs on the API pod's event loop, so the sweep stops at a
        fixed page ceiling".
    """
    from src.shared.datahub.client import _SCROLL_MAX_PAGES

    page = {"n": 0}

    def _endless(*_args, **_kwargs):
        page["n"] += 1
        urn = f"urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.p{page['n']},DEV)"
        return _scroll_page([_dataset_hit(urn, 1_700_000_000_000)], f"cursor-{page['n']}")

    mock_graph.execute_graphql.side_effect = _endless

    result = await client.get_last_ingested()

    assert mock_graph.execute_graphql.call_count == _SCROLL_MAX_PAGES, (
        f"the loop must stop at the {_SCROLL_MAX_PAGES}-page ceiling; got "
        f"{mock_graph.execute_graphql.call_count} requests. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )
    assert len(result) == _SCROLL_MAX_PAGES, (
        "every page read before the ceiling is still returned."
    )


async def test_get_last_ingested_returns_empty_on_an_unreadable_envelope(
    client, mock_graph
) -> None:
    """A response with no readable ``scrollAcrossEntities`` container yields ``{}``.

    Absence propagates as absence rather than as an exception, which is what lets the
    caller's interface-violation exemption mean what it says.

    spec: spec/feature/BACKEND.md §Best-Effort Operations — the exemption holds "because the
        read is a fixed-shape traversal of a GraphQL response in which every element is
        shape-checked"; an envelope with no readable container is such a shape check, so it
        must yield absence rather than an ``AttributeError``/``TypeError`` the caller would
        re-raise out of the whole sweep.
    """
    mock_graph.execute_graphql.return_value = {"errors": [{"message": "boom"}]}

    assert await client.get_last_ingested() == {}


async def test_get_last_ingested_propagates_a_non_retryable_failure(client, mock_graph) -> None:
    """A non-retryable failure propagates unchanged rather than becoming an empty mapping.

    Identity, not type: an implementation that caught it and returned ``{}`` would report
    an estate with nothing observable, which is what the caller's whole guard reads as
    "book nothing" — the failure would then be indistinguishable from a healthy estate of
    undatable datasets.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "Errors propagate to
        the single call site, matching every other client read; containment (best-effort
        degradation of the signal, and the interface-violation exemption from it) is
        defined in BACKEND §Best-Effort Operations."
    """
    transport_failure = ValueError("gms said no")
    mock_graph.execute_graphql.side_effect = transport_failure

    with pytest.raises(ValueError) as exc:
        await client.get_last_ingested()

    assert exc.value is transport_failure, (
        f"the read must propagate the transport's own failure rather than swallowing it "
        f"into an empty mapping or substituting its own; got {exc.value!r}."
    )


async def test_get_last_ingested_surfaces_an_exhausted_retry_as_the_documented_error(
    client, mock_graph
) -> None:
    """A retryable transport fault that outlives the retries raises ``DataHubUnavailableError``.

    This is the shape production actually sees, and it is the one the sub-pass's
    best-effort branch is written against: the caller degrades every exception except
    ``AttributeError``/``TypeError``, so a transport fault that arrived *as* one of those
    would be re-raised out of the whole hourly sweep instead of costing one signal. The
    retry wrapper is what decides that, and this read must not bypass it.

    spec: spec/DATAHUB_INTEGRATION.md §Observed Ingestion Recency — "Errors propagate to
        the single call site, matching every other client read".
    spec: spec/feature/BACKEND.md §Best-Effort Operations — the interface-violation
        exemption applies to ``AttributeError``/``TypeError`` alone; every other failure
        of this read degrades the signal.
    """
    mock_graph.execute_graphql.side_effect = ConnectionError("connection refused")

    # ``pytest.raises`` is the discriminating assertion here: a read that bypassed
    # ``_with_retry`` would let the raw ``ConnectionError`` out and fail this block.
    with pytest.raises(DataHubUnavailableError):
        await client.get_last_ingested()

    assert mock_graph.execute_graphql.call_count == RETRY_MAX_ATTEMPTS, (
        f"the read must go through the shared retry wrapper, which is what converts a "
        f"transport fault into the documented error instead of some type the call site's "
        f"interface-violation branch would re-raise out of the whole sweep; got "
        f"{mock_graph.execute_graphql.call_count} attempts. "
        "spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency."
    )


# ── get_dataset_attributes: the dataset_filter attribute mirror ───────────────
#
# spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "`tag_urns`,
#   `glossary_term_urns` | one paged `scrollAcrossEntities` | Associations that live only
#   in DataHub"; the read "selects `urn`, `tags { tags { tag { urn } } }`, and
#   `glossaryTerms { terms { term { urn } } }` and carries the same four hardening
#   properties as §Observed Ingestion Recency".


def _attribute_hit(urn: str, tag_urns: list[str], term_urns: list[str]) -> dict:
    """One search hit in the association shape DataHub's `Dataset` type returns."""
    return {
        "entity": {
            "urn": urn,
            "tags": {"tags": [{"tag": {"urn": t}} for t in tag_urns]},
            "glossaryTerms": {"terms": [{"term": {"urn": t}} for t in term_urns]},
        }
    }


async def test_get_dataset_attributes_selects_both_associations_in_a_dataset_fragment(
    client, mock_graph
) -> None:
    """`tags` and `glossaryTerms` are selected inside an `... on Dataset` fragment.

    Pinned as query text for the same reason as `get_last_ingested`: both are declared on
    the concrete `Dataset` type rather than the `Entity` interface `entity` resolves to,
    so selecting them on `entity` fails the whole query against a real GMS while a mocked
    graph returns rows either way.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "a mandatory `... on
        Dataset` inline fragment"; the selection list quoted above.
    """
    mock_graph.execute_graphql.return_value = _scroll_page([], None)

    await client.get_dataset_attributes()

    query = mock_graph.execute_graphql.call_args.args[0]
    assert "... on Dataset" in query, f"missing the Dataset inline fragment; got:\n{query}"
    fragment_body = query.split("... on Dataset", 1)[1]
    for selection in ("tags", "glossaryTerms"):
        assert selection in fragment_body, (
            f"{selection} must be selected INSIDE the Dataset fragment; got:\n{query}"
        )


async def test_get_dataset_attributes_maps_each_dataset_to_its_two_urn_lists(
    client, mock_graph
) -> None:
    """The read answers `{urn: (tag_urns, glossary_term_urns)}`.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — the sweep mirrors
        `tag_urns` and `glossary_term_urns` from this read into `dataset_registry`.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [
            _attribute_hit(
                urn,
                ["urn:li:tag:area:catalog", "urn:li:tag:pii"],
                ["urn:li:glossaryTerm:pii.gdpr"],
            )
        ],
        None,
    )

    result = await client.get_dataset_attributes()

    assert result == {
        urn: (["urn:li:tag:area:catalog", "urn:li:tag:pii"], ["urn:li:glossaryTerm:pii.gdpr"])
    }


async def test_get_dataset_attributes_reports_an_untagged_dataset_as_empty_lists(
    client, mock_graph
) -> None:
    """A dataset carrying neither association is present with two empty lists.

    Presence is what distinguishes "read, and it has no tags" from "not read this sweep";
    the sweep's never-blank upsert rule keys on exactly that difference, so an omitted
    entry would make an untagged dataset indistinguishable from an unread one and let its
    stale tags survive forever.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "A dataset absent from the
        attribute read keeps its prior attributes rather than being blanked".
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [{"entity": {"urn": urn, "tags": None, "glossaryTerms": None}}], None
    )

    result = await client.get_dataset_attributes()

    assert result == {urn: ([], [])}


async def test_get_dataset_attributes_merges_pages_and_sends_the_cursor_only_once_set(
    client, mock_graph
) -> None:
    """Two pages merge; the first request transmits no `scrollId` key at all.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "one paged
        `scrollAcrossEntities`"; "`scrollId` transmitted only once non-empty".
    """
    first = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    second = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
    mock_graph.execute_graphql.side_effect = [
        _scroll_page([_attribute_hit(first, ["urn:li:tag:pii"], [])], "cursor-2"),
        _scroll_page([_attribute_hit(second, [], ["urn:li:glossaryTerm:pii.gdpr"])], None),
    ]

    result = await client.get_dataset_attributes()

    assert result == {
        first: (["urn:li:tag:pii"], []),
        second: ([], ["urn:li:glossaryTerm:pii.gdpr"]),
    }
    calls = mock_graph.execute_graphql.call_args_list
    assert len(calls) == 2
    assert "scrollId" not in calls[0].kwargs["variables"]["input"]
    assert calls[1].kwargs["variables"]["input"]["scrollId"] == "cursor-2"


@pytest.mark.parametrize(
    ("label", "hits"),
    [
        ("hit is not a mapping", ["not-a-hit"]),
        ("entity is not a mapping", [{"entity": "not-a-mapping"}]),
        ("entity missing", [{}]),
        ("urn missing", [{"entity": {"tags": None}}]),
        ("urn is not a string", [{"entity": {"urn": 42}}]),
        ("tags container is not a mapping", [{"entity": {"urn": "urn:li:x", "tags": []}}]),
        (
            "association is not a mapping",
            [{"entity": {"urn": "urn:li:y", "tags": {"tags": ["nope"]}}}],
        ),
        (
            "tag urn is not a string",
            [{"entity": {"urn": "urn:li:z", "tags": {"tags": [{"tag": {"urn": 7}}]}}}],
        ),
    ],
)
async def test_get_dataset_attributes_skips_a_malformed_hit_without_raising(
    client, mock_graph, label: str, hits: list
) -> None:
    """A malformed hit is skipped; the well-formed hit in the same page survives.

    The well-formed neighbour is the backstop: an implementation that dropped the whole
    page on a bad element would fail here rather than pass on the skip.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "per-element shape checks";
    spec: spec/feature/BACKEND.md §Best-Effort Operations — an `AttributeError`/`TypeError`
        out of this client can only be a call-shape fault, so a remote payload must never
        reach that branch.
    """
    usable = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [*hits, _attribute_hit(usable, ["urn:li:tag:pii"], [])], None
    )

    result = await client.get_dataset_attributes()

    assert result.get(usable) == (["urn:li:tag:pii"], []), (
        f"{label}: the well-formed hit in the same page must still be read; got {result!r}"
    )


async def test_get_dataset_attributes_stops_on_an_unchanged_cursor(client, mock_graph) -> None:
    """A repeated `nextScrollId` stops the loop rather than burning the page budget.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "a page-capped cursor
        loop", carrying §Observed Ingestion Recency's stop-on-unchanged-cursor property.
    """
    from src.shared.datahub.client import _SCROLL_MAX_PAGES

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    mock_graph.execute_graphql.return_value = _scroll_page(
        [_attribute_hit(urn, ["urn:li:tag:pii"], [])], "stuck"
    )

    result = await client.get_dataset_attributes()

    assert mock_graph.execute_graphql.call_count == 2, (
        f"must stop on the repeated cursor rather than run to the {_SCROLL_MAX_PAGES}-page "
        f"ceiling; got {mock_graph.execute_graphql.call_count} requests"
    )
    assert result == {urn: (["urn:li:tag:pii"], [])}


async def test_get_dataset_attributes_raises_the_documented_error_after_retries(
    client, mock_graph
) -> None:
    """A transport fault surfaces as `DataHubUnavailableError`, not a raw `ConnectionError`.

    spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — the read carries the same
        hardening properties as §Observed Ingestion Recency, whose errors "propagate to the
        single call site, matching every other client read".
    """
    mock_graph.execute_graphql.side_effect = ConnectionError("connection refused")

    with pytest.raises(DataHubUnavailableError):
        await client.get_dataset_attributes()

    assert mock_graph.execute_graphql.call_count == RETRY_MAX_ATTEMPTS
