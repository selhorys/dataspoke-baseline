"""Spot tests for Ingestion Control endpoints.

Concerns covered:
- GET /spoke/common/ingestion — list configs (paginated envelope)
- GET /data/{urn}/attr/ingestion/conf — 404 for unknown URN
- PUT /data/{urn}/attr/ingestion/conf — create config (201)
- PATCH /data/{urn}/attr/ingestion/conf — partial update
- DELETE /data/{urn}/attr/ingestion/conf — remove config (204)
- POST /data/{urn}/method/ingestion/run — dry_run=true triggers without writing
- GET /data/{urn}/event/ingestion — event list returns paginated envelope
- PG comment ingestion — table/column COMMENT ON statements flow through to DataHub aspects
- Typed schema field types — extractor emits SchemaFieldDataTypeClass wrappers, not bare strings
"""
# spec: API.md §Standard Envelope
# spec: BACKEND.md §Ingestion Service
# spec: DATAHUB_INTEGRATION.md §datasetProperties, §schemaMetadata

import asyncio
import os
import urllib.parse
import uuid

import httpx
import pytest

_FAIL_TAIL: frozenset[str] = frozenset({"fail", "failed", "failure", "error", "errored"})

# Dummy-data Postgres: spec/TESTING.md L312-313 — example_db on the dev-env host.
_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")
_VAULT_NAME = "dataspoke-source-cred-spot-pg"
_VAULT_KEY = "password"

# Use a fixed test URN that we know won't conflict with Imazon seed data.
# This dataset is registered in DataHub during the module's DUMMY_DATA_DATAHUB_SCHEMAS reset.
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# Second test URN for seeding N>1 configs in pagination test
_TEST_URN_2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_ENCODED_URN_2 = urllib.parse.quote(_TEST_URN_2, safe="")


@pytest.mark.asyncio
async def test_ingestion_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ingestion returns a paginated collection envelope.

    Seeds N=2 configs, verifies total_count >= N and that limit=1 trims the
    result page to exactly 1 item while total_count still reflects the full set.

    spec: API.md §Standard Envelope — paginated response must carry
    configs[], total_count, offset, limit.
    spec: BACKEND.md §Ingestion Service — list_configs paginates ingestion_configs rows.
    """
    # spec: API.md §Standard Envelope
    _base_conf_1 = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    _base_conf_2 = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/ingestion/conf"

    # Seed two distinct ingestion configs so the list has at least N=2 entries
    _common_payload = {
        "mode": "active-custom",
        "platform": "postgres",
        "locator": {"host": _PG_HOST, "port": _PG_PORT},
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
    }
    await api_client.put(
        _base_conf_1,
        headers=admin_headers,
        json={
            **_common_payload,
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
        },
    )
    await api_client.put(
        _base_conf_2,
        headers=admin_headers,
        json={
            **_common_payload,
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "editions"},
        },
    )

    # List with limit=10 — should show both seeded configs
    resp_all = await api_client.get(
        "/api/v1/spoke/common/ingestion?offset=0&limit=10",
        headers=admin_headers,
    )
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    # spec: API.md §Standard Envelope — required envelope keys
    assert "configs" in body_all
    assert "offset" in body_all
    assert "limit" in body_all
    assert "total_count" in body_all
    assert isinstance(body_all["configs"], list)
    # total_count must reflect ALL configs, not just the current page
    assert body_all["total_count"] >= 2, (
        f"Expected total_count >= 2 after seeding 2 configs, got {body_all['total_count']}"
    )

    # List with limit=1 — page must be trimmed to exactly 1 item
    resp_paged = await api_client.get(
        "/api/v1/spoke/common/ingestion?offset=0&limit=1",
        headers=admin_headers,
    )
    assert resp_paged.status_code == 200
    body_paged = resp_paged.json()
    assert len(body_paged["configs"]) == min(2, 1), (
        f"Expected page size 1 (limit=1), got {len(body_paged['configs'])}"
    )
    # total_count must still reflect the full count despite the small page
    assert body_paged["total_count"] >= 2

    # Cleanup
    await api_client.delete(_base_conf_1, headers=admin_headers)
    await api_client.delete(_base_conf_2, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET ingestion conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/ingestion/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_conf_put_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT creates ingestion config (201), PATCH updates it, DELETE removes it (204)."""
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
    # spec: SECRET_RESOLUTION.md §Data Model — persisted/echoed shape is reference-only
    assert "password" not in put_body["auth"], (
        f"plaintext password leaked into PUT response: {put_body['auth']}"
    )
    assert "force_overwrite" not in put_body["auth"]["secret_ref"], (
        f"transient force_overwrite leaked into PUT response: {put_body['auth']}"
    )
    assert put_body["auth"]["secret_ref"]["name"] == _VAULT_NAME
    assert put_body["auth"]["secret_ref"]["key"] == _VAULT_KEY

    # PATCH — disable (partial update)
    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"is_enabled": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["is_enabled"] is False

    # DELETE — remove
    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    # Verify gone — subsequent GET returns 404
    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/ingestion/run with dry_run=true returns run envelope without writing."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/ingestion/run"

    # Ensure config exists before run
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

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )

    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert "run_id" in run_body
    assert "status" in run_body
    assert run_body["status"].lower() not in _FAIL_TAIL, (
        f"run unexpectedly returned fail-tail status {run_body['status']!r} — "
        "secret resolution or downstream connectivity may be broken"
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/ingestion returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

    # Create config so events endpoint is accessible
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

    events_resp = await api_client.get(base_events, headers=admin_headers)
    assert events_resp.status_code == 200
    body = events_resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_put_active_custom_daily_workflow_dag_id(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT active-custom + schedule_tier='daily' surfaces workflow_dag_id in the response.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — 'Airflow DAG ID of the assigned
    periodic DAG (active-custom mode only)'. Tier DAG IDs follow the pattern
    ingestion-active-{tier}.
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
    put_body = put_resp.json()
    assert put_body["workflow_dag_id"] == "ingestion-active-daily", (
        f"active-custom + daily must surface workflow_dag_id='ingestion-active-daily'; "
        f"got {put_body.get('workflow_dag_id')!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_put_secret_collision_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Second vault PUT against same (name, key) without force_overwrite → 422.

    spec: SECRET_RESOLUTION.md §Validation matrix row 3 / §Vault-write flow step 2
    spec: SECRET_RESOLUTION.md §Error taxonomy — SecretCollision → 422 INVALID_PARAMETER
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    collision_secret = f"dataspoke-source-cred-spot-collision-{uuid.uuid4().hex[:8]}"

    # First PUT: vault writes the secret. force_overwrite=True for idempotency
    # if the test re-runs against an existing secret.
    put1 = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": collision_secret,
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
        },
    )
    assert put1.status_code in (200, 201), put1.text

    try:
        # Second PUT against the same (name, key) without force_overwrite
        put2 = await api_client.put(
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
                        "name": collision_secret,
                        "key": "password",
                        # force_overwrite omitted → defaults to False
                    },
                },
                "is_enabled": False,
            },
        )
        assert put2.status_code == 422, (
            f"second PUT without force_overwrite expected 422, got {put2.status_code}: {put2.text}"
        )
    finally:
        # Cleanup the conf row; the underlying k8s Secret persists per spec.
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_put_invalid_secret_ref_prefix_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """secret_ref.name without dataspoke-source-cred- prefix → 422 SecretRefNameForbidden.

    spec: SECRET_RESOLUTION.md §Name prefix policy
    spec: SECRET_RESOLUTION.md §Validation matrix last row
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": "external-team-pg-secret",  # missing required prefix
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
        },
    )
    assert resp.status_code == 422, (
        f"PUT with non-prefix secret_ref.name expected 422, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_ingestion_conf_put_reference_path_to_existing_secret(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Reference path: vault first, then PUT a different conf with no password
    pointing at the same Secret — verify path succeeds.

    spec: SECRET_RESOLUTION.md §Validation matrix row 4
    spec: SECRET_RESOLUTION.md §Reference-path verify flow
    """
    ref_secret = f"dataspoke-source-cred-spot-ref-{uuid.uuid4().hex[:8]}"
    base_conf_1 = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_conf_2 = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/ingestion/conf"

    # Vault path on URN 1 to establish the Secret in dataspoke-01
    vault_resp = await api_client.put(
        base_conf_1,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {"name": ref_secret, "key": "password", "force_overwrite": True},
            },
            "is_enabled": False,
        },
    )
    assert vault_resp.status_code in (200, 201), vault_resp.text

    try:
        # Reference path on URN 2 — no password, point at the just-vaulted Secret
        ref_resp = await api_client.put(
            base_conf_2,
            headers=admin_headers,
            json={
                "mode": "active-custom",
                "platform": "postgres",
                "locator": {"host": _PG_HOST, "port": _PG_PORT},
                "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "editions"},
                "auth": {
                    "username": _PG_USER,
                    "secret_ref": {"name": ref_secret, "key": "password"},
                },
                "is_enabled": False,
            },
        )
        assert ref_resp.status_code in (200, 201), ref_resp.text
        ref_body = ref_resp.json()
        assert ref_body["auth"]["secret_ref"]["name"] == ref_secret
        assert "password" not in ref_body["auth"], (
            f"reference-path response leaked password key: {ref_body['auth']}"
        )

        # Reference to a NON-existent key on the same Secret should 422
        bad_key_resp = await api_client.put(
            base_conf_2,
            headers=admin_headers,
            json={
                "mode": "active-custom",
                "platform": "postgres",
                "locator": {"host": _PG_HOST, "port": _PG_PORT},
                "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "editions"},
                "auth": {
                    "username": _PG_USER,
                    "secret_ref": {"name": ref_secret, "key": "nonexistent-key"},
                },
                "is_enabled": False,
            },
        )
        assert bad_key_resp.status_code == 422, (
            f"reference path to nonexistent key expected 422, got {bad_key_resp.status_code}"
        )
    finally:
        await api_client.delete(base_conf_1, headers=admin_headers)
        await api_client.delete(base_conf_2, headers=admin_headers)


# ── PG comment ingestion + typed schema field types ───────────────────────────
#
# Seed data: tests/integration/util/fixtures/sql/01_catalog.sql has
#   COMMENT ON TABLE catalog.title_master IS 'Master record for each book title — ...'
#   COMMENT ON COLUMN catalog.title_master.isbn IS 'ISBN-13 identifier ...'
#   COMMENT ON COLUMN catalog.title_master.page_count IS '...'   (integer column)
#   COMMENT ON COLUMN catalog.title_master.title IS '...'        (character varying column)
#
# These assertions verify both features end-to-end via a real DataHub GMS query.
# Reset is handled by the module-level DUMMY_DATA_DATAHUB_SCHEMAS constant above.

_DATAHUB_GMS_URL = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "")
_DATAHUB_TOKEN = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

_ACTIVE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ACTIVE_ENCODED = urllib.parse.quote(_ACTIVE_URN, safe="")
_VAULT_NAME_SPOT_TYPED = "dataspoke-source-cred-spot-typed-schema"
_VAULT_KEY_SPOT_TYPED = "password"


@pytest.mark.asyncio
async def test_ingestion_pg_comments_and_typed_fields_in_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Active-custom run for catalog.title_master emits PG comments and typed field
    types to DataHub.

    Two concerns verified together (one real run is expensive):
    1. PG comment ingestion — COMMENT ON TABLE flows to datasetProperties.description;
       COMMENT ON COLUMN isbn flows to schemaMetadata fields[isbn].description.
    2. Typed PDL union fix — at least one NumberType field (page_count: integer)
       and one StringType field (title: character varying) are present, proving
       the extractor emits SchemaFieldDataTypeClass wrappers rather than bare
       strings that caused every column to appear as 'Struct' in DataHub.

    spec: BACKEND.md §Ingestion Service — PG comment ingestion.
    spec: BACKEND.md §Custom Ingestor Authoring Contract — SchemaMetadata aspect.
    spec: DATAHUB_INTEGRATION.md §datasetProperties — description from ingestion.
    spec: DATAHUB_INTEGRATION.md §schemaMetadata — fields[].type typed union fix.
    spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master (UC1 primary).
    """
    if not _DATAHUB_GMS_URL:
        pytest.skip("DATASPOKE_DATAHUB_GMS_URL not set; skipping DataHub aspect assertions")

    base_conf = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/method/ingestion/run"
    base_events = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/event/ingestion"

    try:
        # ── PUT active-custom config and enable ───────────────────────────────
        put_resp = await api_client.put(
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
                        "name": _VAULT_NAME_SPOT_TYPED,
                        "key": _VAULT_KEY_SPOT_TYPED,
                        "force_overwrite": True,
                    },
                },
                "is_enabled": True,
                "schedule_tier": "daily",
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT active-custom conf failed: {put_resp.status_code} {put_resp.text}"
        )

        # ── Trigger real run (dry_run=false) ──────────────────────────────────
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 200, (
            f"POST ingestion run failed: {run_resp.status_code} {run_resp.text}"
        )
        run_id = run_resp.json()["run_id"]

        # ── Poll until INGESTION.COMPLETE event appears (cap 30s) ─────────────
        # spec: BACKEND.md §Active run pipeline — INGESTION.COMPLETE event recorded
        # spec: feedback_no_increase_timeout — bounded polls, not sleep.
        found_complete = False
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            events_resp = await api_client.get(
                base_events + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_resp.status_code == 200
            for evt in events_resp.json().get("events", []):
                detail = evt.get("detail") or {}
                if detail.get("run_id") == run_id:
                    assert evt["event_type"] == "INGESTION.COMPLETE", (
                        f"Run must produce INGESTION.COMPLETE; got {evt['event_type']!r}"
                    )
                    found_complete = True
                    break
            if found_complete:
                break
            await asyncio.sleep(1.0)

        assert found_complete, (
            f"INGESTION.COMPLETE event with run_id={run_id!r} not found within 30s. "
            "spec: BACKEND.md §Active run pipeline"
        )

        # ── Query DataHub for datasetProperties aspect ────────────────────────
        # spec: DATAHUB_INTEGRATION.md §datasetProperties — read via GET /aspects?urn=...
        gms_headers: dict[str, str] = {}
        if _DATAHUB_TOKEN:
            gms_headers["Authorization"] = f"Bearer {_DATAHUB_TOKEN}"

        encoded_urn = urllib.parse.quote(_ACTIVE_URN, safe="")
        props_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=datasetProperties&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert props_resp.status_code == 200, (
            f"GET datasetProperties from DataHub GMS returned {props_resp.status_code}: "
            f"{props_resp.text}"
        )
        props_body = props_resp.json()
        description = (
            props_body.get("aspect", {})
            .get("com.linkedin.dataset.DatasetProperties", {})
            .get("description", "")
        )
        assert description.startswith("Master record for each book title"), (
            f"datasetProperties.description must begin with seeded COMMENT ON TABLE text; "
            f"got {description!r}. "
            "spec: BACKEND.md §Ingestion Service — PG comment ingestion. "
            "If this fails after a fresh run, the old aspect may still be cached in DataHub "
            "from the pre-comment seed — run --reset-all before retrying."
        )

        # ── Query DataHub for schemaMetadata aspect ───────────────────────────
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata — read via GET /aspects?urn=...
        schema_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/aspects/{encoded_urn}?aspect=schemaMetadata&version=0",
            headers=gms_headers,
            timeout=15.0,
        )
        assert schema_resp.status_code == 200, (
            f"GET schemaMetadata from DataHub GMS returned {schema_resp.status_code}: "
            f"{schema_resp.text}"
        )
        schema_body = schema_resp.json()
        fields_raw = (
            schema_body.get("aspect", {})
            .get("com.linkedin.schema.SchemaMetadata", {})
            .get("fields", [])
        )
        assert fields_raw, "schemaMetadata.fields must be non-empty after ingestion run"

        # ── Assert isbn field description (PG column comment) ─────────────────
        # spec: BACKEND.md §Ingestion Service — column comments → field descriptions.
        # 01_catalog.sql: COMMENT ON COLUMN catalog.title_master.isbn IS 'ISBN-13 identifier...'
        fields_by_path = {f.get("fieldPath"): f for f in fields_raw}
        assert "isbn" in fields_by_path, (
            f"Expected 'isbn' field in schemaMetadata; got {list(fields_by_path.keys())}"
        )
        isbn_field = fields_by_path["isbn"]
        isbn_description = isbn_field.get("description", "")
        assert isbn_description.startswith("ISBN-13 identifier"), (
            f"isbn field description must start with seeded COMMENT ON COLUMN text; "
            f"got {isbn_description!r}. "
            "spec: BACKEND.md §Ingestion Service — PG comment ingestion."
        )

        # ── Assert typed field types (NumberType for page_count, StringType for title)
        # spec: DATAHUB_INTEGRATION.md §schemaMetadata — fields[].type typed union fix.
        # page_count is INTEGER → must surface as NumberType (not Struct/RecordType).
        assert "page_count" in fields_by_path, (
            f"Expected 'page_count' field; got {list(fields_by_path.keys())}"
        )
        page_count_field = fields_by_path["page_count"]
        page_count_type = (
            page_count_field.get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.NumberType")
        )
        assert page_count_type is not None, (
            f"page_count (integer) must have type.type=NumberType; "
            f"got type={page_count_field.get('type')!r}. "
            "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
        )

        # title is character varying → must surface as StringType.
        assert "title" in fields_by_path, (
            f"Expected 'title' field; got {list(fields_by_path.keys())}"
        )
        title_field = fields_by_path["title"]
        title_type = (
            title_field.get("type", {})
            .get("type", {})
            .get("com.linkedin.schema.StringType")
        )
        assert title_type is not None, (
            f"title (character varying) must have type.type=StringType; "
            f"got type={title_field.get('type')!r}. "
            "spec: DATAHUB_INTEGRATION.md §schemaMetadata typed union fix."
        )

    finally:
        # Disable and clean up the conf row.
        # Do NOT soft-delete the dataset in DataHub; --reset-all handles teardown.
        await api_client.delete(base_conf, headers=admin_headers)
