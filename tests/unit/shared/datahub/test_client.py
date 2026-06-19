"""Tests for src/shared/datahub/client.py — verifies the contracts in
spec/DATAHUB_INTEGRATION.md §SDK Patterns, §GraphQL Patterns, and §Error Handling &
Resilience. Covers retry logic, circuit breaker, aspect emission (MCP wrapper), and
downstream lineage query construction."""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.shared.exceptions import DataHubUnavailableError


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
    # spec/DATAHUB_INTEGRATION.md L325: query must filter types: [DATASET]
    assert "DATASET" in query_str, (
        "GraphQL query must include types: [DATASET] per DATAHUB_INTEGRATION.md L325"
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
