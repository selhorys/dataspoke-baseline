"""Dataset-URN search contract across the seven SQL-paged dataset lists.

This API-wired scenario deliberately uses public REST setup and cleanup.  The test
body never reads the operational database; its fixture may reset owned ingestion
sources as permitted by ``spec/TESTING.md``.  It is intentionally not run in the
current change because the requested dev-profile integration run is deferred.

spec: API.md §Data Resource, §Validation, §Ingestion, §Metric, §Metadata Generation
spec: TESTING.md §Api-Wired Integration Tests, §Assertion Discipline
"""

import asyncio
import time
import urllib.parse
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db

DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset(
    {"imazon.orders.events", "imazon.shipping.updates"}
)

_TITLE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_EDITIONS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)
_ORDERS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_SHIPPING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)"
)
_CATALOG_URL = "/api/v1/spoke/common/data"
_VALIDATION_URL = "/api/v1/spoke/validation"
_INGESTION_URL = "/api/v1/spoke/ingestion"
_METAGEN_URL = "/api/v1/spoke/metagen"
_GOVERNANCE_URL = "/api/v1/spoke/governance/metric"
_TITLE_CONF_URL = (
    f"/api/v1/spoke/common/data/{urllib.parse.quote(_TITLE_URN, safe='')}"
    "/attr/validation/conf"
)

# Consumed by the api-wired purge fixture; the source fixture below owns sources.
URNS_TO_PURGE: list[str] = [_TITLE_URN, _EDITIONS_URN, _ORDERS_URN, _SHIPPING_URN]


@pytest_asyncio.fixture(autouse=True)
async def _clean_owned_sources() -> AsyncGenerator[None]:
    """Give the mapping assertions an isolated source estate.

    spec: TESTING.md §Api-Wired Integration Tests — setup/teardown fixtures may use
    integration utilities while the scenario itself remains REST-only.
    """
    await dataspoke_db.reset_ingestion_sources()
    yield
    await dataspoke_db.reset_ingestion_sources()


@pytest_asyncio.fixture(autouse=True)
async def _registry_synced(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """Synchronize the seeded catalog/topics into the registered dataset set."""
    required = {_TITLE_URN, _EDITIONS_URN, _ORDERS_URN, _SHIPPING_URN}
    deadline = time.time() + 180.0
    seen: set[str] = set()
    while time.time() < deadline:
        sync = await api_client.post("/internal/activities/ingestion/sync", headers=internal_headers)
        if sync.status_code == 200:
            catalog = await api_client.get(_CATALOG_URL, headers=admin_headers, params={"limit": 500})
            if catalog.status_code == 200:
                seen = {row["dataset_urn"] for row in catalog.json()["datasets"]}
                if required <= seen:
                    return
        await asyncio.sleep(5)
    raise AssertionError(f"seeded datasets were not registered: {sorted(required - seen)}")


async def _assert_casefolded_page(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    url: str,
    content_key: str,
    needle: str,
    expected_urn: str,
    extra: dict[str, str] | None = None,
) -> None:
    """Prove substring matching and that filtered total_count precedes paging.

    The matching result and deliberately out-of-range page seed both sides of the
    pagination invariant: a server that counts before filtering, or filters after
    paging, cannot satisfy both assertions.
    """
    params = {"dataset_urn": needle, "offset": 0, "limit": 100, **(extra or {})}
    response = await client.get(url, headers=headers, params=params)
    assert response.status_code == 200, f"{url}: {response.status_code} {response.text}"
    body = response.json()
    rows = body[content_key]
    urns = [row if isinstance(row, str) else row["dataset_urn"] for row in rows]
    assert expected_urn in urns, f"case-insensitive search {needle!r} omitted {expected_urn!r}: {urns}"
    assert all(needle.casefold() in urn.casefold() for urn in urns), (
        f"search returned non-matching URNs: needle={needle!r}, rows={urns}"
    )
    assert body["total_count"] == len(rows), "wide filtered page must expose full filtered total"

    beyond = await client.get(
        url,
        headers=headers,
        params={"dataset_urn": needle, "offset": body["total_count"], "limit": 1, **(extra or {})},
    )
    assert beyond.status_code == 200, beyond.text
    beyond_body = beyond.json()
    assert beyond_body["total_count"] == body["total_count"], (
        "total_count must describe the filtered set, independent of page offset"
    )
    assert beyond_body[content_key] == [], "page after filtered total must be empty"


@pytest.mark.asyncio
async def test_uc5_dataset_urn_search_across_dataset_lists(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """Search uses case-insensitive SQL substring matching before count/paging.

    Covers the seven endpoints added to the common search contract:
    catalog, validation (three coverage modes), source mappings, unmanaged,
    metric datasets, metagen uncovered, and per-conf covered datasets.

    Spec: API.md §Data Resource, §Validation, §Ingestion, §Metric, §Metadata
    Generation — each list accepts ``dataset_urn`` as a case-insensitive substring
    filter after its existing scope predicate and before count/pagination.
    """
    source_id: str | None = None
    metagen_conf_id: str | None = None
    metric_id = f"urn-search-{uuid.uuid4().hex[:12]}"
    try:
        # Seed one covered validation dataset; editions remains registered/uncovered.
        validation = await api_client.put(
            _TITLE_CONF_URL,
            headers=admin_headers,
            json={"description": "search fixture", "variables": [{"name": "row_cnt", "description": ""}]},
        )
        assert validation.status_code in (200, 201), validation.text

        # One enabled config covers title_master but not editions.
        metagen = await api_client.post(
            f"{_METAGEN_URL}/conf",
            headers=admin_headers,
            json={
                "name": f"urn-search-{uuid.uuid4().hex[:8]}",
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": f"dataset_urn = '{_TITLE_URN}'",
                "result_limit": 1,
                "overwrite_pending": True,
            },
        )
        assert metagen.status_code == 201, metagen.text
        metagen_conf_id = metagen.json()["id"]

        # Passive Kafka scope supplies a mapped dataset while catalog rows remain unmanaged.
        source = await api_client.post(
            f"{_INGESTION_URL}/sources",
            headers=admin_headers,
            json={
                "mode": "PASSIVE",
                "name": f"urn-search-{uuid.uuid4().hex[:8]}",
                "recipe": {"source": {"type": "kafka", "config": {"topic_patterns": {"allow": ["^imazon\\..*$"]}}}},
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]
        mapping_url = f"{_INGESTION_URL}/sources/{source_id}/datasets"
        deadline = time.time() + 180.0
        while time.time() < deadline:
            sync = await api_client.post("/internal/activities/ingestion/sync", headers=internal_headers)
            assert sync.status_code == 200, sync.text
            mappings = await api_client.get(mapping_url, headers=admin_headers, params={"limit": 100})
            assert mappings.status_code == 200, mappings.text
            if _ORDERS_URN in {row["dataset_urn"] for row in mappings.json()["datasets"]}:
                break
            await asyncio.sleep(5)
        else:
            raise AssertionError("PASSIVE source did not map the seeded orders topic within 180 seconds")

        metric = await api_client.post(
            _GOVERNANCE_URL,
            headers=admin_headers,
            json={
                "metric_id": metric_id,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "URN search metric",
                "description": "scope fixture",
                "metrics": [{"name": "total", "color": "#2563EB", "idx": 1}, {"name": "doc_health", "color": "#16A34A", "idx": 2}],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": f"dataset_urn = '{_TITLE_URN}'",
            },
        )
        assert metric.status_code == 201, metric.text

        await _assert_casefolded_page(api_client, admin_headers, _CATALOG_URL, "datasets", "TiTlE_MaStEr", _TITLE_URN)
        await _assert_casefolded_page(api_client, admin_headers, _VALIDATION_URL, "validations", "TiTlE_MaStEr", _TITLE_URN, {"coverage": "covered"})
        await _assert_casefolded_page(api_client, admin_headers, _VALIDATION_URL, "validations", "EdItIoNs", _EDITIONS_URN, {"coverage": "uncovered"})
        await _assert_casefolded_page(api_client, admin_headers, _VALIDATION_URL, "validations", "CaTaLoG", _TITLE_URN, {"coverage": "both"})
        await _assert_casefolded_page(api_client, admin_headers, mapping_url, "datasets", "ImAzOn.OrDeRs", _ORDERS_URN)
        await _assert_casefolded_page(api_client, admin_headers, f"{_INGESTION_URL}/unmanaged", "dataset_urns", "EdItIoNs", _EDITIONS_URN)
        await _assert_casefolded_page(api_client, admin_headers, f"{_GOVERNANCE_URL}/{metric_id}/dataset", "datasets", "TiTlE_MaStEr", _TITLE_URN)
        await _assert_casefolded_page(api_client, admin_headers, f"{_METAGEN_URL}/uncovered", "datasets", "EdItIoNs", _EDITIONS_URN)
        await _assert_casefolded_page(
            api_client,
            admin_headers,
            f"{_METAGEN_URL}/conf/{metagen_conf_id}/dataset",
            "datasets",
            "TiTlE_MaStEr",
            _TITLE_URN,
            {"include_disallowed": "true"},
        )
    finally:
        if source_id is not None:
            with suppress(Exception):
                await api_client.delete(f"{_INGESTION_URL}/sources/{source_id}", headers=admin_headers)
        if metagen_conf_id is not None:
            with suppress(Exception):
                await api_client.delete(f"{_METAGEN_URL}/conf/{metagen_conf_id}", headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(f"{_GOVERNANCE_URL}/{metric_id}/attr/conf", headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(_TITLE_CONF_URL, headers=admin_headers)
