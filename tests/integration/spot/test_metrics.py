"""Spot tests for Governance Metrics endpoints.

Concerns covered:
- GET /spoke/dg/metric — list metrics (paginated envelope); baseline definitions present
- PUT /spoke/dg/metric/{metric_id}/attr/conf — create definition (201)
- PATCH /spoke/dg/metric/{metric_id}/attr/conf — partial update
- DELETE /spoke/dg/metric/{metric_id}/attr/conf — remove definition (204)
- POST /spoke/dg/metric/{metric_id}/method/run — trigger metric run; breakdown shape; event emission
- POST .../method/run concurrent → 409 METRIC_RUNNING
- POST .../method/run dry_run=true → no persisted result, no RUN_COMPLETE event
- GET /spoke/dg/metric/{metric_id}/attr/result — result list (timeseries envelope)
- GET /spoke/dg/metric/{metric_id}/event — event list envelope

Spec:
- spec/USE_CASE_en.md §UC5 L640-L643 (baseline metric IDs)
- spec/USE_CASE_en.md §UC5 L650-L651 (concurrent run → 409 METRIC_RUNNING)
- spec/USE_CASE_en.md §UC5 L651-L653 (dry_run → no persist, no event)
- spec/feature/BACKEND.md §Metrics Service L447-L459 (dataset_filter, breakdown shape)
- spec/feature/BACKEND.md §Event Catalogue L521 (METRIC.RUN_COMPLETE + unresolved_urns)
"""

import asyncio

import pytest
import httpx

_TEST_METRIC_ID = "spot-test-freshness"

# Spec: spec/USE_CASE_en.md §UC5 L640-L643 — baseline metric IDs seeded at startup
_BASELINE_METRIC_IDS = {"ingestion-freshness", "validation-score"}

# Bounded URN used in run tests to minimise DataHub I/O
_BOUNDED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.catalog.title_master,DEV)"
)


@pytest.mark.asyncio
async def test_metric_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric returns a paginated collection envelope; registered baseline IDs appear.

    The two baseline metric IDs ('ingestion-freshness', 'validation-score') are registered
    via PUT within this test to verify that once registered they appear in the list response.

    Spec: spec/USE_CASE_en.md §UC5 L640-L643 — baseline ships with two supported metrics;
    organisations register them via PUT /spoke/dg/metric/{id}/attr/conf.
    """
    try:
        # Register the two baseline metric definitions as the spec Imazon example shows (L677-L703)
        for mid, title, theme, agg in [
            ("ingestion-freshness", "Ingestion freshness", "freshness", "ingestion-freshness"),
            ("validation-score", "Validation score", "quality", "validation-score"),
        ]:
            await api_client.put(
                f"/api/v1/spoke/dg/metric/{mid}/attr/conf",
                headers=admin_headers,
                json={
                    "title": title,
                    "description": f"Baseline {title} metric.",
                    "theme": theme,
                    "measurement_query": {"aggregation": agg, "dataset_filter": {}},
                    "schedule_tier": "hourly",
                    "is_enabled": True,
                },
            )

        resp = await api_client.get(
            "/api/v1/spoke/dg/metric?offset=0&limit=50",
            headers=admin_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "metrics" in body
        assert "offset" in body
        assert "limit" in body
        assert "total_count" in body
        assert isinstance(body["metrics"], list)

        # Spec: spec/USE_CASE_en.md §UC5 L640-L643 — both baseline definitions appear when registered
        returned_ids = {m["id"] for m in body["metrics"]}
        assert _BASELINE_METRIC_IDS.issubset(returned_ids), (
            f"Baseline metric IDs {_BASELINE_METRIC_IDS} not found in list response "
            f"(got: {returned_ids}). Spec: spec/USE_CASE_en.md §UC5 L640-L643."
        )
    finally:
        # Guarantee teardown even if assertions fail above — prevents state leakage
        # between test runs.
        for mid in _BASELINE_METRIC_IDS:
            await api_client.delete(
                f"/api/v1/spoke/dg/metric/{mid}/attr/conf",
                headers=admin_headers,
            )


@pytest.mark.asyncio
async def test_metric_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT /spoke/dg/metric/{id}/attr/conf creates or replaces a metric definition."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Test Ingestion Freshness",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "pct_fresh"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["id"] == _TEST_METRIC_ID
    assert body["theme"] == "freshness"
    assert body["is_enabled"] is False

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates a field on an existing metric definition."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    # Create first
    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Test Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "pct_fresh"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"schedule_tier": "daily", "title": "Spot Test Metric Updated"},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["schedule_tier"] == "daily"
    assert body["title"] == "Spot Test Metric Updated"

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes metric definition; subsequent GET returns 404."""
    base = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Spot Delete Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "pct_fresh"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_metric_put_with_invalid_urn_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with malformed dataset_urns entry returns 422 INVALID_DATASET_URN.

    Spec: spec/feature/BACKEND.md §Metrics Service — 'URN format is validated at
    PUT/PATCH (422 INVALID_DATASET_URN); entries that don't resolve in DataHub at
    run time are skipped and reported in the METRIC.RUN_COMPLETE event's unresolved_urns'.
    """
    _INVALID_URN_METRIC_ID = "spot-test-invalid-urn"
    base = f"/api/v1/spoke/dg/metric/{_INVALID_URN_METRIC_ID}/attr/conf"

    resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "title": "Invalid URN Test Metric",
            "description": "Tests URN validation at PUT.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {
                    "dataset_urns": ["not-a-valid-urn"]
                },
            },
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    assert resp.status_code == 422, (
        f"Expected 422 for malformed dataset_urns, got {resp.status_code}: {resp.text}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — dataset_filter.dataset_urns "
        "validated at PUT/PATCH (422 INVALID_DATASET_URN)."
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        f"Expected error_code='INVALID_DATASET_URN', got: {body.get('error_code')}. "
        "Spec: spec/feature/BACKEND.md §Metrics Service."
    )


@pytest.mark.asyncio
async def test_metric_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /spoke/dg/metric/{id}/method/run executes synchronously, persists a result row,
    and emits a METRIC.RUN_COMPLETE event with an 'unresolved_urns' key.

    The run is bounded to a single dataset via measurement_query.dataset_filter
    so the test does ~2 DataHub calls (resolve + measure) rather than enumerating
    every dataset in DataHub.

    Spec:
    - spec/feature/BACKEND.md §Metrics Service L457 — unified breakdown shape
      {"dataset_count": int, "datasets": [{urn, category, detail}]}
    - spec/feature/BACKEND.md §Event Catalogue L521 — METRIC.RUN_COMPLETE payload
      carries unresolved_urns for any dataset_filter.dataset_urns entries that
      didn't resolve in DataHub.
    """
    _RUN_METRIC_ID = "spot-test-run-metric"
    base_conf = f"/api/v1/spoke/dg/metric/{_RUN_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_RUN_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/dg/metric/{_RUN_METRIC_ID}/attr/result"
    base_events = f"/api/v1/spoke/dg/metric/{_RUN_METRIC_ID}/event"

    # Clean any state from prior sessions
    await api_client.delete(base_conf, headers=admin_headers)

    # Create metric config bounded to one URN to keep DataHub I/O small
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Run Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )

    assert run_resp.status_code == 200, run_resp.text
    run_body = run_resp.json()
    assert run_body.get("status") == "success"
    assert "run_id" in run_body

    # ── Verify persisted result row with breakdown shape ──────────────────────
    # Spec: spec/feature/BACKEND.md §Metrics Service L457 — breakdown shape
    results_resp = await api_client.get(
        f"{base_results}?offset=0&limit=5",
        headers=admin_headers,
    )
    assert results_resp.status_code == 200
    results_body = results_resp.json()
    assert len(results_body["results"]) >= 1, (
        "A non-dry-run must persist at least one result row. "
        "Spec: spec/feature/BACKEND.md §Metrics Service (run pipeline)."
    )

    result_row = results_body["results"][0]
    assert "breakdown" in result_row, (
        "Result row missing 'breakdown'. Spec: spec/feature/BACKEND.md §Metrics Service L457."
    )
    breakdown = result_row["breakdown"]
    assert isinstance(breakdown, dict), "breakdown must be a dict."
    assert "dataset_count" in breakdown, (
        "breakdown missing 'dataset_count'. Spec: spec/feature/BACKEND.md §Metrics Service L457."
    )
    assert isinstance(breakdown["dataset_count"], int), "dataset_count must be an int."
    assert "datasets" in breakdown, (
        "breakdown missing 'datasets'. Spec: spec/feature/BACKEND.md §Metrics Service L457."
    )
    assert isinstance(breakdown["datasets"], list), "breakdown.datasets must be a list."
    # Each dataset entry must have urn, category, detail
    for entry in breakdown["datasets"]:
        assert "urn" in entry, "dataset entry missing 'urn'. Spec: BACKEND.md §Metrics L457."
        assert "category" in entry, "dataset entry missing 'category'. Spec: BACKEND.md §Metrics L457."
        assert "detail" in entry, "dataset entry missing 'detail'. Spec: BACKEND.md §Metrics L457."

    # ── Verify METRIC.RUN_COMPLETE event with unresolved_urns key ────────────
    # Spec: spec/feature/BACKEND.md §Event Catalogue L521
    events_resp = await api_client.get(
        f"{base_events}?offset=0&limit=20",
        headers=admin_headers,
    )
    assert events_resp.status_code == 200
    events_body = events_resp.json()
    run_complete_events = [
        e for e in events_body["events"]
        if e.get("event_type") == "METRIC.RUN_COMPLETE"
    ]
    assert len(run_complete_events) >= 1, (
        "Expected at least one METRIC.RUN_COMPLETE event after a successful run. "
        "Spec: spec/feature/BACKEND.md §Event Catalogue L521."
    )
    # The event detail must carry the unresolved_urns key (even if [])
    event_detail = run_complete_events[0].get("detail", {})
    assert "unresolved_urns" in event_detail, (
        "METRIC.RUN_COMPLETE event detail must contain 'unresolved_urns' key. "
        "Spec: spec/feature/BACKEND.md §Event Catalogue L521."
    )
    assert isinstance(event_detail["unresolved_urns"], list), (
        "unresolved_urns must be a list. Spec: spec/feature/BACKEND.md §Event Catalogue L521."
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_run_unresolved_urns_in_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """After a run with a dataset_urns entry that doesn't resolve in DataHub, the
    METRIC.RUN_COMPLETE event detail contains that URN in 'unresolved_urns'.

    Spec: spec/feature/BACKEND.md §Metrics Service L452 — entries that don't resolve
    in DataHub at run time are skipped and reported in the METRIC.RUN_COMPLETE event's
    unresolved_urns field.
    Spec: spec/feature/BACKEND.md §Event Catalogue L521.
    """
    _UNRESOLVED_METRIC_ID = "spot-test-unresolved-urns"
    # A syntactically valid URN that does not exist in DataHub dev-env
    _GHOST_URN = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        "example_db.nonexistent.ghost_table,DEV)"
    )
    base_conf = f"/api/v1/spoke/dg/metric/{_UNRESOLVED_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_UNRESOLVED_METRIC_ID}/method/run"
    base_events = f"/api/v1/spoke/dg/metric/{_UNRESOLVED_METRIC_ID}/event"

    # Clean prior state
    await api_client.delete(base_conf, headers=admin_headers)

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Unresolved URN Metric",
            "description": "Tests unresolved_urns reporting.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {"dataset_urns": [_GHOST_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json().get("status") == "success"

    # Verify the event carries the ghost URN in unresolved_urns
    events_resp = await api_client.get(
        f"{base_events}?offset=0&limit=20",
        headers=admin_headers,
    )
    assert events_resp.status_code == 200
    run_complete_events = [
        e for e in events_resp.json()["events"]
        if e.get("event_type") == "METRIC.RUN_COMPLETE"
    ]
    assert len(run_complete_events) >= 1, (
        "Expected at least one METRIC.RUN_COMPLETE event. "
        "Spec: spec/feature/BACKEND.md §Event Catalogue L521."
    )
    event_detail = run_complete_events[0].get("detail", {})
    assert "unresolved_urns" in event_detail, (
        "METRIC.RUN_COMPLETE event detail must contain 'unresolved_urns'. "
        "Spec: spec/feature/BACKEND.md §Metrics Service L452 / §Event Catalogue L521."
    )
    assert _GHOST_URN in event_detail["unresolved_urns"], (
        f"Ghost URN '{_GHOST_URN}' must appear in unresolved_urns. "
        "Spec: spec/feature/BACKEND.md §Metrics Service L452."
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_run_concurrent_returns_409(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Two concurrent POST .../method/run calls for the same metric → second returns 409 METRIC_RUNNING.

    Spec: spec/USE_CASE_en.md §UC5 L650-L651 — 'runs are serialized per metric:
    a duplicate method/run while one is in flight returns 409 METRIC_RUNNING'.
    Spec: spec/feature/BACKEND.md §Concurrency Guards — Airflow DAG run conf-based
    dedup: 'metrics-{metric_id}'; API returns 409 Conflict with METRIC_RUNNING error code.
    """
    _CONCURRENT_METRIC_ID = "spot-test-concurrent"
    base_conf = f"/api/v1/spoke/dg/metric/{_CONCURRENT_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_CONCURRENT_METRIC_ID}/method/run"

    # Clean prior state
    await api_client.delete(base_conf, headers=admin_headers)

    # Create metric config — bounded URN keeps measurement fast/cheap
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Concurrent Metric",
            "description": "Tests concurrent run guard.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    # Fire two concurrent POST run requests
    async with httpx.AsyncClient(
        base_url=api_client.base_url, timeout=60.0
    ) as concurrent_client:
        run1, run2 = await asyncio.gather(
            concurrent_client.post(
                base_run,
                headers=admin_headers,
                json={"dry_run": False},
            ),
            concurrent_client.post(
                base_run,
                headers=admin_headers,
                json={"dry_run": False},
            ),
        )

    status_codes = {run1.status_code, run2.status_code}
    assert 200 in status_codes, (
        "At least one run must succeed (200). "
        "Spec: spec/USE_CASE_en.md §UC5 L650-L651."
    )
    assert 409 in status_codes, (
        "The second concurrent run must return 409 METRIC_RUNNING. "
        "Spec: spec/USE_CASE_en.md §UC5 L650-L651."
    )

    # Verify error code in the 409 response body
    conflict_resp = run1 if run1.status_code == 409 else run2
    conflict_body = conflict_resp.json()
    assert conflict_body.get("error_code") == "METRIC_RUNNING", (
        f"Expected error_code='METRIC_RUNNING', got: {conflict_body.get('error_code')}. "
        "Spec: spec/USE_CASE_en.md §UC5 L650-L651 / spec/feature/BACKEND.md §Concurrency Guards."
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_run_dry_run_does_not_persist(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST .../method/run with dry_run=true returns a result but does not write a result row
    or emit a METRIC.RUN_COMPLETE event.

    Spec: spec/USE_CASE_en.md §UC5 L651-L653 — 'dry_run: true evaluates the query and
    returns the would-be result without persisting to attr/result or emitting events'.
    """
    _DRY_RUN_METRIC_ID = "spot-test-dry-run"
    base_conf = f"/api/v1/spoke/dg/metric/{_DRY_RUN_METRIC_ID}/attr/conf"
    base_run = f"/api/v1/spoke/dg/metric/{_DRY_RUN_METRIC_ID}/method/run"
    base_results = f"/api/v1/spoke/dg/metric/{_DRY_RUN_METRIC_ID}/attr/result"
    base_events = f"/api/v1/spoke/dg/metric/{_DRY_RUN_METRIC_ID}/event"

    # Clean any state from prior sessions: DELETE cascades to metric_results
    # (see MetricsService.delete_metric_config). Ignore 404.
    await api_client.delete(base_conf, headers=admin_headers)

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Dry-Run Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json().get("status") == "success"

    # ── Dry-run must not persist a result row ─────────────────────────────────
    # Spec: spec/USE_CASE_en.md §UC5 L651-L653
    results_resp = await api_client.get(
        f"{base_results}?offset=0&limit=1",
        headers=admin_headers,
    )
    assert results_resp.status_code == 200
    assert results_resp.json()["results"] == [], (
        "Dry-run must not persist any result rows. "
        "Spec: spec/USE_CASE_en.md §UC5 L651-L653."
    )

    # ── Dry-run must not emit a METRIC.RUN_COMPLETE event ────────────────────
    # Spec: spec/USE_CASE_en.md §UC5 L651-L653 — 'without persisting to attr/result or emitting events'
    events_resp = await api_client.get(
        f"{base_events}?offset=0&limit=20",
        headers=admin_headers,
    )
    assert events_resp.status_code == 200
    events_body = events_resp.json()
    run_complete_events = [
        e for e in events_body["events"]
        if e.get("event_type") == "METRIC.RUN_COMPLETE"
    ]
    assert len(run_complete_events) == 0, (
        "Dry-run must not emit any METRIC.RUN_COMPLETE events. "
        "Spec: spec/USE_CASE_en.md §UC5 L651-L653."
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_result_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric/{id}/attr/result returns paginated timeseries envelope."""
    base_conf = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"
    base_results = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/result"

    # Create metric config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Result Metric",
            "theme": "freshness",
            "measurement_query": {"aggregation": "pct_fresh"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    resp = await api_client.get(base_results, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["results"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metric_event_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/metric/{id}/event returns paginated event list (may be empty)."""
    base_conf = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/attr/conf"
    base_events = f"/api/v1/spoke/dg/metric/{_TEST_METRIC_ID}/event"

    # Create metric config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "title": "Spot Event Metric",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {"aggregation": "pct_fresh"},
            "schedule_tier": "hourly",
            "is_enabled": False,
        },
    )

    resp = await api_client.get(base_events, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)
