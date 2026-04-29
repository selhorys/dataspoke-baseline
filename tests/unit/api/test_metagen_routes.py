"""Unit tests for metagen routes:
- /spoke/common/data/{dataset_urn}/attr/metagen/* (per-dataset)
- /spoke/common/data/{dataset_urn}/method/metagen/run
- /spoke/common/metagen (cross-dataset list)
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_metagen_service
from src.api.main import app
from src.shared.exceptions import ConflictError, EntityNotFoundError

from tests.unit.api.conftest import auth_headers
from tests.unit.backend.conftest import make_metagen_result_row

_DATA_BASE = "/api/v1/spoke/common/data"
_METAGEN_BASE = "/api/v1/spoke/common/metagen"
_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,public.users,PROD)"
# URL-encode the parens for path params
_VALID_URN_ENC = _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")


def _make_config_record() -> MagicMock:
    rec = MagicMock()
    rec.id = str(uuid.uuid4())
    rec.dataset_urn = _VALID_URN
    rec.targets = ["dataset.description"]
    rec.code_refs = None
    rec.is_enabled = False
    rec.schedule_tier = None
    rec.status = "active"
    rec.owner = "test@example.com"
    rec.created_at = datetime.now(tz=UTC)
    rec.updated_at = datetime.now(tz=UTC)
    return rec


def _make_result_record() -> MagicMock:
    rec = MagicMock()
    rec.id = str(uuid.uuid4())
    rec.dataset_urn = _VALID_URN
    rec.proposals = {"dataset.description": "A test dataset."}
    rec.field_status = {"dataset.description": "pending"}
    rec.run_id = str(uuid.uuid4())
    rec.generated_at = datetime.now(tz=UTC)
    rec.last_reviewed_at = None
    return rec


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_metagen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_metagen_service, None)


# ── Auth checks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /data/{urn}/attr/metagen/conf without token returns 401."""
    resp = await client.get(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/conf"
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_metagen_list_without_token_returns_401(client) -> None:
    """GET /spoke/common/metagen without token returns 401."""
    resp = await client.get(_METAGEN_BASE)
    assert resp.status_code == 401


# ── Malformed dataset URN ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_malformed_urn_returns_422(client) -> None:
    """GET /data/{bad_urn}/attr/metagen/conf with malformed URN returns 422."""
    resp = await client.get(
        f"{_DATA_BASE}/not-a-valid-urn/attr/metagen/conf",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Conf CRUD ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200_or_201(client, mock_svc: AsyncMock) -> None:
    """PUT /data/{urn}/attr/metagen/conf returns 200 or 201."""
    config = _make_config_record()
    mock_svc.upsert_config = AsyncMock(return_value=(config, True))

    resp = await client.put(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/conf",
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "owner": "test@example.com",
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code in (200, 201)


@pytest.mark.asyncio
async def test_put_conf_invalid_target_returns_422(client, mock_svc: AsyncMock) -> None:
    """PUT /data/{urn}/attr/metagen/conf with invalid target returns 422."""
    resp = await client.put(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/conf",
        json={
            "targets": ["invalid_target"],
            "is_enabled": False,
            "owner": "test@example.com",
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── result PATCH (review) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_result_approve_returns_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /data/{urn}/attr/metagen/result/{id} with approve returns 200."""
    record = _make_result_record()
    mock_svc.review_result = AsyncMock(return_value=record)
    result_id = record.id

    resp = await client.patch(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/result/{result_id}",
        json={"verdict": "approve"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_result_malformed_uuid_returns_422(client, mock_svc: AsyncMock) -> None:
    """PATCH /data/{urn}/attr/metagen/result/{bad_id} with non-UUID returns 422."""
    resp = await client.patch(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/result/not-a-uuid",
        json={"verdict": "approve"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_result_reason_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """PATCH /data/{urn}/attr/metagen/result/{id} with reason > 2000 chars returns 422."""
    result_id = str(uuid.uuid4())
    resp = await client.patch(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/result/{result_id}",
        json={"verdict": "approve", "reason": "x" * 2001},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_result_fields_too_many_returns_422(client, mock_svc: AsyncMock) -> None:
    """PATCH /data/{urn}/attr/metagen/result/{id} with fields > 200 entries returns 422."""
    result_id = str(uuid.uuid4())
    too_many_fields = [f"column.description.col{i}" for i in range(201)]
    resp = await client.patch(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/result/{result_id}",
        json={"verdict": "approve", "fields": too_many_fields},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_result_field_entry_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """PATCH /data/{urn}/attr/metagen/result/{id} with a field entry > 512 chars returns 422."""
    result_id = str(uuid.uuid4())
    resp = await client.patch(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/attr/metagen/result/{result_id}",
        json={"verdict": "approve", "fields": ["x" * 513]},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Run endpoint ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_conflict_returns_409(client, mock_svc: AsyncMock) -> None:
    """POST /data/{urn}/method/metagen/run with GENERATION_RUNNING raises 409."""
    mock_svc.run = AsyncMock(
        side_effect=ConflictError("GENERATION_RUNNING", "Already running")
    )

    resp = await client.post(
        f"{_DATA_BASE}/{_VALID_URN_ENC}/method/metagen/run",
        json={"dry_run": False},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "GENERATION_RUNNING"


# ── Cross-dataset list ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metagen_list_returns_one_row_per_dataset(client, mock_svc: AsyncMock) -> None:
    """GET /spoke/common/metagen returns paginated cross-dataset results."""
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,a,PROD)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,b,PROD)"

    rec1 = MagicMock()
    rec1.dataset_urn = urn1
    rec1.run_id = str(uuid.uuid4())
    rec1.proposals = {}
    rec1.field_status = {}
    rec1.generated_at = datetime.now(tz=UTC)
    rec1.last_reviewed_at = None

    rec2 = MagicMock()
    rec2.dataset_urn = urn2
    rec2.run_id = str(uuid.uuid4())
    rec2.proposals = {}
    rec2.field_status = {}
    rec2.generated_at = datetime.now(tz=UTC)
    rec2.last_reviewed_at = None

    mock_svc.list_metagen = AsyncMock(return_value=([rec1, rec2], 2))

    resp = await client.get(_METAGEN_BASE, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2
    assert len(body["results"]) == 2
