"""Unit tests for RequestLoggingMiddleware.

Tests the spec-mandated behavior of the logging middleware:
- Emits request_started event with method, path, trace_id, client_ip.
- Returns X-Trace-Id response header.
- Uses X-Trace-Id from request when provided; generates UUID when absent.

spec: API.md §Middleware (line 555) — request logging records method, path, trace_id,
      client_ip before the handler.
spec: API.md §Trace ID (lines 574-576) — X-Trace-Id assigned at layer 2; echoed in
      response; request-supplied value reused.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.api.middleware.logging import RequestLoggingMiddleware


# ── Unit tests: dispatch method in isolation ──────────────────────────────────

class _CaseInsensitiveDict(dict):
    """dict subclass that normalises keys to lower-case for case-insensitive header lookup."""

    def get(self, key: str, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)

    def __getitem__(self, key: str):
        return super().__getitem__(key.lower())

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key.lower(), value)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(str(key).lower())


class _FakeRequest:
    """Minimal Starlette-like Request stub.

    Uses a case-insensitive dict for headers so that lookup by ``"X-Trace-Id"``
    matches a header stored as ``"x-trace-id"``, mirroring Starlette's behaviour.
    """

    def __init__(self, method: str = "GET", path: str = "/health", trace_id: str | None = None):
        self.method = method
        self.url = MagicMock()
        self.url.path = path
        self.client = MagicMock()
        self.client.host = "127.0.0.1"
        headers: _CaseInsensitiveDict = _CaseInsensitiveDict()
        if trace_id:
            headers["x-trace-id"] = trace_id
        self.headers = headers


class _FakeResponse:
    """Minimal Starlette-like Response stub."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.headers: dict[str, str] = {}


@pytest.mark.asyncio
async def test_logging_middleware_emits_request_started_with_required_fields() -> None:
    """Middleware must emit request_started with method, path, trace_id, client_ip.

    spec: API.md §Middleware — request_started log record includes spec-mandated fields.
    """
    captured: list[dict] = []

    def _fake_info(event: str, **kwargs: object) -> None:
        captured.append({"event": event, **kwargs})

    request = _FakeRequest(method="GET", path="/api/v1/health")
    response = _FakeResponse(status_code=200)

    middleware = RequestLoggingMiddleware(app=MagicMock())

    async def _call_next(req):
        return response

    with patch("src.api.middleware.logging.logger") as mock_logger:
        mock_logger.info.side_effect = _fake_info
        await middleware.dispatch(request, _call_next)

    started_calls = [c for c in captured if c["event"] == "request_started"]
    assert started_calls, "request_started event must be emitted"
    started = started_calls[0]
    assert "method" in started, "request_started must include 'method'"
    assert "path" in started, "request_started must include 'path'"
    assert "trace_id" in started, "request_started must include 'trace_id'"
    assert "client_ip" in started, "request_started must include 'client_ip'"


@pytest.mark.asyncio
async def test_logging_middleware_sets_trace_id_response_header() -> None:
    """Middleware must add X-Trace-Id to the response headers.

    spec: API.md §Middleware — response carries X-Trace-Id for correlation.
    """
    request = _FakeRequest()
    response = _FakeResponse()
    middleware = RequestLoggingMiddleware(app=MagicMock())

    async def _call_next(req):
        return response

    with patch("src.api.middleware.logging.logger"):
        result = await middleware.dispatch(request, _call_next)

    assert "X-Trace-Id" in result.headers, (
        "Response must carry X-Trace-Id header for client-side correlation."
    )


@pytest.mark.asyncio
async def test_logging_middleware_uses_provided_trace_id() -> None:
    """When X-Trace-Id is in the request, middleware must echo it in the response.

    spec: API.md §Middleware — X-Trace-Id propagation; request-supplied ID echoed back.
    """
    my_trace = "my-custom-trace-123"
    request = _FakeRequest(trace_id=my_trace)
    response = _FakeResponse()
    middleware = RequestLoggingMiddleware(app=MagicMock())

    async def _call_next(req):
        return response

    with patch("src.api.middleware.logging.logger"):
        result = await middleware.dispatch(request, _call_next)

    assert result.headers.get("X-Trace-Id") == my_trace, (
        f"Expected echoed trace_id={my_trace!r}, got {result.headers.get('X-Trace-Id')!r}"
    )


@pytest.mark.asyncio
async def test_logging_middleware_generates_uuid_when_no_trace_id() -> None:
    """When X-Trace-Id is absent, middleware generates a UUID and sets it as the trace_id.

    spec: API.md §Middleware — auto-generate trace_id when not provided by caller.
    """
    import re

    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    request = _FakeRequest(trace_id=None)
    response = _FakeResponse()
    middleware = RequestLoggingMiddleware(app=MagicMock())

    async def _call_next(req):
        return response

    with patch("src.api.middleware.logging.logger"):
        result = await middleware.dispatch(request, _call_next)

    trace_id = result.headers.get("X-Trace-Id", "")
    assert _UUID_RE.match(trace_id), (
        f"Auto-generated trace_id must be a valid UUID v4; got {trace_id!r}"
    )
