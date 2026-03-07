"""Unit tests for DataHub client wrapper."""

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


async def test_enumerate_datasets(client, mock_graph) -> None:
    urns = ["urn:li:dataset:a", "urn:li:dataset:b"]
    mock_graph.get_urns_by_filter.return_value = urns

    result = await client.enumerate_datasets()
    assert result == urns


async def test_emit_aspect_wraps_mcp(client, mock_emitter) -> None:
    aspect = MagicMock()
    await client.emit_aspect("urn:li:dataset:test", aspect)
    mock_emitter.emit_mcp.assert_called_once()
    call_args = mock_emitter.emit_mcp.call_args
    mcp = call_args[0][0]
    assert mcp.entityUrn == "urn:li:dataset:test"
