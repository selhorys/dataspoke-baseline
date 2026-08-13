"""UC5 — Governance: Imazon CDO story through the public REST API.

Maps spec/USE_CASE_en.md §UC5 §Imazon Example paragraphs to executable REST steps.
REST-only per spec/TESTING.md §Api-Wired Integration Tests.

User story:
  'As a governance lead or CDO, I want a small set of always-on metrics —
  ingestion freshness, validation score, and documentation health — that I
  can schedule, scope, and trend over time, so that I can monitor estate
  health without curating dashboards by hand.'

Contract exercised here:
  - Metric creation uses POST /spoke/governance/metric with metric_id in the body (→ 201).
  - PUT /{id}/attr/conf is replace-only (→ 200 on existing, 404 METRIC_NOT_FOUND when absent).
  - metric_conf.time_window_sec is the measurement window (factory default 172800),
    applied uniformly to every dataset the metric scans — api-wired does not assert
    exact in-window counts (real-pipeline timing is nondeterministic).
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

    Steps:
      1   — CDO creates three DEV-scoped daily metrics via POST (metric_id in body) → 201
      1b  — Re-POST same metric_id (collision) → 409 METRIC_EXISTS
      1c  — PUT replace-only: existing id → 200 + change reflected; absent id → 404 METRIC_NOT_FOUND
      2   — CDO triggers immediate first run for each metric → 200 + non-empty run_id
      3   — Trends pulled over a one-week window → at least one result row per metric;
            values is a dict whose keys match the metric's declared metrics list
      4   — Cleanup: DELETE each created metric (204 or 404 both acceptable)
    """
    # The three built-in active metric types — created DEV-scoped, daily, enabled.
    # spec: USE_CASE_en.md §UC5 §Built-in active metric types
    #
    # metric_conf.time_window_sec=172800 is the measurement window (factory default).
    # spec: USE_CASE_en.md §UC5 §Built-in active metric types — "**the** measurement
    # window (positive int seconds, factory default 172800)".
    metrics_to_create = [
        {
            "metric_id": "ingestion-freshness-dev",
            "type": "ingestion-freshness",
            "title": "Ingestion Freshness (DEV)",
            "description": "Daily count of datasets ingested within the configured time window "
            "across DEV",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 172800},
        },
        {
            "metric_id": "validation-score-dev",
            "type": "validation-score",
            "title": "Validation Score (DEV)",
            "description": "Daily sum of dataset validation scores within the configured time "
            "window across DEV",
            "metrics": ["total", "validation_score_sum"],
            "metric_conf": {"time_window_sec": 172800},
        },
        {
            "metric_id": "doc-health-dev",
            "type": "doc-health",
            "title": "Doc Health (DEV)",
            "description": "Daily documentation-completeness check across DEV datasets",
            "metrics": ["total", "doc_health"],
            "metric_conf": {},
        },
    ]

    # Throwaway id used in Step 1c(b) to test absent-id 404.
    _THROWAWAY_ID = "uc5-put-absent-test"
    _throwaway_conf_url = f"/api/v1/spoke/governance/metric/{_THROWAWAY_ID}/attr/conf"

    try:
        # ── Step 1: CDO creates three DEV-scoped daily metrics ────────────────
        # UC5 Imazon Example: "The CDO adds the doc-health metric with a
        # DEV-scoped daily run, supplying the metric_id in the create body."
        # Mirrored across all three built-in active types.
        # spec: USE_CASE_en.md §UC5 §Imazon Example — POST /spoke/governance/metric,
        #       metric_id supplied in body, returns 201.
        # spec: API.md §Metric — POST /spoke/governance/metric creates; 409 METRIC_EXISTS on
        # collision.
        for cfg in metrics_to_create:
            post_resp = await api_client.post(
                "/api/v1/spoke/governance/metric",
                headers=admin_headers,
                json={
                    "metric_id": cfg["metric_id"],
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
            assert post_resp.status_code == 201, (
                f"POST /spoke/governance/metric for '{cfg['metric_id']}' expected 201, "
                f"got {post_resp.status_code}: {post_resp.text}. "
                "spec: USE_CASE_en.md §UC5 §Imazon Example."
            )

        # ── Step 1a: the list route orders by description ─────────────────────
        # `description` is one of the four sortable keys of the list route, and
        # the only non-timestamp one besides `title`, so it is the sharpest probe
        # that the server-side sort map is wired past the timestamp default.
        # spec: API.md §Metric — GET /spoke/governance/metric "sortable by
        #       created_at/updated_at/title/description".
        by_description = await api_client.get(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            params={"sort": "description_asc", "limit": 1000},
        )
        assert by_description.status_code == 200, (
            f"GET /spoke/governance/metric?sort=description_asc expected 200, "
            f"got {by_description.status_code}: {by_description.text}. "
            "spec: API.md §Metric."
        )
        created_ids = {cfg["metric_id"] for cfg in metrics_to_create}
        created_descriptions = [
            m["description"] for m in by_description.json()["metrics"] if m["id"] in created_ids
        ]
        assert created_descriptions == sorted(cfg["description"] for cfg in metrics_to_create), (
            "sort=description_asc must return the three created metrics in ascending "
            f"description order; got {created_descriptions}. spec: API.md §Metric."
        )

        # ── Step 1b: Collision rejection ──────────────────────────────────────
        # Re-POSTing with the same metric_id must return 409 METRIC_EXISTS.
        # spec: API.md §Metric — colliding id returns 409 METRIC_EXISTS.
        # spec: API.md §Error Catalogue — error envelope: top-level error_code field.
        collision_cfg = metrics_to_create[0]  # use ingestion-freshness-dev
        collision_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": collision_cfg["metric_id"],
                "mode": "active",
                "is_enabled": True,
                "metric_type": collision_cfg["type"],
                "title": collision_cfg["title"],
                "description": collision_cfg["description"],
                "metrics": collision_cfg["metrics"],
                "metric_conf": collision_cfg["metric_conf"],
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV"},
            },
        )
        assert collision_resp.status_code == 409, (
            f"Re-POST of existing metric_id '{collision_cfg['metric_id']}' expected 409, "
            f"got {collision_resp.status_code}: {collision_resp.text}. "
            "spec: API.md §Metric — colliding id returns 409 METRIC_EXISTS."
        )
        assert collision_resp.json().get("error_code") == "METRIC_EXISTS", (
            f"Expected error_code='METRIC_EXISTS' on collision; "
            f"got {collision_resp.json().get('error_code')!r}. "
            "spec: API.md §Error Catalogue."
        )

        # ── Step 1c: PUT replace-only semantics ───────────────────────────────
        # (a) PUT on an existing id → 200, change is reflected on GET.
        # spec: API.md §Metric — PUT .../attr/conf replaces existing definition, returns 200.
        replace_cfg = metrics_to_create[2]  # use doc-health-dev
        replace_url = f"/api/v1/spoke/governance/metric/{replace_cfg['metric_id']}/attr/conf"
        replace_resp = await api_client.put(
            replace_url,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": replace_cfg["type"],
                "title": replace_cfg["title"],
                "description": "Updated description for replace-only test",
                "metrics": replace_cfg["metrics"],
                "metric_conf": replace_cfg["metric_conf"],
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV"},
            },
        )
        assert replace_resp.status_code == 200, (
            f"PUT on existing '{replace_cfg['metric_id']}' expected 200, "
            f"got {replace_resp.status_code}: {replace_resp.text}. "
            "spec: API.md §Metric — PUT replaces existing definition, returns 200."
        )
        get_after_replace = await api_client.get(replace_url, headers=admin_headers)
        assert get_after_replace.status_code == 200
        assert (
            get_after_replace.json()["description"] == "Updated description for replace-only test"
        ), (
            "PUT change to 'description' must be reflected on GET. "
            "spec: API.md §Metric."
        )

        # (b) PUT on an absent id (never created) → 404 METRIC_NOT_FOUND.
        # spec: API.md §Metric — PUT returns 404 METRIC_NOT_FOUND when the id is absent
        #       (use POST /spoke/governance/metric to create).
        # Ensure throwaway does not exist before testing.
        await api_client.delete(_throwaway_conf_url, headers=admin_headers)
        absent_put_resp = await api_client.put(
            _throwaway_conf_url,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Should Fail",
                "description": "PUT on absent id must return 404",
                "metrics": ["total", "doc_health"],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": {},
            },
        )
        assert absent_put_resp.status_code == 404, (
            f"PUT on absent '{_THROWAWAY_ID}' expected 404 METRIC_NOT_FOUND, "
            f"got {absent_put_resp.status_code}: {absent_put_resp.text}. "
            "spec: API.md §Metric — PUT returns 404 METRIC_NOT_FOUND when id is absent."
        )
        assert absent_put_resp.json().get("error_code") == "METRIC_NOT_FOUND", (
            f"Expected error_code='METRIC_NOT_FOUND'; "
            f"got {absent_put_resp.json().get('error_code')!r}. "
            "spec: API.md §Error Catalogue."
        )

        # ── Step 2: CDO triggers an immediate first run for each metric ───────
        # UC5 Imazon Example: "The CDO triggers an immediate first run rather
        # than waiting for the schedule."
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        # spec: API.md §Metric — POST .../method/run returns 200 with run_id.
        for cfg in metrics_to_create:
            run_resp = await api_client.post(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/method/run",
                headers=admin_headers,
            )
            assert run_resp.status_code == 200, (
                f"POST method/run for '{cfg['metric_id']}' expected 200, "
                f"got {run_resp.status_code}: {run_resp.text}. "
                "spec: USE_CASE_en.md §UC5 §API Mapping."
            )
            assert run_resp.json().get("run_id"), (
                f"Run response for '{cfg['metric_id']}' must carry a non-empty run_id. "
                "spec: USE_CASE_en.md §UC5 §API Mapping."
            )

        # ── Step 3: A week later, trends are pulled for a board update ────────
        # UC5 Imazon Example: "A week later, trends are pulled for a board update"
        # with from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z (one week span).
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        #
        # No exact in-window count assertions — how much of the estate happens to fall
        # inside the declared window is nondeterministic against real-pipeline timing.
        # The windowing contract itself is covered by the spot suite, which seeds
        # controlled timestamps.
        # spec: TESTING.md §Spot vs Api-Wired Integration Tests.
        now = datetime.now(tz=UTC)
        from_ts = (now - timedelta(days=7)).isoformat()
        # +1 day padding to include the run just triggered
        to_ts = (now + timedelta(days=1)).isoformat()
        for cfg in metrics_to_create:
            results_resp = await api_client.get(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/attr/result",
                params={"from": from_ts, "to": to_ts},
                headers=admin_headers,
            )
            assert results_resp.status_code == 200, (
                f"GET attr/result for '{cfg['metric_id']}' expected 200, "
                f"got {results_resp.status_code}: {results_resp.text}."
            )
            results = results_resp.json().get("results", [])
            assert results, (
                f"Expected at least one result row for '{cfg['metric_id']}' after a successful "
                f"run. "
                "spec: USE_CASE_en.md §UC5."
            )
            assert isinstance(results[0]["values"], dict), (
                "result.values must be a dict. "
                "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
            )
            assert set(results[0]["values"].keys()) == set(cfg["metrics"]), (
                f"'{cfg['metric_id']}' values keys must equal the declared metrics "
                f"{set(cfg['metrics'])}. "
                "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
            )

    finally:
        # ── Step 4: Cleanup ───────────────────────────────────────────────────
        # 204 = deleted; 404 = metric was never created (POST failed) or already gone.
        # Both are acceptable for idempotent teardown.
        for cfg in metrics_to_create:
            del_resp = await api_client.delete(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/attr/conf",
                headers=admin_headers,
            )
            assert del_resp.status_code in (204, 404), (
                f"DELETE '{cfg['metric_id']}' expected 204 or 404, "
                f"got {del_resp.status_code}: {del_resp.text}."
            )
        # Also clean up throwaway id from Step 1c(b) pre-flight delete.
        await api_client.delete(_throwaway_conf_url, headers=admin_headers)
