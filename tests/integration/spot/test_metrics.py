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
- PUT dataset_filter with > 1000 string literals → 422 INVALID_DATASET_FILTER
- POST dataset_filter with exactly 1000 string literals → 201
- PUT dataset_filter="dataset_urn = 'not-a-urn'" → 422 INVALID_DATASET_URN
- a non-dry run writes one metric_dataset_results row per dataset in scope; a dry run
  writes none and leaves the previous run's verdicts readable
- GET .../dataset serves the tri-state met view, its met filter combinations, and the
  scope-relative attrs_synced_at watermark
- DELETE .../attr/conf clears the metric's verdict rows
- PUT metric_id with invalid path chars → 422
- ingestion-freshness applies metric_conf.time_window_sec uniformly: a PASSIVE-owned
  dataset is judged by the declared window (in-time and stale sides both seeded), and a
  dataset with no ingestion_source_dataset row is stale with no readable evidence
- ingestion-freshness reads the run booked on the dataset's owning source
  (entity_type='ingestion_source'), never one keyed by dataset URN — both sides seeded,
  in opposite directions

- validation-score applies metric_conf.time_window_sec uniformly: the latest row inside
  the declared window is counted, the one outside it is not
- POST method/run dry_run=true → no persisted result, no RUN_COMPLETE event
- POST method/run dry_run=false → values is dict[str, float]
- POST method/run concurrent → 409 METRIC_RUNNING
- breakdown.datasets[] has no 'category' field
- breakdown counts derive from seeded state and reconcile with the RUN_COMPLETE event's
  breakdown_summary (bound by run_id): dataset_count == values.total, affected_count == failed count
- metric_id kebab regex acceptance and rejection

Spot is the right layer for the windowing tests (ingestion-freshness and
validation-score) because they require raw ORM/SQL-seeded state (events,
validation_results, ingestion_source/ingestion_source_dataset with controlled
timestamps) that the api-wired pipeline cannot naturally produce.
Tests insert rows directly via asyncpg and clean up in `finally` blocks.

Spec:
- spec/USE_CASE_en.md §UC5 — Factory defaults, Built-in active metric types, API Mapping
- spec/API.md §Metric (/spoke/governance/metric) — field rules, payload caps, error codes,
  create/replace
- spec/feature/BACKEND.md §Metrics Service §Breakdown format, §Create vs replace,
  §Measurement window, §Ingestion evidence
"""

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
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

# Factory default measurement window
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``time_window_sec`` …
# **the** measurement window (positive int seconds … factory default ``172800``)".
_FACTORY_DEFAULT_TIME_WINDOW_SEC = 172800


async def _get_ds_conn() -> asyncpg.Connection:
    """Open a direct asyncpg connection to the DataSpoke operational DB."""
    return await asyncpg.connect(
        host=os.environ.get("DATASPOKE_DEV_POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("DATASPOKE_DEV_POSTGRES_PORT", "9201")),
        user=os.environ.get("DATASPOKE_DEV_POSTGRES_USER", "dataspoke"),
        password=os.environ.get("DATASPOKE_DEV_POSTGRES_PASSWORD", ""),
        database=os.environ.get("DATASPOKE_DEV_POSTGRES_DB", "dataspoke"),
    )


@pytest.fixture(scope="module", autouse=True)
def _reset_factory_metric_conf() -> Iterator[None]:
    """Restore the factory metric rows to their seeded disabled defaults at both
    module setup and teardown.

    Many tests in this module enable a factory metric (is_enabled=True) and scope
    it via dataset_filter while running it; if one dies mid-body before its inline
    restore PATCH, that mutated state would otherwise leak into the next pytest
    session and violate the factory-defaults invariant
    (spec/USE_CASE_en.md §UC5 §Factory defaults — seeds ship is_enabled=False with
    dataset_filter=""). The reset is performed directly in the operational DB
    rather than through the API so that it cannot itself be skipped by an API-layer
    guard, an auth failure, or a metric left in a running state.
    """

    async def _restore() -> None:
        conn = await _get_ds_conn()
        try:
            await conn.execute(
                "UPDATE dataspoke.metric_definitions "
                "SET is_enabled = FALSE, dataset_filter = '' "
                "WHERE id = ANY($1::text[])",
                list(_FACTORY_IDS),
            )
        finally:
            await conn.close()

    asyncio.run(_restore())
    yield
    asyncio.run(_restore())


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

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``time_window_sec``
          … **the** measurement window (positive int seconds … factory default ``172800``)".
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
        "metrics": [
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "doc_health", "color": "#A855F7", "idx": 2},
        ],
        "metric_conf": {},
        "schedule_tier": "daily",
        "dataset_filter": "",
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
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "weekly",
                "dataset_filter": "",
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
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": "",
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
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "weekly",
                "dataset_filter": "origin = 'DEV'",
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
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
            "dataset_filter": "",
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

    Spec: spec/API.md §Metric — "`ingestion-freshness` and `validation-score` require
          `time_window_sec`"; a missing one "returns `422 INVALID_PARAMETER`".
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
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

    Spec: spec/API.md §Metric — time_window_sec is "An integer in `[1, 315360000]` (ten
          years); out of range … returns `422 INVALID_PARAMETER`". -1 is below the
          interval.
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": -1},
            "schedule_tier": "daily",
            "dataset_filter": "",
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "daily",
            "dataset_filter": "",
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
            "metrics": [{"name": "nope", "color": "#64748B", "idx": 1}],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "daily",
            "dataset_filter": "",
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
    """PUT a dataset_filter carrying 1,001 string literals → 422 INVALID_DATASET_FILTER.

    Spec: spec/API.md §`dataset_filter` grammar — Caps: "≤ 1,000 string literals";
    Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "exceeds a payload cap".
    """
    over_cap = "origin IN (" + ", ".join(f"'v{i}'" for i in range(1001)) + ")"
    resp = await api_client.put(
        "/api/v1/spoke/governance/metric/spot-cap-over/attr/conf",
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": False,
            "metric_type": "doc-health",
            "title": "Cap over",
            "description": "Should fail on cap",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": over_cap,
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for 1,001 string literals (over cap), got {resp.status_code}: "
        f"{resp.text}. Spec: spec/API.md §`dataset_filter` grammar — Caps."
    )
    assert resp.json().get("error_code") == "INVALID_DATASET_FILTER", resp.text


@pytest.mark.asyncio
async def test_dataset_filter_at_cap_accepted(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST a dataset_filter carrying exactly 1,000 string literals → 201.

    Spec: spec/API.md §`dataset_filter` grammar — Caps: the cap is inclusive, so
    exactly 1,000 literals MUST be accepted.
    """
    _METRIC_ID = "spot-cap-at"
    at_cap = "origin IN (" + ", ".join(f"'v{i}'" for i in range(1000)) + ")"
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
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": at_cap,
            },
        )
        assert resp.status_code == 201, (
            f"Expected 201 for 1,000 string literals (at cap), got {resp.status_code}: "
            f"{resp.text}. Spec: spec/API.md §`dataset_filter` grammar — Caps."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_invalid_dataset_urn_format_returns_422_with_specific_code(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT dataset_filter="dataset_urn = 'not-a-urn'" → 422 INVALID_DATASET_URN.

    Spec: spec/API.md §Error Catalogue — "A `dataset_urn` literal inside a
          `dataset_filter` is not a well-formed `urn:li:dataset:(…)` URN".
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "dataset_urn = 'not-a-urn'",
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
            "schedule_tier": "daily",
            "dataset_filter": "",
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

    try:
        # Enable the seeded metric and scope to bounded URN
        patch_resp = await api_client.patch(
            base_conf,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text

        # Bind the persistence check by an after-timestamp captured before the run,
        # not a count-delta over a limit window that concurrent runs of the same
        # metric on the shared cluster would invalidate. A small look-back margin
        # absorbs host↔DB clock skew.
        # spec: TESTING.md §Integration Lifecycle & Isolation — bind by identity/after=.
        before_ts = datetime.now(UTC) - timedelta(seconds=5)

        # Dry-run
        run_resp = await api_client.post(
            f"{base_run}?dry_run=true",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, run_resp.text
        run_body = run_resp.json()

        # Response must carry run_id, status, and detail with values dict
        assert "run_id" in run_body, (
            "dry-run response must carry run_id. Spec: spec/API.md §Metric."
        )
        assert "status" in run_body, (
            "dry-run response must carry status. Spec: spec/API.md §Metric."
        )
        run_id = run_body["run_id"]
        detail = run_body.get("detail", {})
        if "values" in detail:
            assert isinstance(detail["values"], dict), (
                "detail.values must be a dict when present. "
                "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
            )

        # (a) No result row is persisted at/after the pre-run timestamp. Result rows
        # carry measured_at (not run_id), so bind the persistence check by that
        # after-timestamp rather than a count-delta over a limit window.
        post_results = await api_client.get(f"{base_results}?limit=100", headers=admin_headers)
        results_since = [
            r for r in post_results.json().get("results", [])
            if datetime.fromisoformat(r["measured_at"]) >= before_ts
        ]
        assert results_since == [], (
            f"dry_run must not persist a result row; found {len(results_since)} "
            "measured at/after the pre-run timestamp. "
            "Spec: spec/USE_CASE_en.md §UC5 — dry_run=true does not persist."
        )

        # (b) No METRIC.RUN_COMPLETE event is persisted for this run_id. Unlike the
        # METAGEN and ONTOGEN.RUN_COMPLETE rows, the METRIC.RUN_COMPLETE row in
        # BACKEND.md §Event Catalogue is NOT marked "recorded for both dry-run and
        # non-dry-run", and dry-run semantics are evaluate-without-persist — so a
        # metrics dry-run emits nothing. The absence assertion, bound by run_id, is the
        # load-bearing check: a regression that persisted a dry-run event would carry
        # this run_id and fail here. (Mechanism: _run_inner returns at `if dry_run:`
        # before _record_event.)
        # spec: BACKEND.md §Event Catalogue (METRIC row) + USE_CASE_en.md §UC5.
        post_events = await api_client.get(f"{base_events}?limit=100", headers=admin_headers)
        complete_for_run = [
            e for e in post_events.json().get("events", [])
            if e.get("event_type") == "METRIC.RUN_COMPLETE"
            and e.get("detail", {}).get("run_id") == run_id
        ]
        assert complete_for_run == [], (
            "metrics dry-run must persist no METRIC.RUN_COMPLETE event for this run_id; "
            f"found {len(complete_for_run)}. Spec: spec/USE_CASE_en.md §UC5 — dry_run "
            "evaluates without persisting."
        )
    finally:
        # Restore factory-default (disabled, unscoped) state unconditionally so a
        # mid-test failure can't leave the metric enabled for later tests.
        with suppress(Exception):
            await api_client.patch(
                base_conf,
                headers=admin_headers,
                json={"is_enabled": False, "dataset_filter": ""},
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
            "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
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
        json={"is_enabled": False, "dataset_filter": ""},
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
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
            "schedule_tier": "daily",
            "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
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
            "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
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
        json={"is_enabled": False, "dataset_filter": ""},
    )


# ── Unresolved URN reporting ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metric_run_unresolved_urns_in_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """METRIC.RUN_COMPLETE event carries unresolved_urns and expected detail shape.

    A `dataset_urn` literal inside dataset_filter that matches no registered dataset
    must appear
    in the METRIC.RUN_COMPLETE event's unresolved_urns field.  The event detail must
    also carry the full key set prescribed by the Event Catalogue.

    Spec: spec/USE_CASE_en.md §UC5 §Concept — "literal URNs matching no registered
          dataset at run time are reported in the `METRIC.RUN_COMPLETE` event's
          `unresolved_urns` field".
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
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                "metric_conf": {"time_window_sec": 172800},
                "dataset_filter": f"dataset_urn = '{_GHOST_URN}'",
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
        # Spec: spec/USE_CASE_en.md §UC5 §Concept.
        assert isinstance(detail.get("unresolved_urns"), list), (
            "METRIC.RUN_COMPLETE detail.unresolved_urns must be a list. "
            "Spec: spec/USE_CASE_en.md §UC5 §Concept."
        )
        assert _GHOST_URN in detail["unresolved_urns"], (
            f"Ghost URN '{_GHOST_URN}' must appear in detail.unresolved_urns. "
            "Spec: spec/USE_CASE_en.md §UC5 §Concept."
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


# ── Breakdown ↔ RUN_COMPLETE event reconciliation ─────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_counts_reconcile_with_run_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Breakdown counts derive from seeded state and reconcile with the RUN_COMPLETE event.

    **Evidence tier exercised: 2 (`source_level`).** The seeded `INGESTION.COMPLETE` rows
    carry `detail = {}` — no `source` key — so they are run-level rows that no observation
    producer wrote, and neither seeded dataset has an observation of its own. Tier 1 is
    therefore empty and the source-level fallback answers, which is what makes the
    one-source-per-dataset shape below still produce a fresh/stale split. The empty
    `detail` also has no `dry_run` key, so the tier-2 dry-run exclusion admits both rows.

    Seeds one ACTIVE_CUSTOM_MANAGED source **per dataset** — on tier 2 a dataset's
    recency is the recency of its owning source's runs, so a shared source would give both
    datasets one verdict and collapse the contrast this test needs — then runs the metric
    with a declared ``time_window_sec=172800``:
      - urn_stale: its source's INGESTION.COMPLETE 200000s ago (> 172800s) → FAILED
      - urn_fresh: its source's INGESTION.COMPLETE 130000s ago (< 172800s) → in-time

    Both sides are seeded (a failed dataset AND an in-time one), so the failed-listing is proven,
    not assumed. The test then reconciles the two governance views:
      - the persisted result breakdown (the "breakdown view" on attr/result), and
      - the RUN_COMPLETE event's breakdown_summary, selected by run_id (identity, not window).

    Asserted invariants (all counts derive from the seeded two-dataset state):
      - values.total == 2 (both scanned); values.ingested_in_time == 1 (only urn_fresh)
      - breakdown.dataset_count == values.total (scanned count == total)
      - breakdown.datasets lists urn_stale, excludes urn_fresh
      - len(breakdown.datasets) == values.total - values.ingested_in_time (failed == total-passed)
      - event.breakdown_summary.dataset_count == breakdown.dataset_count
      - event.breakdown_summary.affected_count == len(breakdown.datasets)

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — dataset_count is the total
          scanned; 'len(datasets) == failed count' is implied; datasets[] lists only failed entries.
    Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE detail keys include run_id
          and breakdown_summary {dataset_count, affected_count}.
    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "``ingested_in_time`` =
          count whose latest ingestion evidence falls within ``metric_conf.time_window_sec``
          of the measurement".
    Spec: spec/TESTING.md §Integration Lifecycle & Isolation — bind event assertions by run_id.
    """
    _METRIC_ID = "spot-breakdown-reconcile"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"
    base_events = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/event"

    urn_fresh = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    urn_stale = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    # Ensure clean state
    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    # Deterministic valid UUID v4 source ids so cleanup is reliable even on partial failure.
    # spec: BACKEND_SCHEMA.md §ingestion_source — id column is UUID.
    source_fresh = str(uuid.UUID("00000000-0000-4000-8000-000000000c01"))
    source_stale = str(uuid.UUID("00000000-0000-4000-8000-000000000c02"))
    source_ids = [source_fresh, source_stale]
    try:
        # Drop any mapping another test left on these URNs: an extra covering source
        # would enter the owning-source ranking and could win it.
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = ANY($1::text[])",
            [urn_fresh, urn_stale],
        )
        # One ingestion_source per dataset (ACTIVE_CUSTOM_MANAGED, daily) + its mapping row.
        # spec: BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.
        for source_id, urn, name in (
            (source_fresh, urn_fresh, "spot-breakdown-reconcile-fresh"),
            (source_stale, urn_stale, "spot-breakdown-reconcile-stale"),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
                "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
                source_id,
                "ACTIVE_CUSTOM_MANAGED",
                name,
                "postgres",
                json.dumps({"source": {"type": "postgres", "config": {}}}),
                "0 0 * * *",
                "daily",
                "OK",
            )
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "emitted",
            )

        # Reset then seed controlled source-keyed INGESTION.COMPLETE timestamps.
        # spec: feature/BACKEND.md §Metrics Service §Ingestion evidence — runs are booked on
        # the owning source (entity_type='ingestion_source', entity_id=source_id).
        await conn.execute(
            "DELETE FROM dataspoke.events"
            " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
            [*source_ids, urn_fresh, urn_stale],
        )
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "ingestion_source", source_fresh, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=130000),  # inside the declared 172800s window → in-time
        )
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "ingestion_source", source_stale, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=200000),  # outside the declared 172800s window → failed
        )

        # Create + enable the metric scoped to exactly the two seeded datasets.
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Breakdown Reconcile Spot Test",
                "description": "Reconciles result breakdown counts with the RUN_COMPLETE event.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                # 130000s is inside this window and 200000s is outside it, so the two
                # seeded sources land on opposite verdicts.
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn IN ('{urn_fresh}', '{urn_stale}')",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text
        # Bind the event to this exact run — identity, not a count-delta window.
        # spec: spec/TESTING.md §Integration Lifecycle & Isolation — bind by run_id.
        target_run_id = run_resp.json()["run_id"]

        # ── Breakdown view (persisted result) ─────────────────────────────────
        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        assert results_resp.status_code == 200, results_resp.text
        results = results_resp.json().get("results", [])
        # Backstop: this fresh metric ran exactly once, so its sole result unambiguously
        # corresponds to target_run_id.
        assert len(results) == 1, (
            "Fresh metric must have exactly one persisted result after one run; "
            f"got {len(results)}."
        )
        row = results[0]
        values = row["values"]
        assert values["total"] == 2.0, (
            f"total must be 2 (both seeded datasets); got {values['total']}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )
        assert values["ingested_in_time"] == 1.0, (
            f"ingested_in_time must be 1 (only urn_fresh); got {values['ingested_in_time']}. "
            "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
        )

        breakdown = row.get("breakdown", {})
        # dataset_count == total scanned == values.total.
        # spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — dataset_count is the
        # total scanned (matching dataset_filter).
        assert breakdown.get("dataset_count") == values["total"], (
            f"breakdown.dataset_count ({breakdown.get('dataset_count')}) must equal values.total "
            f"({values['total']}). "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        breakdown_urns = [e["urn"] for e in breakdown.get("datasets", [])]
        # Both sides seeded: failed (urn_stale) listed, in-time (urn_fresh) excluded.
        assert urn_stale in breakdown_urns, (
            "urn_stale (failed) must appear in breakdown.datasets. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — only failed."
        )
        assert urn_fresh not in breakdown_urns, (
            "urn_fresh (in-time) must NOT appear in breakdown.datasets. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — only failed."
        )
        # len(datasets) == failed count == total - passed.
        # spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — 'len(datasets) ==
        # failed count' is implied.
        failed_count = len(breakdown.get("datasets", []))
        assert failed_count == values["total"] - values["ingested_in_time"], (
            f"len(breakdown.datasets) ({failed_count}) must equal total - ingested_in_time "
            f"({values['total'] - values['ingested_in_time']}). "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )

        # ── RUN_COMPLETE event — selected by run_id ────────────────────────────
        events_resp = await api_client.get(f"{base_events}?limit=20", headers=admin_headers)
        assert events_resp.status_code == 200, events_resp.text
        event = next(
            (
                e for e in events_resp.json().get("events", [])
                if e.get("event_type") == "METRIC.RUN_COMPLETE"
                and e.get("detail", {}).get("run_id") == target_run_id
            ),
            None,
        )
        assert event is not None, (
            f"No METRIC.RUN_COMPLETE event for run_id={target_run_id}. "
            "Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE detail.run_id."
        )
        bs = event["detail"]["breakdown_summary"]
        # The event summary must reconcile with the persisted result breakdown.
        # spec: spec/feature/BACKEND.md §Event Catalogue — breakdown_summary {dataset_count,
        # affected_count}.
        assert bs["dataset_count"] == breakdown["dataset_count"], (
            f"event breakdown_summary.dataset_count ({bs['dataset_count']}) must equal the result "
            f"breakdown.dataset_count ({breakdown['dataset_count']}). "
            "Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE."
        )
        assert bs["affected_count"] == failed_count, (
            f"event breakdown_summary.affected_count ({bs['affected_count']}) must equal the "
            f"number of failed datasets in the result breakdown ({failed_count}). "
            "Spec: spec/feature/BACKEND.md §Event Catalogue — METRIC.RUN_COMPLETE."
        )

    finally:
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE source_id = ANY($1::uuid[])",
                source_ids,
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])", source_ids
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.events"
                " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
                [*source_ids, urn_fresh, urn_stale],
            )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


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


# ── Ingestion-freshness measurement window and evidence ──────────────────────
#
# These tests need raw ORM/SQL-seeded state that the api-wired pipeline cannot
# naturally produce. They insert rows directly via asyncpg and clean up in finally.
#
# The freshness measurer reads ingestion_source + ingestion_source_dataset (via a
# JOIN) to resolve each dataset's owning source, then that source's event feed.
# Seeds insert into those tables directly.
#
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — ingested_in_time counts
#       datasets whose latest ingestion evidence falls within metric_conf.time_window_sec.
# Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window, §Ingestion evidence.
# Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.


@pytest.mark.asyncio
async def test_ingestion_freshness_declared_window_applies_to_a_passive_source(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness: a PASSIVE-owned dataset is judged by the declared window.

    Seeds **one PASSIVE ingestion_source per dataset**, each with its own
    ingestion_source_dataset row (derivation='matched') and its own source-keyed
    INGESTION.COMPLETE. The metric declares ``time_window_sec=172800``:
      - source_fresh / urn_fresh: run 3 hours ago (< 172800s) → in-time
      - source_stale / urn_stale: run 200000s ago (> 172800s) → stale

    The fresh side is the discriminating one. A ``PASSIVE`` source registers no schedule
    at all and DataSpoke books its events on the hourly ``datahub-sync`` sweep, so any
    window scaled to how often something is *expected* to happen would be far narrower
    than 172800s and would call a 3-hour-old evidence row stale. Sync cadence answers a
    different question from how recent the evidence must be to count.

    One source per dataset because freshness is the recency of the *owning source's*
    runs: a single shared source would give both datasets one verdict and the
    fresh/stale contrast would collapse into a vacuous pass.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the window is
          ``metric_conf.time_window_sec``, applied uniformly to every dataset in the run.
          It is a declared SLO the governance lead owns, not a quantity derived from a
          per-dataset fact such as an owning source's registered schedule, a sync-loop
          cadence, or a dataset's observed validation inter-arrival gap."
    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — runs are booked
          with entity_type='ingestion_source', entity_id=source_id.
    Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source / §ingestion_source_dataset.
    """
    _METRIC_ID = "spot-freshness-declared-window"
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
    # Stable, valid UUID v4 source IDs — deterministic so cleanup is reliable even on partial
    # failure.  Non-standard strings are rejected by asyncpg as invalid UUIDs; use proper UUID.
    # spec: BACKEND_SCHEMA.md §ingestion_source — id column is UUID.
    source_fresh = str(uuid.UUID("00000000-0000-4000-8000-000000000b01"))
    source_stale = str(uuid.UUID("00000000-0000-4000-8000-000000000b02"))
    source_ids = [source_fresh, source_stale]
    try:
        # Drop any mapping another test left on these URNs: an extra covering source
        # would enter the owning-source ranking and could win it.
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = ANY($1::text[])",
            [urn_fresh, urn_stale],
        )

        # One PASSIVE ingestion_source per dataset (no schedule / schedule_tier)
        # spec: BACKEND_SCHEMA.md §ingestion_source — PASSIVE sources have no schedule/schedule_tier
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation='matched' for PASSIVE
        for source_id, urn, name in (
            (source_fresh, urn_fresh, "spot-freshness-passive-fresh"),
            (source_stale, urn_stale, "spot-freshness-passive-stale"),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
                "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
                source_id,
                "PASSIVE",
                name,
                "kafka",
                json.dumps({"source": {"type": "kafka", "config": {}}}),
                None,
                None,
                "OK",
            )
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "matched",
            )

        # Clear any pre-existing INGESTION.COMPLETE booked on these sources or URNs
        await conn.execute(
            "DELETE FROM dataspoke.events"
            " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
            [*source_ids, urn_fresh, urn_stale],
        )

        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "ingestion_source", source_fresh, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(hours=3),  # inside the declared 172800s window
        )
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "ingestion_source", source_stale, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=200000),  # outside the declared 172800s window
        )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Declared Window Spot Test",
                "description": "The declared window judges a PASSIVE-owned dataset",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn IN ('{urn_fresh}', '{urn_stale}')",
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
            "the PASSIVE-owned dataset whose run is 3 hours old is inside the declared "
            "172800s window and must be counted; got "
            f"{row['values']['ingested_in_time']}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
        )

        breakdown_urns = [e["urn"] for e in row.get("breakdown", {}).get("datasets", [])]
        assert breakdown_urns == [urn_stale], (
            f"only the dataset whose run predates the declared window may be listed; got "
            f"{breakdown_urns}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        detail = row["breakdown"]["datasets"][0]["detail"]
        assert detail["time_window_sec"] == 172800, (
            "the run must report the window it applied, which is the declared "
            f"metric_conf.time_window_sec; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
        )
        assert "window_source" not in detail, (
            "detail must not name a window provenance: the window is always the declared "
            f"metric_conf.time_window_sec; got keys {sorted(detail)}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        # The stale entry reports the run booked on its own PASSIVE source.
        assert detail["last_event_at"] is not None, (
            "last_event_at must carry the stale source's run; None would mean the "
            "source-keyed INGESTION.COMPLETE was never read. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert datetime.fromisoformat(detail["last_event_at"]) < now - timedelta(
            seconds=172800
        ), (
            f"the reported run must be the out-of-window one that was seeded; got "
            f"{detail['last_event_at']!r}."
        )
        assert detail["evidence_tier"] == "source_level", (
            "the seeded rows carry detail={} so no observation producer wrote them: the "
            "source-level fallback is what supplies last_event_at; got "
            f"evidence_tier={detail.get('evidence_tier')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )

    finally:
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE source_id = ANY($1::uuid[])",
                source_ids,
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])", source_ids
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.events"
                " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
                [*source_ids, urn_fresh, urn_stale],
            )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_freshness_dataset_with_no_owning_source_is_stale(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness: a dataset with no ingestion_source mapping has no readable
    evidence at all, so it is stale and still reports the declared window.

    Uses a URN that has no ingestion_source_dataset row and a declared
    ``time_window_sec=3600``.

    The event seeded here is a **decoy**: an INGESTION.COMPLETE 60s old written
    with entity_type='dataset', entity_id=<the URN> — a shape the sweep never emits.
    It is well inside every window, so a measurer that keyed runs by dataset URN
    would report this dataset as in-time. Asserting ``last_event_at is None``
    therefore has an injected subject rather than being trivially true.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is
          failed when its evidence is "absent on both tiers".
    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "every
          INGESTION.* event is booked on a source (entity_type='ingestion_source',
          entity_id=source_id …) and never on the dataset, so the measurer resolves each
          dataset's owning source first."
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the window is
          ``metric_conf.time_window_sec``, applied uniformly to every dataset in the run."
    Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source_dataset — no row → no owner.
    """
    _METRIC_ID = "spot-freshness-unclaimed-dataset"
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
        # Decoy: a very recent INGESTION.COMPLETE keyed by dataset URN — the shape the
        # sweep never emits. Inside every window, so it is what a measurer reading
        # dataset-keyed events would (wrongly) call in-time.
        _del_decoy = (
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = $1 AND event_type = 'INGESTION.COMPLETE'"
        )
        await conn.execute(_del_decoy, urn)
        await conn.execute(
            "INSERT INTO dataspoke.events "
            "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
            "dataset", urn, "INGESTION.COMPLETE", "success",
            json.dumps({}),
            now - timedelta(seconds=60),
        )

        # Create with a deliberately narrow declared window (3600s). The dataset is stale
        # because it has no owning source and therefore no readable evidence at all — not
        # because of the window: the 60s-old decoy would be inside even a 3600s window.
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Unclaimed Dataset Spot Test",
                "description": (
                    "A dataset with no ingestion_source_dataset row has no evidence to read"
                ),
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                # The declared window the breakdown must report. It is not what makes the
                # dataset stale — the absent owning source is.
                "metric_conf": {"time_window_sec": 3600},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{urn}'",
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
            "A dataset mapped to no source has no owning source's runs to read, so it is "
            "stale however recent the dataset-keyed decoy is (ingested_in_time=0). "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
        stale_entries = row.get("breakdown", {}).get("datasets", [])
        assert stale_entries, "Must have stale entry."
        detail = stale_entries[0]["detail"]
        assert detail["time_window_sec"] == 3600, (
            "the declared metric_conf.time_window_sec must be reported even where no "
            f"owning source exists; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
        )
        assert "window_source" not in detail, (
            "detail must not name a window provenance: the window is always the declared "
            f"metric_conf.time_window_sec; got keys {sorted(detail)}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert detail["last_event_at"] is None, (
            "The seeded dataset-keyed INGESTION.COMPLETE must not be read: runs are booked "
            f"on the owning source, never on the dataset; got {detail['last_event_at']!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )
        assert detail["evidence_tier"] is None, (
            "with no owning source neither tier produced evidence; got "
            f"{detail.get('evidence_tier')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
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


@pytest.mark.asyncio
async def test_ingestion_freshness_reads_source_keyed_events_not_dataset_keyed(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ingestion-freshness reads the run booked on the owning source, not one keyed by
    the dataset URN.

    Both sides of the query predicate are seeded, and in opposite directions, so
    neither leg can pass by accident:

      - urn_a / source_a: source-keyed run 130000s ago (in-window) **and** a decoy
        ``entity_type='dataset'`` run 200000s ago (out-of-window) for the same URN
        → must be **in-time**. Reading the decoy would call it stale.
      - urn_b / source_b: source-keyed run 200000s ago (out-of-window) **and** a decoy
        ``entity_type='dataset'`` run 60s ago (in-window) for the same URN
        → must be **stale**, with ``last_event_at`` the out-of-window timestamp.
        Reading the decoy would call it in-time.

    A dataset-keyed INGESTION.COMPLETE is a shape nothing in the product writes, which
    is exactly why it makes a decoy: only the ``entity_type='ingestion_source'`` /
    ``entity_id=source_id`` rows may be read. Real PostgreSQL is required — the unit
    tier's fake session cannot prove a WHERE clause it would have to reimplement.

    Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence — "every INGESTION.*
          event is booked on a source (entity_type='ingestion_source',
          entity_id=source_id — see the Event Catalogue) and never on the dataset, so the
          measurer resolves each dataset's owning source first. It then reads that
          source's feed in two tiers of evidence."
    Spec: spec/TESTING.md §Assertion Discipline — "Filter/query/matching tests seed both
          sides."
    """
    _METRIC_ID = "spot-freshness-source-keyed-events"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    # Only catalog.* is registered in DataHub by this module's DUMMY_DATA_DATAHUB_SCHEMAS.
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    source_a = str(uuid.UUID("00000000-0000-4000-8000-000000000e01"))
    source_b = str(uuid.UUID("00000000-0000-4000-8000-000000000e02"))
    source_ids = [source_a, source_b]
    try:
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = ANY($1::text[])",
            [urn_a, urn_b],
        )
        for source_id, urn, name in (
            (source_a, urn_a, "spot-freshness-source-keyed-a"),
            (source_b, urn_b, "spot-freshness-source-keyed-b"),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
                "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
                source_id,
                "ACTIVE_CUSTOM_MANAGED",
                name,
                "postgres",
                json.dumps({"source": {"type": "postgres", "config": {}}}),
                "0 0 * * *",
                "daily",
                "OK",
            )
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "emitted",
            )

        await conn.execute(
            "DELETE FROM dataspoke.events"
            " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
            [*source_ids, urn_a, urn_b],
        )
        # The four rows: two source-keyed (must be read) and two dataset-keyed decoys
        # (must be ignored), each pair pointing at the opposite verdict.
        for entity_type, entity_id, seconds_ago in (
            ("ingestion_source", source_a, 130000),  # in-window  → urn_a in-time
            ("dataset", urn_a, 200000),              # decoy, out-of-window
            ("ingestion_source", source_b, 200000),  # out-of-window → urn_b stale
            ("dataset", urn_b, 60),                  # decoy, well in-window
        ):
            await conn.execute(
                "INSERT INTO dataspoke.events "
                "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
                entity_type, entity_id, "INGESTION.COMPLETE", "success",
                json.dumps({}),
                now - timedelta(seconds=seconds_ago),
            )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Source-Keyed Events Spot Test",
                "description": (
                    "Runs are read from the owning source, never from a dataset-keyed row."
                ),
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                # 130000s is inside this window and 200000s is outside it, so the two
                # seeded pairs land on opposite verdicts.
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn IN ('{urn_a}', '{urn_b}')",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        results_resp = await api_client.get(f"{base_results}?limit=5", headers=admin_headers)
        assert results_resp.status_code == 200, results_resp.text
        results = results_resp.json().get("results", [])
        assert len(results) == 1, (
            f"This fresh metric ran once, so it must have exactly one result; got {len(results)}."
        )
        row = results[0]
        values = row["values"]

        assert values["total"] == 2.0, (
            f"total must be 2 (both catalog URNs resolved); got {values['total']}. "
            "If 0, the URN was not registered in DataHub — check DUMMY_DATA_DATAHUB_SCHEMAS."
        )
        assert values["ingested_in_time"] == 1.0, (
            f"exactly urn_a must be in-time; got ingested_in_time={values['ingested_in_time']}. "
            "0 means the source-keyed run was not read; 2 means the dataset-keyed decoy was. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )

        breakdown_urns = [e["urn"] for e in row.get("breakdown", {}).get("datasets", [])]
        assert breakdown_urns == [urn_b], (
            f"only urn_b (whose owning source's run is out of window) may be listed; got "
            f"{breakdown_urns}. Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        detail = row["breakdown"]["datasets"][0]["detail"]
        assert detail["time_window_sec"] == 172800
        assert detail["last_event_at"] is not None, (
            "urn_b's stale entry must report its owning source's run, not None."
        )
        assert datetime.fromisoformat(detail["last_event_at"]) < now - timedelta(
            seconds=172800
        ), (
            f"last_event_at must be the source-keyed out-of-window run, not the 60s-old "
            f"dataset-keyed decoy; got {detail['last_event_at']!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Ingestion evidence."
        )

    finally:
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE source_id = ANY($1::uuid[])",
                source_ids,
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])", source_ids
            )
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.events"
                " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = ANY($1::text[])",
                [*source_ids, urn_a, urn_b],
            )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)


# ── Validation-score measurement window ──────────────────────────────────────
#
# Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — validation_score_sum is
#       the sum of each dataset's latest validation score whose data_time falls within
#       metric_conf.time_window_sec of the measurement.
# Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window.


@pytest.mark.asyncio
async def test_validation_score_declared_window_gates_the_latest_row(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """validation-score: one declared window gates both datasets' latest rows.

    Both datasets are seeded with four results 24 hours apart, and the metric declares
    ``time_window_sec=86400`` — deliberately *narrower* than the 24-hour spacing:
      - urn_fresh: latest result 1h ago (score=1.0) → inside the declared window →
        contributes 1.0 and is not listed.
      - urn_stale: latest result 30h ago (score=0.5) → outside the declared window →
        contributes 0.0 and is listed, with detail.time_window_sec reporting 86400.

    urn_stale's 30-hour age is the load-bearing choice: it sits *outside* the declared
    86400s window but *inside* twice the seeded 24-hour inter-arrival gap. So the
    declared window and any window scaled to the dataset's own validation cadence
    disagree about it, and they disagree on what the run reports —
    ``validation_score_sum`` (0.5 counted or not) and ``detail.time_window_sec`` both
    move with the window. Breakdown *membership* is not part of that signal: urn_stale
    scores 0.5, so it is listed either way — via the "no result inside the window" branch
    under the declared window and via the "score < 1.0" branch under a wider one. Its
    score stays at 0.5 rather than 1.0 so ``detail["score"]`` below still identifies
    whose row was read.

    Real PostgreSQL is required: the latest-per-dataset row is picked by a row_number()
    window function the unit tier's fake session cannot execute — which is also why this
    four-rows-per-dataset shape exists only here and not at the unit tier.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window — "the window is
          ``metric_conf.time_window_sec``, applied uniformly to every dataset in the run
          … not a quantity derived from a per-dataset fact such as … a dataset's observed
          validation inter-arrival gap"; "``validation-score``: the score counted is the
          latest result whose ``data_time`` is inside ``time_window_sec``; a dataset with
          no result in the window contributes ``0.0``".
    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format — a dataset is failed
          when its "latest validation ``score`` inside the window is ``< 1.0`` (or no
          result inside the window)"; detail carries ``latest_data_time`` + ``score`` +
          ``time_window_sec``.
    """
    _METRIC_ID = "spot-validation-declared-window"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/result"

    urn_fresh = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
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

        # urn_fresh: four rows 24h apart, latest at 1h → inside the declared 86400s window.
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

        # urn_stale: same 24h spacing, latest at 30h → outside the declared 86400s window
        # (24h) while still inside twice the seeded 24h gap (48h).
        for offset_hours in [30, 54, 78, 102]:
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
                "title": "Declared Window Spot Test",
                "description": "The declared window gates the latest validation row",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "validation_score_sum", "color": "#3B82F6", "idx": 2},
                ],
                "metric_conf": {"time_window_sec": 86400},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn IN ('{urn_fresh}', '{urn_stale}')",
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
        assert row["values"]["validation_score_sum"] == 1.0, (
            "only urn_fresh's latest row is inside the declared 86400s window, so the sum "
            f"is its 1.0; got {row['values']['validation_score_sum']}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
        )

        breakdown = row.get("breakdown", {})
        breakdown_urns = [e["urn"] for e in breakdown.get("datasets", [])]
        assert breakdown_urns == [urn_stale], (
            "only the dataset with no result inside the declared window may be listed; got "
            f"{breakdown_urns}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        detail = breakdown["datasets"][0]["detail"]
        assert detail["time_window_sec"] == 86400, (
            "the run must report the window it applied, which is the declared "
            f"metric_conf.time_window_sec; got {detail.get('time_window_sec')}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Measurement window."
        )
        assert "window_source" not in detail, (
            "detail must not name a window provenance: the window is always the declared "
            f"metric_conf.time_window_sec; got keys {sorted(detail)}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert detail["score"] == 0.5, (
            "backstop: the listed dataset's own out-of-window row must be what is "
            f"reported; got {detail.get('score')!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert datetime.fromisoformat(detail["latest_data_time"]) < now - timedelta(
            seconds=86400
        ), (
            "latest_data_time must be the out-of-window row that was seeded; got "
            f"{detail['latest_data_time']!r}."
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
                "metrics": [{"name": "total", "color": "#64748B", "idx": 1}],
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{_URN}'",
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/governance/metric must return 201 on create; "
            f"got {create_resp.status_code}: {create_resp.text}. "
            "Spec: spec/USE_CASE_en.md §UC5 §API Mapping."
        )
        assert create_resp.json()["metrics"] == [
            {"name": "total", "color": "#64748B", "idx": 1}
        ], (
            "Created metric must carry the declared single series descriptor for 'total'. "
            "Spec: spec/API.md §Metric §Definition body — `metrics` | object[] | "
            "\"Series descriptors — {name, color, idx}\"."
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


# ── List last_run_at ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_last_run_at_reflects_latest_run_complete_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/governance/metric list rows carry last_run_at = the latest
    METRIC.RUN_COMPLETE occurred_at; a metric that never completed a run carries null.

    Seeded directly because the value derives from the newest METRIC.RUN_COMPLETE
    event for the metric: two events with controlled occurred_at are inserted so the
    NEWEST one must win, and a sibling metric with NO completed-run event must
    surface last_run_at=null. The api-wired pipeline cannot reach the "never run"
    null case (running a metric requires Airflow), so spot owns this with raw-SQL
    state per spec/TESTING.md §Spot integration tests.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric — each row carries
          last_run_at (occurred_at of the latest METRIC.RUN_COMPLETE event, null
          when never run).
    Spec: spec/feature/BACKEND.md §Metrics Service — List last_run_at: newest
          METRIC.RUN_COMPLETE per metric, resolved page-bounded.
    """
    ran_id = "spot-last-run-ran"
    never_id = "spot-last-run-never"
    ran_conf = f"/api/v1/spoke/governance/metric/{ran_id}/attr/conf"
    never_conf = f"/api/v1/spoke/governance/metric/{never_id}/attr/conf"

    _CREATE = {
        "mode": "active",
        "is_enabled": False,
        "metric_type": "doc-health",
        "title": "Last-run spot",
        "description": "Seeds METRIC.RUN_COMPLETE events to assert last_run_at.",
        "metrics": [
            {"name": "total", "color": "#64748B", "idx": 1},
            {"name": "doc_health", "color": "#A855F7", "idx": 2},
        ],
        "metric_conf": {},
        "schedule_tier": "daily",
        "dataset_filter": "",
    }

    # Two RUN_COMPLETE events: the OLDER and the NEWER. last_run_at must equal the
    # newer one's occurred_at (relative ordering, not wall-clock).
    newer = datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC)
    older = newer - timedelta(days=3)

    # Clean slate
    await api_client.delete(ran_conf, headers=admin_headers)
    await api_client.delete(never_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        for mid in (ran_id, never_id):
            resp = await api_client.post(
                "/api/v1/spoke/governance/metric",
                headers=admin_headers,
                json={**_CREATE, "metric_id": mid},
            )
            assert resp.status_code == 201, resp.text

        # Seed only the "ran" metric with two completed-run events.
        for ts in (older, newer):
            await conn.execute(
                "INSERT INTO dataspoke.events "
                "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
                "metric", ran_id, "METRIC.RUN_COMPLETE", "success",
                json.dumps({"run_id": str(uuid.uuid4())}), ts,
            )

        resp = await api_client.get(
            "/api/v1/spoke/governance/metric?limit=200", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        by_id = {m["id"]: m for m in resp.json()["metrics"]}
        assert ran_id in by_id and never_id in by_id, (
            f"both seeded metrics must appear in the list; got {list(by_id)}."
        )

        # The run metric's last_run_at is the NEWER event's occurred_at.
        assert "last_run_at" in by_id[ran_id], (
            "list rows must carry last_run_at. Spec: spec/API.md §Metric."
        )
        served = datetime.fromisoformat(
            by_id[ran_id]["last_run_at"].replace("Z", "+00:00")
        )
        assert served == newer, (
            f"last_run_at must equal the NEWEST METRIC.RUN_COMPLETE occurred_at "
            f"({newer.isoformat()}); got {by_id[ran_id]['last_run_at']!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service — newest RUN_COMPLETE wins."
        )

        # The never-run metric has no completed-run event → last_run_at is null.
        assert by_id[never_id]["last_run_at"] is None, (
            "a metric with no METRIC.RUN_COMPLETE event must carry last_run_at=null. "
            "Spec: spec/API.md §Metric — null when never run."
        )
    finally:
        await conn.execute(
            "DELETE FROM dataspoke.events WHERE entity_type='metric' AND entity_id=$1",
            ran_id,
        )
        await conn.close()
        with suppress(Exception):
            await api_client.delete(ran_conf, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(never_conf, headers=admin_headers)


# ── Per-dataset verdicts and GET .../dataset ──────────────────────────────────
#
# Spot rather than api-wired: these need `metric_dataset_results` read straight from
# the operational DB, and a dry run whose *absence* of writes must be observed against
# a known prior verdict set — state the api-wired pipeline cannot arrange through the
# public API alone (spec/TESTING.md §Integration Testing).

_VERDICT_FRESH = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_VERDICT_STALE = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)


async def _verdict_rows(conn: asyncpg.Connection, metric_id: str) -> list[dict]:
    """Every `metric_dataset_results` row for a metric, ordered by dataset_urn."""
    rows = await conn.fetch(
        "SELECT dataset_urn, met, evidence_at, detail, measured_at "
        "FROM dataspoke.metric_dataset_results WHERE metric_id = $1 ORDER BY dataset_urn",
        metric_id,
    )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_a_real_run_writes_one_verdict_row_per_dataset_in_scope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A non-dry run persists a verdict for every dataset in scope, passing and failing.

    Two datasets are seeded onto opposite sides of the declared window, so a store that
    kept only the failures would be caught by the missing `met = true` row.

    Spec: spec/feature/BACKEND.md §Metrics Service — "**Per-dataset verdict store**
          (`metric_dataset_results`, keyed `(metric_id, dataset_urn)`) holds the
          **latest** verdict only. A non-dry run replaces the metric's rows wholesale
          inside the result transaction";
    Spec: §Verdict contract — "`verdicts` covers **every** dataset in scope, not only
          the failing ones".
    """
    _METRIC_ID = "spot-verdict-write"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    source_fresh = str(uuid.UUID("00000000-0000-4000-8000-000000000d01"))
    source_stale = str(uuid.UUID("00000000-0000-4000-8000-000000000d02"))
    try:
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = ANY($1::text[])",
            [_VERDICT_FRESH, _VERDICT_STALE],
        )
        for source_id, urn, name, age_sec in (
            (source_fresh, _VERDICT_FRESH, "spot-verdict-fresh", 130000),
            (source_stale, _VERDICT_STALE, "spot-verdict-stale", 200000),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
                "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
                source_id,
                "ACTIVE_CUSTOM_MANAGED",
                name,
                "postgres",
                json.dumps({"source": {"type": "postgres", "config": {}}}),
                "0 0 * * *",
                "daily",
                "OK",
            )
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "emitted",
            )
            await conn.execute(
                "DELETE FROM dataspoke.events"
                " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = $1",
                source_id,
            )
            await conn.execute(
                "INSERT INTO dataspoke.events "
                "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
                "ingestion_source", source_id, "INGESTION.COMPLETE", "success",
                json.dumps({}),
                now - timedelta(seconds=age_sec),
            )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Verdict write spot test",
                "description": "One verdict row per dataset in scope.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": (
                    f"dataset_urn IN ('{_VERDICT_FRESH}', '{_VERDICT_STALE}')"
                ),
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        rows = await _verdict_rows(conn, _METRIC_ID)
        assert [row["dataset_urn"] for row in rows] == sorted(
            [_VERDICT_FRESH, _VERDICT_STALE]
        ), (
            f"one row per dataset in scope, passing and failing alike; got {rows!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
        )
        by_urn = {row["dataset_urn"]: row for row in rows}
        assert by_urn[_VERDICT_FRESH]["met"] is True
        assert by_urn[_VERDICT_STALE]["met"] is False
        assert by_urn[_VERDICT_FRESH]["evidence_at"] is not None, (
            "ingestion-freshness dates each verdict by its resolved evidence. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
        )
        assert by_urn[_VERDICT_FRESH]["measured_at"] == by_urn[_VERDICT_STALE]["measured_at"], (
            "both rows come from the same run, so they carry one measured_at. "
            "Spec: spec/feature/BACKEND.md §Metrics Service — "
            "'the store always reflects exactly one run'."
        )

        # A second run replaces the set wholesale rather than accumulating.
        second_resp = await api_client.post(base_run, headers=admin_headers)
        assert second_resp.status_code == 200, second_resp.text
        second_rows = await _verdict_rows(conn, _METRIC_ID)
        assert len(second_rows) == 2, (
            f"a re-run must replace, not accumulate; got {len(second_rows)} rows. "
            "Spec: spec/feature/BACKEND.md §Metrics Service — replaced 'wholesale'."
        )
        assert second_rows[0]["measured_at"] > rows[0]["measured_at"], (
            "backstop: the second run must actually have re-measured"
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.events WHERE entity_id = ANY($1::text[])",
                [source_fresh, source_stale],
            )
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE source_id = ANY($1::uuid[])",
                [source_fresh, source_stale],
            )
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])",
                [source_fresh, source_stale],
            )
        await conn.close()


@pytest.mark.asyncio
async def test_a_dry_run_leaves_the_previous_runs_verdicts_untouched(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A dry run neither writes verdicts nor clears the ones a real run left.

    "Persists nothing" is stronger than "writes nothing new": a dry run that deleted
    first and then skipped the insert would leave the store empty, which reads exactly
    like a metric that never ran.

    Spec: spec/feature/BACKEND.md §Metrics Service — "A **dry run persists nothing** —
          the standing metrics invariant — leaving the previous run's verdicts readable";
    Spec: spec/API.md §Metric — "a dry run persists none, so `/dataset` after a dry run
          still reports the previous run's verdicts".
    """
    _METRIC_ID = "spot-verdict-dry-run"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Dry-run verdict spot test",
                "description": "A dry run must persist no verdicts.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        real_resp = await api_client.post(base_run, headers=admin_headers)
        assert real_resp.status_code == 200, real_resp.text
        after_real = await _verdict_rows(conn, _METRIC_ID)
        assert len(after_real) == 1, (
            f"backstop: the real run must have written a verdict; got {after_real!r}"
        )

        dry_resp = await api_client.post(f"{base_run}?dry_run=true", headers=admin_headers)
        assert dry_resp.status_code == 200, dry_resp.text
        assert dry_resp.json()["detail"]["dry_run"] is True

        after_dry = await _verdict_rows(conn, _METRIC_ID)
        assert after_dry == after_real, (
            "a dry run must leave the previous run's verdicts byte-identical; got "
            f"{after_dry!r} vs {after_real!r}. "
            "Spec: spec/feature/BACKEND.md §Metrics Service — dry run persists nothing."
        )

        # And the read model agrees: /dataset still reports the real run's verdict.
        view = await api_client.get(
            f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset", headers=admin_headers
        )
        assert view.status_code == 200, view.text
        assert [row["met"] for row in view.json()["datasets"]] != ["unknown"], (
            "after a dry run the dataset must still carry the real run's verdict. "
            "Spec: spec/API.md §Metric."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        await conn.close()


@pytest.mark.asyncio
async def test_dataset_view_met_filter_combinations(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """`met` selects any combination of the three states; omitting it selects all.

    The metric is run scoped to two datasets seeded onto opposite sides of the window,
    then widened to every DEV dataset *without* re-running — so the view carries a
    known `true`, a known `false`, and at least one `unknown` (a dataset that entered
    scope after the last run). Each combination therefore has something to include and
    something to exclude.

    Spec: spec/API.md §Metric — "Repeatable `met` query param (default: all three)";
          "`met` is `"unknown"` exactly when the dataset is in the filter's scope but
          carries no verdict — the metric has never run, or the dataset entered scope
          after the last run".
    """
    _METRIC_ID = "spot-dataset-met-filter"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    now = datetime.now(tz=UTC)
    source_fresh = str(uuid.UUID("00000000-0000-4000-8000-000000000d11"))
    source_stale = str(uuid.UUID("00000000-0000-4000-8000-000000000d12"))
    try:
        await conn.execute(
            "DELETE FROM dataspoke.ingestion_source_dataset WHERE dataset_urn = ANY($1::text[])",
            [_VERDICT_FRESH, _VERDICT_STALE],
        )
        for source_id, urn, name, age_sec in (
            (source_fresh, _VERDICT_FRESH, "spot-met-filter-fresh", 130000),
            (source_stale, _VERDICT_STALE, "spot-met-filter-stale", 200000),
        ):
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source "
                "(id, mode, name, platform, recipe, schedule, schedule_tier, status) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) "
                "ON CONFLICT (id) DO UPDATE SET mode=$2, schedule_tier=$7",
                source_id,
                "ACTIVE_CUSTOM_MANAGED",
                name,
                "postgres",
                json.dumps({"source": {"type": "postgres", "config": {}}}),
                "0 0 * * *",
                "daily",
                "OK",
            )
            await conn.execute(
                "INSERT INTO dataspoke.ingestion_source_dataset "
                "(source_id, dataset_urn, derivation) VALUES ($1, $2, $3) "
                "ON CONFLICT (source_id, dataset_urn) DO UPDATE SET derivation=$3",
                source_id, urn, "emitted",
            )
            await conn.execute(
                "DELETE FROM dataspoke.events"
                " WHERE event_type = 'INGESTION.COMPLETE' AND entity_id = $1",
                source_id,
            )
            await conn.execute(
                "INSERT INTO dataspoke.events "
                "(id, entity_type, entity_id, event_type, status, detail, occurred_at) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5::jsonb, $6)",
                "ingestion_source", source_id, "INGESTION.COMPLETE", "success",
                json.dumps({}),
                now - timedelta(seconds=age_sec),
            )

        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "met filter spot test",
                "description": "Exercises every met filter combination.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
                ],
                "metric_conf": {"time_window_sec": 172800},
                "schedule_tier": None,
                "dataset_filter": (
                    f"dataset_urn IN ('{_VERDICT_FRESH}', '{_VERDICT_STALE}')"
                ),
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text

        # Widen scope without re-running: every other DEV dataset is now in scope
        # but was never evaluated, so it reads `unknown`.
        widen = await api_client.patch(
            base_conf, headers=admin_headers, json={"dataset_filter": "origin = 'DEV'"}
        )
        assert widen.status_code == 200, widen.text

        all_states = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
        assert all_states.status_code == 200, all_states.text
        all_body = all_states.json()
        served = {row["dataset_urn"]: row["met"] for row in all_body["datasets"]}
        assert served.get(_VERDICT_FRESH) == "true", served
        assert served.get(_VERDICT_STALE) == "false", served
        unknown_urns = {urn for urn, met in served.items() if met == "unknown"}
        assert unknown_urns, (
            "widening the filter must bring never-evaluated datasets into scope, or the "
            f"`unknown` cases below are vacuous; got {served!r}. "
            "Spec: spec/API.md §Metric — 'the dataset entered scope after the last run'."
        )

        for params, states, known_included in (
            ("?met=true", {"true"}, {_VERDICT_FRESH}),
            ("?met=false", {"false"}, {_VERDICT_STALE}),
            ("?met=unknown", {"unknown"}, set()),
            ("?met=true&met=false", {"true", "false"}, {_VERDICT_FRESH, _VERDICT_STALE}),
            ("?met=false&met=unknown", {"false", "unknown"}, {_VERDICT_STALE}),
            (
                "?met=true&met=false&met=unknown",
                {"true", "false", "unknown"},
                {_VERDICT_FRESH, _VERDICT_STALE},
            ),
        ):
            resp = await api_client.get(f"{base_view}{params}&limit=1000", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            returned = {row["dataset_urn"]: row["met"] for row in body["datasets"]}
            assert set(returned.values()) <= states, (
                f"{params}: only the selected states may be returned; got "
                f"{sorted(set(returned.values()))}. "
                "Spec: spec/API.md §Metric — repeatable met filter."
            )
            assert known_included <= set(returned), (
                f"{params}: the seeded datasets in a selected state must be present; got "
                f"{sorted(returned)}."
            )
            excluded = {_VERDICT_FRESH, _VERDICT_STALE} - known_included
            assert not (excluded & set(returned)), (
                f"{params}: a dataset whose state was not selected must be excluded; got "
                f"{sorted(returned)}."
            )
            expected_count = sum(1 for met in served.values() if met in states)
            assert body["total_count"] == expected_count, (
                f"{params}: total_count must count the filtered set; expected "
                f"{expected_count}, got {body['total_count']}"
            )
            assert body["attrs_synced_at"] == all_body["attrs_synced_at"], (
                f"{params}: attrs_synced_at is scope-relative and must not move with the "
                "met filter. Spec: spec/API.md §Metric — 'unaffected by `met` filtering "
                "or paging'."
            )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        with suppress(Exception):
            await conn.execute(
                "DELETE FROM dataspoke.events WHERE entity_id = ANY($1::text[])",
                [source_fresh, source_stale],
            )
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source_dataset "
                "WHERE source_id = ANY($1::uuid[])",
                [source_fresh, source_stale],
            )
            await conn.execute(
                "DELETE FROM dataspoke.ingestion_source WHERE id = ANY($1::uuid[])",
                [source_fresh, source_stale],
            )
        await conn.close()


@pytest.mark.asyncio
async def test_deleting_a_metric_clears_its_verdict_rows(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE .../attr/conf removes the metric's own `metric_dataset_results` rows.

    A bystander metric is measured alongside it: the deletion must clear *its*
    verdicts, and only its verdicts, so an unscoped DELETE that wipes the estate's
    whole verdict table fails here rather than reading as a pass.

    Spec: spec/feature/BACKEND.md §Metrics Service — "Deleting a metric definition
          clears its verdicts";
    Spec: spec/feature/BACKEND_SCHEMA.md §metric_dataset_results — "`metric_id` | `TEXT`
          FK → `metric_definitions(id)` ON DELETE CASCADE".
    """
    _METRIC_ID = "spot-verdict-delete-cascade"
    _BYSTANDER_ID = "spot-verdict-delete-bystander"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    bystander_conf = f"/api/v1/spoke/governance/metric/{_BYSTANDER_ID}/attr/conf"
    bystander_run = f"/api/v1/spoke/governance/metric/{_BYSTANDER_ID}/method/run"

    await api_client.delete(base_conf, headers=admin_headers)
    await api_client.delete(bystander_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Delete cascade spot test",
                "description": "Verdicts must not outlive their metric.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        bystander_create = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _BYSTANDER_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Delete cascade bystander",
                "description": "Another metric's verdicts must survive the delete.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
            },
        )
        assert bystander_create.status_code == 201, bystander_create.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text
        bystander_run_resp = await api_client.post(bystander_run, headers=admin_headers)
        assert bystander_run_resp.status_code == 200, bystander_run_resp.text
        assert await _verdict_rows(conn, _METRIC_ID), (
            "backstop: the run must have written a verdict before the delete is meaningful"
        )
        assert await _verdict_rows(conn, _BYSTANDER_ID), (
            "backstop: the bystander must hold verdicts for its survival to mean anything"
        )

        delete_resp = await api_client.delete(base_conf, headers=admin_headers)
        assert delete_resp.status_code in (200, 204), delete_resp.text

        assert await _verdict_rows(conn, _METRIC_ID) == [], (
            "the metric's verdict rows must not outlive its definition. "
            "Spec: spec/feature/BACKEND.md §Metrics Service."
        )
        assert await _verdict_rows(conn, _BYSTANDER_ID), (
            "deleting one metric must clear only *its* verdicts — the bystander's rows "
            "were wiped, so the DELETE is unscoped. "
            "Spec: spec/feature/BACKEND_SCHEMA.md §metric_dataset_results — PK "
            "`(metric_id, dataset_urn)` with a per-metric FK."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(bystander_conf, headers=admin_headers)
        await conn.close()


@pytest.mark.asyncio
async def test_dataset_view_reports_the_registry_sync_watermark(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """`attrs_synced_at` echoes the newest `dataset_registry.attrs_synced_at` in scope.

    Read straight from the registry rather than from another API call: the point of the
    field is that it exposes the sweep watermark the scope was filtered against, so the
    assertion has to compare against that column.

    Spec: spec/API.md §Metric — "the **maximum** `dataset_registry.attrs_synced_at` over
          the datasets in scope […] `null` when the scope is empty or no covered dataset
          has ever synced".
    """
    _METRIC_ID = "spot-dataset-attrs-synced"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    conn = await _get_ds_conn()
    try:
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "attrs_synced_at spot test",
                "description": "Scope freshness watermark.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": f"dataset_urn = '{_BOUNDED_URN}'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        expected = await conn.fetchval(
            "SELECT max(attrs_synced_at) FROM dataspoke.dataset_registry "
            "WHERE datahub_registered = TRUE AND dataset_urn = $1",
            _BOUNDED_URN,
        )

        resp = await api_client.get(base_view, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        served = resp.json()["attrs_synced_at"]

        if expected is None:
            assert served is None, (
                "a scope whose datasets have never synced must report null. "
                "Spec: spec/API.md §Metric."
            )
        else:
            assert served is not None, (
                f"the registry holds attrs_synced_at={expected!r} for the scoped dataset, "
                "so the envelope must report it. Spec: spec/API.md §Metric."
            )
            assert datetime.fromisoformat(served.replace("Z", "+00:00")) == expected, (
                f"attrs_synced_at must echo the registry column; got {served!r}, "
                f"registry holds {expected!r}. Spec: spec/API.md §Metric."
            )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
        await conn.close()


@pytest.mark.asyncio
async def test_dataset_view_scope_matches_the_runs_own_scope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The view's dataset list is exactly the set the run measured.

    Both read the filter from the same registry, so `values.total` (the run's scanned
    count) and the view's `total_count` must agree; a divergence is the failure the
    single-resolver decision exists to prevent.

    Spec: spec/feature/BACKEND.md §Metrics Service — "Resolving scope from the same
          registry the run resolved it from is what keeps the run's verdicts and the
          endpoint's dataset list from disagreeing."
    """
    _METRIC_ID = "spot-dataset-scope-agreement"
    base_conf = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    base_view = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(base_conf, headers=admin_headers)

    try:
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Scope agreement spot test",
                "description": "The run and the view resolve one scope.",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": "origin = 'DEV'",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        run_resp = await api_client.post(base_run, headers=admin_headers)
        assert run_resp.status_code == 200, run_resp.text
        scanned = int(run_resp.json()["detail"]["values"]["total"])
        assert scanned > 0, (
            "backstop: the DEV-origin scope must be non-empty, or the agreement below "
            "is vacuous. A zero here means the attribute sweep has not run — see "
            "spec/TESTING.md §Prerequisites."
        )

        view = await api_client.get(f"{base_view}?limit=1000", headers=admin_headers)
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["total_count"] == scanned, (
            f"the view covers {body['total_count']} datasets but the run measured "
            f"{scanned}. Spec: spec/feature/BACKEND.md §Metrics Service."
        )
        assert all(row["met"] in ("true", "false") for row in body["datasets"]), (
            "every dataset in scope was measured by the run just made, so none may read "
            "'unknown'. Spec: spec/API.md §Metric."
        )
    finally:
        with suppress(Exception):
            await api_client.delete(base_conf, headers=admin_headers)
