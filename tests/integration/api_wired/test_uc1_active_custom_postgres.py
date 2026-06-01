"""UC1 Case 2 — ACTIVE_CUSTOM_MANAGED postgres source: end-to-end through public REST API.

DataSpoke owns the extraction for the catalog schema. The team creates the source via the
API; an Airflow tier DAG runs DataSpoke's postgres extractor on the cron schedule.

Steps mirror USE_CASE_en.md §UC1 Case 2:
  1. POST /spoke/ingestion/sources with ACTIVE_CUSTOM_MANAGED + catalog-only recipe
  2. Assert 201 + response body shape (mode, name, schedule, recipe with ${...} ref intact)
  3. Dry-run POST /sources/{id}/method/run {dry_run: true} → success, no datasets emitted
  4. Real run {dry_run: false} → success
  5. Assert catalog.* datasets present in DataHub (ES settle: up to 30s poll)
  6. GET /sources/{id}/datasets → origin='emitted' rows for catalog datasets
  7. GET /sources/{id}/event → INGESTION.COMPLETE event
  8. GET /spoke/common/data/{catalog_urn}/attr/ingestion → reverse-lookup returns this source
  9. Cleanup: DELETE /sources/{id}

The test skips cleanly when the dummy-data-pg K8s Secret is not provisioned in the cluster
(checked via GET /spoke/ingestion/secrets before the first mutation).

spec: USE_CASE_en.md §UC1 Case 2
spec: API.md §Ingestion (/spoke/ingestion/sources)
spec: feature/BACKEND.md §Ingestion Service §Active-custom run pipeline
spec: feature/SECRET_RESOLUTION.md §Reference discovery — skip guard
spec: TESTING.md §Api-Wired Integration Tests
"""

import asyncio
import os
import time
import urllib.parse
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db
from tests.integration.util.datahub import discover_catalog_tables

# ── Environment / credential references ──────────────────────────────────────

# The in-cluster hostname for the dummy-data postgres (resolvable inside the cluster).
_PG_HOST_PORT = os.environ.get(
    "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT",
    "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
)

# Secret reference: K8s Secret dataspoke-source-cred-dummy-data-pg, key 'password'.
# The <name> segment (dummy-data-pg) must be DNS-label-safe (hyphens, no underscores).
# The secret must be pre-created in the cluster; DataSpoke lists and resolves it at run time.
# spec: USE_CASE_en.md §UC1 Case 2 — password: '${dummy-data-pg__password}'
# spec: feature/SECRET_RESOLUTION.md §Name prefix policy — <name> DNS-label-safe
_SECRET_REF = "${dummy-data-pg__password}"
_SECRET_REF_BARE = "dummy-data-pg__password"  # the inside of ${...}

# F4: plaintext password for negative-secret assertion.
# Populated by the dev install from helm-charts/.env DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD;
# mirrors the value stored in the dataspoke-source-cred-dummy-data-pg K8s secret.
_PG_PLAINTEXT_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")

# ── Dummy-data module constants ────────────────────────────────────────────────
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])


@pytest_asyncio.fixture(autouse=True)
async def _clean_ingestion_sources() -> AsyncGenerator[None]:
    """Reset ingestion_source table before and after each test in this module.

    The test body stays REST-only; DB-touching setup/teardown lives in this fixture.
    spec: TESTING.md §Api-Wired Integration Tests — 'the test itself stays REST-only;
          setup/teardown fixtures may use util'.
    spec: feedback_reset_before_api_wired — reset before api-wired tests.
    """
    await dataspoke_db.reset_ingestion_sources()
    yield
    await dataspoke_db.reset_ingestion_sources()


async def _dummy_pg_secret_is_provisioned(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> bool:
    """Return True if the dummy-data-pg secret ref is listed by GET /spoke/ingestion/secrets.

    The test is REST-only and cannot read k8s directly; we probe the API's own
    secret-list endpoint as the skip guard.

    spec: feature/SECRET_RESOLUTION.md §Reference discovery (list flow)
    spec: TESTING.md §Api-Wired Integration Tests — REST-only guard
    """
    resp = await api_client.get("/api/v1/spoke/ingestion/secrets", headers=admin_headers)
    if resp.status_code != 200:
        return False
    return any(
        item.get("ref") == _SECRET_REF_BARE
        for item in resp.json().get("secrets", [])
    )

# ── Catalog URNs (resolvable after DUMMY_DATA_DATAHUB_SCHEMAS seed) ───────────
# spec: project_datahub_resolvable_urns_catalog_only — only catalog.* seeded into DataHub
_CATALOG_TITLE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_CATALOG_EDITIONS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
)
_CATALOG_TITLE_ENCODED = urllib.parse.quote(_CATALOG_TITLE_URN, safe="")


@pytest.mark.asyncio
async def test_uc1_active_custom_postgres(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC1 Case 2 — DataSpoke owns extraction for the catalog schema.

    Narrative from USE_CASE_en.md §UC1 Case 2:
      "DataSpoke is the ingestor. An Airflow tier DAG runs DataSpoke's postgres
       extractor on the schedule. The recipe is DataHub-compatible; the password
       references a k8s secret via ${name__key}."

    Skips when the dataspoke-source-cred-dummy-data-pg K8s Secret is absent from the cluster
    (probed via GET /spoke/ingestion/secrets) so the test fails cleanly rather than with a
    confusing SECRET_REF_NOT_FOUND error.

    spec: USE_CASE_en.md §UC1 Case 2
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (ACTIVE_CUSTOM_MANAGED)
    spec: feature/BACKEND.md §Ingestion Service §Active-custom run pipeline
    spec: feature/SECRET_RESOLUTION.md §Reference discovery — skip guard via list endpoint
    """
    # Skip-guard: probe the API's own secret-list endpoint (REST-only; cannot read k8s directly).
    # spec: feature/SECRET_RESOLUTION.md §Reference discovery (list flow)
    if not await _dummy_pg_secret_is_provisioned(api_client, admin_headers):
        pytest.skip(
            f"Secret ref '{_SECRET_REF_BARE}' not listed by GET /spoke/ingestion/secrets. "
            "Pre-create K8s Secret 'dataspoke-source-cred-dummy-data-pg' with key 'password' "
            "to run the real-run UC1 Case 2 test. "
            "spec: feature/SECRET_RESOLUTION.md §Admin authoring guide."
        )

    source_id: str | None = None

    try:
        # ── Step 1: POST source — ACTIVE_CUSTOM_MANAGED with UC1 Case 2 recipe ──
        # spec: USE_CASE_en.md §UC1 Case 2 — exact recipe YAML → JSON body:
        #   mode: ACTIVE_CUSTOM_MANAGED, schedule: '0 0 * * *',
        #   recipe.source.type: postgres, schema_pattern.allow: ['^catalog$'],
        #   password: '${dummy-data-pg__password}'
        create_resp = await api_client.post(
            "/api/v1/spoke/ingestion/sources",
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": "dummy postgres example_db in catalog schema",
                "schedule": "0 0 * * *",
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "host_port": _PG_HOST_PORT,
                            "database": "example_db",
                            "username": "postgres",
                            "password": _SECRET_REF,
                            "env": "DEV",
                            "schema_pattern": {
                                "allow": ["^catalog$"]
                            },
                        },
                    }
                },
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/ingestion/sources expected 201, got "
            f"{create_resp.status_code}: {create_resp.text}"
        )

        # ── Step 2: Assert 201 body shape ──────────────────────────────────────
        # spec: API.md §Ingestion §Source body shape — response fields
        body = create_resp.json()
        assert "id" in body, "Response must include 'id'"
        source_id = body["id"]
        assert body["mode"] == "ACTIVE_CUSTOM_MANAGED", (
            f"mode must be 'ACTIVE_CUSTOM_MANAGED'; got {body['mode']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 2"
        )
        assert body["name"] == "dummy postgres example_db in catalog schema"
        assert body["schedule"] == "0 0 * * *", (
            f"schedule cron must be '0 0 * * *'; got {body['schedule']!r}. "
            "spec: API.md §Ingestion §Source body shape — schedule is the cron string"
        )
        # spec: API.md §Ingestion §Source body shape — no schedule_tier on the wire
        assert "schedule_tier" not in body, (
            "schedule_tier must NOT appear in the API response body. "
            "spec: API.md §Ingestion §Source body shape"
        )
        # Secret reference must be preserved verbatim (masked form — not plaintext).
        # spec: API.md §Ingestion §Source body shape — ${name__key} refs returned verbatim on GET
        recipe_password = (
            body.get("recipe", {})
            .get("source", {})
            .get("config", {})
            .get("password")
        )
        assert recipe_password == _SECRET_REF, (
            f"recipe.source.config.password must be the ${{name__key}} reference verbatim; "
            f"got {recipe_password!r}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — password stored as masked ref"
        )
        # F4: negative check — the API must never return plaintext credentials.
        # The K8s secret dataspoke-source-cred-dummy-data-pg holds the same value as
        # DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD in helm-charts/.env.
        # spec: API.md §Ingestion §Source body shape — secret refs never expanded in responses
        assert _PG_PLAINTEXT_PASSWORD, (
            "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD not set; "
            "cannot verify plaintext is absent from the API response. "
            "Source helm-charts/.env before running this test."
        )
        create_resp_text = create_resp.text
        assert _PG_PLAINTEXT_PASSWORD not in create_resp_text, (
            "API response must not contain the plaintext postgres password. "
            "spec: API.md §Ingestion §Source body shape — credentials never returned in plaintext"
        )
        assert "status" in body
        assert "created_at" in body
        assert "updated_at" in body

        source_run_url = f"/api/v1/spoke/ingestion/sources/{source_id}/method/run"
        source_datasets_url = f"/api/v1/spoke/ingestion/sources/{source_id}/datasets"
        source_event_url = f"/api/v1/spoke/ingestion/sources/{source_id}/event"

        # ── Step 3: Dry-run — connection check, no DataHub emission ───────────
        # spec: USE_CASE_en.md §UC1 Case 2 — "POST .../method/run {dry_run: true}"
        # spec: feature/BACKEND.md §Active-custom run pipeline — dry_run skips aspect emission
        dry_run_resp = await api_client.post(
            source_run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_run_resp.status_code == 200, (
            f"Dry-run expected 200, got {dry_run_resp.status_code}: {dry_run_resp.text}"
        )
        dry_run_body = dry_run_resp.json()
        assert "run_id" in dry_run_body, "Dry-run response must carry run_id"
        assert "status" in dry_run_body, "Dry-run response must carry status"
        assert dry_run_body["run_id"], "run_id must be non-empty"
        _fail_tail = {"fail", "failed", "failure", "error", "errored"}
        assert dry_run_body["status"].lower() not in _fail_tail, (
            f"Dry-run returned fail status {dry_run_body['status']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — dry_run exercises connectivity"
        )
        # Dry-run must not emit datasets — detail.emitted_urns_count = 0
        dry_detail = dry_run_body.get("detail", {})
        assert dry_detail.get("dry_run") is True, "detail.dry_run must be true for dry runs"
        emitted = dry_detail.get("emitted_urns_count", 0)
        assert emitted == 0, (
            f"Dry-run must not emit any datasets (emitted_urns_count=0); got {emitted}. "
            "spec: feature/BACKEND.md §Active-custom run pipeline"
            " — aspect emission skipped on dry_run"
        )

        # ── Step 4: Real run — emit dataset aspects to DataHub ────────────────
        # spec: USE_CASE_en.md §UC1 Case 2 — "A real run emits dataset aspects + a
        # DataProcessInstance, and records emitted URNs as the authoritative mapping"
        real_run_resp = await api_client.post(
            source_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert real_run_resp.status_code == 200, (
            f"Real run expected 200, got {real_run_resp.status_code}: {real_run_resp.text}"
        )
        real_run_body = real_run_resp.json()
        assert "run_id" in real_run_body
        assert "status" in real_run_body
        run_id = real_run_body["run_id"]
        assert real_run_body["status"].lower() not in _fail_tail, (
            f"Real run returned fail status {real_run_body['status']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — catalog schema must be reachable"
        )
        real_detail = real_run_body.get("detail", {})
        assert real_detail.get("dry_run") is False
        # At least 2 catalog datasets (title_master + editions) must have been emitted.
        real_emitted = real_detail.get("emitted_urns_count", 0)
        assert real_emitted >= 2, (
            f"Real run must emit at least 2 catalog datasets "
            f"(title_master + editions); emitted={real_emitted}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — catalog schema produces multiple datasets"
        )

        # ── Step 5: GET /sources/{id}/datasets → origin='emitted' rows ────────
        # spec: API.md §Ingestion — GET /sources/{id}/datasets returns mapping rows
        # spec: feature/BACKEND.md §Active-custom run pipeline — emitted URNs recorded
        #       into ingestion_source_dataset with origin='emitted'
        #
        # F1: discover_catalog_tables() asserts the dummy-postgres seed is non-empty
        # before we compute expected_urns; prevents vacuous set-equality on both sides
        # empty (environment failure masked as a pass).
        # spec: project_datahub_resolvable_urns_catalog_only — catalog.* always seeded
        expected_urns = await discover_catalog_tables()
        assert expected_urns, "seed discovery returned no catalog tables"
        #
        # F2 (SYNC): _run_inner() in src/backend/ingestion/service.py calls
        # _upsert_dataset_mappings() and awaits its db.commit() BEFORE returning
        # IngestionRunResult.  The mapping rows are therefore committed synchronously
        # before run() returns 200, so a single-shot read is correct here — no poll needed.
        datasets_resp = await api_client.get(source_datasets_url, headers=admin_headers)
        assert datasets_resp.status_code == 200, (
            f"GET /sources/{source_id}/datasets expected 200, "
            f"got {datasets_resp.status_code}: {datasets_resp.text}"
        )
        datasets_body = datasets_resp.json()
        assert "datasets" in datasets_body
        assert "total_count" in datasets_body
        dataset_urns = {d["dataset_urn"] for d in datasets_body["datasets"]}
        # F2: non-empty floor — if mapping rows are absent the run silently produced nothing.
        assert dataset_urns, (
            "no emitted mapping rows after run — "
            "_upsert_dataset_mappings() should have committed rows synchronously before"
            " run() returned. spec: feature/BACKEND.md §Active-custom run pipeline"
        )
        # At least 2 catalog datasets must appear in the mapping.
        assert len(dataset_urns) >= 2, (
            f"Source mapping must list at least 2 catalog datasets; "
            f"got {sorted(dataset_urns)}. "
            "spec: API.md §Ingestion — GET /sources/{id}/datasets"
        )
        # Every discovered catalog table must be mapped (subset check; allows extras).
        assert expected_urns.issubset(dataset_urns), (
            f"Not all catalog tables appeared in the source mapping. "
            f"expected subset: {sorted(expected_urns)}; got: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — all catalog datasets emitted by the run"
        )
        # All emitted rows must carry origin='emitted' (authoritative mapping).
        # spec: feature/BACKEND.md §Active-custom run pipeline — origin=emitted for real runs
        emitted_origins = {d["origin"] for d in datasets_body["datasets"]}
        assert "emitted" in emitted_origins, (
            f"At least one dataset must have origin='emitted' after a real run; "
            f"got origins={emitted_origins}. "
            "spec: feature/BACKEND.md §Ingestion Service §Source→dataset mapping"
        )
        for d in datasets_body["datasets"]:
            assert "first_seen_at" in d
            assert "last_seen_at" in d

        # ── Step 6: GET /sources/{id}/event → INGESTION.COMPLETE ─────────────
        # Poll until the INGESTION.COMPLETE event for this run_id appears.
        # spec: feature/BACKEND.md §Active-custom run pipeline — INGESTION.COMPLETE event recorded
        # spec: USE_CASE_en.md §UC1 — INGESTION.COMPLETE carries status='success'
        event_body: dict = {}
        found_complete = False
        deadline = time.time() + 30.0
        while time.time() < deadline:
            event_resp = await api_client.get(source_event_url, headers=admin_headers)
            assert event_resp.status_code == 200
            event_body = event_resp.json()
            for evt in event_body.get("events", []):
                detail = evt.get("detail", {}) or {}
                if detail.get("run_id") == run_id and evt.get("event_type") == "INGESTION.COMPLETE":
                    found_complete = True
                    # spec: USE_CASE_en.md §UC1 — INGESTION.COMPLETE carries status='success'
                    assert evt.get("status") == "success", (
                        f"INGESTION.COMPLETE event must carry status='success'; "
                        f"got {evt.get('status')!r}. "
                        "spec: USE_CASE_en.md §UC1"
                    )
                    break
            if found_complete:
                break
            await asyncio.sleep(1.0)

        assert found_complete, (
            f"Expected INGESTION.COMPLETE event with run_id={run_id!r} within 30s. "
            f"Events: {event_body.get('events', [])}. "
            "spec: feature/BACKEND.md §Active-custom run pipeline — event recorded on success"
        )

        # ── Step 7: Reverse-lookup — GET /data/{urn}/attr/ingestion ──────────
        # spec: API.md §Data Resource — GET /spoke/common/data/{urn}/attr/ingestion
        # spec: USE_CASE_en.md §UC1 API Mapping — reverse-lookup: the source covering a
        #       dataset, its mode, latest run
        #
        # NOTE: The DataHub ES index may lag up to ~2-3 min after the run seed.
        # The dataset_registry is populated at source-create time; the reverse-lookup
        # queries ingestion_source_dataset which is populated by the run.
        # We use a bounded poll to allow the reverse-lookup to populate.
        # spec: project_es_indexing_lag_after_reset_seed — allow settle time
        reverse_body: dict = {}
        found_reverse = False
        deadline = time.time() + 30.0
        while time.time() < deadline:
            reverse_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{_CATALOG_TITLE_ENCODED}/attr/ingestion",
                headers=admin_headers,
            )
            assert reverse_resp.status_code == 200, (
                f"GET /data/{{urn}}/attr/ingestion expected 200, "
                f"got {reverse_resp.status_code}: {reverse_resp.text}"
            )
            reverse_body = reverse_resp.json()
            if reverse_body.get("source_id") == source_id:
                found_reverse = True
                break
            await asyncio.sleep(1.0)

        assert found_reverse, (
            f"Reverse-lookup for {_CATALOG_TITLE_URN!r} must return source_id={source_id!r}; "
            f"got {reverse_body}. "
            "spec: USE_CASE_en.md §UC1 API Mapping — reverse-lookup returns owning source"
        )
        assert reverse_body.get("mode") == "ACTIVE_CUSTOM_MANAGED", (
            f"Reverse-lookup mode must be 'ACTIVE_CUSTOM_MANAGED'; "
            f"got {reverse_body.get('mode')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping"
        )
        assert reverse_body.get("dataset_urn") == _CATALOG_TITLE_URN
        # latest_run must reflect the real run we just executed.
        latest_run = reverse_body.get("latest_run")
        assert latest_run is not None, (
            "Reverse-lookup must include latest_run after a real run. "
            "spec: API.md §Ingestion — IngestionReverseLookupResponse.latest_run"
        )
        assert latest_run.get("status") == "success", (
            f"latest_run.status must be 'success'; got {latest_run.get('status')!r}"
        )

    finally:
        # ── Cleanup: DELETE the source (cascades ingestion_source_dataset) ───
        if source_id is not None:
            await api_client.delete(
                f"/api/v1/spoke/ingestion/sources/{source_id}",
                headers=admin_headers,
            )
