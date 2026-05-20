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
    """UC5 Imazon Example: CDO creates, runs, and reviews DEV-scoped daily metrics.

    USE_CASE narrative shows doc-health; the test exercises all three built-in
    active metric types in parallel to cover the full loop.
    """
    # The three built-in active metric types — created DEV-scoped, daily, enabled.
    # spec: USE_CASE_en.md §UC5 §Built-in active metric types
    metrics_to_create = [
        {
            "id": "ingestion-freshness-dev",
            "type": "ingestion-freshness",
            "title": "Ingestion Freshness (DEV)",
            "description": "Daily count of datasets ingested within the configured time window across DEV",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 86400},
        },
        {
            "id": "validation-score-dev",
            "type": "validation-score",
            "title": "Validation Score (DEV)",
            "description": "Daily sum of dataset validation scores within the configured time window across DEV",
            "metrics": ["total", "validation_score_sum"],
            "metric_conf": {"time_window_sec": 86400},
        },
        {
            "id": "doc-health-dev",
            "type": "doc-health",
            "title": "Doc Health (DEV)",
            "description": "Daily documentation-completeness check across DEV datasets",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
        },
    ]

    try:
        # ── Step 1: CDO creates three DEV-scoped daily metrics ────────────────
        # UC5 Imazon Example: "The CDO adds the doc-health metric with a
        # DEV-scoped daily run." Mirrored across the three built-in active types.
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        for cfg in metrics_to_create:
            put_resp = await api_client.put(
                f"/api/v1/spoke/dg/metric/{cfg['id']}/attr/conf",
                headers=admin_headers,
                json={
                    "mode": "active",
                    "is_enabled": True,
                    "metric_type": cfg["type"],
                    "title": cfg["title"],
                    "description": cfg["description"],
                    "metrics": cfg["metrics"],
                    "metric_conf": cfg["metric_conf"],
                    "schedule_tier": "daily",
                    "dataset_filter": {"origin": "DEV"},
                },
            )
            assert put_resp.status_code in (200, 201), (
                f"PUT {cfg['id']} failed: {put_resp.status_code} {put_resp.text}"
            )

        # ── Step 2: CDO triggers an immediate first run for each metric ───────
        # UC5 Imazon Example: "The CDO triggers an immediate first run rather
        # than waiting for the schedule."
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        for cfg in metrics_to_create:
            run_resp = await api_client.post(
                f"/api/v1/spoke/dg/metric/{cfg['id']}/method/run",
                headers=admin_headers,
                json={"dry_run": False},
            )
            assert run_resp.status_code == 200, (
                f"POST method/run for {cfg['id']} failed: "
                f"{run_resp.status_code} {run_resp.text}"
            )
            assert run_resp.json().get("run_id"), (
                f"Run response for {cfg['id']} must carry a non-empty run_id. "
                "spec: USE_CASE_en.md §UC5 §API Mapping."
            )

        # ── Step 3: A week later, trends are pulled for a board update ────────
        # UC5 Imazon Example: "A week later, trends are pulled for a board update"
        # with from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z (one week span).
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        now = datetime.now(tz=UTC)
        from_ts = (now - timedelta(days=7)).isoformat()
        to_ts = (now + timedelta(days=1)).isoformat()  # +1 day padding to include the run just triggered
        for cfg in metrics_to_create:
            results_resp = await api_client.get(
                f"/api/v1/spoke/dg/metric/{cfg['id']}/attr/result",
                params={"from": from_ts, "to": to_ts},
                headers=admin_headers,
            )
            assert results_resp.status_code == 200
            results = results_resp.json().get("results", [])
            assert results, (
                f"Expected at least one result row for {cfg['id']} after a successful run. "
                "spec: USE_CASE_en.md §UC5."
            )
            assert isinstance(results[0]["values"], dict), (
                "result.values must be a dict. "
                "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
            )
            assert set(results[0]["values"].keys()) == set(cfg["metrics"]), (
                f"{cfg['id']} values keys must equal the declared metrics "
                f"{set(cfg['metrics'])}. "
                "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
            )

    finally:
        # ── Step 4: Cleanup ───────────────────────────────────────────────────
        # 204 = deleted; 404 = metric never created (PUT failed). Both are correct.
        for cfg in metrics_to_create:
            del_resp = await api_client.delete(
                f"/api/v1/spoke/dg/metric/{cfg['id']}/attr/conf",
                headers=admin_headers,
            )
            assert del_resp.status_code in (204, 404)
