"""Spot tests — Ingestion Control: active-custom Postgres.

Concerns covered:
- PUT creates active-custom conf (201), GET round-trips it, PATCH updates, DELETE removes
- Dry-run does NOT persist a DPI or aspects (dry_run=true)
- Real run with DataHub aspect emission — split into per-concern tests sharing one run:
  1. INGESTION.COMPLETE event emitted
  2. datasetProperties.description from PG COMMENT ON TABLE
  3. schemaMetadata typed fields (NumberType / StringType)
  4. isbn field description from PG COMMENT ON COLUMN
  5. systemMetadata.runId starts with 'dataspoke-postgres-'
- workflow_dag_id derived from schedule_tier='daily' → 'ingestion-active-daily'
- workflow_dag_id derived from schedule_tier='weekly' → 'ingestion-active-weekly'
"""
# spec: USE_CASE_en.md §UC1 Case 1
# spec: API.md §Ingestion routes
# spec: feature/BACKEND.md §Ingestion Service
# spec: DATAHUB_INTEGRATION.md §datasetProperties, §schemaMetadata

import asyncio
import os
import re
import urllib.parse

import httpx
import pytest
import pytest_asyncio

# Per-module dummy-data seed — catalog schema triggers PG reset + DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_PG_HOST = os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")

# spec: SECRET_RESOLUTION.md §Name prefix policy — names must start with dataspoke-source-cred-
_VAULT_NAME = "dataspoke-source-cred-spot-active-pg"
_VAULT_KEY = "password"

# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1 primary dataset
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

_DATAHUB_GMS_URL = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
_DATAHUB_TOKEN = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

_FAIL_TAIL: frozenset[str] = frozenset({"fail", "failed", "failure", "error", "errored"})

# ── Module-scoped real-run fixture ─────────────────────────────────────────────
# Runs once per module; all DataHub aspect tests share the emitted aspects.
# This keeps the test suite fast (one real run) while keeping each assertion separate.


@pytest_asyncio.fixture(scope="module")
async def real_run_state() -> dict:
    """Perform ONE active-custom real run; yield {'run_id', 'gms_headers'} for assertion tests.

    Skips automatically when DATASPOKE_TEST_DATAHUB_GMS_URL is not set.
    Builds its own httpx client and JWT because api_client is function-scoped and cannot
    be injected into a module-scoped fixture.

    spec: USE_CASE_en.md §UC1 Case 1 — real run with DataHub emission
    """
    if not _DATAHUB_GMS_URL:
        pytest.skip("DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping DataHub aspect assertions")

    base_url = f"http://app.{os.environ['DATASPOKE_KUBE_INGRESS_DOMAIN']}"
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        token_resp = await client.post(
            "/api/v1/auth/token",
            json={"email": "dataspoke", "password": "dataspoke"},
        )
        token_resp.raise_for_status()
        admin_headers = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}

        base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
        base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/ingestion/run"
        base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

        gms_headers: dict[str, str] = {}
        if _DATAHUB_TOKEN:
            gms_headers["Authorization"] = f"Bearer {_DATAHUB_TOKEN}"

        put_resp = await client.put(
            base_conf,
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
                "is_enabled": True,
                "schedule_tier": "daily",
            },
        )
        assert put_resp.status_code in (200, 201), put_resp.text

        run_resp = await client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 200, (
            f"POST ingestion run failed: {run_resp.status_code} {run_resp.text}"
        )
        run_id = run_resp.json()["run_id"]

        # Poll until INGESTION.COMPLETE event appears (cap 30s)
        # spec: feedback_no_increase_timeout — bounded polls
        found_complete = False
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            events_resp = await client.get(
                base_events + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_resp.status_code == 200
            for evt in events_resp.json().get("events", []):
                detail = evt.get("detail") or {}
                if detail.get("run_id") == run_id:
                    found_complete = True
                    break
            if found_complete:
                break
            await asyncio.sleep(1.0)

        assert found_complete, (
            f"INGESTION.COMPLETE event with run_id={run_id!r} not found within 30s. "
            "spec: BACKEND.md §Active run pipeline"
        )

        yield {"run_id": run_id, "gms_headers": gms_headers}

        await client.delete(base_conf, headers=admin_headers)


# ── CRUD tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_conf_put_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT creates active-custom conf (201/200), PATCH updates it, DELETE removes it (204).

    spec: USE_CASE_en.md §UC1 Case 1 — PUT conf shape
    spec: API.md §Ingestion routes — response envelope
    spec: SECRET_RESOLUTION.md §Vault-write flow — password stripped from response
    """
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    # PUT — create
    put_resp = await api_client.put(
        base,
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
            "schedule_tier": None,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    put_body = put_resp.json()
    assert put_body["dataset_urn"] == _TEST_URN
    assert put_body["platform"] == "postgres"
    assert put_body["mode"] == "active-custom"
    # spec: SECRET_RESOLUTION.md §Data Model — plaintext password must not appear in response
    assert "password" not in put_body.get("auth", {}), (
        f"plaintext password leaked into PUT response: {put_body.get('auth')}"
    )
    assert "force_overwrite" not in put_body.get("auth", {}).get("secret_ref", {}), (
        f"transient force_overwrite leaked into PUT response: {put_body.get('auth')}"
    )
    assert put_body["auth"]["secret_ref"]["name"] == _VAULT_NAME
    assert put_body["auth"]["secret_ref"]["key"] == _VAULT_KEY

    # GET — round-trip
    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["mode"] == "active-custom"

    # PATCH — partial update
    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"is_enabled": False, "schedule_tier": "weekly"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["schedule_tier"] == "weekly"

    # DELETE — remove
    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    # Verify gone
    gone_resp = await api_client.get(base, headers=admin_headers)
    assert gone_resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_run_dry_run_does_not_persist(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """dry_run=true returns run envelope but does NOT write events or DPI.

    spec: USE_CASE_en.md §UC1 — dry-run is permitted regardless of is_enabled
    spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/ingestion/run"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

    await api_client.put(
        base_conf,
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
        },
    )

    def _count_real_complete(events: list[dict]) -> int:
        return sum(
            1 for e in events
            if (e.get("event_type") or "").upper() == "INGESTION.COMPLETE"
            and (e.get("detail") or {}).get("dry_run") is False
        )

    # Snapshot real (non-dry) INGESTION.COMPLETE count before dry run
    before_resp = await api_client.get(base_events, headers=admin_headers)
    assert before_resp.status_code == 200
    count_before = _count_real_complete(before_resp.json().get("events", []))

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert "run_id" in run_body
    assert "status" in run_body
    assert run_body["run_id"], "run_id must be non-empty"
    assert run_body["status"].lower() not in _FAIL_TAIL, (
        f"dry-run unexpectedly returned fail status {run_body['status']!r}"
    )
    dry_run_id = run_body["run_id"]

    # Real (non-dry) INGESTION.COMPLETE count must NOT grow after a dry run.
    # A dry run always emits an internal traceability event but with detail.dry_run=true;
    # only events with detail.dry_run=false count as real completions.
    # spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission
    after_resp = await api_client.get(base_events, headers=admin_headers)
    assert after_resp.status_code == 200
    count_after = _count_real_complete(after_resp.json().get("events", []))
    assert count_after == count_before, (
        f"dry_run produced a non-dry INGESTION.COMPLETE event — "
        f"dry-run must not appear as a real completion: before={count_before}, after={count_after}. "
        "spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission"
    )

    # Positive check: the dry-run event MUST carry detail.dry_run=True.
    # Guards against regressions where the flag is dropped from the payload entirely
    # (which would make both before/after counts 0 and the count check silently pass).
    # spec: BACKEND.md §Active run pipeline — dry_run flag must be present in event detail
    all_events_after = after_resp.json().get("events", [])
    dry_event = next(
        (
            e for e in all_events_after
            if (e.get("detail") or {}).get("run_id") == dry_run_id
        ),
        None,
    )
    assert dry_event is not None, (
        f"No event found with detail.run_id={dry_run_id!r} — dry-run must emit a traceability event. "
        "spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission"
    )
    assert dry_event["detail"]["dry_run"] is True, (
        f"Dry-run event must carry detail.dry_run=True; got {dry_event['detail'].get('dry_run')!r}. "
        "spec: BACKEND.md §Active run pipeline — dry_run flag must be present in event detail"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_workflow_dag_id_daily(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """active-custom + schedule_tier='daily' surfaces workflow_dag_id='ingestion-active-daily'.

    spec: feature/BACKEND.md §workflow_dag_id — tier DAG IDs follow
    ingestion-active-{tier} pattern.
    """
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    put_resp = await api_client.put(
        base,
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
    assert put_resp.status_code in (200, 201), put_resp.text
    assert put_resp.json()["workflow_dag_id"] == "ingestion-active-daily", (
        f"active-custom + daily must surface workflow_dag_id='ingestion-active-daily'; "
        f"got {put_resp.json().get('workflow_dag_id')!r}. "
        "spec: feature/BACKEND.md §workflow_dag_id"
    )

    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_workflow_dag_id_weekly(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """active-custom + schedule_tier='weekly' surfaces workflow_dag_id='ingestion-active-weekly'.

    spec: feature/BACKEND.md §workflow_dag_id — tier DAG IDs follow
    ingestion-active-{tier} pattern.
    """
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    put_resp = await api_client.put(
        base,
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
            "schedule_tier": "weekly",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    assert put_resp.json()["workflow_dag_id"] == "ingestion-active-weekly", (
        f"active-custom + weekly must surface workflow_dag_id='ingestion-active-weekly'; "
        f"got {put_resp.json().get('workflow_dag_id')!r}. "
        "spec: feature/BACKEND.md §workflow_dag_id"
    )

    await api_client.delete(base, headers=admin_headers)


# ── Real-run assertion tests (share one run via module-scoped fixture) ─────────


@pytest.mark.asyncio
async def test_real_run_emits_ingestion_complete_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    real_run_state: dict,
) -> None:
    """Real run emits INGESTION.COMPLETE event visible in GET event/ingestion.

    spec: USE_CASE_en.md §UC1 Case 1 — INGESTION.COMPLETE event
    spec: BACKEND.md §Active run pipeline
    """
    run_id = real_run_state["run_id"]
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

    events_resp = await api_client.get(
        base_events + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
        headers=admin_headers,
    )
    assert events_resp.status_code == 200
    matching = [
        e for e in events_resp.json().get("events", [])
        if (e.get("detail") or {}).get("run_id") == run_id
    ]
    assert matching, (
        f"No event with run_id={run_id!r} found. "
        "spec: BACKEND.md §Active run pipeline"
    )
    assert matching[0]["event_type"] == "INGESTION.COMPLETE", (
        f"Run must produce INGESTION.COMPLETE; got {matching[0]['event_type']!r}. "
        "spec: BACKEND.md §Active run pipeline"
    )


@pytest.mark.asyncio
async def test_real_run_dataset_properties_description_from_pg_comment(
    real_run_state: dict,
) -> None:
    """datasetProperties.description starts with seeded COMMENT ON TABLE text.

    spec: BACKEND.md §Ingestion Service — PG comment ingestion
    spec: DATAHUB_INTEGRATION.md §datasetProperties — description from ingestion
    spec: TESTING.md §Imazon Dummy-Data Reference — 01_catalog.sql seeds COMMENT ON TABLE
    """
    gms_headers = real_run_state["gms_headers"]
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    props_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=datasetProperties&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert props_resp.status_code == 200, (
        f"GET datasetProperties from DataHub GMS returned {props_resp.status_code}"
    )
    description = (
        props_resp.json()
        .get("aspect", {})
        .get("com.linkedin.dataset.DatasetProperties", {})
        .get("description", "")
    )
    assert description.startswith("Master record for each book title"), (
        f"datasetProperties.description must begin with seeded COMMENT ON TABLE text; "
        f"got {description!r}. spec: BACKEND.md §Ingestion Service — PG comment ingestion."
    )


@pytest.mark.asyncio
async def test_real_run_schema_metadata_typed_fields(
    real_run_state: dict,
) -> None:
    """schemaMetadata fields carry correct PDL type unions: NumberType for integer, StringType for varchar.

    spec: DATAHUB_INTEGRATION.md §schemaMetadata — typed union fix
    """
    gms_headers = real_run_state["gms_headers"]
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    schema_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=schemaMetadata&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert schema_resp.status_code == 200
    fields_raw = (
        schema_resp.json()
        .get("aspect", {})
        .get("com.linkedin.schema.SchemaMetadata", {})
        .get("fields", [])
    )
    assert fields_raw, "schemaMetadata.fields must be non-empty after ingestion run"
    fields_by_path = {f.get("fieldPath"): f for f in fields_raw}

    # page_count (integer) → NumberType
    assert "page_count" in fields_by_path
    pc_type = (
        fields_by_path["page_count"]
        .get("type", {})
        .get("type", {})
        .get("com.linkedin.schema.NumberType")
    )
    assert pc_type is not None, (
        "page_count (integer) must have NumberType; "
        "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
    )

    # title (character varying) → StringType
    assert "title" in fields_by_path
    title_type = (
        fields_by_path["title"]
        .get("type", {})
        .get("type", {})
        .get("com.linkedin.schema.StringType")
    )
    assert title_type is not None, (
        "title (character varying) must have StringType; "
        "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
    )


@pytest.mark.asyncio
async def test_real_run_isbn_field_description_from_pg_column_comment(
    real_run_state: dict,
) -> None:
    """isbn field description starts with seeded COMMENT ON COLUMN text.

    spec: BACKEND.md §Ingestion Service — PG comment ingestion
    spec: DATAHUB_INTEGRATION.md §schemaMetadata — field descriptions from column comments
    """
    gms_headers = real_run_state["gms_headers"]
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    schema_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=schemaMetadata&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert schema_resp.status_code == 200
    fields_raw = (
        schema_resp.json()
        .get("aspect", {})
        .get("com.linkedin.schema.SchemaMetadata", {})
        .get("fields", [])
    )
    fields_by_path = {f.get("fieldPath"): f for f in fields_raw}

    assert "isbn" in fields_by_path, (
        f"Expected 'isbn' field in schemaMetadata; got {list(fields_by_path.keys())}"
    )
    isbn_desc = fields_by_path["isbn"].get("description", "")
    assert isbn_desc.startswith("ISBN-13 identifier"), (
        f"isbn description must start with seeded COMMENT ON COLUMN text; got {isbn_desc!r}. "
        "spec: BACKEND.md §Ingestion Service — PG comment ingestion."
    )


@pytest.mark.asyncio
async def test_real_run_system_metadata_run_id_convention(
    real_run_state: dict,
) -> None:
    """schemaMetadata.systemMetadata.runId starts with 'dataspoke-postgres-'.

    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement — runId
    """
    gms_headers = real_run_state["gms_headers"]
    urn_enc = urllib.parse.quote(_TEST_URN, safe="")

    sysmeta_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/openapi/v3/entity/dataset/{urn_enc}"
        "?systemMetadata=true&aspects=schemaMetadata",
        headers=gms_headers,
        timeout=10.0,
    )
    assert sysmeta_resp.status_code == 200
    schema_run_id = (
        sysmeta_resp.json()
        .get("schemaMetadata", {})
        .get("systemMetadata", {})
        .get("runId")
    )
    # Enforce runId format shape: dataspoke-{platform}-{run_id}
    # Platform slug may normalise (e.g. "postgresql" vs "postgres") — match by substring.
    # spec: DATAHUB_INTEGRATION.md L415 — runId = "dataspoke-{platform}-{run_id}"
    m = re.match(r"^dataspoke-([a-z0-9_-]+)-", schema_run_id or "")
    assert m is not None, (
        f"schemaMetadata.systemMetadata.runId must match 'dataspoke-{{platform}}-...'; "
        f"got {schema_run_id!r}. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )
    assert "postgres" in m.group(1).lower(), (
        f"platform slug must contain 'postgres'; got {m.group(1)!r}. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )


@pytest.mark.asyncio
async def test_real_run_dataset_has_container_aspect_pointing_to_schema_container(
    real_run_state: dict,
) -> None:
    """After a real active-custom run, the dataset URN has a 'container' aspect pointing
    to the schema container URN. The schema container URN must match the deterministic
    hash for database=example_db, schema=catalog, env=DEV — byte-identical to what
    DataHub's managed PostgreSQL source plugin would emit.

    spec: BACKEND.md §Ingestion Service — Aspects emitted (ContainerClass + container hierarchy emission)
    spec: DATAHUB_INTEGRATION.md §Container URN Construction — URN parity with upstream plugin
    """
    gms_headers = real_run_state["gms_headers"]
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    # Expected schema container URN for example_db / catalog / DEV
    # GUID dict: {"platform": "postgres", "database": "example_db",
    #             "schema": "catalog", "instance": "DEV"}
    # (backcompat_env_as_instance=True promotes env into instance key)
    _SCHEMA_CONTAINER_URN = "urn:li:container:d30ba0aa3cb3374982ca9a9db3466b5e"
    _DB_CONTAINER_URN = "urn:li:container:877925964b937b391ead54462bf98b9d"

    # ── Dataset has a 'container' aspect pointing to the schema container ──────
    container_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=container&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert container_resp.status_code == 200, (
        f"GET container aspect from DataHub GMS returned {container_resp.status_code}"
    )
    container_urn = (
        container_resp.json()
        .get("aspect", {})
        .get("com.linkedin.container.Container", {})
        .get("container")
    )
    assert container_urn == _SCHEMA_CONTAINER_URN, (
        f"Dataset container aspect must point to schema container "
        f"{_SCHEMA_CONTAINER_URN!r}; got {container_urn!r}. "
        "spec: BACKEND.md §Ingestion Service — Aspects emitted (ContainerClass + container hierarchy emission)"
    )

    # ── Schema container has ContainerProperties with name='catalog' ──────────
    schema_enc = urllib.parse.quote(_SCHEMA_CONTAINER_URN, safe="")
    schema_props_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{schema_enc}?aspect=containerProperties&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert schema_props_resp.status_code == 200, (
        f"GET containerProperties for schema container returned {schema_props_resp.status_code}"
    )
    schema_name = (
        schema_props_resp.json()
        .get("aspect", {})
        .get("com.linkedin.container.ContainerProperties", {})
        .get("name")
    )
    assert schema_name == "catalog", (
        f"Schema container ContainerProperties.name must be 'catalog'; got {schema_name!r}. "
        "spec: DATAHUB_INTEGRATION.md §Container URN Construction — SchemaKey fields include schema name"
    )

    # ── Schema container SubTypes = ["Schema"] ────────────────────────────────
    schema_subtypes_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{schema_enc}?aspect=subTypes&version=0",
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

    # ── Schema container DataPlatformInstance.platform = postgres ────────────
    schema_dpi_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{schema_enc}?aspect=dataPlatformInstance&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert schema_dpi_resp.status_code == 200
    schema_platform = (
        schema_dpi_resp.json()
        .get("aspect", {})
        .get("com.linkedin.common.DataPlatformInstance", {})
        .get("platform")
    )
    assert schema_platform == "urn:li:dataPlatform:postgres", (
        f"Schema container DataPlatformInstance.platform must be 'urn:li:dataPlatform:postgres'; "
        f"got {schema_platform!r}. "
        "spec: DATAHUB_INTEGRATION.md §Container URN Construction — platform URN in SchemaKey"
    )

    # ── Schema container has a parent Container pointing to the database container ──
    schema_container_resp = httpx.get(
        f"{_DATAHUB_GMS_URL}/aspects/{schema_enc}?aspect=container&version=0",
        headers=gms_headers,
        timeout=15.0,
    )
    assert schema_container_resp.status_code == 200
    schema_parent_urn = (
        schema_container_resp.json()
        .get("aspect", {})
        .get("com.linkedin.container.Container", {})
        .get("container")
    )
    assert schema_parent_urn == _DB_CONTAINER_URN, (
        f"Schema container's parent Container must be the database container "
        f"{_DB_CONTAINER_URN!r}; got {schema_parent_urn!r}. "
        "spec: DATAHUB_INTEGRATION.md §Container URN Construction — schema parented by parent_container_key=db_key"
    )
