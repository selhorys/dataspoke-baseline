"""Unit tests verifying that all stubbed routes return 501 when authenticated.

Unauthenticated requests to protected routes must return 401, not 501.
"""

import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import auth_headers

# Headers for a user with all groups
_ALL_GROUPS = auth_headers(groups=["de", "da", "dg", "admin"])
_DE_HEADERS = auth_headers(groups=["de"])
_DG_HEADERS = auth_headers(groups=["dg"])

# ── Common/ontology ────────────────────────────────────────────────────────────
COMMON_ONTOLOGY_ROUTES = [
    ("GET", "/api/v1/spoke/common/ontology"),
    ("GET", "/api/v1/spoke/common/ontology/concept-1"),
    ("GET", "/api/v1/spoke/common/ontology/concept-1/attr"),
    ("GET", "/api/v1/spoke/common/ontology/concept-1/event"),
    ("POST", "/api/v1/spoke/common/ontology/concept-1/method/approve"),
    ("POST", "/api/v1/spoke/common/ontology/concept-1/method/reject"),
]

# ── Common/data ────────────────────────────────────────────────────────────────
_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,db.table,PROD)"
COMMON_DATA_ROUTES: list[tuple[str, str]] = [
    # GET /{urn}, GET /{urn}/attr, GET /{urn}/event — implemented by DatasetService
    # Ingestion routes — implemented by IngestionService
    # Validation routes — implemented by ValidationService
    # Generation routes — implemented by GenerationService
]

# ── Common/ingestion — implemented by IngestionService ────────────────────────
COMMON_INGESTION_ROUTES: list[tuple[str, str]] = []

# ── Common/validation — implemented by ValidationService ──────────────────────
COMMON_VALIDATION_ROUTES: list[tuple[str, str]] = []

# ── Common/gen — implemented by GenerationService ─────────────────────────────
COMMON_GEN_ROUTES: list[tuple[str, str]] = []

# ── Common/search — implemented by SearchService ──────────────────────────────
COMMON_SEARCH_ROUTES: list[tuple[str, str]] = []

# ── DG/metric ──────────────────────────────────────────────────────────────────
DG_METRIC_ROUTES = [
    ("GET", "/api/v1/spoke/dg/metric"),
    ("GET", "/api/v1/spoke/dg/metric/metric-id-1"),
    ("GET", "/api/v1/spoke/dg/metric/metric-id-1/attr"),
    ("GET", "/api/v1/spoke/dg/metric/metric-id-1/attr/conf"),
    ("PUT", "/api/v1/spoke/dg/metric/metric-id-1/attr/conf"),
    ("PATCH", "/api/v1/spoke/dg/metric/metric-id-1/attr/conf"),
    ("DELETE", "/api/v1/spoke/dg/metric/metric-id-1/attr/conf"),
    ("GET", "/api/v1/spoke/dg/metric/metric-id-1/attr/result"),
    ("POST", "/api/v1/spoke/dg/metric/metric-id-1/method/run"),
    ("POST", "/api/v1/spoke/dg/metric/metric-id-1/method/activate"),
    ("POST", "/api/v1/spoke/dg/metric/metric-id-1/method/deactivate"),
    ("GET", "/api/v1/spoke/dg/metric/metric-id-1/event"),
]

# ── DG/overview ────────────────────────────────────────────────────────────────
DG_OVERVIEW_ROUTES = [
    ("GET", "/api/v1/spoke/dg/overview"),
    ("GET", "/api/v1/spoke/dg/overview/attr"),
    ("PATCH", "/api/v1/spoke/dg/overview/attr"),
]


async def _assert_501(client: AsyncClient, method: str, path: str, headers: dict) -> None:
    resp = await client.request(method, path, headers=headers)
    assert resp.status_code == 501, f"{method} {path} → expected 501, got {resp.status_code}"


async def _assert_401(client: AsyncClient, method: str, path: str) -> None:
    resp = await client.request(method, path)
    assert resp.status_code == 401, (
        f"{method} {path} (no auth) → expected 401, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_common_ontology_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_ONTOLOGY_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_common_data_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_DATA_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_common_ingestion_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_INGESTION_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_common_validation_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_VALIDATION_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_common_gen_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_GEN_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_common_search_stubs_return_501(client: AsyncClient) -> None:
    for method, path in COMMON_SEARCH_ROUTES:
        await _assert_501(client, method, path, _DE_HEADERS)


@pytest.mark.asyncio
async def test_dg_metric_stubs_return_501(client: AsyncClient) -> None:
    for method, path in DG_METRIC_ROUTES:
        await _assert_501(client, method, path, _DG_HEADERS)


@pytest.mark.asyncio
async def test_dg_overview_stubs_return_501(client: AsyncClient) -> None:
    for method, path in DG_OVERVIEW_ROUTES:
        await _assert_501(client, method, path, _DG_HEADERS)


@pytest.mark.asyncio
async def test_unauthenticated_common_routes_return_401(client: AsyncClient) -> None:
    """Protected routes without auth must return 401, not 501."""
    for method, path in COMMON_ONTOLOGY_ROUTES[:2]:
        await _assert_401(client, method, path)


@pytest.mark.asyncio
async def test_unauthenticated_dg_routes_return_401(client: AsyncClient) -> None:
    for method, path in DG_METRIC_ROUTES[:2]:
        await _assert_401(client, method, path)
