"""UC1 Case 2 — ACTIVE_CUSTOM_MANAGED postgres source: end-to-end through public REST API.

DataSpoke owns the extraction for the catalog schema. The team creates the source via the
API; an Airflow tier DAG runs DataSpoke's postgres extractor on the cron schedule.

Steps mirror USE_CASE_en.md §UC1 Case 2:
  1. POST /spoke/ingestion/sources with ACTIVE_CUSTOM_MANAGED + catalog-only recipe
  2. Assert 201 + response body shape (mode, name, schedule, recipe with ${...} ref intact)
  3. Dry-run POST /sources/{id}/method/run?dry_run=true → success, no datasets emitted
  4. Real run (no dry_run param) → success
  5. Assert catalog.* datasets present in DataHub (ES settle: up to 30s poll)
  6. GET /sources/{id}/datasets → derivation='emitted' rows for catalog datasets
  7. GET /sources/{id}/event → INGESTION.COMPLETE event
  8. GET /spoke/common/data/{catalog_urn}/attr/ingestion → reverse-lookup returns this source
  8b. lastIngested observation + per-dataset timeline — the run stamped a non-default
      systemMetadata.runId, so DataHub can now date both emitted catalog tables; a sweep
      books one last_ingested_observation each, and title_master's timeline shows its own
      observation plus the run-level rows but NOT the editions sibling's observation
  9. Cleanup: DELETE /sources/{id}

The K8s Secret dataspoke-source-cred-dummy-data-pg is provisioned in the setup fixture
(create-if-absent, idempotent) using DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD from
helm-charts/.env.dev. The test skips cleanly only if that env var is unset.

spec: USE_CASE_en.md §UC1 Case 2
spec: API.md §Ingestion (/spoke/ingestion/sources)
spec: feature/BACKEND.md §Ingestion Service §Active-custom run pipeline
spec: feature/SECRET_RESOLUTION.md §Reference-only model — out-of-band secret authoring
spec: TESTING.md §Api-Wired Integration Tests — secret-mutating setup in fixtures
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

# In-cluster cluster-DNS address of the dummy-data postgres (resolvable inside
# the cluster; mode-independent — the recipe is consumed by the API pod / DataHub
# executor IN-CLUSTER). Populated by install.sh; required (no default) so an unset
# env fails loud rather than guessing a namespace.
_PG_HOST_PORT = os.environ["DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT"]

# Secret reference: K8s Secret dataspoke-source-cred-dummy-data-pg, key 'password'.
# The <name> segment (dummy-data-pg) must be DNS-label-safe (hyphens, no underscores).
# DataSpoke lists and resolves it at run time; the test fixture provisions it beforehand.
# spec: USE_CASE_en.md §UC1 Case 2 — password: '${dummy-data-pg__password}'
# spec: feature/SECRET_RESOLUTION.md §Name prefix policy — <name> DNS-label-safe
_SECRET_REF = "${dummy-data-pg__password}"
_SECRET_REF_BARE = "dummy-data-pg__password"  # the inside of ${...}

# F4: plaintext password for negative-secret assertion.
# Populated by the dev install from helm-charts/.env.dev DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD;
# mirrors the value stored in the dataspoke-source-cred-dummy-data-pg K8s secret.
_PG_PLAINTEXT_PASSWORD = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")

# ── Dummy-data module constants ────────────────────────────────────────────────
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])


@pytest.fixture(scope="module", autouse=True)
def _provision_source_cred_secret() -> None:
    """Provision dataspoke-source-cred-dummy-data-pg K8s Secret before any test in this module.

    Creates the Secret idempotently (never overwrites an existing value) using
    DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD from helm-charts/.env.dev. This is the
    test-setup analogue of the operator authoring step in
    spec/feature/SECRET_RESOLUTION.md §Admin authoring guide.

    Secret-mutating setup belongs in the fixture, not the REST-only test body.
    spec: spec/TESTING.md §Api-Wired Integration Tests — REST-only test body
    spec: spec/feature/SECRET_RESOLUTION.md §Reference-only model — out-of-band provisioning

    The Secret is NOT deleted on teardown: it is operator-owned and idempotent with
    the install.sh post-install provisioning step (install.sh:873).
    """
    password = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD", "")
    if not password:
        pytest.skip(
            "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD is not set. "
            "Source helm-charts/.env.dev before running this test. "
            "spec: feature/SECRET_RESOLUTION.md §Admin authoring guide."
        )

    from tests.integration.util.k8s import ensure_source_cred_secret

    ensure_source_cred_secret("dummy-data-pg", "password", password)
    # No teardown — the Secret is operator-owned, idempotent on re-run.


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
    internal_headers: dict[str, str],
) -> None:
    """UC1 Case 2 — DataSpoke owns extraction for the catalog schema.

    Narrative from USE_CASE_en.md §UC1 Case 2:
      "DataSpoke is the ingestor. An Airflow tier DAG runs DataSpoke's postgres
       extractor on the schedule. The recipe is DataHub-compatible; the password
       references a k8s secret via ${name__key}."

    The K8s Secret dataspoke-source-cred-dummy-data-pg is provisioned by
    _provision_source_cred_secret() (module-scoped fixture) before this test runs.
    The test then asserts that GET /spoke/ingestion/secrets lists the ref — confirming
    the DataSpoke secret-list endpoint sees the just-provisioned Secret.

    spec: USE_CASE_en.md §UC1 Case 2
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (ACTIVE_CUSTOM_MANAGED)
    spec: feature/BACKEND.md §Ingestion Service §Active-custom run pipeline
    spec: feature/SECRET_RESOLUTION.md §Reference discovery (list flow)
    """
    # Assert the provisioned secret is now visible via GET /spoke/ingestion/secrets.
    # This confirms the DataSpoke list endpoint reflects the out-of-band Secret creation.
    # spec: feature/SECRET_RESOLUTION.md §Reference discovery — list flow
    secrets_resp = await api_client.get("/api/v1/spoke/ingestion/secrets", headers=admin_headers)
    assert secrets_resp.status_code == 200, (
        f"GET /spoke/ingestion/secrets expected 200, got {secrets_resp.status_code}: "
        f"{secrets_resp.text}"
    )
    listed_refs = [item.get("ref") for item in secrets_resp.json().get("secrets", [])]
    assert _SECRET_REF_BARE in listed_refs, (
        f"Secret ref '{_SECRET_REF_BARE}' not listed by GET /spoke/ingestion/secrets "
        f"after provisioning. Listed refs: {listed_refs}. "
        "spec: feature/SECRET_RESOLUTION.md §Reference discovery (list flow)"
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
        # DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD in helm-charts/.env.dev.
        # spec: API.md §Ingestion §Source body shape — secret refs never expanded in responses
        assert _PG_PLAINTEXT_PASSWORD, (
            "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD not set; "
            "cannot verify plaintext is absent from the API response. "
            "Source helm-charts/.env.dev before running this test."
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
        # spec: USE_CASE_en.md §UC1 Case 2 — "POST .../method/run?dry_run=true"
        # spec: feature/BACKEND.md §Active-custom run pipeline — dry_run skips aspect emission
        dry_run_resp = await api_client.post(
            f"{source_run_url}?dry_run=true",
            headers=admin_headers,
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
        # A dry-run DOES discover but emits nothing.
        # spec: API.md §Ingestion — method/run — discovered_urns is the "would emit"
        # plan, present on both dry-run and real runs; emitted_urns is empty with
        # count 0 on a dry-run.
        dry_detail = dry_run_body.get("detail", {})
        assert dry_detail.get("dry_run") is True, "detail.dry_run must be true for dry runs"
        dry_discovered = dry_detail.get("discovered_urns", [])
        dry_discovered_count = dry_detail.get("discovered_urns_count", 0)
        assert dry_discovered_count >= 2, (
            f"Dry-run must discover ≥2 catalog datasets (title_master + editions); "
            f"got discovered_urns_count={dry_discovered_count}. "
            "spec: API.md §Ingestion — method/run — discovered_urns present on dry-run"
        )
        # The two seeded catalog datasets must appear in the discovery plan.
        # spec: USE_CASE_en.md §Imaginary Company Profile: Imazon — catalog holds
        #   title_master + editions (catalog.* always seeded; project memory
        #   project_datahub_resolvable_urns_catalog_only)
        for catalog_urn in (_CATALOG_TITLE_URN, _CATALOG_EDITIONS_URN):
            assert catalog_urn in dry_discovered, (
                f"{catalog_urn!r} must appear in dry-run discovered_urns; "
                f"got {sorted(dry_discovered)}. "
                "spec: API.md §Ingestion — method/run — discovered_urns are the "
                "filtered dataset URNs"
            )
        # Dry-run emits nothing.
        assert dry_detail.get("emitted_urns_count", -1) == 0, (
            f"Dry-run must not emit any datasets (emitted_urns_count=0); "
            f"got {dry_detail.get('emitted_urns_count')}. "
            "spec: API.md §Ingestion — method/run — emitted_urns empty on dry-run"
        )
        assert dry_detail.get("emitted_urns", None) == [], (
            f"Dry-run emitted_urns must be []; got {dry_detail.get('emitted_urns')!r}. "
            "spec: API.md §Ingestion — method/run — dry-run emits nothing"
        )

        # ── Step 4: Real run — emit dataset aspects to DataHub ────────────────
        # spec: USE_CASE_en.md §UC1 Case 2 — "A real run emits dataset aspects + a
        # DataProcessInstance, and records emitted URNs as the authoritative mapping"
        real_run_resp = await api_client.post(
            source_run_url,
            headers=admin_headers,
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
        # A real run discovers and emits; emitted_urns ⊆ discovered_urns.
        # spec: API.md §Ingestion — method/run — both lists populated on a real
        # run; emitted ⊆ discovered.
        real_discovered = real_detail.get("discovered_urns", [])
        real_discovered_count = real_detail.get("discovered_urns_count", 0)
        real_emitted = real_detail.get("emitted_urns_count", 0)
        real_emitted_urns = real_detail.get("emitted_urns", [])
        assert real_discovered_count >= 2, (
            f"Real run must discover at least 2 catalog datasets "
            f"(title_master + editions); discovered={real_discovered_count}. "
            "spec: API.md §Ingestion — method/run — discovered_urns present on real runs"
        )
        # At least 2 catalog datasets (title_master + editions) must have been emitted.
        assert real_emitted >= 2, (
            f"Real run must emit at least 2 catalog datasets "
            f"(title_master + editions); emitted={real_emitted}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — catalog schema produces multiple datasets"
        )
        # Core invariant: emitted_urns ⊆ discovered_urns.
        assert set(real_emitted_urns).issubset(set(real_discovered)), (
            f"emitted_urns must be a subset of discovered_urns; "
            f"emitted={sorted(real_emitted_urns)} discovered={sorted(real_discovered)}. "
            "spec: API.md §Ingestion — method/run — emitted_urns ⊆ discovered_urns"
        )

        # ── Step 5: GET /sources/{id}/datasets → derivation='emitted' rows ─────
        # spec: API.md §Ingestion — GET /sources/{id}/datasets returns mapping rows
        # spec: feature/BACKEND.md §Active-custom run pipeline — emitted URNs recorded
        #       into ingestion_source_dataset with derivation='emitted'
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
        # All emitted rows must carry derivation='emitted' (authoritative mapping).
        # spec: feature/BACKEND.md §Active-custom run pipeline — derivation=emitted for real runs.
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted→authority=high.
        derivations = {d["derivation"] for d in datasets_body["datasets"]}
        assert "emitted" in derivations, (
            f"At least one dataset must have derivation='emitted' after a real run; "
            f"got derivations={derivations}. "
            "spec: feature/BACKEND.md §Ingestion Service §Source→dataset mapping"
        )
        for d in datasets_body["datasets"]:
            if d["derivation"] == "emitted":
                assert d["authority"] == "high", (
                    f"Dataset {d['dataset_urn']!r} has derivation='emitted' but "
                    f"authority={d['authority']!r}; expected 'high'. "
                    "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted→high."
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

        # ── Step 7b: lastIngested observation, and the per-dataset timeline ──
        # The run just emitted DataSpoke's own aspects with a non-default
        # systemMetadata.runId, so DataHub can now date these two catalog tables and
        # Dataset.lastIngested advances for both. This is the only arc in the api-wired
        # suite where that is true — every other harness emit leaves the
        # "no-run-id-provided" sentinel behind, which keeps lastIngested null — so it is
        # the only place the observation sub-pass can be proved end-to-end against a real
        # DataHub.
        #
        # Two things are asserted, and the second is what makes the first safe:
        #   1. the sweep books one last_ingested_observation per emitted dataset, carrying
        #      detail.dataset_urn and exactly the two spec'd keys;
        #   2. the per-dataset timeline for title_master shows its OWN observation plus the
        #      run-level rows, and NOT the sibling editions observation. Without (2) the
        #      observations of every dataset a source covers appear on every one of its
        #      datasets' timelines.
        #
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — "For every dataset
        #   mapped to a source, when DataHub reports a non-null Dataset.lastIngested, the
        #   sweep books an INGESTION.COMPLETE carrying detail.dataset_urn."
        # spec: DATAHUB_INTEGRATION.md §Observed Ingestion Recency — lastIngested is
        #   "computed by scanning each aspect's systemMetadata.runId".
        # spec: feature/BACKEND.md §Event Catalogue — last_ingested_observation detail
        #   keys are "source, dataset_urn".
        observed_urns: set[str] = set()
        deadline = time.time() + 180.0
        while time.time() < deadline:
            sync_resp = await api_client.post(
                "/internal/activities/ingestion/sync", headers=internal_headers
            )
            assert sync_resp.status_code == 200, (
                f"POST /internal/activities/ingestion/sync expected 200, "
                f"got {sync_resp.status_code}: {sync_resp.text}"
            )
            assert "last_ingested_observed" in sync_resp.json(), (
                f"the sweep summary must report last_ingested_observed; got "
                f"{sorted(sync_resp.json())}. A missing counter reads exactly like an "
                "estate with nothing observable. "
                "spec: feature/BACKEND.md §Ingestion Service — Sweep summary."
            )

            feed_resp = await api_client.get(
                f"/api/v1/spoke/ingestion/sources/{source_id}/event?offset=0&limit=1000",
                headers=admin_headers,
            )
            assert feed_resp.status_code == 200, (
                f"GET /sources/{source_id}/event expected 200, got "
                f"{feed_resp.status_code}: {feed_resp.text}"
            )
            observed_urns = {
                (ev.get("detail") or {})["dataset_urn"]
                for ev in feed_resp.json()["events"]
                if (ev.get("detail") or {}).get("source") == "last_ingested_observation"
            }
            if {_CATALOG_TITLE_URN, _CATALOG_EDITIONS_URN} <= observed_urns:
                break
            await asyncio.sleep(5.0)

        assert {_CATALOG_TITLE_URN, _CATALOG_EDITIONS_URN} <= observed_urns, (
            f"both emitted catalog tables must be observed via Dataset.lastIngested after "
            f"a real run and a sweep; observed {sorted(observed_urns)}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4."
        )
        for ev in feed_resp.json()["events"]:
            detail = ev.get("detail") or {}
            if detail.get("source") != "last_ingested_observation":
                continue
            assert set(detail) == {"source", "dataset_urn"}, (
                f"last_ingested_observation detail keys must be exactly "
                f"{{source, dataset_urn}}; got {sorted(detail)}. "
                "spec: feature/BACKEND.md §Event Catalogue."
            )
            assert ev["event_type"] == "INGESTION.COMPLETE" and ev["status"] == "success", (
                "observation is success-only — lastIngested advances when aspects are "
                "written and cannot express a failure. "
                "spec: feature/BACKEND.md §Ingestion Service — Sync step 4."
            )

        # The per-dataset timeline: title_master's own observation and the run-level rows
        # (which carry no scalar dataset_urn) are kept; editions' observation is excluded.
        # total_count is asserted against the returned length as well — the predicate lives
        # on the shared base select, so the page query and the count over its subquery
        # cannot diverge, and a divergence is otherwise invisible until a caller paginates.
        #
        # spec: feature/BACKEND.md §Querying Events — a source row qualifies "when its
        #   detail.dataset_urn is this URN **or is absent** … The predicate belongs to the
        #   shared base select, so the page query and its total_count cannot diverge."
        timeline_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_CATALOG_TITLE_ENCODED}/event/ingestion"
            "?offset=0&limit=1000",
            headers=admin_headers,
        )
        assert timeline_resp.status_code == 200, (
            f"GET /data/{{urn}}/event/ingestion expected 200, got "
            f"{timeline_resp.status_code}: {timeline_resp.text}"
        )
        timeline = timeline_resp.json()
        timeline_urns = {
            (ev.get("detail") or {}).get("dataset_urn") for ev in timeline["events"]
        }
        assert _CATALOG_TITLE_URN in timeline_urns, (
            f"this dataset's own observation must appear on its timeline; got "
            f"{sorted(u for u in timeline_urns if u)}. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert _CATALOG_EDITIONS_URN not in timeline_urns, (
            f"a sibling dataset's observation must NOT appear on this dataset's timeline; "
            f"got {sorted(u for u in timeline_urns if u)}. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert any(
            (ev.get("detail") or {}).get("run_id") == run_id for ev in timeline["events"]
        ), (
            f"the run-level INGESTION.COMPLETE for run_id={run_id!r} must stay on the "
            "timeline — it carries no scalar dataset_urn, and an equality-only predicate "
            "would delete precisely the run and FAIL rows from every dataset's timeline. "
            "spec: feature/BACKEND.md §Querying Events."
        )
        assert timeline["total_count"] == len(timeline["events"]), (
            f"total_count must equal the returned row count on an unpaginated read; got "
            f"total_count={timeline['total_count']} with {len(timeline['events'])} rows. "
            "spec: feature/BACKEND.md §Querying Events."
        )

        # ── Step 8: Dataset catalog reflects ingestion coverage ──────────────
        # The dataset-catalog collection root GET /spoke/common/data composes the
        # all-sources reverse-lookup per row. After this real run the catalog row
        # for title_master must list the source we just created in its ingestion
        # array, carrying {source_id, name, mode, platform} (batched reverse-lookup).
        # spec: API.md §Data Resource — GET /spoke/common/data row.ingestion shape
        #       (list of {source_id, name, mode, platform}, empty when none)
        catalog_resp = await api_client.get(
            "/api/v1/spoke/common/data",
            headers=admin_headers,
            params={"limit": 200, "offset": 0},
        )
        assert catalog_resp.status_code == 200, (
            f"GET /spoke/common/data expected 200, got "
            f"{catalog_resp.status_code}: {catalog_resp.text}"
        )
        catalog_body = catalog_resp.json()
        for key in ("offset", "limit", "total_count", "datasets"):
            assert key in catalog_body, (
                f"catalog envelope missing key {key!r}; got {list(catalog_body)}. "
                "spec: API.md §Data Resource — GET /spoke/common/data envelope"
            )
        title_row = next(
            (d for d in catalog_body["datasets"] if d["dataset_urn"] == _CATALOG_TITLE_URN),
            None,
        )
        assert title_row is not None, (
            f"{_CATALOG_TITLE_URN!r} must appear in the dataset catalog "
            "(it is a registered catalog dataset). "
            "spec: API.md §Data Resource — GET /spoke/common/data lists registered datasets"
        )
        assert isinstance(title_row["ingestion"], list) and title_row["ingestion"], (
            "title_master catalog row must list ingestion coverage after the run "
            f"that emitted it; got ingestion={title_row['ingestion']!r}. row={title_row}. "
            "spec: API.md §Data Resource — row.ingestion is the all-sources reverse-lookup"
        )
        covering = next(
            (s for s in title_row["ingestion"] if s["source_id"] == source_id), None
        )
        assert covering is not None, (
            f"catalog row.ingestion must include the source {source_id!r} that emitted "
            f"title_master; got {title_row['ingestion']!r}. "
            "spec: API.md §Data Resource — row.ingestion lists every covering source"
        )
        assert covering["mode"] == "ACTIVE_CUSTOM_MANAGED", (
            f"catalog row.ingestion.mode must be 'ACTIVE_CUSTOM_MANAGED'; "
            f"got {covering.get('mode')!r}. "
            "spec: API.md §Data Resource — row.ingestion.mode echoes the source mode"
        )
        assert covering["platform"] == "postgres", (
            f"catalog row.ingestion.platform must be 'postgres' (the source platform); "
            f"got {covering.get('platform')!r}. "
            "spec: API.md §Data Resource — row.ingestion.platform"
        )
        assert covering.get("name"), (
            "catalog row.ingestion.name must be the owning source name (non-empty). "
            "spec: API.md §Data Resource — row.ingestion.name"
        )

    finally:
        # ── Cleanup: DELETE the source (cascades ingestion_source_dataset) ───
        if source_id is not None:
            await api_client.delete(
                f"/api/v1/spoke/ingestion/sources/{source_id}",
                headers=admin_headers,
            )
