"""Unit tests for DataHubClient — no real DataHub connection needed."""

from unittest.mock import MagicMock, patch

import pytest

from src.shared.exceptions import DataHubUnavailableError


@pytest.fixture
def mock_graph():
    return MagicMock()


@pytest.fixture
def mock_emitter():
    return MagicMock()


def _make_client(mock_graph, mock_emitter):
    with (
        patch("src.shared.datahub.client.DataHubGraph", return_value=mock_graph),
        patch("src.shared.datahub.client.DatahubRestEmitter", return_value=mock_emitter),
    ):
        from src.shared.datahub.client import DataHubClient

        return DataHubClient(gms_url="http://localhost:8080", token="test-token")


@pytest.mark.asyncio
async def test_get_aspect_returns_result(mock_graph, mock_emitter):
    aspect = MagicMock()
    mock_graph.get_aspect.return_value = aspect

    client = _make_client(mock_graph, mock_emitter)
    result = await client.get_aspect("urn:li:dataset:test", type(aspect))

    assert result is aspect


@pytest.mark.asyncio
async def test_get_aspect_returns_none_for_missing(mock_graph, mock_emitter):
    mock_graph.get_aspect.return_value = None

    client = _make_client(mock_graph, mock_emitter)
    result = await client.get_aspect("urn:li:dataset:missing", MagicMock)

    assert result is None


@pytest.mark.asyncio
async def test_emit_aspect_calls_emitter(mock_graph, mock_emitter):
    with patch("datahub.emitter.mcp.MetadataChangeProposalWrapper") as mcp_cls:
        client = _make_client(mock_graph, mock_emitter)
        aspect = MagicMock()
        await client.emit_aspect("urn:li:dataset:test", aspect)

        mock_emitter.emit.assert_called_once()


@pytest.mark.asyncio
async def test_check_connectivity_returns_true(mock_graph, mock_emitter):
    mock_graph.test_connection.return_value = None

    client = _make_client(mock_graph, mock_emitter)
    assert await client.check_connectivity() is True


@pytest.mark.asyncio
async def test_check_connectivity_returns_false_on_failure(mock_graph, mock_emitter):
    mock_graph.test_connection.side_effect = Exception("connection refused")

    client = _make_client(mock_graph, mock_emitter)
    # With 3 retries, this will exhaust and catch DataHubUnavailableError internally
    result = await client.check_connectivity()
    assert result is False


@pytest.mark.asyncio
async def test_retry_on_transient_error(mock_graph, mock_emitter):
    """First call fails, second succeeds — should return the result."""
    aspect = MagicMock()
    mock_graph.get_aspect.side_effect = [Exception("transient"), aspect]

    client = _make_client(mock_graph, mock_emitter)
    result = await client.get_aspect("urn:li:dataset:test", type(aspect))

    assert result is aspect
    assert mock_graph.get_aspect.call_count == 2


@pytest.mark.asyncio
async def test_exhausted_retries_raises_unavailable(mock_graph, mock_emitter):
    mock_graph.get_aspect.side_effect = Exception("persistent failure")

    client = _make_client(mock_graph, mock_emitter)
    with pytest.raises(DataHubUnavailableError, match="failed after"):
        await client.get_aspect("urn:li:dataset:test", MagicMock)


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold(mock_graph, mock_emitter):
    """After CIRCUIT_BREAKER_THRESHOLD consecutive failures, circuit opens."""
    mock_graph.get_aspect.side_effect = Exception("fail")

    client = _make_client(mock_graph, mock_emitter)

    # Exhaust retries multiple times to trip the circuit breaker
    # Each call makes RETRY_MAX_ATTEMPTS=3 failures, so after 2 calls = 6 failures > threshold=5
    for _ in range(2):
        with pytest.raises(DataHubUnavailableError):
            await client.get_aspect("urn:li:dataset:test", MagicMock)

    # Next call should fail immediately with circuit breaker open
    with pytest.raises(DataHubUnavailableError, match="Circuit breaker is open"):
        await client.get_aspect("urn:li:dataset:test", MagicMock)


@pytest.mark.asyncio
async def test_enumerate_datasets(mock_graph, mock_emitter):
    mock_graph.get_urns_by_filter.return_value = ["urn:li:dataset:a", "urn:li:dataset:b"]

    client = _make_client(mock_graph, mock_emitter)
    result = await client.enumerate_datasets()

    assert result == ["urn:li:dataset:a", "urn:li:dataset:b"]


@pytest.mark.asyncio
async def test_enumerate_datasets_with_platform_filter(mock_graph, mock_emitter):
    mock_graph.get_urns_by_filter.return_value = ["urn:li:dataset:a"]

    client = _make_client(mock_graph, mock_emitter)
    result = await client.enumerate_datasets(platform="snowflake")

    assert result == ["urn:li:dataset:a"]
    call_kwargs = mock_graph.get_urns_by_filter.call_args
    assert "snowflake" in str(call_kwargs)
