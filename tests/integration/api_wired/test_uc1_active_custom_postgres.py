"""UC1 Case 1 — Active-custom Postgres ingestion: end-to-end through public REST API.

DataSpoke owns the extraction for catalog.title_master. An Airflow tier DAG runs the
platform extractor on the configured schedule_tier and emits results to DataHub. Steps
cover PUT conf, dry-run guard, disabled-run 409 rejection, PATCH enable, real run,
DPI/aspect assertions against DataHub GMS, and cross-dataset overview verification.
"""
# spec: USE_CASE_en.md §UC1

import asyncio
import os
import time
import urllib.parse

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

# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1 primary dataset
_ACTIVE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ACTIVE_ENCODED = urllib.parse.quote(_ACTIVE_URN, safe="")

# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_SCHEMAS: frozenset[str] = frozenset(["catalog"])
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])


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
        assert put_body["workflow_dag_id"] == "ingestion-active-daily", (
            f"active-custom + daily must surface workflow_dag_id='ingestion-active-daily'; "
            f"got {put_body.get('workflow_dag_id')!r}. "
            "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
        )
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
        # Reject {"run_id": null, "status": null} — both fields must be usable
        assert dry_run_body["run_id"], "run_id must be non-empty"
        assert dry_run_body["status"], "status must be non-empty"
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
        enable_body = enable_resp.json()
        assert enable_body["workflow_dag_id"] == "ingestion-active-daily", (
            f"PATCH is_enabled=true must preserve workflow_dag_id='ingestion-active-daily'; "
            f"got {enable_body.get('workflow_dag_id')!r}. "
            "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
        )

        # Capture run_start_ms before the run so lastIngested.time comparison is bounded.
        # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        #     lastObserved is set to int(time.time()*1000) at run time.
        run_start_ms = int(time.time() * 1000)
        enabled_run_resp = await api_client.post(
            active_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert enabled_run_resp.status_code == 200, (
            f"Real run after enable failed: {enabled_run_resp.status_code}: {enabled_run_resp.text}"
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
                    # spec: USE_CASE_en.md §UC1 Case 2 — INGESTION.COMPLETE rows
                    # carry status: "success"; INGESTION.FAIL → status: "failure"
                    assert evt.get("status") == "success", (
                        f"INGESTION.COMPLETE event must carry status='success'; "
                        f"got {evt.get('status')!r}"
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

        # ── Step 5b: Assert DataHub aspects carry PG comments + typed fields ──
        # spec: BACKEND.md §Ingestion Service — PG comment ingestion.
        # spec: DATAHUB_INTEGRATION.md §datasetProperties — description from ingestion.
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata — typed union fix.
        # spec: BACKEND.md §Active run pipeline lines 246-257 — a non-dry-run completing
        # with INGESTION.COMPLETE must have emitted both aspects in full.
        datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "")
        datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

        if not datahub_gms_url:
            pytest.skip("DATASPOKE_DATAHUB_GMS_URL not set; skipping aspect verification")

        gms_headers: dict[str, str] = {}
        if datahub_token:
            gms_headers["Authorization"] = f"Bearer {datahub_token}"

        encoded_active_urn = urllib.parse.quote(_ACTIVE_URN, safe="")

        # datasetProperties — description must start with seeded COMMENT ON TABLE text.
        # spec: TESTING.md §Imazon Dummy-Data Reference — seed in 01_catalog.sql:
        #   COMMENT ON TABLE catalog.title_master IS 'Master record for each book title — ...'
        props_resp = httpx.get(
            f"{datahub_gms_url}/aspects/{encoded_active_urn}?aspect=datasetProperties&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert props_resp.status_code == 200, (
            f"GMS GET datasetProperties failed: {props_resp.status_code} {props_resp.text}"
        )
        dp_description = (
            props_resp.json()
            .get("aspect", {})
            .get("com.linkedin.dataset.DatasetProperties", {})
            .get("description", "")
        )
        assert dp_description.startswith("Master record for each book title"), (
            f"datasetProperties.description must begin with seeded COMMENT ON TABLE text "
            f"after UC1 active-custom run; got {dp_description!r}. "
            "spec: BACKEND.md §Ingestion Service — PG comment ingestion. "
            "spec: USE_CASE_en.md §UC1 Case 1"
        )

        # schemaMetadata — at least one NumberType field (page_count: integer) and
        # one StringType field (title: character varying).
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata — typed union fix.
        schema_resp = httpx.get(
            f"{datahub_gms_url}/aspects/{encoded_active_urn}?aspect=schemaMetadata&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert schema_resp.status_code == 200, (
            f"GMS GET schemaMetadata failed: {schema_resp.status_code} {schema_resp.text}"
        )
        fields_raw = (
            schema_resp.json()
            .get("aspect", {})
            .get("com.linkedin.schema.SchemaMetadata", {})
            .get("fields", [])
        )
        assert fields_raw, (
            "schemaMetadata.fields must be non-empty after UC1 active-custom run. "
            "spec: BACKEND.md §Active run pipeline — aspects emitted per discovered dataset."
        )
        fields_by_path = {f.get("fieldPath"): f for f in fields_raw}

        # page_count (integer) → NumberType
        assert "page_count" in fields_by_path, (
            f"Expected 'page_count' field in schemaMetadata; got {list(fields_by_path.keys())}. "
            "spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master schema"
        )
        pc_type = (
            fields_by_path["page_count"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.NumberType")
        )
        assert pc_type is not None, (
            f"page_count (integer) must have type.type=NumberType after UC1 run; "
            f"got type={fields_by_path['page_count'].get('type')!r}. "
            "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
        )

        # title (character varying) → StringType
        assert "title" in fields_by_path, (
            f"Expected 'title' field in schemaMetadata; got {list(fields_by_path.keys())}. "
            "spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master schema"
        )
        title_type = (
            fields_by_path["title"]
            .get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.StringType")
        )
        assert title_type is not None, (
            f"title (character varying) must have type.type=StringType after UC1 run; "
            f"got type={fields_by_path['title'].get('type')!r}. "
            "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
        )

        # ── Step 5b (cont.): Assert container hierarchy in DataHub ───────────
        # spec: BACKEND.md §Ingestion Service — Aspects emitted (ContainerClass + container hierarchy emission).
        # spec: DATAHUB_INTEGRATION.md §Container URN Construction — URN parity with upstream plugin;
        # backcompat_env_as_instance=True is mandatory. GUID dict for schema container:
        # {"platform": "postgres", "database": "example_db", "schema": "catalog", "instance": "DEV"}

        _SCHEMA_CONTAINER_URN = "urn:li:container:d30ba0aa3cb3374982ca9a9db3466b5e"
        _DB_CONTAINER_URN = "urn:li:container:877925964b937b391ead54462bf98b9d"

        container_resp = httpx.get(
            f"{datahub_gms_url}/aspects/{encoded_active_urn}?aspect=container&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert container_resp.status_code == 200, (
            f"GMS GET container aspect failed: {container_resp.status_code} {container_resp.text}"
        )
        container_urn = (
            container_resp.json()
            .get("aspect", {})
            .get("com.linkedin.container.Container", {})
            .get("container")
        )
        assert container_urn == _SCHEMA_CONTAINER_URN, (
            f"After UC1 active-custom run, dataset container aspect must point to schema "
            f"container {_SCHEMA_CONTAINER_URN!r}; got {container_urn!r}. "
            "spec: BACKEND.md §Ingestion Service — Aspects emitted (ContainerClass + container hierarchy emission). "
            "spec: DATAHUB_INTEGRATION.md §Container URN Construction — dataset emits ContainerClass(container=schema_key.as_urn()). "
            "spec: USE_CASE_en.md §UC1 Case 1"
        )

        # Schema container must exist and have name='catalog', subType='Schema', platform=postgres
        schema_enc = urllib.parse.quote(_SCHEMA_CONTAINER_URN, safe="")
        schema_props_resp = httpx.get(
            f"{datahub_gms_url}/aspects/{schema_enc}?aspect=containerProperties&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert schema_props_resp.status_code == 200, (
            f"GMS GET containerProperties for schema container failed: "
            f"{schema_props_resp.status_code}"
        )
        schema_container_name = (
            schema_props_resp.json()
            .get("aspect", {})
            .get("com.linkedin.container.ContainerProperties", {})
            .get("name")
        )
        assert schema_container_name == "catalog", (
            f"Schema container ContainerProperties.name must be 'catalog'; "
            f"got {schema_container_name!r}. "
            "spec: DATAHUB_INTEGRATION.md §Container URN Construction — SchemaKey fields include schema name"
        )

        schema_subtypes_resp = httpx.get(
            f"{datahub_gms_url}/aspects/{schema_enc}?aspect=subTypes&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert schema_subtypes_resp.status_code == 200
        schema_subtypes = (
            schema_subtypes_resp.json()
            .get("aspect", {})
            .get("com.linkedin.common.SubTypes", {})
            .get("typeNames", [])
        )
        assert "Schema" in schema_subtypes, (
            f"Schema container SubTypes must include 'Schema'; got {schema_subtypes!r}. "
            "spec: DATAHUB_INTEGRATION.md §Container URN Construction — sub_types=['Schema'] and parent_container_key=db_key"
        )

        # ── Step 5b (cont.): Assert dataset.lastIngested via GraphQL ─────────
        # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        #     "When all aspects carry the default sentinel, lastIngested stays null
        #     and the UI's 'Synced X ago from <Platform>' badge does not render."
        #     After a DataSpoke active-custom run with a non-default runId, the field
        #     MUST be non-null.
        # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        #     "runId='dataspoke-{platform}-{run_id}'"
        # spec: USE_CASE_en.md §UC1 Case 1 — active-custom run emits systemMetadata
        #     on every dataset-aspect emit so DataHub computes lastIngested.
        gql_headers = {}
        if datahub_token:
            gql_headers["Authorization"] = f"Bearer {datahub_token}"
        gql_headers["Content-Type"] = "application/json"

        last_ingested_resp = httpx.post(
            f"{datahub_gms_url}/api/graphql",
            headers=gql_headers,
            json={
                "query": (
                    "query getLastIngested($urn: String!) { dataset(urn: $urn) { lastIngested } }"
                ),
                "variables": {"urn": _ACTIVE_URN},
            },
            timeout=10.0,
        )
        assert last_ingested_resp.status_code == 200, last_ingested_resp.text
        last_ingested_ms = (
            last_ingested_resp.json().get("data", {}).get("dataset", {}).get("lastIngested")
        )
        assert last_ingested_ms is not None, (
            "dataset.lastIngested must be non-null after active-custom run; "
            "the UI badge depends on this. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert last_ingested_ms >= run_start_ms, (
            f"lastIngested ({last_ingested_ms}) must be >= run start ({run_start_ms}). "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement — "
            "lastObserved is epoch-ms of the run"
        )

        # Verify runId via the openapi v3 endpoint — dataset.lastIngested is a scalar Long
        # and does not expose runId; schemaMetadata.systemMetadata carries it instead.
        # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        #     runId='dataspoke-{platform}-{run_id}'
        dataset_urn_enc = urllib.parse.quote(_ACTIVE_URN, safe="")
        sysmeta_resp = httpx.get(
            f"{datahub_gms_url}/openapi/v3/entity/dataset/{dataset_urn_enc}"
            "?systemMetadata=true&aspects=schemaMetadata",
            headers=gms_headers,
            timeout=10.0,
        )
        assert sysmeta_resp.status_code == 200, sysmeta_resp.text
        schema_runId = (
            sysmeta_resp.json().get("schemaMetadata", {}).get("systemMetadata", {}).get("runId")
        )
        assert schema_runId is not None and schema_runId.startswith("dataspoke-postgres-"), (
            f"schemaMetadata.systemMetadata.runId must start with 'dataspoke-postgres-'; "
            f"got {schema_runId!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement — "
            "runId='dataspoke-{platform}-{run_id}'"
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
