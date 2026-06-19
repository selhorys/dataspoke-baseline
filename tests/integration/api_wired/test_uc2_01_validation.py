"""UC2 — Validation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC2` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Test in this module:
  - test_uc2_passive_result_store: caller creates conf for two datasets (a Postgres
    table and a Kafka topic), pipelines POST results, caller queries historical
    series in descending data_time order, cross-dataset list shows both datasets,
    caller deletes (freezes) the Postgres conf, then restores (undeletes) it as-is —
    the frozen variables and the preserved result history come back unchanged.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/api_wired/test_uc2_01_validation.py

spec: USE_CASE_en.md §UC2
spec: VALIDATION.md §API Surface, §Rule Configuration, §Validation Result
"""
# spec: USE_CASE_en.md §UC2

from datetime import UTC, datetime, timedelta

import httpx
import pytest

# Declare fixture dependencies so module_dummy_data seeds PG + DataHub automatically.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"orders"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})


def _enc(urn: str) -> str:
    return urn.replace("(", "%28").replace(")", "%29").replace(",", "%2C")


def _var(name: str, description: str = "") -> dict[str, str]:
    """Conf variable object: {name, description}.

    spec: VALIDATION.md §Rule Configuration — each variable is a {name, description}.
    """
    return {"name": name, "description": description}


# Dataset URNs used in UC2:
#   - daily_fulfillment_summary (Postgres table) — primary subject of the narrative.
#   - imazon.orders.events (Kafka topic) — second dataset, exercises the cross-dataset
#     list with a different platform.
# spec: TESTING.md §Imazon Dummy-Data Reference
_PG_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_KAFKA_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,"
    "example_kafka.imazon.orders.events,DEV)"
)

_PG_CONF_URL = f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/attr/validation/conf"
_PG_RESTORE_URL = (
    f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/attr/validation/conf/method/restore"
)
_PG_RESULT_URL = f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/attr/validation/result"
_KAFKA_CONF_URL = f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/conf"
_KAFKA_RESULT_URL = (
    f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/result"
)
_VALIDATION_LIST_URL = "/api/v1/spoke/validation"

# Consumed by the api-wired `purge_urns` autouse fixture (see conftest.py).
URNS_TO_PURGE: list[str] = [_PG_URN, _KAFKA_URN]

_PG_DESCRIPTION = (
    "Daily order fulfillment quality: row count, fill rate, and anomaly score"
)


@pytest.mark.asyncio
async def test_uc2_passive_result_store(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'As a data team member, I want to register validation rules for
    both a fulfillment summary table and an upstream events topic, POST daily quality
    results from each pipeline, and later query the historical series as a baseline,
    so that I can detect anomalies in today's partition without re-scanning sources.'

    Steps mirror USE_CASE_en.md §UC2:
      1. PUT validation conf for postgres + kafka datasets — 2 × 201
      2. Pipelines POST results: 3 days for postgres, 2 days for kafka — 5 × 201
      3. GET postgres result?from=…&until=… → 3 rows, descending by data_time
      4. Cross-dataset GET /validation → shows BOTH datasets with their descriptions,
         variable counts, latest_data_time, latest_score
      5. DELETE postgres conf → 204 (freeze); GET conf → 404 VALIDATION_CONF_REMOVED;
         PATCH on tombstone → 404 VALIDATION_CONF_REMOVED; PUT on tombstone → 409
         VALIDATION_CONF_REMOVED (PUT does not resurrect); ?removed=true includes
         postgres, ?removed=false includes kafka but not postgres
      6. POST conf/method/restore → 200 reinstating the SAME frozen variables (no
         new variable set); the prior result series is still queryable and unchanged;
         editing the now-active rule via PUT/PATCH works
    """
    try:
        # ── Step 1: Caller creates confs for both datasets ───────────────────
        # UC2 narrative: "The caller registers validation slots for the
        # fulfillment table and the upstream order-events topic."
        # spec: VALIDATION.md §Rule Configuration — description + variables required.

        pg_variables = [
            _var("row_cnt", "Daily fulfillment row count"),
            _var("fill_rate", "Fraction of orders fully shipped"),
            _var("anomaly_score", "Detector score for the day"),
        ]
        put_pg_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": _PG_DESCRIPTION, "variables": pg_variables},
        )
        assert put_pg_resp.status_code == 201, (
            f"Step 1: PUT postgres conf expected 201, "
            f"got {put_pg_resp.status_code}: {put_pg_resp.text}"
        )
        # spec: VALIDATION.md §Rule Configuration — variables round-trip as objects.
        assert put_pg_resp.json()["variables"] == pg_variables

        kafka_variables = [
            _var("msg_cnt", "Messages produced in the window"),
            _var("lag_seconds", "Consumer lag in seconds"),
        ]
        put_kafka_resp = await api_client.put(
            _KAFKA_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Order events stream quality: message count and lag",
                "variables": kafka_variables,
            },
        )
        assert put_kafka_resp.status_code == 201, (
            f"Step 1: PUT kafka conf expected 201, "
            f"got {put_kafka_resp.status_code}: {put_kafka_resp.text}"
        )
        assert put_kafka_resp.json()["variables"] == kafka_variables

        # ── Step 2: Pipelines POST results for both datasets ─────────────────
        # UC2 narrative: "Each night, the validation task runs after the partition
        # write and POSTs the day's metrics to DataSpoke."
        # spec: VALIDATION.md §Validation Result — data_time is partition timestamp.
        # spec: API.md §HTTP Status Codes — POST that creates a resource returns 201.

        day_0 = datetime(2026, 5, 1, tzinfo=UTC)
        day_1 = datetime(2026, 5, 2, tzinfo=UTC)
        day_2 = datetime(2026, 5, 3, tzinfo=UTC)

        # Postgres: 3 days
        for payload in [
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
        ]:
            resp = await api_client.post(_PG_RESULT_URL, headers=admin_headers, json=payload)
            assert resp.status_code == 201, (
                f"Step 2: POST postgres result for {payload['data_time']} expected 201, "
                f"got {resp.status_code}: {resp.text}"
            )

        # Kafka: 2 days
        for payload in [
            {
                "data_time": day_0.isoformat(),
                "score": 1.0,
                "variables": {"msg_cnt": 48000.0, "lag_seconds": 1.2},
            },
            {
                "data_time": day_1.isoformat(),
                "score": 0.85,
                "variables": {"msg_cnt": 47220.0, "lag_seconds": 5.4},
            },
        ]:
            resp = await api_client.post(_KAFKA_RESULT_URL, headers=admin_headers, json=payload)
            assert resp.status_code == 201, (
                f"Step 2: POST kafka result for {payload['data_time']} expected 201, "
                f"got {resp.status_code}: {resp.text}"
            )

        # ── Step 3: GET postgres result?from=…&until=… (historical range) ────
        # UC2 narrative: "The next day's validation task GETs the prior 30-day
        # series to compute a rolling baseline without re-scanning source tables."
        # spec: VALIDATION.md §GET result — from inclusive, until exclusive;
        # rows ordered by data_time descending (newest first).

        from_dt = day_0.isoformat()
        until_dt = (day_2 + timedelta(days=1)).isoformat()  # exclusive upper bound

        # Pass as `params=` so httpx URL-encodes the timezone `+` to `%2B`.
        # An inline f-string in the URL would let the server decode `+` as a space.
        get_resp = await api_client.get(
            _PG_RESULT_URL,
            params={"from": from_dt, "until": until_dt, "limit": 10},
            headers=admin_headers,
        )
        assert get_resp.status_code == 200, (
            f"Step 3: GET result expected 200, got {get_resp.status_code}: {get_resp.text}"
        )
        result_payload = get_resp.json()

        assert result_payload["total_count"] == 3, (
            f"Step 3: expected total_count=3, got {result_payload['total_count']}"
        )
        results = result_payload["results"]
        assert len(results) == 3, (
            f"Step 3: expected 3 rows in results, got {len(results)}"
        )

        # spec: VALIDATION.md §GET result — descending data_time order.
        returned_dates = [r["data_time"][:10] for r in results]
        expected_dates = ["2026-05-03", "2026-05-02", "2026-05-01"]
        assert returned_dates == expected_dates, (
            f"Step 3: expected descending order {expected_dates}, got {returned_dates}"
        )

        # ── Step 4: Cross-dataset list shows BOTH datasets ───────────────────
        # UC2 narrative: "The caller checks the cross-dataset validation list
        # to see which datasets have recent quality signals."
        # spec: VALIDATION.md §API Surface — GET /spoke/validation aggregates
        # conf + latest result.

        list_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?limit=100",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"Step 4: GET /validation expected 200, "
            f"got {list_resp.status_code}: {list_resp.text}"
        )
        items = list_resp.json()["validations"]
        by_urn = {i["dataset_urn"]: i for i in items}
        assert _PG_URN in by_urn, (
            f"Step 4: postgres dataset not found in cross-dataset list; "
            f"got: {list(by_urn)}"
        )
        assert _KAFKA_URN in by_urn, (
            f"Step 4: kafka dataset not found in cross-dataset list; "
            f"got: {list(by_urn)}"
        )

        # spec: VALIDATION.md §API Surface — each row aggregates:
        # description + variable_count + latest result.
        pg_item = by_urn[_PG_URN]
        assert pg_item["description"] == _PG_DESCRIPTION
        assert pg_item["variable_count"] == 3, (
            f"Step 4: postgres expected variable_count=3, got {pg_item['variable_count']}"
        )
        assert pg_item["latest_data_time"] is not None
        assert pg_item["latest_score"] is not None
        assert pg_item["is_removed"] is False

        kafka_item = by_urn[_KAFKA_URN]
        assert kafka_item["description"] == "Order events stream quality: message count and lag"
        assert kafka_item["variable_count"] == 2, (
            f"Step 4: kafka expected variable_count=2, got {kafka_item['variable_count']}"
        )
        assert kafka_item["latest_data_time"] is not None
        assert kafka_item["latest_score"] is not None
        assert kafka_item["is_removed"] is False

        # ── Step 5: DELETE postgres → 204 (freeze); GET conf → 404; list visibility ─
        # UC2 narrative: "The DE retires the rule for the fulfillment table."
        # spec: VALIDATION.md §Rule Configuration — DELETE performs a soft delete
        # (freeze): the conf and the entire result history are preserved untouched.

        del_resp = await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"Step 5: DELETE expected 204, got {del_resp.status_code}: {del_resp.text}"
        )

        # spec: VALIDATION.md §Rule Configuration — after DELETE, GET conf returns 404
        # with error_code VALIDATION_CONF_REMOVED (a *restorable* tombstone, distinct
        # from CONFIG_NOT_FOUND for a never-created slot).
        get_after_del = await api_client.get(_PG_CONF_URL, headers=admin_headers)
        assert get_after_del.status_code == 404, (
            f"Step 5: GET conf after DELETE expected 404, got {get_after_del.status_code}"
        )
        assert get_after_del.json().get("error_code") == "VALIDATION_CONF_REMOVED", (
            f"Step 5: GET on frozen slot must carry error_code VALIDATION_CONF_REMOVED; "
            f"got: {get_after_del.json()}"
        )

        # ?removed=true → includes postgres (kafka is still active so not required here)
        list_removed_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=true&limit=100",
            headers=admin_headers,
        )
        assert list_removed_resp.status_code == 200
        removed_urns = [i["dataset_urn"] for i in list_removed_resp.json()["validations"]]
        assert _PG_URN in removed_urns, (
            f"Step 5: ?removed=true should include postgres dataset; got: {removed_urns}"
        )

        # ?removed=false → kafka present, postgres absent
        list_active_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=false&limit=100",
            headers=admin_headers,
        )
        assert list_active_resp.status_code == 200
        active_urns = [i["dataset_urn"] for i in list_active_resp.json()["validations"]]
        assert _PG_URN not in active_urns, (
            f"Step 5: ?removed=false must NOT include deleted postgres dataset; "
            f"got: {active_urns}"
        )
        assert _KAFKA_URN in active_urns, (
            f"Step 5: ?removed=false should still include active kafka dataset; "
            f"got: {active_urns}"
        )

        # ── Step 5.5: PATCH and PUT on the frozen slot are rejected ───────────────
        # spec: VALIDATION.md §Rule Configuration — PATCH on a tombstoned slot returns
        # 404 VALIDATION_CONF_REMOVED; PUT does NOT resurrect — it is rejected with
        # 409 VALIDATION_CONF_REMOVED. The rule must be restored first.
        patch_deleted_resp = await api_client.patch(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": "should not apply to soft-deleted slot"},
        )
        assert patch_deleted_resp.status_code == 404, (
            f"Step 5.5: PATCH on frozen conf expected 404, "
            f"got {patch_deleted_resp.status_code}: {patch_deleted_resp.text}"
        )
        assert patch_deleted_resp.json().get("error_code") == "VALIDATION_CONF_REMOVED", (
            f"Step 5.5: PATCH on frozen slot must carry VALIDATION_CONF_REMOVED; "
            f"got: {patch_deleted_resp.json()}"
        )

        # PUT on the tombstone → 409 VALIDATION_CONF_REMOVED (PUT does not resurrect).
        put_deleted_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={
                "description": "PUT must not resurrect a frozen rule",
                "variables": [_var("null_rate", "Null rate of key columns")],
            },
        )
        assert put_deleted_resp.status_code == 409, (
            f"Step 5.5: PUT on frozen conf expected 409 (PUT does not resurrect), "
            f"got {put_deleted_resp.status_code}: {put_deleted_resp.text}"
        )
        assert put_deleted_resp.json().get("error_code") == "VALIDATION_CONF_REMOVED", (
            f"Step 5.5: PUT on frozen slot must carry VALIDATION_CONF_REMOVED; "
            f"got: {put_deleted_resp.json()}"
        )

        # ── Step 6: Restore (undelete) reinstates the FROZEN rule unchanged ──
        # UC2 narrative: "The DE restores the retired rule; it comes back exactly as
        # it was, with its result history intact, and is edited afterward."
        # spec: VALIDATION.md §Rule Configuration — restore via method/restore returns
        # 200 and reinstates the frozen description/variables exactly as they were —
        # no redefinition on restore.

        restore_resp = await api_client.post(_PG_RESTORE_URL, headers=admin_headers)
        assert restore_resp.status_code == 200, (
            f"Step 6: POST conf/method/restore expected 200, "
            f"got {restore_resp.status_code}: {restore_resp.text}"
        )
        restored = restore_resp.json()
        # The frozen description + variables are reinstated verbatim — NOT a new
        # variable set. The original pg_variables (no null_rate) come back.
        assert restored["description"] == _PG_DESCRIPTION, (
            "Step 6: restore must reinstate the frozen description; "
            f"got {restored['description']!r}"
        )
        assert restored["variables"] == pg_variables, (
            f"Step 6: restore must reinstate the SAME frozen variables (no redefinition); "
            f"got {restored['variables']}"
        )
        restored_names = [v["name"] for v in restored["variables"]]
        assert "null_rate" not in restored_names, (
            "Step 6: restore must NOT introduce a new variable (e.g. null_rate); "
            "the frozen variable set is reinstated as-is"
        )

        # GET conf is active again (200) and matches the restored rule.
        get_after_restore = await api_client.get(_PG_CONF_URL, headers=admin_headers)
        assert get_after_restore.status_code == 200, (
            "Step 6: GET conf after restore expected 200, "
            f"got {get_after_restore.status_code}: {get_after_restore.text}"
        )
        assert get_after_restore.json()["variables"] == pg_variables

        # The preserved result series is still queryable and unchanged after restore —
        # the 3 original postgres rows remain consistent with the restored variables.
        # spec: VALIDATION.md §Rule Configuration — validation_results survive the
        # freeze/restore cycle and stay consistent with the restored variable set.
        get_results_after_restore = await api_client.get(
            _PG_RESULT_URL,
            params={"from": from_dt, "until": until_dt, "limit": 10},
            headers=admin_headers,
        )
        assert get_results_after_restore.status_code == 200, (
            f"Step 6: GET result after restore expected 200, "
            f"got {get_results_after_restore.status_code}: {get_results_after_restore.text}"
        )
        restored_results = get_results_after_restore.json()
        assert restored_results["total_count"] == 3, (
            f"Step 6: result history must survive the freeze/restore cycle (expected 3 rows), "
            f"got total_count={restored_results['total_count']}"
        )
        restored_dates = [r["data_time"][:10] for r in restored_results["results"]]
        assert restored_dates == ["2026-05-03", "2026-05-02", "2026-05-01"], (
            f"Step 6: restored result series must be unchanged; got {restored_dates}"
        )

        # ── Step 6.5: Edit the now-active rule (restore then edit) ───────────
        # spec: VALIDATION.md §Rule Configuration — "To redefine a rule after
        # restoring, edit the now-active slot with the normal PUT/PATCH."
        # PUT replaces the active rule (200).
        edit_variables = [
            _var("row_cnt", "Daily fulfillment row count"),
            _var("fill_rate", "Fraction of orders fully shipped"),
            _var("anomaly_score", "Detector score for the day"),
            _var("null_rate", "Null rate of key columns"),
        ]
        edit_put_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Reinstated quality check with extended variables",
                "variables": edit_variables,
            },
        )
        assert edit_put_resp.status_code == 200, (
            f"Step 6.5: PUT on the restored (active) slot replaces it → expected 200, "
            f"got {edit_put_resp.status_code}: {edit_put_resp.text}"
        )
        assert edit_put_resp.json()["variables"] == edit_variables

        # PATCH on the active slot adjusts the description only.
        edit_patch_resp = await api_client.patch(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": "Patched after restore"},
        )
        assert edit_patch_resp.status_code == 200, (
            f"Step 6.5: PATCH on the active slot → expected 200, "
            f"got {edit_patch_resp.status_code}: {edit_patch_resp.text}"
        )
        assert edit_patch_resp.json()["description"] == "Patched after restore"
        # The variable set from the prior PUT is unchanged by a description-only PATCH.
        assert [v["name"] for v in edit_patch_resp.json()["variables"]] == [
            "row_cnt",
            "fill_rate",
            "anomaly_score",
            "null_rate",
        ]

    finally:
        # Cleanup — best effort: delete both confs to restore clean state
        await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        await api_client.delete(_KAFKA_CONF_URL, headers=admin_headers)
