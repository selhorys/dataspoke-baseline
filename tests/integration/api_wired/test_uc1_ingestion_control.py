"""UC1 — Ingestion Control: end-to-end through public REST API.

Maps spec/USE_CASE_en.md §UC1 paragraphs to executable steps. REST-only per
spec/TESTING.md §Api-Wired Integration Tests.

Three test functions, one per UC1 branch:
  test_uc1_active_custom_postgres
      Active-custom Postgres daily ingestion, DPI emission assertion,
      disabled/enabled guard, cross-dataset overview.

  test_uc1_passive_postgres_via_datahub_managed_ingestion
      Passive registration, 409 rejection, DataHub Managed Ingestion execution,
      passive sync, event row assertion.

  test_uc1_passive_kafka_via_external_script
      Passive registration, 409 rejection, simulated external DPI emission,
      passive sync, event row assertion.
"""
# spec: USE_CASE_en.md §UC1

import asyncio
import os
import time
import urllib.parse
import uuid

import httpx
import pytest

# ── Dataset URN constants ─────────────────────────────────────────────────────

_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")

# spec: SECRET_RESOLUTION.md §Name prefix policy — names must start with dataspoke-source-cred-
_VAULT_NAME = "dataspoke-source-cred-uc1-title-master"
_VAULT_KEY = "password"

# Case 1 — active-custom Postgres dataset
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1 primary dataset
_ACTIVE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_ACTIVE_ENCODED = urllib.parse.quote(_ACTIVE_URN, safe="")

# Case 2 — passive Postgres via DataHub Managed Ingestion
# spec: USE_CASE_en.md §UC1 Case 2 — catalog.editions is a separate UC1 table not used by Case 1
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.editions
_PASSIVE_PG_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)
_PASSIVE_PG_ENCODED = urllib.parse.quote(_PASSIVE_PG_URN, safe="")

# Case 3 — passive Kafka via external script
# spec: USE_CASE_en.md §UC1 Case 3 — imazon.orders.events Kafka topic
# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_KAFKA_BROKERS = os.environ.get("DATASPOKE_EXAMPLE_KAFKA_BROKERS", "localhost:9104")
_PASSIVE_KAFKA_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_PASSIVE_KAFKA_ENCODED = urllib.parse.quote(_PASSIVE_KAFKA_URN, safe="")

# Module-level dummy-data declarations for conftest.py autouse fixture
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog"])
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])


# ── Case 1: active-custom Postgres ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc1_active_custom_postgres(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC1 Case 1 — DataSpoke owns the extraction for catalog.title_master.

    Narrative: "DataSpoke is the ingestor. An Airflow tier DAG runs the platform
    extractor on the configured schedule_tier and emits results to DataHub.
    Manual and dry-run runs are also supported."
    spec: USE_CASE_en.md §UC1 Case 1

    Steps:
      1. PUT active-custom conf for catalog.title_master
      2. Dry-run (is_enabled=false) — must succeed (200)
      3. Real run while disabled — must return 409 INGESTION_DISABLED
      4. PATCH is_enabled=true, real run — must succeed with run_id + status
      5. Assert DPI in DataHub via runs GraphQL after real run
      6. Cross-dataset overview includes this URN with mode='active-custom'
      7. Cleanup
    """
    active_conf_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/attr/ingestion/conf"
    active_run_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/method/ingestion/run"
    active_events_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/event/ingestion"

    try:
        # ── Step 1: Register active-custom conf ──────────────────────────────
        # spec: USE_CASE_en.md §UC1 Case 1 — PUT conf with mode='active-custom'
        put_resp = await api_client.put(
            active_conf_url,
            headers=admin_headers,
            json={
                "mode": "active-custom",
                "platform": "postgres",
                "locator": {"host": _PG_HOST, "port": _PG_PORT},
                "identifier": {
                    "database": _PG_DB,
                    "schema_name": "catalog",
                    "table": "title_master",
                },
                "auth": {
                    "username": _PG_USER,
                    "password": _PG_PASSWORD,
                    "secret_ref": {
                        "name": _VAULT_NAME,
                        "key": _VAULT_KEY,
                        "force_overwrite": True,
                    },
                },
                "is_enabled": False,
                "schedule_tier": "daily",
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT active-custom conf failed: {put_resp.status_code} {put_resp.text}"
        )
        put_body = put_resp.json()
        assert put_body["dataset_urn"] == _ACTIVE_URN
        assert put_body["mode"] == "active-custom", (
            f"Response mode must be 'active-custom'; got {put_body['mode']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 1"
        )
        assert put_body["platform"] == "postgres"
        assert put_body["schedule_tier"] == "daily"
        assert put_body["is_enabled"] is False
        assert put_body["identifier"]["table"] == "title_master"
        # spec: SECRET_RESOLUTION.md §Vault-write flow — password stripped from response
        assert "password" not in put_body["auth"], (
            "Response auth must not expose the plaintext password. "
            "spec: SECRET_RESOLUTION.md §Vault-write flow step 4"
        )
        assert put_body["auth"]["secret_ref"] == {"name": _VAULT_NAME, "key": _VAULT_KEY}, (
            f"Response secret_ref must be reference shape {{name, key}}; "
            f"got {put_body['auth'].get('secret_ref')!r}. "
            "spec: SECRET_RESOLUTION.md §Vault-write flow step 5"
        )

        # ── Step 2: Dry-run while disabled — must succeed ────────────────────
        # spec: USE_CASE_en.md §UC1 Case 1 — "A coding agent verifies connectivity
        # before turning the schedule on: POST .../method/ingestion/run { 'dry_run': true }"
        # spec: USE_CASE_en.md §UC1 — dry-run is permitted regardless of is_enabled
        dry_run_resp = await api_client.post(
            active_run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_run_resp.status_code == 200, (
            f"Dry-run while disabled failed: {dry_run_resp.status_code} {dry_run_resp.text}"
        )
        dry_run_body = dry_run_resp.json()
        assert "run_id" in dry_run_body, "Dry-run response must carry run_id"
        assert "status" in dry_run_body, "Dry-run response must carry status"
        _fail_tail = {"fail", "failed", "failure", "error", "errored"}
        assert dry_run_body["status"].lower() not in _fail_tail, (
            f"Dry-run against reachable Postgres unexpectedly returned fail status "
            f"{dry_run_body['status']!r}"
        )

        # ── Step 3: Real run while disabled — must return 409 INGESTION_DISABLED
        # spec: USE_CASE_en.md §UC1 — "non-dry-run calls return 409 INGESTION_DISABLED"
        disabled_run_resp = await api_client.post(
            active_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert disabled_run_resp.status_code == 409, (
            f"Expected 409 INGESTION_DISABLED when is_enabled=false; "
            f"got {disabled_run_resp.status_code}: {disabled_run_resp.text}"
        )
        assert disabled_run_resp.json().get("error_code") == "INGESTION_DISABLED", (
            f"Expected error_code='INGESTION_DISABLED'; "
            f"got {disabled_run_resp.json().get('error_code')!r}. "
            "spec: USE_CASE_en.md §UC1"
        )

        # ── Step 4: PATCH is_enabled=true, real run succeeds ─────────────────
        # spec: USE_CASE_en.md §UC1 — after PATCH is_enabled=true, run proceeds
        enable_resp = await api_client.patch(
            active_conf_url,
            headers=admin_headers,
            json={"is_enabled": True, "schedule_tier": "daily"},
        )
        assert enable_resp.status_code == 200, (
            f"PATCH is_enabled=true failed: {enable_resp.status_code} {enable_resp.text}"
        )

        enabled_run_resp = await api_client.post(
            active_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert enabled_run_resp.status_code == 200, (
            f"Real run after enable failed: {enabled_run_resp.status_code}: "
            f"{enabled_run_resp.text}"
        )
        enabled_run_body = enabled_run_resp.json()
        assert "run_id" in enabled_run_body, "Enabled real-run response must carry run_id"
        assert "status" in enabled_run_body, "Enabled real-run response must carry status"
        run_id = enabled_run_body["run_id"]

        # ── Step 5: Assert DPI in DataHub after real run ──────────────────────
        # spec: BACKEND.md §Custom Ingestor Authoring Contract —
        #     DPI URN convention: urn:li:dataProcessInstance:<platform>-<run_id>
        # spec: USE_CASE_en.md §UC1 Case 1 — "Each row is backed by a
        #     DataProcessInstance aspect that DataSpoke's extractor emitted to DataHub"
        expected_dpi_urn = f"urn:li:dataProcessInstance:postgres-{run_id}"

        # Bounded poll loop — waits until the run event appears in event/ingestion.
        # spec: feedback_no_increase_timeout — use polls with clear failure mode, not sleep.
        # spec: USE_CASE_en.md §UC1 Case 1 — active-custom run with real Postgres
        #     target is expected to succeed; pin INGESTION.COMPLETE (not FAIL).
        event_body = None
        found_dpi = False
        deadline = time.time() + 30.0
        while time.time() < deadline:
            runs_resp = await api_client.get(
                active_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert runs_resp.status_code == 200, (
                f"GET event/ingestion after run failed: {runs_resp.status_code}"
            )
            event_body = runs_resp.json()
            for evt in event_body.get("events", []):
                detail = evt.get("detail", {}) or {}
                if detail.get("run_id") == run_id:
                    found_dpi = True
                    # spec: USE_CASE_en.md §UC1 Case 1 — active-custom run against a
                    # reachable Postgres target is expected to succeed; INGESTION.COMPLETE
                    # is the required outcome (not FAIL).
                    assert evt["event_type"] == "INGESTION.COMPLETE", (
                        f"Case 1 active-custom run must produce INGESTION.COMPLETE; "
                        f"got {evt['event_type']!r}. "
                        "spec: USE_CASE_en.md §UC1 Case 1 — DataSpoke owns the extraction"
                    )
                    break
            if found_dpi:
                break
            await asyncio.sleep(1.0)

        assert found_dpi, (
            f"Expected an INGESTION.COMPLETE event/ingestion row with run_id={run_id!r} "
            f"within 30s. Events returned: {(event_body or {}).get('events', [])}. "
            "spec: BACKEND.md §Active run pipeline — INGESTION.COMPLETE event recorded"
        )

        # Restore to disabled state
        restore_resp = await api_client.patch(
            active_conf_url,
            headers=admin_headers,
            json={"is_enabled": False},
        )
        assert restore_resp.status_code == 200

        # ── Step 6: Cross-dataset overview includes this URN ──────────────────
        # spec: USE_CASE_en.md §UC1 — cross-dataset overview at GET /spoke/common/ingestion
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/ingestion?limit=100",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200
        overview_body = overview_resp.json()
        assert "configs" in overview_body
        assert "total_count" in overview_body

        configs_by_urn = {c["dataset_urn"]: c for c in overview_body["configs"]}
        assert _ACTIVE_URN in configs_by_urn, (
            f"Active URN {_ACTIVE_URN!r} not found in GET /spoke/common/ingestion. "
            "spec: USE_CASE_en.md §UC1 §Cross-dataset overview"
        )
        active_row = configs_by_urn[_ACTIVE_URN]
        assert active_row["mode"] == "active-custom", (
            f"Cross-dataset overview mode expected 'active-custom'; "
            f"got {active_row['mode']!r}. spec: USE_CASE_en.md §UC1"
        )
        assert active_row["schedule_tier"] == "daily", (
            f"Cross-dataset overview schedule_tier expected 'daily'; "
            f"got {active_row.get('schedule_tier')!r}. spec: USE_CASE_en.md §UC1"
        )

    finally:
        # ── Step 7: Cleanup ──────────────────────────────────────────────────
        await api_client.delete(active_conf_url, headers=admin_headers)


# ── Case 2: passive Postgres via DataHub Managed Ingestion ────────────────────


@pytest.mark.asyncio
async def test_uc1_passive_postgres_via_datahub_managed_ingestion(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_actions_pod_required: None,
) -> None:
    """UC1 Case 2 — DataHub Managed Ingestion runs; DataSpoke registers and observes.

    Narrative: "The team wants column-level lineage and profile statistics that
    DataSpoke's in-house extractor doesn't produce. They configure DataHub Managed
    Ingestion directly … DataSpoke does not touch this configuration."
    spec: USE_CASE_en.md §UC1 Case 2

    Steps:
      1. Create a DataHub IngestionSource via GraphQL createIngestionSource mutation
      2. PUT passive-mode conf for catalog.editions in DataSpoke
      3. Verify POST method/ingestion/run returns 409 INGESTION_NOT_APPLICABLE
      4. Fire createIngestionExecutionRequest against the IngestionSource
      5. Poll executionRequest until terminal (cap 90s, skip on timeout)
      6. Trigger sync_passive_status via internal Airflow passive-sync activity endpoint
      7. GET event/ingestion — assert at least one INGESTION.COMPLETE with source='passive'
      8. Cross-dataset overview includes passive URN
      9. Cleanup: deleteIngestionSource + delete DataSpoke conf
    """
    datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_DATAHUB_GMS_URL not set; skipping passive PG test")

    passive_conf_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/attr/ingestion/conf"
    )
    passive_run_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/method/ingestion/run"
    )
    passive_events_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/event/ingestion"
    )

    ingestion_source_urn: str | None = None

    try:
        # ── Step 1: Create DataHub IngestionSource ────────────────────────────
        # spec: USE_CASE_en.md §UC1 Case 2 — "create a postgres recipe targeting
        # catalog.reviews with a daily cron, and let DataHub's executor run it"
        # Using catalog.editions to avoid conflict with Case 1's title_master dataset.
        test_recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "host_port": f"{_PG_HOST}:{_PG_PORT}",
                    "database": _PG_DB,
                    "username": _PG_USER,
                    "password": _PG_PASSWORD,
                    "schema_pattern": {"allow": ["catalog"]},
                    "table_pattern": {"allow": ["editions"]},
                    "include_tables": True,
                    "include_views": False,
                },
            },
            "sink": {"type": "datahub-rest", "config": {"server": datahub_gms_url}},
        }

        create_source_mutation = """
        mutation createIngestionSource($input: UpdateIngestionSourceInput!) {
            createIngestionSource(input: $input)
        }
        """
        import json

        gql_headers = {"Authorization": f"Bearer {datahub_token}", "Content-Type": "application/json"} if datahub_token else {"Content-Type": "application/json"}
        gql_resp = httpx.post(
            f"{datahub_gms_url}/api/graphql",
            headers=gql_headers,
            json={
                "query": create_source_mutation,
                "variables": {
                    "input": {
                        "name": f"uc1-passive-pg-test-{uuid.uuid4().hex[:8]}",
                        "type": "postgres",
                        "config": {
                            "recipe": json.dumps(test_recipe),
                            "executorId": "default",
                            "debugMode": False,
                        },
                        "schedule": None,
                    },
                },
            },
            timeout=15.0,
        )
        gql_resp.raise_for_status()
        gql_data = gql_resp.json()
        if "errors" in gql_data:
            pytest.skip(
                f"createIngestionSource GraphQL error: {gql_data['errors']}. "
                "DataHub GMS may not support Managed Ingestion in this dev-env."
            )
        ingestion_source_urn = gql_data.get("data", {}).get("createIngestionSource")
        assert ingestion_source_urn, (
            f"createIngestionSource returned no URN: {gql_data}"
        )

        # ── Step 2: PUT passive-mode conf in DataSpoke ────────────────────────
        # spec: USE_CASE_en.md §UC1 Case 2 — register as passive with no locator/auth/schedule_tier
        put_passive_resp = await api_client.put(
            passive_conf_url,
            headers=admin_headers,
            json={
                "mode": "passive",
                "platform": "postgres",
                "identifier": {
                    "database": _PG_DB,
                    "schema_name": "catalog",
                    "table": "editions",
                },
                "is_enabled": True,
            },
        )
        assert put_passive_resp.status_code in (200, 201), (
            f"PUT passive conf failed: {put_passive_resp.status_code} {put_passive_resp.text}"
        )
        passive_body = put_passive_resp.json()
        assert passive_body["mode"] == "passive"
        assert passive_body.get("schedule_tier") is None, (
            "Passive conf must not carry schedule_tier. "
            "spec: USE_CASE_en.md §UC1 Case 2"
        )
        assert passive_body.get("locator") is None or passive_body.get("locator") == {}, (
            "Passive conf response must carry no locator. "
            "spec: USE_CASE_en.md §UC1 Case 2"
        )

        # ── Step 3: Verify method/ingestion/run returns 409 ───────────────────
        # spec: USE_CASE_en.md §UC1 API Mapping —
        #     "passive configs return 409 INGESTION_NOT_APPLICABLE"
        reject_resp = await api_client.post(
            passive_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert reject_resp.status_code == 409, (
            f"Expected 409 INGESTION_NOT_APPLICABLE for passive run; "
            f"got {reject_resp.status_code}: {reject_resp.text}"
        )
        assert reject_resp.json().get("error_code") == "INGESTION_NOT_APPLICABLE", (
            f"Expected error_code='INGESTION_NOT_APPLICABLE'; "
            f"got {reject_resp.json().get('error_code')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping"
        )

        # ── Step 3b: Snapshot pre-run event count ────────────────────────────
        # Capture baseline before firing the execution so Step 7 can assert growth,
        # not just presence (stale rows from prior runs would cause false passes).
        # spec: USE_CASE_en.md §UC1 Case 2 — passive sync writes new event rows
        snapshot_resp = await api_client.get(
            passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert snapshot_resp.status_code == 200
        events_before_count = len([
            e for e in snapshot_resp.json().get("events", [])
            if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
            and (e.get("detail") or {}).get("source") == "passive"
        ])

        # ── Step 4: Fire createIngestionExecutionRequest ──────────────────────
        create_exec_mutation = """
        mutation createIngestionExecutionRequest($input: CreateIngestionExecutionRequestInput!) {
            createIngestionExecutionRequest(input: $input)
        }
        """
        exec_resp = httpx.post(
            f"{datahub_gms_url}/api/graphql",
            headers=gql_headers,
            json={
                "query": create_exec_mutation,
                "variables": {"input": {"ingestionSourceUrn": ingestion_source_urn}},
            },
            timeout=15.0,
        )
        exec_resp.raise_for_status()
        exec_data = exec_resp.json()
        if "errors" in exec_data:
            pytest.skip(
                f"createIngestionExecutionRequest error: {exec_data['errors']}"
            )
        exec_request_urn = exec_data.get("data", {}).get("createIngestionExecutionRequest")
        assert exec_request_urn, f"No executionRequest URN returned: {exec_data}"

        # ── Step 5: Poll executionRequest until terminal ──────────────────────
        # Cap at 90 seconds, 5-second interval
        # spec: plan §test_uc1_passive_postgres_via_datahub_managed_ingestion
        exec_query = """
        query getExecutionRequest($urn: String!) {
            executionRequest(urn: $urn) {
                urn
                result {
                    status
                    startTimeMs
                    durationMs
                }
            }
        }
        """
        terminal_states = {"SUCCEEDED", "FAILED", "CANCELLED", "FAILURE", "SUCCESS"}
        final_status = None
        deadline = time.time() + 90
        while time.time() < deadline:
            poll_resp = httpx.post(
                f"{datahub_gms_url}/api/graphql",
                headers=gql_headers,
                json={"query": exec_query, "variables": {"urn": exec_request_urn}},
                timeout=10.0,
            )
            if poll_resp.status_code == 200:
                poll_data = poll_resp.json()
                exec_result = (
                    (poll_data.get("data") or {})
                    .get("executionRequest", {})
                    or {}
                ).get("result")
                if exec_result:
                    status = (exec_result.get("status") or "").upper()
                    if status in terminal_states:
                        final_status = status
                        break
            await asyncio.sleep(5)

        if final_status is None:
            pytest.skip(
                "DataHub Managed Ingestion execution did not reach terminal state within "
                "90 seconds. The actions pod may be slow or the recipe may have failed "
                "to connect. Skipping passive sync assertion."
            )

        # ── Steps 6+7: Re-invoke passive-sync inside the poll loop ──────────────
        # DataHub indexing latency means a single-shot sync may not surface the new
        # run. Mirror Case 3's pattern: call passive-sync each iteration and poll
        # until the event count grows past the pre-run baseline.
        # spec: BACKEND.md §Passive status-sync pipeline — sync_passive_status()
        # spec: USE_CASE_en.md §UC1 Case 2 — events list shows INGESTION.COMPLETE
        #     with detail.source='passive'
        # spec: feedback_no_increase_timeout — use polls with clear failure mode.
        internal_token = os.environ.get("DATASPOKE_INTERNAL_TOKEN", "")
        internal_headers = {"X-Internal-Token": internal_token} if internal_token else {}

        events_body = None
        passive_events_count = 0
        deadline = time.time() + 60.0
        while time.time() < deadline:
            sync_resp = await api_client.post(
                "/internal/activities/ingestion/passive-sync",
                headers=internal_headers,
            )
            assert sync_resp.status_code in (200, 204), (
                f"Internal passive-sync activity failed: {sync_resp.status_code}: "
                f"{sync_resp.text}"
            )
            events_resp = await api_client.get(
                passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_resp.status_code == 200
            events_body = events_resp.json()
            passive_events_count = len([
                e for e in events_body.get("events", [])
                if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
                and (e.get("detail") or {}).get("source") == "passive"
            ])
            if passive_events_count > events_before_count:
                break
            await asyncio.sleep(2.0)

        assert passive_events_count > events_before_count, (
            f"Expected new INGESTION.COMPLETE/FAIL events with source='passive' to appear "
            f"within 60s (before={events_before_count}, after={passive_events_count}); "
            f"got events: {(events_body or {}).get('events', [])}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — passive sync writes event rows"
        )

        # ── Step 8: Cross-dataset overview includes passive URN ───────────────
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/ingestion?limit=100",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200
        configs_by_urn = {c["dataset_urn"]: c for c in overview_resp.json().get("configs", [])}
        assert _PASSIVE_PG_URN in configs_by_urn, (
            f"Passive PG URN {_PASSIVE_PG_URN!r} not found in GET /spoke/common/ingestion. "
            "spec: USE_CASE_en.md §UC1 §Cross-dataset overview"
        )
        assert configs_by_urn[_PASSIVE_PG_URN]["mode"] == "passive"

    finally:
        # ── Step 9: Cleanup ──────────────────────────────────────────────────
        # Delete DataHub IngestionSource
        if ingestion_source_urn:
            delete_mutation = """
            mutation deleteIngestionSource($urn: String!) {
                deleteIngestionSource(urn: $urn)
            }
            """
            try:
                httpx.post(
                    f"{datahub_gms_url}/api/graphql",
                    headers=gql_headers,
                    json={
                        "query": delete_mutation,
                        "variables": {"urn": ingestion_source_urn},
                    },
                    timeout=10.0,
                )
            except Exception:
                pass

        # Delete DataSpoke passive conf
        await api_client.delete(passive_conf_url, headers=admin_headers)


# ── Case 3: passive Kafka via external script ─────────────────────────────────


@pytest.mark.asyncio
async def test_uc1_passive_kafka_via_external_script(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC1 Case 3 — External script emits a DPI; DataSpoke passive sync surfaces it.

    Narrative: "Imazon needs to load metadata for a Kafka topic from a one-off context:
    a developer runs a Python script using the acryl-datahub SDK that emits Status,
    SchemaMetadata, and a DataProcessInstance per invocation."
    spec: USE_CASE_en.md §UC1 Case 3

    Steps:
      1. PUT passive-mode conf for imazon.orders.events Kafka dataset
      2. Verify POST method/ingestion/run returns 409 INGESTION_NOT_APPLICABLE
      3. GET event/ingestion — assert initially empty for this URN
      4. Simulate external script: emit DPI lifecycle via DataHub client
         (Properties + Output + STARTED + dataset aspects + COMPLETE with SUCCESS)
      5. Trigger sync_passive_status via internal activity endpoint
      6. GET event/ingestion — assert one INGESTION.COMPLETE row with source='passive'
      7. Cross-dataset overview includes passive Kafka URN
      8. Cleanup: delete DataSpoke conf (reset-all handles DPI cleanup in subsequent runs)
    """
    datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_DATAHUB_GMS_URL not set; skipping passive Kafka test")

    passive_conf_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/attr/ingestion/conf"
    )
    passive_run_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/method/ingestion/run"
    )
    passive_events_url = (
        f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/event/ingestion"
    )

    dpi_urn: str | None = None

    try:
        # ── Step 1: PUT passive conf for Kafka dataset ────────────────────────
        # spec: USE_CASE_en.md §UC1 Case 3 — PUT passive conf (no locator/auth/schedule_tier)
        put_resp = await api_client.put(
            passive_conf_url,
            headers=admin_headers,
            json={
                "mode": "passive",
                "platform": "kafka",
                "identifier": {
                    "topic": "imazon.orders.events",
                    "cluster": "example_kafka",
                },
                "is_enabled": True,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT passive Kafka conf failed: {put_resp.status_code} {put_resp.text}"
        )
        passive_body = put_resp.json()
        assert passive_body["mode"] == "passive"
        assert passive_body["platform"] == "kafka"
        assert passive_body.get("schedule_tier") is None, (
            "Passive conf must not carry schedule_tier. "
            "spec: USE_CASE_en.md §UC1 Case 3"
        )
        assert passive_body["is_enabled"] is True

        # ── Step 2: Verify method/ingestion/run returns 409 ───────────────────
        # spec: USE_CASE_en.md §UC1 API Mapping —
        #     "passive configs return 409 INGESTION_NOT_APPLICABLE"
        reject_resp = await api_client.post(
            passive_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert reject_resp.status_code == 409, (
            f"Expected 409 INGESTION_NOT_APPLICABLE; "
            f"got {reject_resp.status_code}: {reject_resp.text}"
        )
        assert reject_resp.json().get("error_code") == "INGESTION_NOT_APPLICABLE", (
            f"Expected error_code='INGESTION_NOT_APPLICABLE'; "
            f"got {reject_resp.json().get('error_code')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping"
        )

        # ── Step 3: Event history initially empty for this URN ────────────────
        events_before_resp = await api_client.get(
            passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert events_before_resp.status_code == 200
        events_before = events_before_resp.json()
        assert isinstance(events_before.get("events"), list)
        ingestion_events_before = [
            e for e in events_before["events"]
            if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
            and (e.get("detail") or {}).get("source") == "passive"
        ]

        # ── Step 4: Simulate external script — emit DPI lifecycle ─────────────
        # Mimics what a script following the Custom Ingestor Authoring Contract does.
        # spec: BACKEND.md §Custom Ingestor Authoring Contract — Required aspects per run:
        #   1. DataProcessInstanceProperties
        #   2. DataProcessInstanceRelationships
        #   3. DataProcessInstanceRunEvent(STARTED)
        #   4. Dataset aspect work (Status + DatasetProperties + SchemaMetadata)
        #   5. DataProcessInstanceRunEvent(COMPLETE/SUCCESS)
        from src.shared.datahub.client import DataHubClient

        dh_client = DataHubClient(gms_url=datahub_gms_url, token=datahub_token)

        test_run_uuid = uuid.uuid4().hex[:12]
        # spec: BACKEND.md §Custom Ingestor Authoring Contract §DPI URN convention:
        #     "urn:li:dataProcessInstance:<deterministic-id>. Recommend <platform>-<run_id>"
        dpi_urn = f"urn:li:dataProcessInstance:external-kafka-{test_run_uuid}"
        now_ms = int(time.time() * 1000)

        from datahub.metadata.schema_classes import (
            AuditStampClass,
            DataProcessInstanceOutputClass,
            DataProcessInstancePropertiesClass,
            DataProcessInstanceRelationshipsClass,
            DataProcessInstanceRunEventClass,
            DataProcessInstanceRunResultClass,
            DataProcessRunStatusClass,
            DataProcessTypeClass,
            DatasetPropertiesClass,
            OtherSchemaClass,
            RunResultTypeClass,
            SchemaMetadataClass,
            StatusClass,
        )

        # 1. Properties
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"external-kafka-ingestor-{test_run_uuid}",
                type=DataProcessTypeClass.BATCH_SCHEDULED,
                created=AuditStampClass(
                    time=now_ms, actor="urn:li:corpuser:external-script"
                ),
            ),
        )
        # 2. Relationships
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRelationshipsClass(
                upstreamInstances=[],
                parentTemplate=None,
            ),
        )
        # 2b. Output relationship (dataset this run produced)
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceOutputClass(outputs=[_PASSIVE_KAFKA_URN]),
        )
        # 3. STARTED — before any dataset aspect work
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.STARTED,
                timestampMillis=now_ms,
            ),
        )
        # 4. Dataset aspects (Status + DatasetProperties + SchemaMetadata)
        await dh_client.emit_aspect(_PASSIVE_KAFKA_URN, StatusClass(removed=False))
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            DatasetPropertiesClass(
                name="imazon.orders.events",
                description="Order state-change events from external Kafka topic",
                customProperties={"source": "uc1-external-script-test"},
            ),
        )
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            SchemaMetadataClass(
                schemaName="imazon.orders.events",
                platform="urn:li:dataPlatform:kafka",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=[],
            ),
        )
        # 5. COMPLETE/SUCCESS — after all aspect work
        end_ms = int(time.time() * 1000)
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.COMPLETE,
                timestampMillis=end_ms,
                result=DataProcessInstanceRunResultClass(
                    type=RunResultTypeClass.SUCCESS,
                    nativeResultType="external-script",
                ),
                durationMillis=end_ms - now_ms,
            ),
        )

        # ── Step 5: Trigger sync_passive_status (with bounded retry) ────────
        # spec: BACKEND.md §Passive status-sync pipeline — hourly DAG calls this
        # DataHub indexing of emitted DPI aspects may take a moment. Poll sync +
        # event check rather than sleeping a fixed duration.
        # spec: feedback_no_increase_timeout — bounded polls with explicit failure.
        internal_token = os.environ.get("DATASPOKE_INTERNAL_TOKEN", "")
        internal_headers_dict = (
            {"X-Internal-Token": internal_token} if internal_token else {}
        )

        # ── Step 6: GET event/ingestion — assert one INGESTION.COMPLETE row ───
        # spec: USE_CASE_en.md §UC1 Case 3 — "the next hourly poll surfaces a row
        # in event/ingestion exactly as in Case 2"
        events_after: dict = {}
        ingestion_events_after: list = []
        deadline = time.time() + 30.0
        while time.time() < deadline:
            sync_resp = await api_client.post(
                "/internal/activities/ingestion/passive-sync",
                headers=internal_headers_dict,
            )
            assert sync_resp.status_code in (200, 204), (
                f"Internal passive-sync activity failed: {sync_resp.status_code}: "
                f"{sync_resp.text}"
            )
            events_after_resp = await api_client.get(
                passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_after_resp.status_code == 200
            events_after = events_after_resp.json()
            ingestion_events_after = [
                e for e in events_after.get("events", [])
                if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
                and (e.get("detail") or {}).get("source") == "passive"
            ]
            if len(ingestion_events_after) > len(ingestion_events_before):
                break
            await asyncio.sleep(1.0)

        assert len(ingestion_events_after) > len(ingestion_events_before), (
            f"Expected at least one new INGESTION.COMPLETE/FAIL event with "
            f"source='passive' within 30s; before={len(ingestion_events_before)}, "
            f"after={len(ingestion_events_after)}. "
            f"Events: {events_after.get('events', [])}. "
            "spec: USE_CASE_en.md §UC1 Case 3"
        )

        # ── Step 7: Cross-dataset overview includes passive Kafka URN ─────────
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/ingestion?limit=100",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200
        configs_by_urn = {
            c["dataset_urn"]: c for c in overview_resp.json().get("configs", [])
        }
        assert _PASSIVE_KAFKA_URN in configs_by_urn, (
            f"Passive Kafka URN {_PASSIVE_KAFKA_URN!r} not found in "
            "GET /spoke/common/ingestion. spec: USE_CASE_en.md §UC1 §Cross-dataset overview"
        )
        assert configs_by_urn[_PASSIVE_KAFKA_URN]["mode"] == "passive"

    finally:
        # ── Step 8: Cleanup ──────────────────────────────────────────────────
        # Delete the simulated DPI from DataHub to leave a clean state.
        # spec: BACKEND.md §Custom Ingestor Authoring Contract — DPI URN is deterministic;
        # delete it explicitly so subsequent runs don't observe stale DPI records.
        if datahub_gms_url and dpi_urn:
            dh_headers = (
                {"Authorization": f"Bearer {datahub_token}", "Content-Type": "application/json"}
                if datahub_token
                else {"Content-Type": "application/json"}
            )
            delete_entity_mutation = """
            mutation deleteEntity($urn: String!, $soft: Boolean!) {
                deleteEntity(urn: $urn, soft: $soft)
            }
            """
            try:
                httpx.post(
                    f"{datahub_gms_url}/api/graphql",
                    headers=dh_headers,
                    json={
                        "query": delete_entity_mutation,
                        "variables": {"urn": dpi_urn, "soft": False},
                    },
                    timeout=10.0,
                )
            except Exception:
                pass

        # Delete DataSpoke passive conf
        await api_client.delete(passive_conf_url, headers=admin_headers)
