"""Spot tests for Governance Metrics endpoints.

Concerns covered (each test targets one spec contract):
- GET /spoke/dg/metric — factory-seeded entries present after reset
- PUT/PATCH/GET/DELETE round-trip on a custom metric
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
- POST method/run dry_run=true → no persisted result, no RUN_COMPLETE event
- POST method/run dry_run=false → values is dict[str, float]
- POST method/run concurrent → 409 METRIC_RUNNING
- breakdown.datasets[] has no 'category' field
- metric_id kebab regex acceptance and rejection

Spec:
- spec/USE_CASE_en.md §UC5 — Factory defaults, Built-in active metric types, API Mapping
- spec/API.md §Metric (/spoke/dg/metric) — field rules, payload caps, error codes
- spec/feature/BACKEND.md §Metrics Service §Breakdown format
"""

import asyncio
from contextlib import suppress

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


# ── Factory defaults ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_factory_defaults_present_after_reset(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric returns the three factory-seeded entries.

    Each must have is_enabled=False, mode='active', schedule_tier='daily'.

    Spec: spec/USE_CASE_en.md §UC5 §Factory defaults — seeds ship disabled,
          mode='active', schedule_tier='daily'.
    """
    resp = await api_client.get(
        "/api/v1/spoke/dg/metric?limit=100",
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


# ── CRUD round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_get_patch_delete_round_trip(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT/GET/PATCH/DELETE round-trip on a custom doc-health metric.

    Spec: spec/API.md §Metric — PUT creates/replaces, PATCH updates fields, DELETE returns 204.
    """
    base = "/api/v1/spoke/dg/metric/doc-health-custom/attr/conf"

    try:
        # PUT
        put_resp = await api_client.put(
            base,
            headers=admin_headers,
            json={
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
        assert put_resp.status_code in (200, 201), put_resp.text
        put_body = put_resp.json()
        assert put_body["id"] == "doc-health-custom"
        assert put_body["metric_type"] == "doc-health"
        assert put_body["is_enabled"] is True
        assert put_body["schedule_tier"] == "weekly"

        # GET
        get_resp = await api_client.get(base, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["id"] == "doc-health-custom"
        assert get_body["is_enabled"] is True
        assert get_body["metric_type"] == "doc-health"

        # PATCH
        patch_resp = await api_client.patch(
            base,
            headers=admin_headers,
            json={"is_enabled": False},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_enabled"] is False

        # DELETE
        del_resp = await api_client.delete(base, headers=admin_headers)
        assert del_resp.status_code == 204

        # Verify gone
        gone_resp = await api_client.get(base, headers=admin_headers)
        assert gone_resp.status_code == 404

    finally:
        with suppress(Exception):
            await api_client.delete(base, headers=admin_headers)


# ── Validation rejections ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passive_mode_returns_501(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with mode='passive' → 501 NOT_IMPLEMENTED.

    Spec: spec/API.md §Metric — 'passive is reserved; PUT with mode: passive returns 501 NOT_IMPLEMENTED'.
    Spec: spec/USE_CASE_en.md §UC5 §Modes.
    """
    resp = await api_client.put(
        "/api/v1/spoke/dg/metric/passive-test/attr/conf",
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
        "/api/v1/spoke/dg/metric/bogus-type-test/attr/conf",
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
        "/api/v1/spoke/dg/metric/spot-missing-tw/attr/conf",
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
        "/api/v1/spoke/dg/metric/spot-neg-tw/attr/conf",
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
        "/api/v1/spoke/dg/metric/spot-dochealth-conf/attr/conf",
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
        "/api/v1/spoke/dg/metric/spot-unknown-key/attr/conf",
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
        "/api/v1/spoke/dg/metric/spot-cap-over/attr/conf",
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
    """PUT dataset_filter.dataset_urns=[<1000 well-formed urns>] → 200/201.

    Spec: spec/API.md §Metric §Payload caps — exactly 1,000 MUST be accepted.
    """
    urns_1000 = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)"
        for i in range(1000)
    ]
    base = "/api/v1/spoke/dg/metric/spot-cap-at/attr/conf"
    try:
        resp = await api_client.put(
            base,
            headers=admin_headers,
            json={
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
        assert resp.status_code in (200, 201), (
            f"Expected 200/201 for 1000 dataset_urns (at cap), got {resp.status_code}: {resp.text}. "
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
        "/api/v1/spoke/dg/metric/spot-bad-urn/attr/conf",
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
        "/api/v1/spoke/dg/metric/UPPER!/attr/conf",
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
    base_conf = "/api/v1/spoke/dg/metric/ingestion-freshness/attr/conf"
    base_run = "/api/v1/spoke/dg/metric/ingestion-freshness/method/run"
    base_results = "/api/v1/spoke/dg/metric/ingestion-freshness/attr/result"
    base_events = "/api/v1/spoke/dg/metric/ingestion-freshness/event"

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
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
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
    base_conf = "/api/v1/spoke/dg/metric/ingestion-freshness/attr/conf"
    base_run = "/api/v1/spoke/dg/metric/ingestion-freshness/method/run"
    base_results = "/api/v1/spoke/dg/metric/ingestion-freshness/attr/result"

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
        json={"dry_run": False},
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
    base_conf = f"/api/v1/spoke/dg/metric/{_CONCURRENT_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_CONCURRENT_ID}/method/run"

    await api_client.delete(base_conf, headers=admin_headers)

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "mode": "active",
            "is_enabled": True,
            "metric_type": "ingestion-freshness",
            "title": "Concurrent Guard Test",
            "description": "Tests concurrent run guard.",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "daily",
            "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
        },
    )

    async with httpx.AsyncClient(
        base_url=api_client.base_url, timeout=120.0
    ) as concurrent_client:

        async def _fire():
            return await concurrent_client.post(
                base_run,
                headers=admin_headers,
                json={"dry_run": False},
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
        f"Expected error_code='METRIC_RUNNING'. "
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
    base_conf = "/api/v1/spoke/dg/metric/doc-health/attr/conf"
    base_run = "/api/v1/spoke/dg/metric/doc-health/method/run"
    base_results = "/api/v1/spoke/dg/metric/doc-health/attr/result"

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
        json={"dry_run": False},
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
    base_conf = f"/api/v1/spoke/dg/metric/{_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_METRIC_ID}/method/run"
    base_events = f"/api/v1/spoke/dg/metric/{_METRIC_ID}/event"

    try:
        # PUT a fresh metric scoped to a ghost URN only — fast and deterministic.
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": "ingestion-freshness",
                "title": "Unresolved URN Spot Test",
                "description": "Verifies ghost URN appears in unresolved_urns event field.",
                "metrics": ["total", "ingested_in_time"],
                "metric_conf": {"time_window_sec": 86400},
                "dataset_filter": {"dataset_urns": [_GHOST_URN]},
                "schedule_tier": None,
            },
        )
        assert put_resp.status_code in (200, 201), put_resp.text

        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
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
        for key in ("run_id", "metric_id", "values", "dry_run", "unresolved_urns", "breakdown_summary"):
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

    Valid: 'ingestion-freshness', 'doc-health-prod', single-char 'a'.
    Invalid: 'UPPER', 'with_underscore', 'with space', '-leading', 'trailing-'.

    Spec: spec/API.md §Metric — metric_id kebab-case slug
          ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$.
    """
    # Valid IDs that the regex must accept (route 200/201 or 404 — anything but 422)
    valid_ids = ["a", "doc-health-prod", "ingestion-freshness"]
    for mid in valid_ids:
        resp = await api_client.get(
            f"/api/v1/spoke/dg/metric/{mid}/attr/conf",
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
            f"/api/v1/spoke/dg/metric/{mid}/attr/conf",
            headers=admin_headers,
        )
        assert resp.status_code == 422, (
            f"Invalid metric_id '{mid}' was not rejected (expected 422, got {resp.status_code}). "
            "Spec: spec/API.md §Metric — metric_id kebab-case slug."
        )
