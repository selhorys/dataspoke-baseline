"""Unit tests for exception-to-HTTP response mapping."""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, field_validator

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

    class _ValueErrorProbe(BaseModel):
        """A field whose @field_validator interpolates the submitted value into
        its ValueError message — the handler must scrub that message."""

        token: str

        @field_validator("token")
        @classmethod
        def _reject_token(cls, v: str) -> str:
            raise ValueError(f"{v!r} is not allowed")

    @app.post("/test/probe-value-error")
    async def probe_value_error(body: _ValueErrorProbe) -> dict:
        return {"token": body.token}

    @app.get("/test/raw-validation")
    async def raw_validation(value: str) -> dict:
        # Raise a *raw* pydantic.ValidationError from inside the handler body
        # (not via FastAPI request-model validation) so `_handle_validation`
        # — registered for pydantic.ValidationError — is what catches it.
        _ValueErrorProbe.model_validate({"token": value})
        return {}  # unreachable

    class _ExtraForbidProbe(BaseModel):
        """Rejects unknown keys — mirrors RuntimeConfPatchRequest's extra='forbid'."""

        model_config = ConfigDict(extra="forbid")

        real_field: str

    @app.post("/test/probe-extra-forbid")
    async def probe_extra_forbid(body: _ExtraForbidProbe) -> dict:
        return {"real_field": body.real_field}

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
#                                                    carries one {loc, msg, type} entry
#                                                    per failed field; the rejected
#                                                    value is not echoed
#   spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
#                                                    unknown write-body fields → 422
#                                                    INVALID_PARAMETER; the envelope
#                                                    names the field via
#                                                    detail.errors[].loc and never
#                                                    reproduces the offending value
# ──────────────────────────────────────────────────────────────────────────────


async def test_missing_required_field_returns_422_standard_envelope(
    exc_client: AsyncClient,
) -> None:
    """Missing required body field → 422 INVALID_PARAMETER in standard envelope.

    Spec: spec/API.md §Application Error Codes (INVALID_PARAMETER → 422);
          spec/API.md §Error Catalogue — loc/msg/type per failed field; the
          rejected value is not echoed.
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

    # Each entry identifies the offending field via loc/msg/type — and ONLY those.
    # spec/API.md §Error Catalogue — loc/msg/type per failed field; the rejected
    # value is not echoed (request bodies routinely carry credentials).
    first_err = errors[0]
    assert "loc" in first_err
    assert "msg" in first_err
    assert "type" in first_err
    assert "input" not in first_err  # the rejected value is never echoed
    assert set(first_err) == {"loc", "msg", "type"}, (
        f"entry must be exactly {{loc, msg, type}}; got {sorted(first_err)}"
    )
    # The scrub is selective: only `value_error` entries get the fixed string;
    # a built-in `missing` error keeps its value-free diagnostic message.
    # spec/API.md §Error Catalogue — detail.errors is FastAPI's .errors() list
    # (loc/msg/type per failed field); value-free built-in messages survive.
    assert first_err["msg"] != "Invalid value."

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


async def test_value_error_validator_does_not_echo_submitted_value(
    exc_client: AsyncClient,
) -> None:
    """A @field_validator ValueError never leaks the submitted value into the 422 body.

    A custom validator may interpolate the rejected value into its ValueError
    message; the handler replaces the `msg` of every `value_error` entry with a
    fixed string and drops `input`/`ctx`, so a credential-looking body value
    appears nowhere in the response — top-level `message` included.

    Spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
          the error envelope "never reproduces the offending value, because
          write-request bodies routinely carry credentials".
    Spec: spec/API.md §Error Catalogue — "The rejected value is not echoed".
    """
    secret = "sk-SEKRET-abc123"
    resp = await exc_client.post(
        "/test/probe-value-error",
        json={"token": secret},
    )

    assert resp.status_code == 422
    body = resp.json()

    assert body["error_code"] == "INVALID_PARAMETER"
    first_err = body["detail"]["errors"][0]
    # Backstop: the validator's ValueError actually drove this entry.
    assert first_err["type"] == "value_error"
    # msg is the fixed replacement, not the validator's interpolated string.
    assert first_err["msg"] == "Invalid value."
    assert "token" in ".".join(str(s) for s in first_err["loc"])

    assert secret not in json.dumps(body), (
        "the submitted value must not appear anywhere in the 422 envelope "
        "(msg, input, ctx, or top-level message) per "
        "spec/API_DESIGN_PRINCIPLE_en.md §4"
    )


async def test_extra_forbidden_rejection_does_not_echo_submitted_value(
    exc_client: AsyncClient,
) -> None:
    """An extra='forbid' rejection names the bad key via loc but never echoes its value.

    Spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
          unknown write-body fields are rejected 422 INVALID_PARAMETER; the
          envelope names the field through `detail.errors[].loc` and "never
          reproduces the offending value".
    """
    secret = "sk-SEKRET-xyz789"
    resp = await exc_client.post(
        "/test/probe-extra-forbid",
        json={"real_field": "ok", "definitely_not_a_field": secret},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "INVALID_PARAMETER"

    errors = body["detail"]["errors"]
    forbidden = [e for e in errors if e["type"] == "extra_forbidden"]
    assert len(forbidden) == 1, f"expected one extra_forbidden entry, got {errors}"
    assert forbidden[0]["loc"][-1] == "definitely_not_a_field", (
        f"loc must end with the rejected key; got {forbidden[0]['loc']}"
    )
    # The scrub is selective — an `extra_forbidden` error keeps its value-free
    # built-in message; only `value_error` msgs are replaced.
    # spec/API.md §Error Catalogue — detail.errors is FastAPI's .errors() list.
    assert forbidden[0]["msg"] != "Invalid value."

    assert secret not in json.dumps(body), (
        "the rejected value must not appear anywhere in the 422 envelope per "
        "spec/API_DESIGN_PRINCIPLE_en.md §4"
    )


async def test_raw_pydantic_validation_error_scrubbed_like_request_path(
    exc_client: AsyncClient,
) -> None:
    """A raw pydantic.ValidationError raised inside a handler is scrubbed identically
    to the request-validation path — `_handle_validation` shares the sanitiser
    with `_handle_request_validation`.

    Without this test, reverting only `_handle_validation` to emit
    `exc.errors()` verbatim would leak `input` and the interpolated
    `value_error` msg while every request-path test stayed green.

    Spec: spec/API.md §Error Catalogue — INVALID_PARAMETER → detail.errors with
          loc/msg/type per failed field; the rejected value is not echoed.
    Spec: spec/API_DESIGN_PRINCIPLE_en.md §4 (Unknown Fields in Write Requests) —
          the error envelope never reproduces the offending value.
    """
    secret = "sk-SEKRET-raw-999"
    resp = await exc_client.get("/test/raw-validation", params={"value": secret})

    assert resp.status_code == 422
    body = resp.json()

    assert body["error_code"] == "INVALID_PARAMETER"
    # Top-level message is non-empty and must not embed the rejected value —
    # the earlier code passed str(exc) here, which included pydantic's
    # `input_value=` repr. spec/API_DESIGN_PRINCIPLE_en.md §4 — the envelope
    # never reproduces the offending value.
    assert body["message"]
    assert secret not in body["message"]

    first_err = body["detail"]["errors"][0]
    assert set(first_err) == {"loc", "msg", "type"}, (
        f"entry must be exactly {{loc, msg, type}} (no `input`); got {sorted(first_err)}"
    )
    # Backstop: the value_error path is what produced this entry, and its msg was
    # replaced with the fixed string.
    assert first_err["type"] == "value_error"
    assert first_err["msg"] == "Invalid value."

    assert secret not in json.dumps(body), (
        "the rejected value must not appear anywhere in the 422 envelope "
        "(handler shares the sanitiser) per spec/API_DESIGN_PRINCIPLE_en.md §4"
    )
    assert secret not in resp.text
