"""Unit tests for validation routes (per-dataset and cross-dataset).

Routes under test:
  GET/PUT/PATCH/DELETE /spoke/common/data/{urn}/attr/validation/conf
  POST/GET             /spoke/common/data/{urn}/attr/validation/result
  GET                  /spoke/validation

Conf request/response `variables` is an array of {name, description} objects.

spec: VALIDATION.md §API Surface
spec: API.md §Data Resource (validation rows)
spec: API.md §Validation (/spoke/validation)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_validation_service
from src.api.main import app
from src.shared.exceptions import PreconditionFailedError
from tests.unit.api.conftest import auth_headers

_DATA_BASE = "/api/v1/spoke/common/data"
_VALIDATION_BASE = "/api/v1/spoke/validation"

# The dataset URN used in tests — includes parens that must be URL-encoded.
_VALID_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_VALID_URN_ENC = _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")

_CONF_URL = f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/validation/conf"
_RESULT_URL = f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/validation/result"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _var(name: str, description: str = "") -> dict[str, str]:
    return {"name": name, "description": description}


_DEFAULT_VARS = [_var("row_cnt", "Daily row count"), _var("col1_mean", "Mean of col1")]


def _make_conf_record() -> MagicMock:
    rec = MagicMock()
    payload = {
        "dataset_urn": _VALID_URN,
        "description": "Daily row count check",
        "variables": _DEFAULT_VARS,
        "created_at": datetime.now(tz=UTC),
        "updated_at": datetime.now(tz=UTC),
    }
    for key, value in payload.items():
        setattr(rec, key, value)
    # needed for model_validate(rec)
    rec.__dict__.update(payload)
    return rec


def _make_result_record() -> MagicMock:
    rec = MagicMock()
    payload = {
        "data_time": datetime(2026, 5, 1, tzinfo=UTC),
        "score": 1.0,
        "variables": {"row_cnt": 50.0},
    }
    for key, value in payload.items():
        setattr(rec, key, value)
    rec.__dict__.update(payload)
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
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", _CONF_URL),
        ("PUT", _CONF_URL),
        ("POST", _RESULT_URL),
    ],
)
async def test_route_without_token_returns_401(client, method, url) -> None:
    """Every validation /spoke route rejects an unauthenticated request.

    The auth dependency runs before body validation, so a bodyless write is
    still rejected with 401 (not 422).

    spec: API.md §Authentication — all spoke/common routes require valid JWT.
    """
    resp = await client.request(method, url)
    assert resp.status_code == 401, (
        f"{method} {url} without a token must return 401, got {resp.status_code}"
    )


# ── GET /attr/validation/conf ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 200 when the config exists.

    spec: VALIDATION.md §API Surface — GET returns existing configuration.
    spec: VALIDATION.md §Rule Configuration — variables = [{name, description}].
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.get_config = AsyncMock(
        return_value=ValidationConfigRecord(
            dataset_urn=_VALID_URN,
            description="Daily row count check",
            variables=_DEFAULT_VARS,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataset_urn"] == _VALID_URN
    assert data["variables"] == _DEFAULT_VARS


@pytest.mark.asyncio
async def test_get_conf_404_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 404 when config not found.

    spec: VALIDATION.md §API Surface — 404 if not found.
    """
    mock_svc.get_config = AsyncMock(return_value=None)

    resp = await client.get(_CONF_URL, headers=auth_headers())
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
                variables=_DEFAULT_VARS,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            True,  # created=True
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={"description": "Daily row count check", "variables": _DEFAULT_VARS},
        headers=auth_headers(),
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
                variables=[_var("row_cnt")],
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            False,  # created=False
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={"description": "Updated check", "variables": [_var("row_cnt")]},
        headers=auth_headers(),
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
            variables=_DEFAULT_VARS,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.patch(
        _CONF_URL,
        json={"description": "Updated description"},
        headers=auth_headers(),
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

    resp = await client.delete(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 204


# ── POST /attr/validation/result ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_result_201(client, mock_svc: AsyncMock) -> None:
    """POST /attr/validation/result returns 201 Created with the recorded row.

    spec: API.md §HTTP Status Codes — POST that creates a resource returns 201.
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
        headers=auth_headers(),
    )
    assert resp.status_code == 201, (
        f"POST /attr/validation/result must return 201 Created (resource-creating POST); "
        f"got {resp.status_code}. spec: API.md §HTTP Status Codes."
    )
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
        headers=auth_headers(),
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

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] →
    422 INVALID_SCORE. The service is the single source of INVALID_SCORE.
    """
    mock_svc.record_result = AsyncMock(
        side_effect=PreconditionFailedError(
            "INVALID_SCORE",
            "score must be in [0.0, 1.0], got 1.5",
            detail={"score": 1.5},
        )
    )

    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 0.5,  # valid at Pydantic level; service raises anyway for test
            "variables": {"row_cnt": 50.0},
        },
        headers=auth_headers(),
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
        headers=auth_headers(),
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
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    call_kwargs = mock_svc.get_results.call_args.kwargs
    assert call_kwargs.get("limit") == 10
    assert call_kwargs.get("from_dt") is not None
    assert call_kwargs.get("until_dt") is not None


# ── GET /spoke/validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_validation_list_item_shape_has_no_removed_field(
    client, mock_svc: AsyncMock
) -> None:
    """GET /spoke/validation rows carry the aggregated fields and no is_removed flag.

    spec: API.md §GET /spoke/validation — the aggregated row no longer carries
    is_removed, and there is no ?removed query param (soft-delete is gone).
    """
    from src.backend.validation.service import ValidationListItem

    item = ValidationListItem(
        dataset_urn=_VALID_URN,
        description="Daily row count check",
        variable_count=2,
        latest_data_time=datetime(2026, 5, 8, tzinfo=UTC),
        latest_score=1.0,
        updated_at=datetime.now(tz=UTC),
    )
    mock_svc.list_configs = AsyncMock(return_value=([item], 1))

    resp = await client.get(f"{_VALIDATION_BASE}?limit=10", headers=auth_headers())
    assert resp.status_code == 200

    data = resp.json()
    assert "validations" in data and "total_count" in data
    rows = data["validations"]
    assert len(rows) == 1
    row = rows[0]
    assert row["dataset_urn"] == _VALID_URN
    assert row["variable_count"] == 2
    assert "is_removed" not in row, (
        "validation list row must not carry an is_removed field — soft-delete is gone. "
        f"got keys: {list(row.keys())}"
    )
    # The service is queried without any removed_filter argument.
    call_kwargs = mock_svc.list_configs.call_args.kwargs
    assert "removed_filter" not in call_kwargs


@pytest.mark.asyncio
async def test_get_validation_list_ignores_removed_query_param(
    client, mock_svc: AsyncMock
) -> None:
    """GET /spoke/validation?removed=… does not thread a removed_filter — the param is gone.

    spec: API.md §GET /spoke/validation — the ?removed query param was removed
    along with soft-delete. A stray ?removed must not become a removed_filter
    argument to the service (the filter no longer exists).
    """
    mock_svc.list_configs = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_VALIDATION_BASE}?removed=true",
        headers=auth_headers(),
    )

    # FastAPI ignores the unknown ?removed param, so the request succeeds and the
    # service IS reached — assert that first, so the test fails loudly rather than
    # passing vacuously if a future routing change rejects the request.
    assert resp.status_code == 200, (
        f"a stray ?removed must be ignored, not rejected; got {resp.status_code}: {resp.text}"
    )
    assert mock_svc.list_configs.call_args is not None, (
        "list_configs was never awaited — the ?removed request must still reach the service "
        "(the param is simply ignored)."
    )

    # The service must never receive a removed_filter argument.
    call_kwargs = mock_svc.list_configs.call_args.kwargs
    assert "removed_filter" not in call_kwargs, (
        "the ?removed param must not be threaded into the service as removed_filter"
    )


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
            variables=[_var("row_cnt")],
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    mock_svc.get_config = capture_get_config

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 200
    assert received_urn == _VALID_URN, (
        f"Expected received_urn={_VALID_URN!r}, got {received_urn!r}"
    )


# ── Recreate after delete: PUT after a hard delete returns 201 ───────────────


@pytest.mark.asyncio
async def test_put_after_delete_creates_new_conf_201(
    client, mock_svc: AsyncMock
) -> None:
    """PUT after a hard delete creates a fresh conf (201) — no resurrection concept.

    spec: API.md §DELETE attr/validation/conf — after delete the dataset reads as
    never-created; a fresh PUT creates a new conf (201). The route returns 201
    whenever the service reports created=True.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.upsert_config = AsyncMock(
        return_value=(
            ValidationConfigRecord(
                dataset_urn=_VALID_URN,
                description="Freshly recreated daily row count check",
                variables=[_var("row_cnt")],
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            True,  # created=True — a brand-new conf, not a resurrection
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={
            "description": "Freshly recreated daily row count check",
            "variables": [_var("row_cnt")],
        },
        headers=auth_headers(),
    )

    assert resp.status_code == 201, (
        f"Expected HTTP 201 for PUT after delete (new conf); "
        f"got {resp.status_code}: {resp.text}"
    )


# ── Real service score validation (F14) ──────────────────────────────────────
# These tests use a real ValidationService (not mocked) so the service-layer
# INVALID_SCORE check is exercised end-to-end through the HTTP layer.


def _make_real_svc_override():
    """Return a factory that injects a real ValidationService with mocked deps.

    The real service checks score via math.isfinite before touching the DB,
    so the mocked DB/datahub are never actually called for invalid-score requests.
    """
    from unittest.mock import AsyncMock as _AsyncMock

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

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] →
    422 INVALID_SCORE. The schema does not enforce range; the service does.
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
            headers=auth_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_SCORE"


@pytest.mark.asyncio
async def test_post_result_real_score_negative_returns_invalid_score(client) -> None:
    """POST result with score=-0.1 → 422 INVALID_SCORE (real service, not mocked).

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] →
    422 INVALID_SCORE.
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
            headers=auth_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_SCORE"


@pytest.mark.asyncio
async def test_post_result_real_score_nan_returns_invalid_score(client) -> None:
    """POST result with score=NaN → 422 from some layer (schema or service).

    spec: VALIDATION.md §Validation rules on POST — score must be in [0.0, 1.0];
    NaN is non-finite and must not be accepted.

    NaN handling path: the JSON spec forbids the NaN token, so stdlib json.loads
    raises JSONDecodeError before the body reaches Pydantic or the service.
    FastAPI converts that to a 422, satisfying the spec contract.
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
            headers={**auth_headers(), "Content-Type": "application/json"},
        )
    finally:
        app.dependency_overrides.pop(get_validation_service, None)

    assert resp.status_code == 422
