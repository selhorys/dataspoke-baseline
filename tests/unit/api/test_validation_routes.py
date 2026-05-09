"""Unit tests for validation routes (per-dataset and cross-dataset).

Routes under test:
  GET/PUT/PATCH/DELETE /spoke/common/data/{urn}/attr/validation/conf
  POST/GET             /spoke/common/data/{urn}/attr/validation/result
  GET                  /spoke/common/validation

spec: VALIDATION.md §API Surface
spec: API.md §Data Resource (validation rows) lines 305-326
spec: API.md §Validation (/spoke/common/validation) lines 239-252
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_validation_service
from src.api.main import app
from src.shared.exceptions import (
    DataHubUnavailableError,
    EntityNotFoundError,
    PreconditionFailedError,
)

from tests.unit.api.conftest import auth_headers

_DATA_BASE = "/api/v1/spoke/common/data"
_VALIDATION_BASE = "/api/v1/spoke/common/validation"

# The dataset URN used in tests — includes parens that must be URL-encoded.
_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"
_VALID_URN_ENC = (
    _VALID_URN
    .replace("(", "%28")
    .replace(")", "%29")
    .replace(",", "%2C")
)

_CONF_URL = f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/validation/conf"
_RESULT_URL = f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/validation/result"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_conf_record(is_removed: bool = False) -> MagicMock:
    rec = MagicMock()
    rec.dataset_urn = _VALID_URN
    rec.description = "Daily row count check"
    rec.variables = ["row_cnt", "col1_mean"]
    rec.is_removed = is_removed
    rec.created_at = datetime.now(tz=UTC)
    rec.updated_at = datetime.now(tz=UTC)
    # needed for model_validate(rec)
    rec.__dict__.update({
        "dataset_urn": _VALID_URN,
        "description": "Daily row count check",
        "variables": ["row_cnt", "col1_mean"],
        "is_removed": is_removed,
        "created_at": datetime.now(tz=UTC),
        "updated_at": datetime.now(tz=UTC),
    })
    return rec


def _make_result_record() -> MagicMock:
    rec = MagicMock()
    rec.data_time = datetime(2026, 5, 1, tzinfo=UTC)
    rec.score = 1.0
    rec.variables = {"row_cnt": 50.0}
    rec.__dict__.update({
        "data_time": datetime(2026, 5, 1, tzinfo=UTC),
        "score": 1.0,
        "variables": {"row_cnt": 50.0},
    })
    return rec


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_validation_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_validation_service, None)


# ── Auth: no token → 401 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /attr/validation/conf without token returns 401.

    spec: API.md §Authentication — all spoke/common routes require valid JWT.
    """
    resp = await client.get(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_conf_without_token_returns_401(client) -> None:
    """PUT /attr/validation/conf without token returns 401."""
    resp = await client.put(
        _CONF_URL,
        json={"description": "check", "variables": ["row_cnt"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_result_without_token_returns_401(client) -> None:
    """POST /attr/validation/result without token returns 401."""
    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 1.0,
            "variables": {"row_cnt": 50.0},
        },
    )
    assert resp.status_code == 401


# ── GET /attr/validation/conf ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 200 when the config exists.

    spec: VALIDATION.md §API Surface — GET returns existing configuration.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.get_config = AsyncMock(
        return_value=ValidationConfigRecord(
            dataset_urn=_VALID_URN,
            description="Daily row count check",
            variables=["row_cnt", "col1_mean"],
            is_removed=False,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.get(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataset_urn"] == _VALID_URN
    assert data["variables"] == ["row_cnt", "col1_mean"]


@pytest.mark.asyncio
async def test_get_conf_404_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 404 when config not found.

    spec: VALIDATION.md §API Surface — 404 if not found.
    """
    mock_svc.get_config = AsyncMock(return_value=None)

    resp = await client.get(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 404


# ── PUT /attr/validation/conf ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_201_on_first_create(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/validation/conf returns 201 on first creation.

    spec: VALIDATION.md §API Surface — PUT returns 201 on create.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.upsert_config = AsyncMock(
        return_value=(
            ValidationConfigRecord(
                dataset_urn=_VALID_URN,
                description="Daily row count check",
                variables=["row_cnt", "col1_mean"],
                is_removed=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            True,  # created=True
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={
            "description": "Daily row count check",
            "variables": ["row_cnt", "col1_mean"],
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_put_conf_200_on_subsequent_update(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/validation/conf returns 200 on subsequent update.

    spec: VALIDATION.md §API Surface — PUT is create-or-replace; 200 on update.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.upsert_config = AsyncMock(
        return_value=(
            ValidationConfigRecord(
                dataset_urn=_VALID_URN,
                description="Updated check",
                variables=["row_cnt"],
                is_removed=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            False,  # created=False
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={
            "description": "Updated check",
            "variables": ["row_cnt"],
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


# ── PATCH /attr/validation/conf ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_200_with_merged_response(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/validation/conf returns 200 with the merged configuration.

    spec: VALIDATION.md §Rule Configuration — PATCH accepts partial body.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.patch_config = AsyncMock(
        return_value=ValidationConfigRecord(
            dataset_urn=_VALID_URN,
            description="Updated description",
            variables=["row_cnt", "col1_mean"],
            is_removed=False,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.patch(
        _CONF_URL,
        json={"description": "Updated description"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Updated description"


# ── DELETE /attr/validation/conf ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conf_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /attr/validation/conf returns 204 No Content.

    spec: VALIDATION.md §API Surface — DELETE returns 204.
    """
    mock_svc.delete_config = AsyncMock(return_value=None)

    resp = await client.delete(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 204


# ── POST /attr/validation/result ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_result_200(client, mock_svc: AsyncMock) -> None:
    """POST /attr/validation/result returns 200 with the recorded row.

    spec: VALIDATION.md §Validation Result — pipeline POSTs results.
    """
    from src.backend.validation.service import ValidationResultRecord

    mock_svc.record_result = AsyncMock(
        return_value=ValidationResultRecord(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )
    )

    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 1.0,
            "variables": {"row_cnt": 50.0},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 1.0


@pytest.mark.asyncio
async def test_post_result_unknown_variable_returns_422_with_code(
    client, mock_svc: AsyncMock
) -> None:
    """POST /attr/validation/result with unknown variable key → 422 UNKNOWN_VARIABLE.

    spec: VALIDATION.md §Validation rules on POST — unknown keys → 422 UNKNOWN_VARIABLE.
    """
    mock_svc.record_result = AsyncMock(
        side_effect=PreconditionFailedError(
            "UNKNOWN_VARIABLE",
            "unknown variable keys: ['bad_var']",
            detail={"unknown": ["bad_var"]},
        )
    )

    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 1.0,
            "variables": {"bad_var": 1.0},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data["error_code"] == "UNKNOWN_VARIABLE"
    assert data["detail"] == {"unknown": ["bad_var"]}


@pytest.mark.asyncio
async def test_post_result_invalid_score_returns_422_with_code(
    client, mock_svc: AsyncMock
) -> None:
    """POST /attr/validation/result with invalid score → 422 INVALID_SCORE.

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] → 422 INVALID_SCORE.
    Note: Pydantic rejects score out-of-range at the schema layer first (ge/le constraints).
    If service raises it, the HTTP layer must map it to 422.
    """
    mock_svc.record_result = AsyncMock(
        side_effect=PreconditionFailedError(
            "INVALID_SCORE",
            "score must be in [0.0, 1.0], got 1.5",
            detail={"score": 1.5},
        )
    )

    # Use a valid Pydantic payload — service raises the error (simulating internal re-check)
    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 0.5,  # valid at Pydantic level but service raises anyway for test
            "variables": {"row_cnt": 50.0},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "INVALID_SCORE"
    assert body["detail"] == {"score": 1.5}


@pytest.mark.asyncio
async def test_post_result_dataset_not_in_datahub_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """POST result when dataset not in DataHub → 422 DATASET_NOT_IN_DATAHUB.

    spec: VALIDATION.md §API Surface — write routes require dataset in DataHub.
    """
    mock_svc.record_result = AsyncMock(
        side_effect=PreconditionFailedError(
            "DATASET_NOT_IN_DATAHUB",
            "Dataset not registered in DataHub",
        )
    )

    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 1.0,
            "variables": {"row_cnt": 50.0},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "DATASET_NOT_IN_DATAHUB"


# ── GET /attr/validation/result ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_result_honors_from_until_limit_params(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/validation/result honors from/until/limit query params.

    spec: VALIDATION.md §GET result — from, until, limit query params.
    """
    mock_svc.get_results = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_RESULT_URL}?from=2026-05-01T00:00:00Z&until=2026-05-08T00:00:00Z&limit=10",
        headers=auth_headers(["da"]),
    )
    assert resp.status_code == 200
    call_kwargs = mock_svc.get_results.call_args.kwargs
    assert call_kwargs.get("limit") == 10
    assert call_kwargs.get("from_dt") is not None
    assert call_kwargs.get("until_dt") is not None


# ── GET /spoke/common/validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_validation_list_removed_true_filter(
    client, mock_svc: AsyncMock
) -> None:
    """GET /spoke/common/validation?removed=true filters to soft-deleted entries.

    spec: VALIDATION.md §API Surface — cross-dataset list filterable by removed status.
    """
    mock_svc.list_configs = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_VALIDATION_BASE}?removed=true",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    call_kwargs = mock_svc.list_configs.call_args.kwargs
    assert call_kwargs.get("removed_filter") is True


@pytest.mark.asyncio
async def test_get_validation_list_removed_false_filter(
    client, mock_svc: AsyncMock
) -> None:
    """GET /spoke/common/validation?removed=false filters to active entries.

    spec: VALIDATION.md §API Surface — cross-dataset list filterable by removed status.
    """
    mock_svc.list_configs = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_VALIDATION_BASE}?removed=false",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    call_kwargs = mock_svc.list_configs.call_args.kwargs
    assert call_kwargs.get("removed_filter") is False


# ── DatasetUrnPath URL encoding ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_with_encoded_parens_decoded_correctly(
    client, mock_svc: AsyncMock
) -> None:
    """A properly encoded dataset_urn (parens → %28/%29) reaches the service decoded.

    spec: API.md §Dataset URN path param — DatasetUrnPath validates urn:li:dataset:(...)
    spec: VALIDATION.md §Assertion URN — URN with parens is the key identifier.
    """
    from src.backend.validation.service import ValidationConfigRecord

    received_urn = None

    async def capture_get_config(dataset_urn: str):
        nonlocal received_urn
        received_urn = dataset_urn
        return ValidationConfigRecord(
            dataset_urn=dataset_urn,
            description="check",
            variables=["row_cnt"],
            is_removed=False,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    mock_svc.get_config = capture_get_config

    resp = await client.get(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    # The service must receive the fully decoded URN — full equality check
    assert received_urn == _VALID_URN, (
        f"Expected received_urn={_VALID_URN!r}, got {received_urn!r}"
    )


# ── Real service score validation (F14) ──────────────────────────────────────
# These tests use a real ValidationService (not mocked) so the service-layer
# INVALID_SCORE check is exercised end-to-end through the HTTP layer.
# Since override_service is autouse, we override get_validation_service again
# with a real ValidationService backed by a mocked DB+datahub.


def _make_real_svc_override():
    """Return a factory that injects a real ValidationService with mocked dependencies.

    The real service checks score via math.isfinite before touching the DB,
    so the mocked DB/datahub are never actually called for invalid-score requests.
    """
    from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock
    from src.backend.validation.service import ValidationService

    mock_datahub = _AsyncMock()
    mock_db = _AsyncMock()
    svc = ValidationService(datahub=mock_datahub, db=mock_db)

    async def _factory():
        return svc

    return _factory


@pytest.mark.asyncio
async def test_post_result_real_score_above_1_returns_invalid_score(client) -> None:
    """POST result with score=1.5 → 422 INVALID_SCORE (real service, not mocked).

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] → 422 INVALID_SCORE.
    The schema no longer enforces range; the service is the single source of INVALID_SCORE.
    """
    app.dependency_overrides[get_validation_service] = _make_real_svc_override()
    try:
        resp = await client.post(
            _RESULT_URL,
            json={
                "data_time": "2026-05-01T00:00:00Z",
                "score": 1.5,
                "variables": {"row_cnt": 50.0},
            },
            headers=auth_headers(["de"]),
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    assert resp.status_code == 422, (
        f"Expected 422 for score=1.5, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "INVALID_SCORE", (
        f"Expected error_code=INVALID_SCORE, got: {resp.json()}"
    )


@pytest.mark.asyncio
async def test_post_result_real_score_negative_returns_invalid_score(client) -> None:
    """POST result with score=-0.1 → 422 INVALID_SCORE (real service, not mocked).

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] → 422 INVALID_SCORE.
    """
    app.dependency_overrides[get_validation_service] = _make_real_svc_override()
    try:
        resp = await client.post(
            _RESULT_URL,
            json={
                "data_time": "2026-05-01T00:00:00Z",
                "score": -0.1,
                "variables": {"row_cnt": 50.0},
            },
            headers=auth_headers(["de"]),
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    assert resp.status_code == 422, (
        f"Expected 422 for score=-0.1, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error_code"] == "INVALID_SCORE", (
        f"Expected error_code=INVALID_SCORE, got: {resp.json()}"
    )


@pytest.mark.asyncio
async def test_post_result_real_score_nan_returns_invalid_score(client) -> None:
    """POST result with score=NaN → 422 from some layer (schema or service).

    spec: VALIDATION.md §Validation rules on POST — score must be in [0.0, 1.0];
    NaN is non-finite and must not be accepted.

    NaN handling path: the JSON spec forbids the NaN token, so stdlib json.loads raises
    JSONDecodeError before the body reaches Pydantic or the service.  FastAPI converts
    that to a 422 Unprocessable Entity, satisfying the spec contract.  We therefore
    send the raw body with the literal NaN token and assert only on the 422 status
    code — not on error_code, which is absent for JSON-parse failures.

    NaN at the service layer is covered independently by
    tests/unit/backend/test_validation_service.py::test_record_result_score_out_of_range_raises
    (passes float("nan") directly to the service).
    """
    app.dependency_overrides[get_validation_service] = _make_real_svc_override()
    try:
        raw_body = (
            b'{"data_time":"2026-05-01T00:00:00Z","score":NaN,'
            b'"variables":{"row_cnt":1.0}}'
        )
        resp = await client.post(
            _RESULT_URL,
            content=raw_body,
            headers={**auth_headers(["de"]), "Content-Type": "application/json"},
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    # FastAPI rejects the malformed JSON (NaN token) with a 422 before reaching the
    # service; the spec contract (non-finite → not accepted) is satisfied.
    assert resp.status_code == 422, (
        f"Expected 422 for NaN score, got {resp.status_code}: {resp.text}"
    )
