"""UC5 — Governance: Imazon CDO story through the public REST API.

Maps spec/USE_CASE_en.md §UC5 §Imazon Example paragraphs to executable REST steps.
REST-only per spec/TESTING.md §Api-Wired Integration Tests.

User story:
  'As a governance lead or CDO, I want a small set of always-on metrics —
  ingestion freshness, validation score, and documentation health — that I
  can schedule, scope, and trend over time, so that I can monitor estate
  health without curating dashboards by hand.'
"""

# spec: USE_CASE_en.md §UC5 §Imazon Example

from datetime import UTC, datetime, timedelta

import httpx
import pytest

# Declare fixture dependencies so module_dummy_data seeds catalog schema + DataHub.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})


@pytest.mark.asyncio
async def test_uc5_governance_imazon_example(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC5 Imazon Example: CDO sets up, runs, and reviews the PROD doc-health metric.

    Steps mirror the Imazon Example paragraph in spec/USE_CASE_en.md §UC5.
    """
    # ── Step 1: Inspect factory defaults ────────────────────────────────────
    # UC5 narrative: DataSpoke seeds one metric of each built-in type on first start.
    # spec: USE_CASE_en.md §UC5 §Factory defaults
    list_resp = await api_client.get(
        "/api/v1/spoke/dg/metric?limit=100",
        headers=admin_headers,
    )
    assert list_resp.status_code == 200
    listed_ids = {m["id"] for m in list_resp.json()["metrics"]}
    for fid in ("ingestion-freshness", "validation-score", "doc-health"):
        assert fid in listed_ids, (
            f"Factory metric '{fid}' not found. "
            "spec: USE_CASE_en.md §UC5 §Factory defaults."
        )
    # Disabled-by-default invariant (is_enabled=False on factory seeds) is verified by
    # tests/integration/spot/test_metrics.py::test_factory_defaults_present_after_reset.
    # Step 2 below shows the CDO opting in explicitly via PUT (is_enabled=True).
    # spec: USE_CASE_en.md §UC5 §Factory defaults — "seeds ship disabled so the
    # governance lead opts in explicitly".

    try:
        # ── Step 2: CDO creates the PROD-scoped weekly custom variant ────────
        # UC5 Imazon Example: "The CDO replaces the daily doc-health default with
        # a PROD-scoped weekly run."
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        put_resp = await api_client.put(
            "/api/v1/spoke/dg/metric/doc-health-prod/attr/conf",
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Doc Health (PROD)",
                "description": "Weekly documentation-completeness check across PROD datasets",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "weekly",
                "dataset_filter": {"origin": "PROD"},
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT doc-health-prod failed: {put_resp.status_code} {put_resp.text}"
        )

        # ── Step 3: CDO triggers an immediate first run ───────────────────────
        # UC5 Imazon Example: "The CDO triggers an immediate first run rather
        # than waiting for the schedule."
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        run_resp = await api_client.post(
            "/api/v1/spoke/dg/metric/doc-health-prod/method/run",
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 200, (
            f"POST method/run failed: {run_resp.status_code} {run_resp.text}"
        )
        assert run_resp.json().get("run_id"), (
            "Run response must carry a non-empty run_id. "
            "spec: USE_CASE_en.md §UC5 §API Mapping."
        )

        # ── Step 4: A week later, trends are pulled for a board update ────────
        # UC5 Imazon Example: "A week later, trends are pulled for a board update"
        # with from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z (one week span).
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        now = datetime.now(tz=UTC)
        from_ts = (now - timedelta(days=7)).isoformat()
        to_ts = (now + timedelta(days=1)).isoformat()  # +1 day padding to include the run just triggered
        results_resp = await api_client.get(
            "/api/v1/spoke/dg/metric/doc-health-prod/attr/result",
            params={"from": from_ts, "to": to_ts},
            headers=admin_headers,
        )
        assert results_resp.status_code == 200
        results = results_resp.json().get("results", [])
        assert results, (
            "Expected at least one result row after a successful run. "
            "spec: USE_CASE_en.md §UC5."
        )
        assert isinstance(results[0]["values"], dict), (
            "result.values must be a dict. "
            "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
        )
        assert set(results[0]["values"].keys()) == {"total", "doc_health"}, (
            "doc-health values keys must equal the declared metrics {total, doc_health}. "
            "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
        )

    finally:
        # ── Step 5: Cleanup ───────────────────────────────────────────────────
        # 204 = deleted; 404 = metric never created (PUT failed). Both are correct.
        del_resp = await api_client.delete(
            "/api/v1/spoke/dg/metric/doc-health-prod/attr/conf",
            headers=admin_headers,
        )
        assert del_resp.status_code in (204, 404)
