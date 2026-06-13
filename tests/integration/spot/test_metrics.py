"""Spot tests for Governance Metrics endpoints.

Concerns covered (each test targets one spec contract):
- GET /spoke/governance/metric — factory-seeded entries present after reset
- factory defaults have time_window_sec=172800 for windowed types
- POST /spoke/governance/metric → 201 (create), 409 METRIC_EXISTS (duplicate), 422 (bad metric_id)
- PUT .../attr/conf → 200 replace existing, 404 METRIC_NOT_FOUND when absent
- POST create + PUT replace full flow (create → duplicate → replace → absent 404)
- PATCH/GET/DELETE round-trip on a custom metric
- POST mode='passive' → 501 NOT_IMPLEMENTED
- PUT mode='passive' → 501 NOT_IMPLEMENTED
- PUT metric_type='bogus' → 422
- PUT ingestion-freshness with metric_conf={} (missing time_window_sec) → 422
- PUT ingestion-freshness with time_window_sec=-1 → 422
- PUT doc-health with non-empty metric_conf → 422
- PUT ingestion-freshness with unknown metrics[] key → 422
- PUT dataset_filter.dataset_urns > 1000 entries → 422
- PUT dataset_filter.dataset_urns == 1000 entries → 200/201
- PUT dataset_filter.dataset_urns=['not-a-urn'] → 422 INVALID_DATASET_URN
- PUT metric_id with invalid path chars → 422
- ingestion-freshness per-dataset window: ACTIVE_CUSTOM_MANAGED daily (in-time and stale),
  PASSIVE (boundary ~7200s), no ingestion_source_dataset row (uses metric_conf fallback)
- validation-score per-dataset window: ≥ N+1 rows derive window from intervals
  (window_source='intervals'); sparse < N+1 rows fall back (window_source='default')
- POST method/run dry_run=true → no persisted result, no RUN_COMPLETE event
- POST method/run dry_run=false → values is dict[str, float]
- POST method/run concurrent → 409 METRIC_RUNNING
- breakdown.datasets[] has no 'category' field
- metric_id kebab regex acceptance and rejection

Spot is the right layer for the window-math tests (ingestion-freshness and
validation-score per-dataset windows) because they require raw ORM/SQL-seeded state
(events, validation_results, ingestion_source/ingestion_source_dataset with controlled
timestamps) that the api-wired pipeline cannot naturally produce.
Tests insert rows directly via asyncpg and clean up in `finally` blocks.

Spec:
- spec/USE_CASE_en.md §UC5 — Factory defaults, Built-in active metric types, API Mapping
- spec/API.md §Metric (/spoke/governance/metric) — field rules, payload caps, error codes,
  create/replace
- spec/feature/BACKEND.md §Metrics Service §Breakdown format, §Create vs replace, §Time windows
"""

import asyncio
import json
import os
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

# Per-module dummy-data seed: re-seed catalog schema in PG and ingest into DataHub
# before this module's tests run (autoused by tests/integration/conftest.py).
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Bounded URN used in run tests to minimise DataHub I/O.
# Spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master
_BOUNDED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.title_master,DEV)"
)

# Factory-seeded metric IDs
# Spec: spec/USE_CASE_en.md §UC5 §Factory defaults
_FACTORY_IDS = {"ingestion-freshness", "validation-score", "doc-health"}

# Per-dataset freshness windows per USE_CASE_en.md §UC5: tier period x2 (daily 172800,
# hourly/passive 7200). Spec literals, not derived from src/shared/schedule.py.
_DAILY_WINDOW_SEC = 86400 * 2    # 172800
_HOURLY_WINDOW_SEC = 3600 * 2   # 7200
_PASSIVE_WINDOW_SEC = 3600 * 2  # 7200

# Factory default fallback time_window_sec
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — factory default 172800
_FACTORY_DEFAULT_TIME_WINDOW_SEC = 172800


async def _get_ds_conn() -> asyncpg.Connection:
    """Open a direct asyncpg connection to the DataSpoke operational DB."""
    return await asyncpg.connect(
        host=os.environ.get("DATASPOKE_TEST_POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("DATASPOKE_TEST_POSTGRES_PORT", "9201")),
        user=os.environ.get("DATASPOKE_TEST_POSTGRES_USER", "dataspoke"),
        password=os.environ.get("DATASPOKE_TEST_POSTGRES_PASSWORD", ""),
        database=os.environ.get("DATASPOKE_TEST_POSTGRES_DB", "dataspoke"),
    )


# ── Factory defaults ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_factory_defaults_present_after_reset(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/governance/metric returns the three factory-seeded entries.

    Each must have is_enabled=False, mode='active', schedule_tier='daily'.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — seeds ship disabled,
          mode='active', schedule_tier='daily'.
    """
    resp = await api_client.get(
        "/api/v1/spoke/governance/metric?limit=100",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body

    by_id = {m["id"]: m for m in body["metrics"]}
    for fid in _FACTORY_IDS:
        assert fid in by_id, (
            f"Factory metric '{fid}' not found in list. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )
        m = by_id[fid]
        assert m["is_enabled"] is False, (
            f"Factory metric '{fid}' must be is_enabled=False. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )
        assert m["mode"] == "active", (
            f"Factory metric '{fid}' must be mode='active'. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )
        assert m["schedule_tier"] == "daily", (
            f"Factory metric '{fid}' must have schedule_tier='daily'. "
            "Spec: spec/USE_CASE_en.md §UC5 §Factory defaults."
        )


# ── Factory default time_window_sec ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_factory_defaults_time_window_sec_is_172800(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Factory-seeded ingestion-freshness and validation-score have time_window_sec=172800.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_conf
          factory default time_window_sec is 172800 (2-day fallback window).
    Spec: spec/feature/BACKEND.md §Metrics Service §Factory defaults —
          metric_conf={"time_window_sec": 172800} for the two windowed types.
    """
    for metric_id in ("ingestion-freshness", "validation-score"):
        resp = await api_client.get(
            f"/api/v1/spoke/governance/metric/{metric_id}/attr/conf",
            headers=admin_headers,
        )
        assert resp.status_code == 200, (
            f"Factory metric '{metric_id}' must exist; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        tw = body.get("metric_conf", {}).get("time_window_sec")
        assert tw == _FACTORY_DEFAULT_TIME_WINDOW_SEC, (
            f"Factory '{metric_id}' metric_conf.time_window_sec must be "
            f"{_FACTORY_DEFAULT_TIME_WINDOW_SEC}, got {tw}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )


# ── Create/replace flow ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_replace_flow(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST create (201) → POST same id (409 METRIC_EXISTS) → PUT replace (200) →
    PUT absent id (404) → bad metric_id (422).

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — POST /spoke/governance/metric creates;
          PUT .../attr/conf replaces an existing definition and returns 404 when absent.
    Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace.
    """
    _METRIC_ID = "spot-create-replace-flow"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    create_url = "/api/v1/spoke/governance/metric"

    _CREATE_BODY = {
        "metric_id": _METRIC_ID,
        "mode": "active",
        "is_enabled": False,
        "metric_type": "doc-health",
        "title": "Create Replace Flow",
        "description": "Spot test for create/replace semantics",
        "metrics": ["total", "doc_health"],
        "metric_conf": {},
        "schedule_tier": "daily",
        "dataset_filter": {},
    }

    # Ensure clean state
    await api_client.delete(base_conf, headers=admin_headers)

    try:
        # 1. POST create → 201
        create_resp = await api_client.post(
            create_url,
            headers=admin_headers,
            json=_CREATE_BODY,
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/governance/metric must return 201 on create; "
            f"got {create_resp.status_code}: {create_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        assert create_resp.json()["id"] == _METRIC_ID

        # 2. POST same id → 409 METRIC_EXISTS
        dup_resp = await api_client.post(
            create_url,
            headers=admin_headers,
            json=_CREATE_BODY,
        )
        assert dup_resp.status_code == 409, (
            f"POST with duplicate metric_id must return 409; "
            f"got {dup_resp.status_code}: {dup_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping — colliding id returns 409 METRIC_EXISTS."
        )
        assert dup_resp.json().get("error_code") == "METRIC_EXISTS", (
            f"Expected error_code='METRIC_EXISTS'; got {dup_resp.json().get('error_code')!r}. "
            "Spec: spec/API.md §Error Catalogue."
        )

        # 3. PUT replace existing → 200
        replace_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Replaced Title",
                "description": "Replaced description",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "weekly",
                "dataset_filter": {},
            },
        )
        assert replace_resp.status_code == 200, (
            f"PUT .../attr/conf must return 200 when replacing existing; "
            f"got {replace_resp.status_code}: {replace_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        assert replace_resp.json()["title"] == "Replaced Title"
        assert replace_resp.json()["is_enabled"] is True

        # 4. DELETE the metric so we can test 404 on absent PUT
        del_resp = await api_client.delete(base_conf, headers=admin_headers)
        assert del_resp.status_code == 204

        # 5. PUT absent id → 404 METRIC_NOT_FOUND
        absent_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Should Fail",
                "description": "PUT on absent id",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": {},
            },
        )
        assert absent_resp.status_code == 404, (
            f"PUT .../attr/conf must return 404 when id is absent; "
            f"got {absent_resp.status_code}: {absent_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping — "
            "PUT returns 404 METRIC_NOT_FOUND when absent."
        )
        assert absent_resp.json().get("error_code") == "METRIC_NOT_FOUND", (
            "Expected error_code='METRIC_NOT_FOUND'; "
            f"got {absent_resp.json().get('error_code')!r}. "
            "Spec: spec/API.md §Error Catalogue."
        )

        # 6. Bad-format metric_id in POST body → 422
        bad_id_resp = await api_client.post(
            create_url,
            headers=admin_headers,
            json={**_CREATE_BODY, "metric_id": "UPPER_INVALID"},
        )
        assert bad_id_resp.status_code == 422, (
            f"POST with uppercase metric_id must return 422; got {bad_id_resp.status_code}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Create vs replace — bad format → 422."
        )

    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


# ── CRUD round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_get_patch_delete_round_trip(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST create / GET / PATCH / DELETE round-trip on a custom doc-health metric.

    Spec: spec/API.md §Metric — POST creates, PATCH updates fields, DELETE returns 204.
    """
    _METRIC_ID = "doc-health-custom"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    create_url = "/api/v1/spoke/governance/metric"

    # Ensure clean state
    await api_client.delete(base_conf, headers=admin_headers)

    try:
        # POST create
        create_resp = await api_client.post(
            create_url,
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Doc Health Custom",
                "description": "Custom doc-health for tests",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "weekly",
                "dataset_filter": {"origin": "DEV"},
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        create_body = create_resp.json()
        assert create_body["id"] == _METRIC_ID
        assert create_body["metric_type"] == "doc-health"
        assert create_body["is_enabled"] is True
        assert create_body["schedule_tier"] == "weekly"

        # GET
        get_resp = await api_client.get(base_conf, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["id"] == _METRIC_ID
        assert get_body["is_enabled"] is True
        assert get_body["metric_type"] == "doc-health"

        # PATCH
        patch_resp = await api_client.patch(
            base_conf,
            headers=admin_headers,
            json={"is_enabled": False},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_enabled"] is False

        # DELETE
        del_resp = await api_client.delete(base_conf, headers=admin_headers)
        assert del_resp.status_code == 204

        # Verify gone
        gone_resp = await api_client.get(base_conf, headers=admin_headers)
        assert gone_resp.status_code == 404

    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


# ── Validation rejections ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_passive_mode_returns_501(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /spoke/governance/metric with mode='passive' → 501 NOT_IMPLEMENTED.

    Spec: spec/USE_CASE_en.md §UC5 §Modes — passive is reserved;
          POST with mode:'passive' returns 501 NOT_IMPLEMENTED.
    Spec: spec/API.md §Metric.
    """
    resp = await api_client.post(
        "/api/v1/spoke/governance/metric",
        headers=admin_headers,
        json={
            "metric_id": "spot-passive-test-post",
            "mode": "passive",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Passive Test",
            "description": "Should fail",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive' on POST create, got {resp.status_code}: {resp.text}. "
        "Spec: spec/USE_CASE_en.md §UC5 §Modes."
    )
    body = resp.json()
    assert body.get("error_code") == "NOT_IMPLEMENTED", (
        f"Expected error_code='NOT_IMPLEMENTED', got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_put_passive_mode_returns_501(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with mode='passive' → 501 NOT_IMPLEMENTED.

    Spec: spec/API.md §Metric — passive is reserved; PUT with mode: passive returns
          501 NOT_IMPLEMENTED.
    Spec: spec/USE_CASE_en.md §UC5 §Modes.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/passive-test/attr/conf",
        headers=admin_headers,
        json={
            "mode": "passive",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Passive Test",
            "description": "Should fail",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 501, (
        f"Expected 501 for mode='passive', got {resp.status_code}: {resp.text}. "
        "Spec: spec/API.md §Metric — passive mode returns 501 NOT_IMPLEMENTED."
    )
    body = resp.json()
    assert body.get("error_code") == "NOT_IMPLEMENTED", (
        f"Expected error_code='NOT_IMPLEMENTED', got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_unknown_metric_type_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with metric_type='bogus' → 422.

    Spec: spec/API.md §Metric — unsupported values return 422 INVALID_PARAMETER.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/bogus-type-test/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "bogus",
            "title": "Bogus Type",
            "description": "Should fail",
            "metrics": [],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for unknown metric_type, got {resp.status_code}: {resp.text}. "
        "Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_metric_conf_missing_time_window_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT ingestion-freshness with metric_conf={} (no time_window_sec) → 422.

    Spec: spec/API.md §Metric — ingestion-freshness requires time_window_sec (positive int).
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-missing-tw/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Missing time_window",
            "description": "Should fail",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for missing time_window_sec on ingestion-freshness, "
        f"got {resp.status_code}: {resp.text}. Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_metric_conf_negative_time_window_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT ingestion-freshness with time_window_sec=-1 → 422.

    Spec: spec/API.md §Metric — time_window_sec must be positive int.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-neg-tw/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Negative time_window",
            "description": "Should fail",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": -1},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for negative time_window_sec, got {resp.status_code}: {resp.text}. "
        "Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_doc_health_nonempty_metric_conf_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT doc-health with non-empty metric_conf → 422.

    Spec: spec/API.md §Metric — doc-health takes metric_conf={}.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-dochealth-conf/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Doc Health conf check",
            "description": "Should fail",
            "metrics": ["total", "doc_health"],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for non-empty metric_conf on doc-health, "
        f"got {resp.status_code}: {resp.text}. Spec: spec/API.md §Metric."
    )


@pytest.mark.asyncio
async def test_metrics_list_unknown_key_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT ingestion-freshness with metrics=['nope'] → 422.

    Spec: spec/API.md §Metric — unknown metrics[] keys return 422 INVALID_PARAMETER.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-unknown-key/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "ingestion-freshness",
            "title": "Unknown metrics key",
            "description": "Should fail",
            "metrics": ["nope"],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for unknown metrics key, got {resp.status_code}: {resp.text}. "
        "Spec: spec/API.md §Metric."
    )


# ── dataset_filter cap boundary ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dataset_filter_cap_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT dataset_filter.dataset_urns=[<1001 well-formed urns>] → 422.

    Spec: spec/API.md §Metric §Payload caps — dataset_urns ≤ 1,000 entries.
    """
    urns_1001 = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)"
        for i in range(1001)
    ]
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-cap-over/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Cap over",
            "description": "Should fail on cap",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {"dataset_urns": urns_1001},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for 1001 dataset_urns (over cap), got {resp.status_code}. "
        "Spec: spec/API.md §Metric §Payload caps — cap is 1,000."
    )


@pytest.mark.asyncio
async def test_dataset_filter_at_cap_accepted(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST dataset_filter.dataset_urns=[<1000 well-formed urns>] → 201.

    Spec: spec/API.md §Metric §Payload caps — exactly 1,000 MUST be accepted.
    """
    _METRIC_ID = "spot-cap-at"
    urns_1000 = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)"
        for i in range(1000)
    ]
    base = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    # Ensure clean state
    await api_client.delete(base, headers=admin_headers)
    try:
        resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Cap at boundary",
                "description": "Should succeed at cap",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": urns_1000},
            },
        )
        assert resp.status_code == 201, (
            f"Expected 201 for 1000 dataset_urns (at cap), got {resp.status_code}: {resp.text}. "
            "Spec: spec/API.md §Metric §Payload caps — exactly 1,000 MUST be accepted."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_invalid_dataset_urn_format_returns_422_with_specific_code(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT dataset_filter.dataset_urns=['not-a-urn'] → 422 INVALID_DATASET_URN.

    Spec: spec/API.md §Metric — dataset_urns URN format validated at PUT/PATCH
          (422 INVALID_DATASET_URN).
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-bad-urn/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Bad URN",
            "description": "Should fail on URN format",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {"dataset_urns": ["not-a-urn"]},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for malformed dataset URN, got {resp.status_code}: {resp.text}. "
        "Spec: spec/API.md §Metric — 422 INVALID_DATASET_URN."
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        f"Expected error_code='INVALID_DATASET_URN', got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_invalid_metric_id_path_param_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT /metric/UPPER!/attr/conf → 422 (FastAPI path regex rejection).

    Spec: spec/API.md §Metric — metric_id is kebab-case slug:
          ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$.
    """
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/UPPER!/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Invalid ID",
            "description": "Should fail on path param regex",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for invalid metric_id path param, got {resp.status_code}. "
        "Spec: spec/API.md §Metric — metric_id kebab-case regex."
    )


# ── Dry-run semantics ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_does_not_persist_result_or_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/run with dry_run=true: returns run_id + status + detail with values,
    but does NOT persist a result row and does NOT emit a METRIC.RUN_COMPLETE event
    (or if emitted, detail.dry_run is True).

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — dry_run: true evaluates without persisting.
    Spec: spec/API.md §Metric — POST method/run dry_run.
    """
    base_conf = "/api/v1/spoke/governance/metric/ingestion-freshness/attr/conf"
    base_run = "/api/v1/spoke/governance/metric/ingestion-freshness/method/run"
    base_results = "/api/v1/spoke/governance/metric/ingestion-freshness/attr/result"
    base_events = "/api/v1/spoke/governance/metric/ingestion-freshness/event"

    # Enable the seeded metric and scope to bounded URN
    patch_resp = await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # Snapshot counts before dry-run
    pre_results = await api_client.get(f"{base_results}?limit=100", headers=admin_headers)
    pre_count = pre_results.json().get("total_count", 0)

    pre_events = await api_client.get(f"{base_events}?limit=100", headers=admin_headers)
    pre_event_count = len([
        e for e in pre_events.json().get("events", [])
        if e.get("event_type") == "METRIC.RUN_COMPLETE"
    ])

    # Dry-run
    run_resp = await api_client.post(
        f"{base_run}?dry_run=true",
        headers=admin_headers,
    )
    assert run_resp.status_code == 200, run_resp.text
    run_body = run_resp.json()

    # Response must carry run_id, status, and detail with values dict
    assert "run_id" in run_body, "dry-run response must carry run_id. Spec: spec/API.md §Metric."
    assert "status" in run_body, "dry-run response must carry status. Spec: spec/API.md §Metric."
    detail = run_body.get("detail", {})
    if "values" in detail:
        assert isinstance(detail["values"], dict), (
            "detail.values must be a dict when present. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )

    # (a) No new result row persisted
    post_results = await api_client.get(f"{base_results}?limit=100", headers=admin_headers)
    post_count = post_results.json().get("total_count", 0)
    assert post_count == pre_count, (
        f"dry_run persisted a result: result count went from {pre_count} to {post_count}. "
        "Spec: spec/USE_CASE_en.md §UC5 — dry_run=true does not persist."
    )

    # (b) No new METRIC.RUN_COMPLETE event (or if emitted it has dry_run=True in detail)
    post_events = await api_client.get(f"{base_events}?limit=100", headers=admin_headers)
    post_complete = [
        e for e in post_events.json().get("events", [])
        if e.get("event_type") == "METRIC.RUN_COMPLETE"
    ]
    new_complete = post_complete[pre_event_count:]
    for ev in new_complete:
        assert ev.get("detail", {}).get("dry_run") is True, (
            "Any METRIC.RUN_COMPLETE emitted by dry_run must have detail.dry_run=True. "
            "Spec: spec/USE_CASE_en.md §UC5."
        )

    # Restore factory default state
    await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={"is_enabled": False, "dataset_filter": {}},
    )


@pytest.mark.asyncio
async def test_metric_run_persists_values_dict(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/run dry_run=false → persists result row; values is dict[str, float].

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — ingestion-freshness
          emits {total, ingested_in_time}.
    Spec: spec/API.md §Metric — attr/result carries values: dict[str,float].
    """
    base_conf = "/api/v1/spoke/governance/metric/ingestion-freshness/attr/conf"
    base_run = "/api/v1/spoke/governance/metric/ingestion-freshness/method/run"
    base_results = "/api/v1/spoke/governance/metric/ingestion-freshness/attr/result"

    # Enable metric, scope to bounded URN
    patch_resp = await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        )
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json().get("status") == "success"

    results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
    assert results_resp.status_code == 200
    results = results_resp.json().get("results", [])
    assert results, (
        "Non-dry-run must persist at least one result row. "
        "Spec: spec/USE_CASE_en.md §UC5."
    )
    row = results[0]

    # values must be a dict with at least the keys declared in the metric's metrics list
    assert isinstance(row["values"], dict), (
        "result.values must be a dict. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    # ingestion-freshness emits at minimum {total, ingested_in_time}
    assert "total" in row["values"], (
        "ingestion-freshness values must include 'total'. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert "ingested_in_time" in row["values"], (
        "ingestion-freshness values must include 'ingested_in_time'. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert all(isinstance(v, (int, float)) for v in row["values"].values()), (
        "All values must be numeric. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )

    # Restore factory state
    await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={"is_enabled": False, "dataset_filter": {}},
    )


# ── Concurrency guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_run_concurrent_returns_409(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Two concurrent POST .../method/run calls → second returns 409 METRIC_RUNNING.

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — concurrent runs return 409 METRIC_RUNNING.
    Spec: spec/API.md §Metric — POST method/run concurrent runs return 409 METRIC_RUNNING.
    """
    _CONCURRENT_ID = "spot-test-concurrent"
    base_conf = f"/api/v1/spoke/governance/metric/{_CONCURRENT_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_CONCURRENT_ID}/method/run"

    await api_client.delete(base_conf, headers=admin_headers)

    create_resp = await api_client.post(
        "/api/v1/spoke/governance/metric",
        headers=admin_headers,
        json={
            "metric_id": _CONCURRENT_ID,
            "mode": "active",
            "is_enabled": True,
            "metric_type": "ingestion-freshness",
            "title": "Concurrent Guard Test",
            "description": "Tests concurrent run guard.",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
        },
    )
    assert create_resp.status_code == 201, create_resp.text

    async with httpx.AsyncClient(
        base_url=api_client.base_url, timeout=120.0
    ) as concurrent_client:

        async def _fire():
            return await concurrent_client.post(
                base_run,
                headers=admin_headers,
                )

        results = await asyncio.gather(
            _fire(), _fire(), _fire(), _fire(), _fire(),
            return_exceptions=True,
        )

    status_codes = [
        r.status_code for r in results if isinstance(r, httpx.Response)
    ]
    assert 200 in status_codes, (
        "At least one concurrent run must succeed (200). "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    assert 409 in status_codes, (
        f"At least one concurrent run must return 409 METRIC_RUNNING; got {status_codes}. "
        "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
    )
    conflict_resp = next(
        r for r in results if isinstance(r, httpx.Response) and r.status_code == 409
    )
    assert conflict_resp.json().get("error_code") == "METRIC_RUNNING", (
        "Expected error_code='METRIC_RUNNING'. "
        "Spec: spec/API.md §Error Catalogue."
    )

    await api_client.delete(base_conf, headers=admin_headers)


# ── Breakdown shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_datasets_has_no_category_field(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """After a run, breakdown.datasets[] entries have keys {urn, detail?}; no 'category'.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] carries only failed entries: {urn, detail?}. No 'category' field.
    """
    base_conf = "/api/v1/spoke/governance/metric/doc-health/attr/conf"
    base_run = "/api/v1/spoke/governance/metric/doc-health/method/run"
    base_results = "/api/v1/spoke/governance/metric/doc-health/attr/result"

    # Enable doc-health, scope to bounded URN
    patch_resp = await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        )
    assert run_resp.status_code == 200, run_resp.text

    results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
    assert results_resp.status_code == 200
    results = results_resp.json().get("results", [])
    assert results, "Expected at least one result row after a successful run."

    breakdown = results[0].get("breakdown", {})
    for entry in breakdown.get("datasets", []):
        assert "urn" in entry, (
            "Breakdown entry must have 'urn'. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert "category" not in entry, (
            "Breakdown entry must NOT have 'category'. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert isinstance(entry.get("detail", {}), dict), (
            "Breakdown entry 'detail' must be a dict when present. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )

    # Restore
    await api_client.patch(
        base_conf,
        headers=admin_headers,
        json={"is_enabled": False, "dataset_filter": {}},
    )


# ── Unresolved URN reporting ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_run_unresolved_urns_in_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """METRIC.RUN_COMPLETE event carries unresolved_urns and expected detail shape.

    A URN present in dataset_filter.dataset_urns but absent from DataHub must appear
    in the METRIC.RUN_COMPLETE event's unresolved_urns field.  The event detail must
    also carry the full key set prescribed by the Event Catalogue.

    Spec: spec/USE_CASE_en.md §UC5 §dataset_filter — unresolved-at-runtime entries
          are skipped and reported in the METRIC.RUN_COMPLETE event's unresolved_urns.
    Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE detail keys:
          {run_id, metric_id, values, dry_run, unresolved_urns, breakdown_summary};
          breakdown_summary has {dataset_count, affected_count}.
    Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE detail key run_id.
    """
    _GHOST_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        "nonexistent.ghost.table,DEV)"
    )
    # Use a fresh custom metric to avoid mutating the factory-seeded ingestion-freshness
    # without a guaranteed-restore guard.  Pattern mirrors test_dataset_filter_at_cap_accepted.
    _METRIC_ID = "unresolved-urns-spot-test"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_events = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/event"

    # Ensure clean state before creating
    await api_client.delete(base_conf, headers=admin_headers)

    try:
        # POST a fresh metric scoped to a ghost URN only — fast and deterministic.
        put_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Unresolved URN Spot Test",
                "description": "Verifies ghost URN appears in unresolved_urns event field.",
                "metrics": ["total", "ingested_in_time"],
                "metric_conf": {"time_window_sec": 172800},
                "dataset_filter": {"dataset_urns": [_GHOST_URN]},
                "schedule_tier": None,
            },
        )
        assert put_resp.status_code == 201, put_resp.text

        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            )
        assert run_resp.status_code == 200, run_resp.text
        # Capture the run_id to match the event exactly — avoids relying on default sort order.
        # Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE detail key run_id.
        target_run_id = run_resp.json()["run_id"]

        events_resp = await api_client.get(
            f"{base_events}?limit=20",
            headers=admin_headers,
        )
        assert events_resp.status_code == 200

        complete_events = [
            e for e in events_resp.json().get("events", [])
            if e.get("event_type") == "METRIC.RUN_COMPLETE"
        ]

        # Select event by run_id, not list position.
        event = next(
            (e for e in complete_events if e["detail"]["run_id"] == target_run_id),
            None,
        )
        assert event is not None, (
            f"No METRIC.RUN_COMPLETE event for run_id={target_run_id}. "
            "Spec: spec/feature/BACKEND.md §Event Catalogue."
        )
        detail = event["detail"]

        # Ghost URN must appear in unresolved_urns.
        # Spec: spec/USE_CASE_en.md §UC5 §dataset_filter.
        assert isinstance(detail.get("unresolved_urns"), list), (
            "METRIC.RUN_COMPLETE detail.unresolved_urns must be a list. "
            "Spec: spec/USE_CASE_en.md §UC5 §dataset_filter."
        )
        assert _GHOST_URN in detail["unresolved_urns"], (
            f"Ghost URN '{_GHOST_URN}' must appear in detail.unresolved_urns. "
            "Spec: spec/USE_CASE_en.md §UC5 §dataset_filter."
        )

        # Full event-shape invariant — all required keys must be present.
        # Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE.
        _required_keys = ("run_id", "metric_id", "values", "dry_run", "unresolved_urns",
                           "breakdown_summary")
        for key in _required_keys:
            assert key in detail, (
                f"METRIC.RUN_COMPLETE detail missing required key '{key}'. "
                "Spec: spec/feature/BACKEND.md §Event Catalogue."
            )
        bs = detail["breakdown_summary"]
        assert "dataset_count" in bs, (
            "breakdown_summary missing 'dataset_count'. "
            "Spec: spec/feature/BACKEND.md §Event Catalogue."
        )
        assert "affected_count" in bs, (
            "breakdown_summary missing 'affected_count'. "
            "Spec: spec/feature/BACKEND.md §Event Catalogue."
        )

    finally:
        del_resp = await api_client.delete(base_conf, headers=admin_headers)
        assert del_resp.status_code in (204, 404)


# ── metric_id kebab regex ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_id_path_regex_acceptance_and_rejection(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """metric_id kebab regex: accepts valid IDs; rejects invalid ones.

    Valid: 'ingestion-freshness', 'doc-health-dev', single-char 'a'.
    Invalid: 'UPPER', 'with_underscore', 'with space', '-leading', 'trailing-'.

    Spec: spec/API.md §Metric — metric_id kebab-case slug
          ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$.
    """
    # Valid IDs that the regex must accept (route 200/201 or 404 — anything but 422)
    valid_ids = ["a", "doc-health-dev", "ingestion-freshness"]
    for mid in valid_ids:
        resp = await api_client.get(
            f"/api/v1/spoke/governance/metric/{mid}/attr/conf",
            headers=admin_headers,
        )
        assert resp.status_code != 422, (
            f"Valid metric_id '{mid}' rejected by path regex with 422. "
            "Spec: spec/API.md §Metric — metric_id kebab-case slug."
        )

    # Invalid IDs that FastAPI path regex must reject with 422
    invalid_ids = ["UPPER", "with_underscore", "-leading", "trailing-"]
    for mid in invalid_ids:
        resp = await api_client.get(
            f"/api/v1/spoke/governance/metric/{mid}/attr/conf",
            headers=admin_headers,
        )
        assert resp.status_code == 422, (
            f"Invalid metric_id '{mid}' was not rejected (expected 422, got {resp.status_code}). "
            "Spec: spec/API.md §Metric — metric_id kebab-case slug."
        )


# ── Ingestion-freshness per-dataset window ────────────────────────────────────
#
# These tests need raw ORM/SQL-seeded state that the api-wired pipeline cannot
# naturally produce. They insert rows directly via asyncpg and clean up in finally.
#
# The freshness measurer reads ingestion_source + ingestion_source_dataset (via a
# JOIN) to resolve the per-dataset window. Seeds insert into those tables directly.
#
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — per-dataset
#       freshness window derived from ingestion_source / ingestion_source_dataset.
# Spec: spec/feature/BACKEND.md §Metrics Service §Time windows.
# Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.


@pytest.mark.asyncio
async def test_ingestion_freshness_active_custom_daily_window(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness: ACTIVE_CUSTOM_MANAGED daily source → window = 172800s.

    Seeds an ingestion_source row with mode='ACTIVE_CUSTOM_MANAGED', schedule_tier='daily'
    and ingestion_source_dataset rows (derivation='emitted') for two datasets:
      - urn_fresh: INGESTION.COMPLETE 130000s ago (< 172800s) → in-time
      - urn_stale: INGESTION.COMPLETE 200000s ago (> 172800s) → stale

    Breakdown stale entry detail must include time_window_sec=172800 and
    window_source='managed:daily'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          ACTIVE_CUSTOM_MANAGED / DATAHUB_MANAGED daily → SCHEDULE_TIER_SECONDS[daily] × 2
          = 86400 × 2 = 172800s.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — breakdown detail
          carries time_window_sec and window_source.
    Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.
    """
    _METRIC_ID = "spot-freshness-daily-window"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    urn_fresh = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    # Ensure clean state
    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    # Stable, valid UUID v4 source ID — deterministic so cleanup is reliable even on partial
    # failure.  Non-standard strings like "00000000-spot-fresh-daily-w-0000" are rejected by
    # asyncpg as invalid UUIDs; use proper UUID format.
    # spec: BACKEND_SCHEMA.md §ingestion_source — id column is UUID.
    source_id = str(uuid.UUID("00000000-0000-4000-8000-0000000000d1"))
    try:
        # Insert ingestion_source (ACTIVE_CUSTOM_MANAGED, daily schedule_tier)
        # spec: BACKEND_SCHEMA.md §ingestion_source — columns: id, mode, name, platform, recipe,
        #   schedule, schedule_tier, datahub_source_urn, status, created_at, updated_at
        await conn.execute(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
            "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
            source_id,
            "ACTIVE_CUSTOM_MANAGED",
            "spot-freshness-daily-test",
            "postgres",
            json.dumps({"source": {"type": "postgres", "config": {}}}),
            "0 0 * * *",
            "daily",
            "OK",
        )

        # Insert ingestion_source_dataset rows (derivation='emitted') for both datasets
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — (source_id, dataset_urn, derivation, ...)
        for urn in (urn_fresh, urn_stale):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "emitted",
            )

        # Clear any pre-existing INGESTION.COMPLETE events for these URNs
        _del_ingestion = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        for urn in (urn_fresh, urn_stale):
            await conn.execute(_del_ingestion, urn)

        # Insert INGESTION.COMPLETE events with controlled timestamps
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn_fresh, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=130000),  # within 172800s window
        )
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn_stale, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=200000),  # outside 172800s window
        )

        # Create and enable the metric scoped to these two datasets
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Daily Window Spot Test",
                "description": "Tests per-dataset daily window from ingestion_source mapping",
                "metrics": ["total", "ingested_in_time"],
                "metric_conf": {"time_window_sec": 3600},  # fallback — must NOT be used
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [urn_fresh, urn_stale]},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        # Run
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            )
        assert run_resp.status_code == 200, run_resp.text

        # Read results
        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        assert results_resp.status_code == 200
        results = results_resp.json().get("results", [])
        assert results, "Expected at least one result row after run."
        row = results[0]

        values = row["values"]
        assert values["total"] == 2.0, (
            f"total must be 2 (both datasets); got {values['total']}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )
        assert values["ingested_in_time"] == 1.0, (
            f"ingested_in_time must be 1 (only urn_fresh); got {values['ingested_in_time']}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "
            "ACTIVE_CUSTOM_MANAGED daily window=172800s."
        )

        # Stale breakdown entry must have time_window_sec=172800 and window_source='managed:daily'
        # spec: feature/BACKEND.md §Metrics Service §Time windows — window_source='managed:{tier}'
        # for ACTIVE_CUSTOM_MANAGED / DATAHUB_MANAGED sources.
        breakdown = row.get("breakdown", {})
        stale_entries = [e for e in breakdown.get("datasets", []) if e["urn"] == urn_stale]
        assert len(stale_entries) == 1, (
            f"urn_stale must appear exactly once in breakdown; got {breakdown.get('datasets')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        detail = stale_entries[0]["detail"]
        assert detail["time_window_sec"] == _DAILY_WINDOW_SEC, (
            f"Stale entry time_window_sec must be {_DAILY_WINDOW_SEC}; "
            f"got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert detail["window_source"] == "managed:daily", (
            f"Stale entry window_source must be 'managed:daily' for ACTIVE_CUSTOM_MANAGED daily; "
            f"got {detail.get('window_source')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — "
            "window_source='managed:{tier}' for MANAGED modes."
        )
        # urn_fresh must NOT appear in breakdown (it's in-time)
        fresh_entries = [e for e in breakdown.get("datasets", []) if e["urn"] == urn_fresh]
        assert not fresh_entries, (
            "urn_fresh must not appear in breakdown (it is in-time). "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — only stale entries."
        )

    finally:
        # Clean up seeds and metric
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset WHERE source_id = $1", source_id
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = $1", source_id
            )
        _del_ev = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        for urn in (urn_fresh, urn_stale):
            with suppress(Exception):
                await conn.execute(_del_ev, urn)
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_freshness_passive_window(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness: PASSIVE source → window = 7200s (2 × hourly sync cadence).

    Seeds an ingestion_source with mode='PASSIVE' and ingestion_source_dataset rows
    (derivation='matched') for two datasets:
      - urn_fresh: event 3600s ago (< 7200s) → in-time
      - urn_stale: event 8000s ago (> 7200s) → stale, detail.window_source='passive'

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          PASSIVE → twice the DataHub-sync cadence (hourly → 7200s).
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='passive'.
    Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.
    """
    _METRIC_ID = "spot-freshness-passive-window"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    # Use catalog schema URNs — only catalog.* is seeded into DataHub by this module's
    # DUMMY_DATA_DATAHUB_SCHEMAS constant. Non-catalog URNs are unresolved → total=0 → vacuous pass.
    urn_fresh = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    # Stable, valid UUID v4 source ID — deterministic so cleanup is reliable even on partial
    # failure.  Non-standard strings are rejected by asyncpg as invalid UUIDs; use proper UUID.
    # spec: BACKEND_SCHEMA.md §ingestion_source — id column is UUID.
    source_id = str(uuid.UUID("00000000-0000-4000-8000-0000000000d2"))
    try:
        # Insert ingestion_source (PASSIVE, no schedule_tier)
        # spec: BACKEND_SCHEMA.md §ingestion_source — PASSIVE sources have no schedule/schedule_tier
        await conn.execute(
            "INSERT INTO dataspoke.ingestion_source "
            "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
            "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
            source_id,
            "PASSIVE",
            "spot-freshness-passive-test",
            "kafka",
            json.dumps({"source": {"type": "kafka", "config": {}}}),
            None,
            None,
            "OK",
        )

        # Insert ingestion_source_dataset rows (derivation='matched') for both datasets
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation='matched' for PASSIVE
        for urn in (urn_fresh, urn_stale):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "matched",
            )

        # Clear any pre-existing INGESTION.COMPLETE events for these URNs
        _del_ev_passive = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        for urn in (urn_fresh, urn_stale):
            await conn.execute(_del_ev_passive, urn)

        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn_fresh, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=3600),  # within 7200s passive window
        )
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn_stale, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=8000),  # outside 7200s passive window
        )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Passive Window Spot Test",
                "description": "Tests per-dataset passive window from ingestion_source mapping",
                "metrics": ["total", "ingested_in_time"],
                "metric_conf": {"time_window_sec": 86400},  # fallback — must NOT be used
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [urn_fresh, urn_stale]},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        results = results_resp.json().get("results", [])
        assert results
        row = results[0]

        assert row["values"]["total"] == 2.0, (
            f"total must be 2 (both catalog URNs resolved); got {row['values']['total']}. "
            "If 0, the URN was not registered in DataHub — check DUMMY_DATA_DATAHUB_SCHEMAS."
        )
        assert row["values"]["ingested_in_time"] == 1.0, (
            "PASSIVE fresh dataset must be counted (event 3600s < 7200s window). "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — passive window=7200s."
        )

        stale_entries = [
            e for e in row.get("breakdown", {}).get("datasets", [])
            if e["urn"] == urn_stale
        ]
        assert stale_entries, "urn_stale must be in breakdown."
        detail = stale_entries[0]["detail"]
        assert detail["window_source"] == "passive", (
            "window_source for PASSIVE source must be 'passive'; "
            f"got {detail.get('window_source')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert detail["time_window_sec"] == _PASSIVE_WINDOW_SEC, (
            f"Passive window must be {_PASSIVE_WINDOW_SEC}s; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )

    finally:
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset WHERE source_id = $1", source_id
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = $1", source_id
            )
        _del_ev_p = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        for urn in (urn_fresh, urn_stale):
            with suppress(Exception):
                await conn.execute(_del_ev_p, urn)
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_freshness_no_config_fallback(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness: dataset with no ingestion_source mapping uses
    metric_conf.time_window_sec.

    Uses a URN that has no ingestion_source_dataset row, and an event 50000s ago.
    With fallback time_window_sec=3600 → 50000s > 3600s → stale, window_source='default'.

    The no-mapping condition is enforced by ensuring no ingestion_source_dataset row
    covers this specific URN (delete any stale rows before seeding the event).
    Resolvability in DataHub and absence of source mapping are independent concerns.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          dataset mapped to no source → metric_conf.time_window_sec.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='default'.
    Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source_dataset — no row → fallback.
    """
    _METRIC_ID = "spot-freshness-fallback-window"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    # Use a catalog URN — only catalog.* is seeded into DataHub by this module's
    # DUMMY_DATA_DATAHUB_SCHEMAS constant.
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    try:
        # Ensure no ingestion_source_dataset row maps this URN (removes any stale entry
        # left by a prior test run that seeded a source covering catalog.editions).
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — absence means no source mapping
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = $1", urn
        )
        # Insert an event 50000s ago
        _del_fallback = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        await conn.execute(_del_fallback, urn)
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=50000),
        )

        # Create with small fallback window (3600s) → 50000s outside → stale
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Fallback Window Spot Test",
                "description": (
                    "Tests metric_conf fallback when no ingestion_source_dataset row exists"
                ),
                "metrics": ["total", "ingested_in_time"],
                "metric_conf": {"time_window_sec": 3600},  # 50000s > 3600s → stale
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [urn]},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        results = results_resp.json().get("results", [])
        assert results
        row = results[0]

        assert row["values"]["total"] == 1.0, (
            f"total must be 1 (catalog.editions URN resolved); got {row['values']['total']}. "
            "If 0, the URN was not registered in DataHub — check DUMMY_DATA_DATAHUB_SCHEMAS."
        )
        assert row["values"]["ingested_in_time"] == 0.0, (
            "No source mapping + 50000s event + 3600s fallback → stale (ingested_in_time=0). "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        stale_entries = row.get("breakdown", {}).get("datasets", [])
        assert stale_entries, "Must have stale entry."
        detail = stale_entries[0]["detail"]
        assert detail["window_source"] == "default", (
            "No source mapping → window_source must be 'default'; "
            f"got {detail.get('window_source')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert detail["time_window_sec"] == 3600, (
            f"Fallback time_window_sec must be 3600; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )

    finally:
        _del_fb = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        with suppress(Exception):
            await conn.execute(_del_fb, urn)
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


# ── Validation-score per-dataset window ──────────────────────────────────────
#
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — validation-score
#       per-dataset window = 2 × mean(last N inter-arrival gaps), N default 3.
#       Fewer than N+1 rows → fallback metric_conf.time_window_sec.
# Spec: spec/feature/BACKEND.md §Metrics Service §Time windows.


@pytest.mark.asyncio
async def test_validation_score_intervals_window(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """validation-score: ≥ N+1 rows derive window from intervals; window_source='intervals'.

    Seeds two datasets:
      - urn_fresh: N+1=4 rows evenly spaced 24h, latest 1h ago (score=1.0) → in-time.
        Window = 2 × mean([24h, 24h, 24h]) = 48h = 172800s.
      - urn_stale: N+1=4 rows evenly spaced 24h, latest 200h ago (score=0.5) → outside
        172800s window → in breakdown; detail.time_window_sec must be 172800 (spec literal).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — per-dataset window
          = 2 × mean(last N inter-arrival gaps); N default 3.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='intervals'.
    """
    _METRIC_ID = "spot-validation-intervals"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    urn_fresh = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    # urn_stale: same 24h spacing but latest row is 200h ago — outside 172800s (48h) window.
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    try:
        # Remove any pre-existing validation_results for these URNs
        for urn in (urn_fresh, urn_stale):
            await conn.execute(
                "DELETE FROM dataspoke.validation_results WHERE dataset_urn = $1", urn
            )

        # urn_fresh: N+1=4 rows evenly spaced 24h apart (N=3 → 3 gaps of 24h each)
        # Latest at 1h → within 172800s window → score=1.0 counted
        for offset_hours in [1, 25, 49, 73]:
            await conn.execute(
                "INSERT INTO dataspoke.validation_results "
                "(id, dataset_urn, score, data_time, variables) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4::jsonb)",
                urn_fresh,
                1.0,
                now - timedelta(hours=offset_hours),
                json.dumps({"row_cnt": 1250.0}),
            )

        # urn_stale: N+1=4 rows evenly spaced 24h apart, but latest at 200h → outside window
        # Spec literal: window = 2 × mean([24h, 24h, 24h]) = 172800s; 200h > 48h → stale
        for offset_hours in [200, 224, 248, 272]:
            await conn.execute(
                "INSERT INTO dataspoke.validation_results "
                "(id, dataset_urn, score, data_time, variables) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4::jsonb)",
                urn_stale,
                0.5,
                now - timedelta(hours=offset_hours),
                json.dumps({"row_cnt": 980.0}),
            )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "validation-score",
                "title": "Intervals Window Spot Test",
                "description": "Tests intervals-derived window",
                "metrics": ["total", "validation_score_sum"],
                "metric_conf": {"time_window_sec": 3600},  # fallback — must NOT be used
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [urn_fresh, urn_stale]},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        results = results_resp.json().get("results", [])
        assert results
        row = results[0]

        assert row["values"]["total"] == 2.0, (
            f"total must be 2 (both catalog URNs resolved); got {row['values']['total']}. "
            "If 0, the URN was not registered in DataHub — check DUMMY_DATA_DATAHUB_SCHEMAS."
        )
        # urn_fresh: latest at 1h is within 172800s window → score=1.0 counted
        assert row["values"]["validation_score_sum"] == 1.0, (
            "Latest in-window row score=1.0 must be counted; urn_stale is out-of-window → 0.0. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )

        # urn_stale must appear in breakdown with window_source='intervals'
        # and time_window_sec = spec literal 172800 (2 × mean of three 24h gaps).
        breakdown = row.get("breakdown", {})
        stale_entries = [e for e in breakdown.get("datasets", []) if e["urn"] == urn_stale]
        assert stale_entries, (
            "urn_stale (200h > 48h window) must appear in breakdown. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        detail = stale_entries[0]["detail"]
        assert detail["window_source"] == "intervals", (
            f"window_source must be 'intervals'; got {detail.get('window_source')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        # Spec literal: 2 × mean([24h, 24h, 24h]) × 3600 = 172800s
        assert detail["time_window_sec"] == 172800, (
            f"time_window_sec must be 172800 (2 × mean of three 24h gaps); "
            f"got {detail.get('time_window_sec')}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )

        # urn_fresh must NOT appear in breakdown (score=1.0 → not failed)
        fresh_entries = [e for e in breakdown.get("datasets", []) if e["urn"] == urn_fresh]
        assert not fresh_entries, (
            "urn_fresh (score=1.0, in-time) must not appear in breakdown. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )

    finally:
        for urn in (urn_fresh, urn_stale):
            with suppress(Exception):
                await conn.execute(
                    "DELETE FROM dataspoke.validation_results WHERE dataset_urn = $1", urn
                )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_score_sparse_fallback_window(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """validation-score: sparse dataset (< N+1 rows) uses fallback; window_source='default'.

    Seeds only 2 rows (< N+1=4) for one dataset.
    Fallback time_window_sec=3600. Latest row 5000s ago → stale → window_source='default'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          fewer than N intervals falls back to metric_conf.time_window_sec.
    Spec: spec/feature/BACKEND.md §Metrics Service §Time windows — window_source='default'.
    """
    _METRIC_ID = "spot-validation-sparse"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    # Use a catalog URN — only catalog.* is seeded into DataHub by this module's
    # DUMMY_DATA_DATAHUB_SCHEMAS constant. Non-catalog URNs are unresolved → total=0 → vacuous pass.
    # catalog.editions is chosen (distinct from urn_fresh/urn_stale in the intervals test).
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    try:
        await conn.execute(
            "DELETE FROM dataspoke.validation_results WHERE dataset_urn = $1", urn
        )

        # Only 2 rows (< N+1=4 for N=3) — insufficient for intervals computation
        for offset_secs in [5000, 100000]:
            await conn.execute(
                "INSERT INTO dataspoke.validation_results "
                "(id, dataset_urn, score, data_time, variables) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4::jsonb)",
                urn,
                0.9,
                now - timedelta(seconds=offset_secs),
                json.dumps({"row_cnt": 750.0}),
            )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "validation-score",
                "title": "Sparse Window Spot Test",
                "description": "Tests fallback window for sparse datasets",
                "metrics": ["total", "validation_score_sum"],
                "metric_conf": {"time_window_sec": 3600},  # latest at 5000s > 3600s → stale
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [urn]},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        results = results_resp.json().get("results", [])
        assert results
        row = results[0]

        assert row["values"]["total"] == 1.0, (
            f"total must be 1 (catalog.editions URN resolved); got {row['values']['total']}. "
            "If 0, the URN was not registered in DataHub — check DUMMY_DATA_DATAHUB_SCHEMAS."
        )
        # Latest at 5000s > 3600s fallback → stale → 0.0
        assert row["values"]["validation_score_sum"] == 0.0, (
            "Sparse dataset: 5000s > 3600s fallback → stale → 0.0. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )

        breakdown = row.get("breakdown", {})
        stale_entries = breakdown.get("datasets", [])
        assert stale_entries, "Sparse stale dataset must appear in breakdown."
        detail = stale_entries[0]["detail"]
        assert detail["window_source"] == "default", (
            "Sparse dataset must have window_source='default'; "
            f"got {detail.get('window_source')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )
        assert detail["time_window_sec"] == 3600, (
            f"Fallback time_window_sec must be 3600; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Time windows."
        )

    finally:
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.validation_results WHERE dataset_urn = $1", urn
            )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


# ── metrics[] subset filter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_values_filtered_to_declared_subset(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Persisted result.values contains ONLY the keys declared in metrics[].

    ingestion-freshness emits both 'total' and 'ingested_in_time', but when
    the metric definition declares only metrics=['total'], the service's
    _measure() must filter all_values down to that subset before persisting.

    The key assertion is: result.values.keys() == {'total'} exactly — i.e.
    'ingested_in_time' is ABSENT.  Failure here means the subset filter in
    MetricsService._measure() (lines filtered_values = {k: v ... if k in
    definition.metrics}) was removed or bypassed.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metrics[] is
          a subset of the type's emitted keys; the persisted values dict must
          contain ONLY the declared subset.
    Spec: spec/API.md §Metric — 'metrics': Subset of the type's emitted keys;
          the server filters the raw measurer output to this declared subset
          before persisting.
    """
    _METRIC_ID = "spot-subset-filter-check"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    # catalog.title_master is registered in DataHub by this module's
    # DUMMY_DATA_DATAHUB_SCHEMAS={"catalog"} constant — guaranteed resolvable.
    _URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

    # Ensure clean state
    await api_client.delete(base_conf, headers=admin_headers)

    try:
        # Create: ingestion-freshness with metrics=['total'] ONLY — omitting 'ingested_in_time'
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Subset Filter Check",
                "description": "Verifies values dict is filtered to the declared metrics[] subset",
                "metrics": ["total"],
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": {"dataset_urns": [_URN]},
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/governance/metric must return 201 on create; "
            f"got {create_resp.status_code}: {create_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        assert create_resp.json()["metrics"] == ["total"], (
            "Created metric must carry the declared metrics=['total'] subset. "
            "Spec: spec/API.md §Metric — metrics[] is a subset of emitted keys."
        )

        # Run (dry_run=false so the result is persisted)
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            )
        assert run_resp.status_code == 200, (
            f"POST method/run must return 200; got {run_resp.status_code}: {run_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        assert run_resp.json().get("status") == "success"

        # GET the latest result and assert the key-set is exactly {'total'}
        results_resp = await api_client.get(
            f"{base_results}?limit=5",
            headers=admin_headers,
        )
        assert results_resp.status_code == 200, results_resp.text
        results = results_resp.json().get("results", [])
        assert results, (
            "Non-dry-run must persist at least one result row. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        row = results[0]
        values = row["values"]

        # ── The key assertion: subset filter ──────────────────────────────────
        assert set(values.keys()) == {"total"}, (
            f"result.values keys must be exactly {{'total'}} (the declared subset); "
            f"got {set(values.keys())}. "
            "'ingested_in_time' must be absent — the server must filter all_values to "
            "definition.metrics before persisting. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metrics[] "
            "is a subset of the type's emitted keys. "
            "Spec: spec/API.md §Metric — persisted values must contain ONLY the declared subset."
        )
        assert "ingested_in_time" not in values, (
            "Undeclared key 'ingested_in_time' must be absent from result.values. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )
        assert isinstance(values["total"], float), (
            f"result.values['total'] must be a float; got {type(values['total']).__name__}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — values are float."
        )

    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
