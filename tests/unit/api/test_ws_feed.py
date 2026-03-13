"""Unit tests for WebSocket feed endpoints (validation + metrics).

Uses Starlette's sync TestClient for WebSocket testing since httpx.AsyncClient
does not support WebSocket connections natively.
"""

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from src.api.main import app
from tests.unit.api.conftest import make_token

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,db.table,PROD)"
_VALIDATION_WS = f"/api/v1/spoke/common/data/{_TEST_URN}/stream/validation"
_METRICS_WS = "/api/v1/spoke/dg/metric/stream"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _auth_message(groups: list[str] | None = None, subject: str = "test-user") -> str:
    if groups is None:
        groups = ["de", "da", "dg"]
    token = make_token(groups=groups, subject=subject)
    return json.dumps({"type": "auth", "token": token})


def _mock_redis(messages: list[dict]):
    """Create a mock RedisClient whose subscribe() yields *messages* as JSON strings."""

    async def _fake_subscribe(channel: str) -> AsyncIterator[str]:
        for msg in messages:
            yield json.dumps(msg)

    mock = AsyncMock()
    mock.subscribe = _fake_subscribe
    return mock


# ── Validation: auth handshake ─────────────────────────────────────────────────


class TestStreamValidationAuth:
    def test_auth_ok(self) -> None:
        messages = [
            {"type": "progress", "step": "fetch_aspects", "pct": 20, "msg": "Fetching"},
            {
                "type": "result",
                "status": "completed",
                "quality_score": 78,
                "issues": [],
                "recommendations": [],
            },
        ]
        with patch(
            "src.api.routers.spoke.common.data.get_redis", return_value=_mock_redis(messages)
        ):
            client = TestClient(app)
            with client.websocket_connect(_VALIDATION_WS) as ws:
                ws.send_text(_auth_message())
                resp = ws.receive_json()
                assert resp == {"type": "auth_ok"}

                # Should receive both messages
                msg1 = json.loads(ws.receive_text())
                assert msg1["type"] == "progress"

                msg2 = json.loads(ws.receive_text())
                assert msg2["type"] == "result"

    def test_auth_invalid_token(self) -> None:
        client = TestClient(app)
        with client.websocket_connect(_VALIDATION_WS) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": "invalid.jwt.token"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_error"
            assert resp["error_code"] == "UNAUTHORIZED"

    def test_auth_missing_type(self) -> None:
        token = make_token(groups=["de"])
        client = TestClient(app)
        with client.websocket_connect(_VALIDATION_WS) as ws:
            ws.send_text(json.dumps({"token": token}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_error"
            assert resp["error_code"] == "UNAUTHORIZED"

    def test_auth_no_token_field(self) -> None:
        client = TestClient(app)
        with client.websocket_connect(_VALIDATION_WS) as ws:
            ws.send_text(json.dumps({"type": "auth"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_error"
            assert resp["error_code"] == "UNAUTHORIZED"

    def test_auth_malformed_json(self) -> None:
        client = TestClient(app)
        with client.websocket_connect(_VALIDATION_WS) as ws:
            ws.send_text("not-json-at-all")
            resp = ws.receive_json()
            assert resp["type"] == "auth_error"
            assert resp["error_code"] == "UNAUTHORIZED"


# ── Validation: message forwarding ─────────────────────────────────────────────


class TestStreamValidationMessages:
    def test_closes_on_result(self) -> None:
        messages = [
            {"type": "progress", "step": "check_freshness", "pct": 50, "msg": "Checking"},
            {
                "type": "result",
                "status": "completed",
                "quality_score": 92,
                "issues": [],
                "recommendations": [],
            },
        ]
        with patch(
            "src.api.routers.spoke.common.data.get_redis", return_value=_mock_redis(messages)
        ):
            client = TestClient(app)
            with client.websocket_connect(_VALIDATION_WS) as ws:
                ws.send_text(_auth_message())
                ws.receive_json()  # auth_ok

                ws.receive_text()  # progress
                ws.receive_text()  # result
                # Connection should close after result

    def test_subscribes_to_correct_channel(self) -> None:
        messages = [{"type": "result", "status": "completed"}]
        mock_redis = _mock_redis(messages)

        # Track which channel is subscribed to
        original_subscribe = mock_redis.subscribe
        subscribed_channels: list[str] = []

        async def _tracking_subscribe(channel: str) -> AsyncIterator[str]:
            subscribed_channels.append(channel)
            async for msg in original_subscribe(channel):
                yield msg

        mock_redis.subscribe = _tracking_subscribe

        with patch("src.api.routers.spoke.common.data.get_redis", return_value=mock_redis):
            client = TestClient(app)
            with client.websocket_connect(_VALIDATION_WS) as ws:
                ws.send_text(_auth_message())
                ws.receive_json()  # auth_ok
                ws.receive_text()  # result

        assert len(subscribed_channels) == 1
        assert subscribed_channels[0] == f"ws:validation:{_TEST_URN}"


# ── Metrics: auth handshake ────────────────────────────────────────────────────


class TestStreamMetricsAuth:
    def test_auth_ok(self) -> None:
        messages = [
            {"type": "metric_update", "metric_id": "m1", "value": 42.0},
        ]

        async def _fake_subscribe(channel: str) -> AsyncIterator[str]:
            for msg in messages:
                yield json.dumps(msg)

        mock_redis = AsyncMock()
        mock_redis.subscribe = _fake_subscribe

        with patch("src.api.routers.spoke.dg.metrics.get_redis", return_value=mock_redis):
            client = TestClient(app)
            with client.websocket_connect(_METRICS_WS) as ws:
                ws.send_text(_auth_message(groups=["dg"]))
                resp = ws.receive_json()
                assert resp == {"type": "auth_ok"}

                msg = json.loads(ws.receive_text())
                assert msg["type"] == "metric_update"
                assert msg["metric_id"] == "m1"

    def test_auth_invalid_token(self) -> None:
        client = TestClient(app)
        with client.websocket_connect(_METRICS_WS) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": "bad.token.here"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_error"
            assert resp["error_code"] == "UNAUTHORIZED"


# ── Metrics: message forwarding ────────────────────────────────────────────────


class TestStreamMetricsMessages:
    def test_forwards_multiple_updates(self) -> None:
        messages = [
            {"type": "metric_update", "metric_id": "m1", "value": 10.0},
            {"type": "metric_update", "metric_id": "m2", "value": 20.0},
            {"type": "metric_update", "metric_id": "m3", "value": 30.0},
        ]

        with patch(
            "src.api.routers.spoke.dg.metrics.get_redis", return_value=_mock_redis(messages)
        ):
            client = TestClient(app)
            with client.websocket_connect(_METRICS_WS) as ws:
                ws.send_text(_auth_message(groups=["dg"]))
                ws.receive_json()  # auth_ok

                received = []
                for _ in range(3):
                    received.append(json.loads(ws.receive_text()))

                assert len(received) == 3
                assert [m["metric_id"] for m in received] == ["m1", "m2", "m3"]

    def test_subscribes_to_correct_channel(self) -> None:
        messages = [{"type": "metric_update", "metric_id": "m1", "value": 1.0}]

        subscribed_channels: list[str] = []

        async def _tracking_subscribe(channel: str) -> AsyncIterator[str]:
            subscribed_channels.append(channel)
            for msg in messages:
                yield json.dumps(msg)

        mock_redis = AsyncMock()
        mock_redis.subscribe = _tracking_subscribe

        with patch("src.api.routers.spoke.dg.metrics.get_redis", return_value=mock_redis):
            client = TestClient(app)
            with client.websocket_connect(_METRICS_WS) as ws:
                ws.send_text(_auth_message(groups=["dg"]))
                ws.receive_json()  # auth_ok
                ws.receive_text()  # one message

        assert subscribed_channels == ["ws:metric:updates"]
