"""Unit tests — _handle_datahub 502 response body and logging (Group A10).

Spec sources:
- spec/DATAHUB_INTEGRATION.md §Error Handling & Resilience
- src/api/main.py _handle_datahub function
"""

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.shared.exceptions import DataHubUnavailableError


def _make_app_with_datahub_raise(inner_message: str) -> FastAPI:
    """Create a minimal app that raises DataHubUnavailableError with the given message."""
    from src.api.main import create_app

    app = create_app()

    @app.get("/test/datahub-error")
    async def raise_datahub_error():
        raise DataHubUnavailableError(inner_message)

    return app


@pytest.fixture
async def datahub_err_client() -> AsyncClient:
    """Client for a test app that raises DataHubUnavailableError with a URL in the message."""
    app = _make_app_with_datahub_raise(
        "http://datahub-gms:8080/aspects?action=ingestProposal failed with status 503"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


# ── 502 response body ─────────────────────────────────────────────────────────


async def test_handle_datahub_returns_502(datahub_err_client: AsyncClient) -> None:
    """DATAHUB_INTEGRATION.md §Error Handling: DataHub unreachable → 502."""
    resp = await datahub_err_client.get("/test/datahub-error")
    assert resp.status_code == 502


async def test_handle_datahub_body_error_code_is_datahub_unavailable(datahub_err_client: AsyncClient) -> None:
    """DATAHUB_INTEGRATION.md §Error Handling: error_code must be DATAHUB_UNAVAILABLE."""
    resp = await datahub_err_client.get("/test/datahub-error")
    body = resp.json()
    assert body.get("error_code") == "DATAHUB_UNAVAILABLE"


async def test_handle_datahub_body_message_is_generic(datahub_err_client: AsyncClient) -> None:
    """BACKEND.md _handle_datahub: message must be a non-empty generic string (not internal GMS URL)."""
    resp = await datahub_err_client.get("/test/datahub-error")
    body = resp.json()
    # Spec: generic message, server-side log only for the inner detail.
    # Exact wording is an impl/copy choice — spec only requires non-empty and generic.
    message = body.get("message")
    assert isinstance(message, str) and message  # non-empty


async def test_handle_datahub_body_does_not_leak_internal_url(datahub_err_client: AsyncClient) -> None:
    """Security: 502 body must NOT contain the GMS URL or any internal URL."""
    resp = await datahub_err_client.get("/test/datahub-error")
    body_text = resp.text
    assert "datahub-gms" not in body_text, (
        "502 response body must not leak 'datahub-gms' hostname"
    )
    assert "ingestProposal" not in body_text, (
        "502 response body must not contain internal endpoint path"
    )


async def test_handle_datahub_body_has_trace_id(datahub_err_client: AsyncClient) -> None:
    """API contract: 502 response must include trace_id field."""
    resp = await datahub_err_client.get("/test/datahub-error")
    body = resp.json()
    assert "trace_id" in body


# ── Server-side logging ───────────────────────────────────────────────────────


async def test_handle_datahub_logs_inner_exception_at_warning(caplog) -> None:
    """BACKEND.md _handle_datahub: inner exception text logged server-side at WARN level."""
    inner_msg = "http://datahub-gms:8080/aspects?action=ingestProposal failed"
    app = _make_app_with_datahub_raise(inner_msg)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        with caplog.at_level(logging.WARNING, logger="src.api.main"):
            resp = await client.get("/test/datahub-error")

    assert resp.status_code == 502

    # Must have a WARNING log record
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) > 0, "_handle_datahub must emit a WARNING-level log"

    # Spec invariant: server-side log carries the inner exception text somewhere.
    # Build a combined string from message + full record dict (covers extra keys, exc_info, etc.)
    combined = " ".join(
        record.getMessage() + " " + str(record.__dict__)
        for record in caplog.records
    )
    assert inner_msg in combined or "datahub-gms" in combined or "ingestProposal" in combined, (
        "Server-side log must contain the inner exception detail for operator debugging"
    )


async def test_handle_datahub_generic_message_not_in_client_response(caplog) -> None:
    """Contrast: inner detail logged server-side but NOT returned to client."""
    sensitive_inner = "Connection refused: postgresql://dataspoke:s3cr3t@internal-pg:5432"
    app = _make_app_with_datahub_raise(sensitive_inner)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/test/datahub-error")

    body = resp.json()
    # Client receives a non-empty generic message (exact wording is impl/copy choice)
    message = body.get("message")
    assert isinstance(message, str) and message  # non-empty
    # Sensitive inner text must NOT appear in the client response
    assert sensitive_inner not in resp.text
    assert "s3cr3t" not in resp.text
    assert "postgresql://" not in resp.text
