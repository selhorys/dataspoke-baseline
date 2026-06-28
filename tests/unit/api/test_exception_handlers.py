"""Unit tests for exception-to-HTTP response mapping."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    PeripheralNotConfiguredError,
    StorageUnavailableError,
)


def _make_app() -> FastAPI:
    """Create a minimal app with the exception handlers from main.py."""
    from src.api.main import create_app

    app = create_app()

    @app.get("/test/not-found")
    async def raise_not_found() -> None:
        raise EntityNotFoundError("dataset", "ds-123")

    @app.get("/test/conflict")
    async def raise_conflict() -> None:
        raise ConflictError("DUPLICATE_CONFIG", "Config already exists")

    @app.get("/test/datahub")
    async def raise_datahub() -> None:
        raise DataHubUnavailableError("GMS unreachable")

    @app.get("/test/storage")
    async def raise_storage() -> None:
        raise StorageUnavailableError("PostgreSQL unreachable")

    @app.get("/test/peripheral")
    async def raise_peripheral(name: str) -> None:
        raise PeripheralNotConfiguredError(name)

    @app.get("/test/generic")
    async def raise_generic() -> None:
        raise DataSpokeError("Something went wrong")

    class _ProbeBody(BaseModel):
        required_field: str

    @app.post("/test/probe-body")
    async def probe_body(body: _ProbeBody) -> dict:
        return {"echo": body.required_field}

    return app


@pytest.fixture
async def exc_client() -> AsyncClient:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


async def test_not_found_returns_404(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/not-found")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "DATASET_NOT_FOUND"
    assert "ds-123" in body["message"]
    assert "trace_id" in body


async def test_conflict_returns_409(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/conflict")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "DUPLICATE_CONFIG"


async def test_datahub_returns_502(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/datahub")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "DATAHUB_UNAVAILABLE"


async def test_storage_returns_503(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/storage")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "STORAGE_UNAVAILABLE"


@pytest.mark.parametrize("peripheral", ["datahub", "smtp"])
async def test_peripheral_not_configured_returns_503_with_detail(
    exc_client: AsyncClient, peripheral: str
) -> None:
    """PeripheralNotConfiguredError → 503 PERIPHERAL_NOT_CONFIGURED, detail.peripheral set.

    Covers both the new "datahub" case and the existing "smtp" case through the
    same global handler envelope.

    spec: spec/ARCHITECTURE.md §Peripheral availability contract — DataHub fails
          closed: unconfigured → 503 PERIPHERAL_NOT_CONFIGURED with
          detail.peripheral = "datahub".
    spec: spec/API.md §Application Error Codes — PERIPHERAL_NOT_CONFIGURED (503);
          detail.peripheral identifies which peripheral ("datahub" / "smtp").
    """
    resp = await exc_client.get("/test/peripheral", params={"name": peripheral})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "PERIPHERAL_NOT_CONFIGURED"
    assert body["detail"]["peripheral"] == peripheral
    assert "trace_id" in body


async def test_generic_dataspoke_returns_500(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/generic")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_ERROR"


async def test_trace_id_echoed(exc_client: AsyncClient) -> None:
    resp = await exc_client.get(
        "/test/not-found",
        headers={"X-Trace-Id": "trace-abc-123"},
    )
    body = resp.json()
    assert body["trace_id"] == "trace-abc-123"


async def test_error_response_has_required_fields(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/conflict")
    body = resp.json()
    assert "error_code" in body
    assert "message" in body
    assert "trace_id" in body


# ── RequestValidationError handler (_handle_request_validation) ──────────────
#
# Spec anchors:
#   spec/API.md §Error Catalogue (error envelope) — top-level keys: error_code,
#                                                    message, trace_id, resp_time
#   spec/API.md §Application Error Codes          — INVALID_PARAMETER → 422
#   spec/API.md §Error Catalogue (error envelope) — INVALID_PARAMETER → detail.errors
#                                                    carries FastAPI's .errors() list
#                                                    (loc/msg/type/input per failed field)
# ──────────────────────────────────────────────────────────────────────────────


async def test_missing_required_field_returns_422_standard_envelope(
    exc_client: AsyncClient,
) -> None:
    """Missing required body field → 422 INVALID_PARAMETER in standard envelope.

    Spec: spec/API.md §Application Error Codes (INVALID_PARAMETER → 422);
          spec/API.md §Error Catalogue (error envelope) (INVALID_PARAMETER →
          detail.errors with loc/msg/type/input per failed field).
    """
    resp = await exc_client.post(
        "/test/probe-body",
        json={},  # required_field is absent
    )

    assert resp.status_code == 422

    body = resp.json()

    # Standard envelope keys — spec/API.md §Error Catalogue (error envelope)
    assert body["error_code"] == "INVALID_PARAMETER"
    assert body["message"]  # non-empty
    assert "trace_id" in body
    assert "resp_time" in body

    # detail.errors carries the field-error list — spec/API.md §Error Catalogue (error envelope)
    detail = body["detail"]
    assert isinstance(detail, dict), "detail must be an object, not a list"
    errors = detail["errors"]
    assert isinstance(errors, list)
    assert len(errors) >= 1

    # Each entry must identify the offending field via loc/msg/type/input
    # spec/API.md §Error Catalogue (error envelope) — loc/msg/type/input per failed field
    first_err = errors[0]
    assert "loc" in first_err
    assert "msg" in first_err
    assert "type" in first_err
    assert "input" in first_err  # presence only; value is impl-incidental

    # loc must reference the missing field
    loc_str = ".".join(str(s) for s in first_err["loc"])
    assert "required_field" in loc_str


async def test_malformed_json_body_returns_422_standard_envelope(
    exc_client: AsyncClient,
) -> None:
    """Malformed JSON body triggers RequestValidationError → 422 standard envelope.

    Spec: spec/API.md §Application Error Codes (INVALID_PARAMETER → 422);
          spec/API.md §Error Catalogue (error envelope) (error_code, message,
          trace_id, resp_time on every error response).
    """
    resp = await exc_client.post(
        "/test/probe-body",
        content=b"{ not valid json }",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422

    body = resp.json()

    # Standard envelope — spec/API.md §Error Catalogue (error envelope)
    assert body["error_code"] == "INVALID_PARAMETER"
    assert body["message"]
    assert "trace_id" in body
    assert "resp_time" in body

    # detail must be present and be an object with an errors list
    assert "detail" in body
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert "errors" in detail
    assert isinstance(detail["errors"], list)


async def test_request_validation_error_is_not_fastapi_default_shape(
    exc_client: AsyncClient,
) -> None:
    """Standard envelope is returned, NOT FastAPI's bare {"detail": [...]} response.

    Spec: spec/API.md §Error Catalogue (error envelope) — all errors follow the
          envelope with top-level error_code, message, trace_id, resp_time.
          FastAPI's default RequestValidationError handler returns
          {"detail": [...]}, which is rejected by the spec.
    """
    resp = await exc_client.post(
        "/test/probe-body",
        json={},  # triggers RequestValidationError
    )

    body = resp.json()

    # Confirm top-level error_code present (standard envelope)
    assert "error_code" in body

    # FastAPI's default shape has "detail" as a top-level *list*; our handler
    # wraps it inside detail.errors (an object).  Unconditional: detail must
    # always be present and be an object for INVALID_PARAMETER.
    assert isinstance(body.get("detail"), dict), (
        "detail must be an object {errors: [...]}, not a bare list or absent"
    )
    assert "errors" in body["detail"]
