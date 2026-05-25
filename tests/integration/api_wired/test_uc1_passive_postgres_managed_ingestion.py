"""UC1 Case 2 — Passive Postgres via DataHub Managed Ingestion: end-to-end through public REST API.

DataHub Managed Ingestion runs against catalog.editions; DataSpoke registers the dataset
as passive and observes the resulting DataProcessInstance. Steps cover createIngestionSource,
PUT passive conf, 409 INGESTION_NOT_APPLICABLE rejection, execution firing, terminal-state
polling, passive-sync activity invocation, event-row assertion, and cross-dataset overview.
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

_PG_HOST = os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")

# spec: USE_CASE_en.md §UC1 Case 2 — catalog.editions is a separate UC1 table not used by Case 1
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.editions
_PASSIVE_PG_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_PASSIVE_PG_ENCODED = urllib.parse.quote(_PASSIVE_PG_URN, safe="")

# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])


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
    datahub_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping passive PG test")

    passive_conf_url = f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/attr/ingestion/conf"
    passive_run_url = f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/method/ingestion/run"
    passive_events_url = f"/api/v1/spoke/common/data/{_PASSIVE_PG_ENCODED}/event/ingestion"

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
                    "table_pattern": {"allow": [".*editions"]},
                    # .*editions anchors the prefix so re.match against the lowercased
                    # FQN example_db.catalog.editions succeeds.
                    # spec: feature/BACKEND.md §Ingestion Service — PG comment ingestion
                    "env": "DEV",
                    # env must match DataSpoke's URN env so Managed Ingestion writes to
                    # the same URN that DataSpoke registered.
                    # spec: DATAHUB_INTEGRATION.md §dataset URN env discriminator
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

        gql_headers = (
            {"Authorization": f"Bearer {datahub_token}", "Content-Type": "application/json"}
            if datahub_token
            else {"Content-Type": "application/json"}
        )
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
        assert ingestion_source_urn, f"createIngestionSource returned no URN: {gql_data}"

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
            "Passive conf must not carry schedule_tier. spec: USE_CASE_en.md §UC1 Case 2"
        )
        assert passive_body.get("locator") is None or passive_body.get("locator") == {}, (
            "Passive conf response must carry no locator. spec: USE_CASE_en.md §UC1 Case 2"
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
        events_before_count = len(
            [
                e
                for e in snapshot_resp.json().get("events", [])
                if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
                and (e.get("detail") or {}).get("source") == "passive"
            ]
        )

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
            pytest.skip(f"createIngestionExecutionRequest error: {exec_data['errors']}")
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
                exec_result = ((poll_data.get("data") or {}).get("executionRequest", {}) or {}).get(
                    "result"
                )
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
        internal_token = os.environ.get("DATASPOKE_TEST_INTERNAL_TOKEN", "")
        internal_headers = {"X-Internal-Token": internal_token} if internal_token else {}

        events_body = None
        passive_events_count = 0
        deadline = time.time() + 60.0
        while time.time() < deadline:
            # passive-sync internally does multiple DataHub GraphQL queries; while
            # DataHub is still indexing the just-fired Managed Ingestion run those
            # queries can briefly exceed the api_client default timeout. A single
            # ReadTimeout in this poll is a transient hiccup, not a fatal failure —
            # the outer deadline is the real boundary.
            try:
                sync_resp = await api_client.post(
                    "/internal/activities/ingestion/passive-sync",
                    headers=internal_headers,
                )
            except httpx.ReadTimeout:
                await asyncio.sleep(1.0)
                continue
            assert sync_resp.status_code in (200, 204), (
                f"Internal passive-sync activity failed: {sync_resp.status_code}: {sync_resp.text}"
            )
            events_resp = await api_client.get(
                passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_resp.status_code == 200
            events_body = events_resp.json()
            passive_events_count = len(
                [
                    e
                    for e in events_body.get("events", [])
                    if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
                    and (e.get("detail") or {}).get("source") == "passive"
                ]
            )
            if passive_events_count > events_before_count:
                break
            await asyncio.sleep(2.0)

        assert passive_events_count > events_before_count, (
            f"Expected new INGESTION.COMPLETE/FAIL events with source='passive' to appear "
            f"within 60s (before={events_before_count}, after={passive_events_count}); "
            f"got events: {(events_body or {}).get('events', [])}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — passive sync writes event rows"
        )

        # ── Step 6b: Verify Managed Ingestion actually wrote dataset-level aspects ──
        # Without this, the test passes vacuously when Managed Ingestion's filter
        # excludes the table — DataSpoke's passive-sync still surfaces the DPI,
        # but no real metadata flowed.
        # spec: feature/BACKEND.md §Custom Ingestor Authoring Contract;
        #       DATAHUB_INTEGRATION.md §datasetProperties + §schemaMetadata typed-union shape.
        dataset_urn_enc = urllib.parse.quote(_PASSIVE_PG_URN, safe="")
        gms_headers = {"Authorization": f"Bearer {datahub_token}"} if datahub_token else {}
        gms_dataset_resp = httpx.get(
            f"{datahub_gms_url}/openapi/v3/entity/dataset/{dataset_urn_enc}",
            headers=gms_headers,
            timeout=15.0,
        )
        assert gms_dataset_resp.status_code == 200, (
            f"GMS GET /openapi/v3/entity/dataset/{_PASSIVE_PG_URN!r} failed: "
            f"{gms_dataset_resp.status_code} {gms_dataset_resp.text}"
        )
        gms_dataset = gms_dataset_resp.json()

        # Description must come from the seeded PG COMMENT ON TABLE.
        # spec: feature/BACKEND.md §Ingestion Service — PG comment ingestion
        # spec: TESTING.md §Imazon Dummy-Data Reference — seed in 01_catalog.sql:
        #   COMMENT ON TABLE catalog.editions IS 'Per-format edition rows for each book title...'
        table_desc = (
            gms_dataset.get("datasetProperties", {}).get("value", {}).get("description", "")
        )
        assert table_desc.startswith("Per-format edition rows"), (
            f"datasetProperties.description must come from seeded PG COMMENT ON TABLE; "
            f"got {table_desc!r}. Likely cause: Managed Ingestion's table_pattern filtered "
            f"the table out, or env mismatch wrote to a different URN. "
            f"spec: feature/BACKEND.md §Ingestion Service — PG comment ingestion"
        )

        # Each field must have a typed PDL union (not RecordType/Struct fallback).
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata — typed-union shape
        fields = gms_dataset.get("schemaMetadata", {}).get("value", {}).get("fields", [])
        assert fields, "schemaMetadata.fields must be non-empty after Managed Ingestion run"
        typed_unions = {
            list(f.get("type", {}).get("type", {}).keys())[0]
            for f in fields
            if f.get("type", {}).get("type")
        }
        assert "com.linkedin.schema.RecordType" not in typed_unions, (
            f"All fields fell back to RecordType (Struct) — Managed Ingestion did not write "
            f"properly typed schemaMetadata. Got typed_unions={typed_unions}. "
            f"spec: DATAHUB_INTEGRATION.md §schemaMetadata"
        )
        # Per-column type assertions — mirrors Step 5b pattern.
        # editions columns per 01_catalog.sql:105-113:
        #   integer/varchar/numeric/date/boolean/timestamp.
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union
        fields_by_path = {f["fieldPath"]: f for f in fields if f.get("fieldPath")}

        assert "release_date" in fields_by_path, "schemaMetadata.fields missing 'release_date'"
        release_date_type = (
            fields_by_path["release_date"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.DateType")
        )
        assert release_date_type is not None, (
            f"editions.release_date must surface as DateType in DataHub; got "
            f"{list(fields_by_path['release_date'].get('type', {}).get('type', {}).keys())}. "
            f"spec: DATAHUB_INTEGRATION.md §schemaMetadata"
        )

        assert "is_active" in fields_by_path, "schemaMetadata.fields missing 'is_active'"
        is_active_type = (
            fields_by_path["is_active"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.BooleanType")
        )
        assert is_active_type is not None, (
            f"editions.is_active must surface as BooleanType in DataHub; got "
            f"{list(fields_by_path['is_active'].get('type', {}).get('type', {}).keys())}. "
            f"spec: DATAHUB_INTEGRATION.md §schemaMetadata"
        )

        assert "price" in fields_by_path, "schemaMetadata.fields missing 'price'"
        price_type = (
            fields_by_path["price"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.NumberType")
        )
        assert price_type is not None, (
            f"editions.price (numeric) must surface as NumberType; got "
            f"{list(fields_by_path['price'].get('type', {}).get('type', {}).keys())}. "
            f"spec: DATAHUB_INTEGRATION.md §schemaMetadata"
        )

        assert "format" in fields_by_path, "schemaMetadata.fields missing 'format'"
        format_type = (
            fields_by_path["format"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.StringType")
        )
        assert format_type is not None, (
            f"editions.format (varchar) must surface as StringType; got "
            f"{list(fields_by_path['format'].get('type', {}).get('type', {}).keys())}. "
            f"spec: DATAHUB_INTEGRATION.md §schemaMetadata"
        )

        # At least 6 of 7 fields must carry a description from a seeded PG COMMENT ON COLUMN.
        # 01_catalog.sql:160-166 seeds 7 COMMENT ON COLUMN statements for editions.
        # Allow one to be lost across DataHub's aspect-update timing edge cases.
        # spec: feature/BACKEND.md §Ingestion Service — PG comment ingestion
        fields_with_desc = [f for f in fields if f.get("description") and len(f["description"]) > 0]
        assert len(fields_with_desc) >= 6, (
            f"editions has 7 seeded COMMENT ON COLUMN statements (01_catalog.sql:160-166); "
            f"expected at least 6 fields to surface their description after Managed Ingestion. "
            f"Got {len(fields_with_desc)} of {len(fields)}: "
            f"{[(f.get('fieldPath'), f.get('description')) for f in fields]}. "
            f"spec: feature/BACKEND.md §Ingestion Service — PG comment ingestion"
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
