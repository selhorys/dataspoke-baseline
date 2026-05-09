"""UC2 — Validation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC2` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Test in this module:
  - test_uc2_passive_result_store: DE creates conf, pipeline POSTs 3 days of results,
    DA queries historical series, cross-dataset list verifies aggregated view,
    DE deletes conf, resurrection cycle verified.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./dev_env/dataspoke-test-mode.sh --skip-build
  uv run python -m tests.integration.util --reset-all
  DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/test_uc2_validation.py

spec: USE_CASE_en.md §UC2
spec: VALIDATION.md §API Surface, §Rule Configuration, §Validation Result
"""
# spec: USE_CASE_en.md §UC2

from datetime import UTC, datetime, timedelta

import httpx
import pytest

# Dataset URN: orders.daily_fulfillment_summary is the Imazon table used in UC2.
# spec: TESTING.md §Imazon Dummy-Data Reference — UC2 primary: daily_fulfillment_summary
_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_ENC_URN = (
    _DATASET_URN
    .replace("(", "%28")
    .replace(")", "%29")
    .replace(",", "%2C")
)

_CONF_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/conf"
_RESULT_URL = f"/api/v1/spoke/common/data/{_ENC_URN}/attr/validation/result"
_VALIDATION_LIST_URL = "/api/v1/spoke/common/validation"


@pytest.mark.asyncio
async def test_uc2_passive_result_store(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'As a data engineer, I want to register a validation rule for
    orders.daily_fulfillment_summary, POST daily quality results from the pipeline,
    and later query the historical series as a 30-day baseline, so that I can detect
    anomalies in today's partition without re-scanning source tables.'

    Steps mirror USE_CASE_en.md §UC2:
      1. DE creates conf via PUT — returns 201
      2. Pipeline POSTs 3 days of results (inline payloads) — 3 × 200
      3. DA queries GET result?from=…&until=… → 3 rows; total_count=3
      4. Cross-dataset GET /validation → shows dataset with description, variable_count=3,
         latest_data_time, latest_score
      5. DELETE → 204; GET conf → 404; GET /validation?removed=true shows it;
         GET /validation?removed=false does not
      6. PUT again → 201 (resurrected); GET conf → 200 with new description
    """
    try:
        # ── Step 1: DE creates conf (PUT /attr/validation/conf) ──────────────
        # UC2 narrative: "The data engineer registers a validation slot for the table."
        # spec: VALIDATION.md §Rule Configuration — description + variables required.

        put_resp = await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={
                "description": "Daily order fulfillment quality: row count, fill rate, and anomaly score",
                "variables": ["row_cnt", "fill_rate", "anomaly_score"],
            },
        )
        assert put_resp.status_code == 201, (
            f"Step 1: PUT conf expected 201, got {put_resp.status_code}: {put_resp.text}"
        )
        conf_data = put_resp.json()
        assert conf_data["variables"] == ["row_cnt", "fill_rate", "anomaly_score"]
        assert conf_data["is_removed"] is False

        # ── Step 2: Pipeline POSTs 3 days of results ─────────────────────────
        # UC2 narrative: "Each night, the validation task runs after the partition
        # write and POSTs the day's metrics to DataSpoke."
        # spec: VALIDATION.md §Validation Result — data_time is partition timestamp.

        day_0 = datetime(2026, 5, 1, tzinfo=UTC)
        day_1 = datetime(2026, 5, 2, tzinfo=UTC)
        day_2 = datetime(2026, 5, 3, tzinfo=UTC)

        result_posts = [
            {
                "data_time": day_0.isoformat(),
                "score": 1.0,
                "variables": {"row_cnt": 1250.0, "fill_rate": 0.98, "anomaly_score": 0.02},
            },
            {
                "data_time": day_1.isoformat(),
                "score": 0.9,
                "variables": {"row_cnt": 1180.0, "fill_rate": 0.92, "anomaly_score": 0.08},
            },
            {
                "data_time": day_2.isoformat(),
                "score": 1.0,
                "variables": {"row_cnt": 1305.0, "fill_rate": 0.99, "anomaly_score": 0.01},
            },
        ]

        for payload in result_posts:
            resp = await api_client.post(
                _RESULT_URL,
                headers=admin_headers,
                json=payload,
            )
            assert resp.status_code == 200, (
                f"Step 2: POST result for {payload['data_time']} expected 200, "
                f"got {resp.status_code}: {resp.text}"
            )

        # ── Step 3: DA queries historical GET result?from=…&until=… ──────────
        # UC2 narrative: "The next day's validation task GETs the prior 30-day
        # series to compute a rolling baseline without re-scanning source tables."
        # spec: VALIDATION.md §GET result — from inclusive, until exclusive.
        # VALIDATION.md is silent on result list ordering; test asserts set equality.

        from_dt = day_0.isoformat()
        until_dt = (day_2 + timedelta(days=1)).isoformat()  # exclusive upper bound

        get_resp = await api_client.get(
            f"{_RESULT_URL}?from={from_dt}&until={until_dt}&limit=10",
            headers=admin_headers,
        )
        assert get_resp.status_code == 200, (
            f"Step 3: GET result expected 200, got {get_resp.status_code}: {get_resp.text}"
        )
        result_payload = get_resp.json()

        # 3 rows returned
        assert result_payload["total_count"] == 3, (
            f"Step 3: expected total_count=3, got {result_payload['total_count']}"
        )
        results = result_payload["results"]
        assert len(results) == 3, (
            f"Step 3: expected 3 rows in results, got {len(results)}"
        )

        # VALIDATION.md is silent on result list ordering — assert set equality only.
        # The three POSTed data_time values must all be present; ordering is impl detail.
        returned_dates = {r["data_time"][:10] for r in results}
        expected_dates = {"2026-05-01", "2026-05-02", "2026-05-03"}
        assert returned_dates == expected_dates, (
            f"Step 3: expected data_time dates {expected_dates}, got {returned_dates}"
        )

        # ── Step 4: Cross-dataset list shows the dataset ──────────────────────
        # UC2 narrative: "The data analyst checks the cross-dataset validation list
        # to see which tables have recent quality signals."
        # spec: VALIDATION.md §API Surface — GET /spoke/common/validation aggregates conf + latest result.

        list_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?limit=100",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"Step 4: GET /validation expected 200, got {list_resp.status_code}: {list_resp.text}"
        )
        items = list_resp.json()["items"]
        found = [i for i in items if i["dataset_urn"] == _DATASET_URN]
        assert len(found) == 1, (
            f"Step 4: dataset not found in cross-dataset list; items: {[i['dataset_urn'] for i in items]}"
        )
        item = found[0]

        # spec: VALIDATION.md §API Surface — each row aggregates description + variable_count + latest result
        assert item["description"] == "Daily order fulfillment quality: row count, fill rate, and anomaly score"
        assert item["variable_count"] == 3, f"Step 4: expected variable_count=3, got {item['variable_count']}"
        assert item["latest_data_time"] is not None, "Step 4: expected latest_data_time to be set"
        assert item["latest_score"] is not None, "Step 4: expected latest_score to be set"
        assert item["is_removed"] is False

        # ── Step 5: DELETE → 204; GET conf → 404; list visibility ────────────
        # UC2 narrative: "The DE retires the rule for this table."
        # spec: VALIDATION.md §Rule Configuration — DELETE performs soft delete.

        del_resp = await api_client.delete(_CONF_URL, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"Step 5: DELETE expected 204, got {del_resp.status_code}: {del_resp.text}"
        )

        # GET conf → 404 after DELETE
        get_after_del = await api_client.get(_CONF_URL, headers=admin_headers)
        assert get_after_del.status_code == 404, (
            f"Step 5: GET conf after DELETE expected 404, got {get_after_del.status_code}"
        )

        # GET /validation?removed=true → shows dataset
        list_removed_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=true&limit=100",
            headers=admin_headers,
        )
        assert list_removed_resp.status_code == 200
        removed_items = list_removed_resp.json()["items"]
        removed_urns = [i["dataset_urn"] for i in removed_items]
        assert _DATASET_URN in removed_urns, (
            f"Step 5: ?removed=true should include the dataset; got: {removed_urns}"
        )

        # GET /validation?removed=false → does NOT show dataset
        list_active_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=false&limit=100",
            headers=admin_headers,
        )
        assert list_active_resp.status_code == 200
        active_urns = [i["dataset_urn"] for i in list_active_resp.json()["items"]]
        assert _DATASET_URN not in active_urns, (
            f"Step 5: ?removed=false must NOT include deleted dataset; got: {active_urns}"
        )

        # ── Step 6: PUT-after-DELETE resurrects the assertion ─────────────────
        # UC2 narrative: "The DE reinstates the rule with updated variable names."
        # spec: VALIDATION.md §Rule Configuration — subsequent PUT resurrects; same URN reused.

        resurrect_resp = await api_client.put(
            _CONF_URL,
            headers=admin_headers,
            json={
                "description": "Reinstated quality check with extended variables",
                "variables": ["row_cnt", "fill_rate", "anomaly_score", "null_rate"],
            },
        )
        assert resurrect_resp.status_code == 201, (
            f"Step 6: PUT-after-DELETE expected 201, got {resurrect_resp.status_code}: {resurrect_resp.text}"
        )

        # Follow-up GET conf → 200
        get_after_resurrect = await api_client.get(_CONF_URL, headers=admin_headers)
        assert get_after_resurrect.status_code == 200, (
            f"Step 6: GET conf after resurrection expected 200, got {get_after_resurrect.status_code}"
        )
        resurrected = get_after_resurrect.json()
        assert resurrected["description"] == "Reinstated quality check with extended variables"
        assert resurrected["is_removed"] is False
        assert "null_rate" in resurrected["variables"]

    finally:
        # Cleanup — best effort: delete conf to restore clean state
        await api_client.delete(_CONF_URL, headers=admin_headers)
