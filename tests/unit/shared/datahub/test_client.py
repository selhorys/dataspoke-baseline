"""Tests for src/shared/datahub/client.py — verifies the contracts in
spec/DATAHUB_INTEGRATION.md §SDK Patterns, §GraphQL Patterns, and §Error Handling &
Resilience. Covers retry logic, circuit breaker, aspect emission (MCP wrapper), and
downstream lineage query construction."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _all_filter_rules(or_filters: list[dict]) -> list[dict]:
    """Flatten every leaf filter rule out of an extra_or_filters structure
    (a list of {"and": [rule, ...]} groups)."""
    rules: list[dict] = []
    for group in or_filters or []:
        for rule in group.get("and", []):
            rules.append(rule)
    return rules


async def test_enumerate_datasets_emits_values_array_filter_shape(client, mock_graph) -> None:
    """enumerate_datasets builds each search-filter rule in the DataHub 1.6
    ``{field, values: [...]}`` array form — never the dropped singular ``value`` scalar.

    Spec: spec/DATAHUB_INTEGRATION.md §Origin filter group — 'Each filter rule uses the
    **values array** form ({field, values: [...]}) — the DataHub search filter API
    expresses a single match as a one-element array, not a singular value scalar.'
    """
    mock_graph.get_urns_by_filter.return_value = []

    await client.enumerate_datasets(
        platform="postgres",
        tags=["urn:li:tag:PII"],
        glossary_terms=["urn:li:glossaryTerm:gdpr"],
        origin="PROD",
    )

    or_filters = mock_graph.get_urns_by_filter.call_args.kwargs["extra_or_filters"]
    rules = _all_filter_rules(or_filters)
    assert rules, "expected at least one filter rule to be emitted"

    for rule in rules:
        # 1.6 shape: a values array, not a singular scalar.
        assert "values" in rule, f"rule must carry a values array: {rule!r}"
        assert "value" not in rule, (
            f"singular 'value' key was dropped in DataHub 1.6: {rule!r}"
        )
        assert isinstance(rule["values"], list) and rule["values"], (
            f"values must be a non-empty list: {rule!r}"
        )
        assert "field" in rule, f"rule must name a field: {rule!r}"

    # Each non-origin OR group must AND-in the origin clause (also in values form).
    fields = {r["field"] for r in rules}
    assert {"platform", "tags", "glossaryTerms", "origin"} <= fields
    origin_rules = [r for r in rules if r["field"] == "origin"]
    assert all(r["values"] == ["PROD"] for r in origin_rules)


async def test_enumerate_datasets_origin_only_single_and_clause(client, mock_graph) -> None:
    """With no OR-dimension filters but an origin set, enumerate_datasets emits a single
    AND group containing only the origin rule (in values-array form).

    Spec: spec/DATAHUB_INTEGRATION.md §Origin filter group — 'When the OR-group dimensions
    are all empty, origin becomes the single AND-clause and the enumeration returns every
    dataset with that origin.'
    """
    mock_graph.get_urns_by_filter.return_value = []

    await client.enumerate_datasets(origin="DEV")

    or_filters = mock_graph.get_urns_by_filter.call_args.kwargs["extra_or_filters"]
    rules = _all_filter_rules(or_filters)
    assert rules == [{"field": "origin", "values": ["DEV"]}]


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
