"""UC1 Case 1 — DATAHUB_MANAGED source sync: end-to-end through public REST API.

DataHub's own recipe + cron run the ingestion; DataSpoke syncs the source
definition down and exposes it read-only. DataSpoke is NOT the ingestor.

Steps mirror USE_CASE_en.md §UC1 Case 1:
  1. Create a DataHub Secret (UC1_POSTGRES_PASSWORD) and an IngestionSource
     whose recipe uses password = "${UC1_POSTGRES_PASSWORD}" (DataHub best practice).
  2. Trigger the DataSpoke sync sweep via POST /internal/activities/ingestion/sync.
  3. Assert the source appears as a DATAHUB_MANAGED row in
     GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED.
  4. Assert credential-handling invariant:
       password == "${UC1_POSTGRES_PASSWORD}" (secret reference preserved verbatim,
       not masked, not resolved)
       spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references are
             preserved as-is"
  5. Assert read-only enforcement:
       PUT / PATCH → 409 INGESTION_SOURCE_READONLY
       method/run → 409 INGESTION_RUN_NOT_APPLICABLE
  6. Poll GET /sources/{id}/datasets (≤180s, ES budget);
     assert non-empty, valid derivation enum, non-catalog URNs, ≥1 matched derivation.
  7. Assert schedule round-trips ('0 0 * * *') and schedule_tier is absent from wire.
  8. Execute the source in DataHub via createIngestionExecutionRequest; poll to terminal
     SUCCESS (≤180s); re-run sync; verify DataSpoke reflects the run:
       PRIMARY:   GET /sources/{id}/event has INGESTION.COMPLETE with
                  detail.execution_request_urn present and detail.source='datahub_sync'.
       SECONDARY: GET /sources/{id}/datasets has ≥1 row with derivation='pipeline_name'
                  and authority='high'.
     Tolerant: skip if executor unavailable or run does not reach SUCCESS in budget.
  9. Cleanup: deleteIngestionSource, deleteSecret, re-run sync to remove mirrored rows.

spec: USE_CASE_en.md §UC1 Case 1
spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors the run as INGESTION.COMPLETE
      and upgrades datasets from matched/medium to pipeline_name/high
spec: API.md §Ingestion — DATAHUB_MANAGED, read-only invariant (409 INGESTION_SOURCE_READONLY)
spec: feature/BACKEND.md §Ingestion Service §Sync sweep steps 3-4
spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation→authority pairing
spec: TESTING.md §Api-Wired Integration Tests
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db
from tests.integration.util.datahub import (
    PG_INSTANCE,
    TARGET_SCHEMAS,
)

# ── Dummy-data module constants ────────────────────────────────────────────────
# spec: TESTING.md §Per-Module Dummy-Data Reset
# Seed all TARGET_SCHEMAS so the matcher sweep has non-catalog URNs to map.
# The recipe in this test denies the catalog schema, so only orders/customers/
# reviews/shipping datasets should appear in /sources/{id}/datasets.
# spec: project_datahub_resolvable_urns_catalog_only memory — seed catalog too
#   so the full expected set is available for the sync; non-catalog schemas seeded here
#   for the DATAHUB_MANAGED recipe's matcher to find.
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(
    {"catalog", "orders", "customers", "reviews", "shipping"}
)

# Expected URNs for the non-catalog schemas that the DATAHUB_MANAGED recipe covers.
# Derived from the seed: TARGET_SCHEMAS minus 'catalog' = orders, customers, reviews, shipping.
# All use env=DEV (from datahub.py: ENV = "DEV") and platform=postgres, db=example_db.
# spec: datahub.py §ENV constant + §_make_pg_urn — urn format uses ENV='DEV' not 'PROD'.
# spec: TESTING.md §Imazon Dummy-Data Reference — non-catalog tables for UC1 DATAHUB_MANAGED.
_NON_CATALOG_SCHEMAS = TARGET_SCHEMAS - {"catalog"}

# The managed recipe denies catalog, information_schema, pg_*. After sync the
# matcher should map all non-catalog URNs from example_db.
# We don't hardcode individual table names — assert by URN substring pattern.
_EXPECTED_URN_INFIX = f",{PG_INSTANCE}."  # e.g. ",example_db."
_EXPECTED_NON_CATALOG_SCHEMAS = _NON_CATALOG_SCHEMAS

# The secret value stored in the DataHub Secret (used only in the createSecret call).
# This value must NOT appear anywhere in any DataSpoke API response — on the secret-ref
# path DataHub returns only the reference string, so the value never reaches DataSpoke at all.
_PLAINTEXT_PW_IN_FIXTURE = "ExampleDev2024!"

# The DataHub secret name used for the secret-ref path.
# spec: feature/BACKEND.md §Sync sweep step 1 — ${...} references preserved as-is.
_SECRET_NAME = "UC1_POSTGRES_PASSWORD"
_SECRET_REF = f"${{{_SECRET_NAME}}}"  # "${UC1_POSTGRES_PASSWORD}"


@dataclass
class _ManagedSource:
    """Typed container for the single DATAHUB_MANAGED source provisioned by the fixture.

    id: DataSpoke source ID (from GET /sources).
    urn: DataHub ingestion source URN.
    secret_urn: DataHub secret URN (urn:li:dataHubSecret:UC1_POSTGRES_PASSWORD).
    """

    id: str
    urn: str
    secret_urn: str


@pytest_asyncio.fixture
async def _managed_source_setup(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> AsyncGenerator[_ManagedSource]:
    """Provision a DataHub Secret + one IngestionSource, run sync, yield the DataSpoke id.

    The source recipe uses password = "${UC1_POSTGRES_PASSWORD}" — the DataHub-recommended
    best practice of referencing a pre-created DataHub Secret rather than embedding a
    plaintext credential.

    Teardown (guaranteed on mid-test failure): deleteIngestionSource, deleteSecret for the
    secret URN, then re-run sync to remove the mirrored DataSpoke row.

    spec: TESTING.md §Api-Wired Integration Tests — fixture teardown prevents
          managed sources leaking into DataHub for subsequent runs.
    spec: USE_CASE_en.md §UC1 Case 1 — DataHub-managed source exposed read-only via DataSpoke.
    """
    datahub_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping DATAHUB_MANAGED UC1 test")

    gql_headers: dict[str, str] = {"Content-Type": "application/json"}
    if datahub_token:
        gql_headers["Authorization"] = f"Bearer {datahub_token}"

    # Clean slate before test — spec: TESTING.md §Integration Testing §Per-Module reset
    await dataspoke_db.reset_ingestion_sources()

    # Idempotency: drop any leftover DataHub Secret from a prior interrupted run.
    # DataHub Secrets are name-keyed (urn:li:dataHubSecret:<name>) and survive a DataSpoke
    # reset-seed, so without this createSecret below fails with "This Secret already exists!".
    # Best-effort — ignore errors when the secret is absent (mirrors the teardown deleteSecret
    # and the e2e uc1-01 step-1 pre-delete).
    httpx.post(
        f"{datahub_gms_url}/api/graphql",
        headers=gql_headers,
        json={
            "query": "mutation deleteSecret($urn: String!) { deleteSecret(urn: $urn) }",
            "variables": {"urn": f"urn:li:dataHubSecret:{_SECRET_NAME}"},
        },
        timeout=10.0,
    )

    # ── Step 1a: Create DataHub Secret ────────────────────────────────────────
    # spec: USE_CASE_en.md §UC1 Case 1 — DataHub-recommended credential pattern uses
    #   createSecret + ${SECRET_NAME} reference in the recipe.
    # The secret value itself must NEVER appear in any DataSpoke API response.
    create_secret_mutation = """
    mutation createSecret($input: CreateSecretInput!) {
        createSecret(input: $input)
    }
    """
    secret_resp = httpx.post(
        f"{datahub_gms_url}/api/graphql",
        headers=gql_headers,
        json={
            "query": create_secret_mutation,
            "variables": {
                "input": {
                    "name": _SECRET_NAME,
                    "value": _PLAINTEXT_PW_IN_FIXTURE,
                    "description": "UC1 test secret: postgres password for DATAHUB_MANAGED fixture",
                }
            },
        },
        timeout=15.0,
    )
    secret_resp.raise_for_status()
    secret_data = secret_resp.json()
    if "errors" in secret_data:
        pytest.skip(
            f"createSecret GraphQL error: {secret_data['errors']}. "
            "DataHub GMS may not support Managed Secrets in this dev-env."
        )
    secret_urn = secret_data.get("data", {}).get("createSecret")
    assert secret_urn, f"createSecret returned no URN: {secret_data}"

    # ── Step 1b: Create IngestionSource — secret-ref recipe ──────────────────
    # spec: feature/BACKEND.md §Sync sweep step 1 — ${...} secret references are
    #   preserved as-is (not masked, not resolved).
    # The password field holds only the reference; the actual credential value is
    # stored in the DataHub Secret and never returned by DataHub to DataSpoke.
    name = f"uc1-datahub-managed-secretref-{uuid.uuid4().hex[:8]}"
    recipe = {
        "source": {
            "type": "postgres",
            "config": {
                "host_port": "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
                "database": "example_db",
                "username": "postgres",
                "password": _SECRET_REF,  # "${UC1_POSTGRES_PASSWORD}"
                "include_tables": True,
                "include_views": False,
                "env": "DEV",
                "schema_pattern": {
                    "deny": [
                        "^information_schema$",
                        "^pg_.*$",
                        "^catalog$",
                    ]
                },
            },
        },
        "sink": {"type": "datahub-rest", "config": {"server": datahub_gms_url}},
    }

    create_mutation = """
    mutation createIngestionSource($input: UpdateIngestionSourceInput!) {
        createIngestionSource(input: $input)
    }
    """
    gql_resp = httpx.post(
        f"{datahub_gms_url}/api/graphql",
        headers=gql_headers,
        json={
            "query": create_mutation,
            "variables": {
                "input": {
                    "name": name,
                    "type": "postgres",
                    "config": {
                        "recipe": json.dumps(recipe),
                        "executorId": "default",
                        "debugMode": False,
                    },
                    # spec: USE_CASE_en.md §UC1 Case 1 — "scheduled daily"
                    "schedule": {"interval": "0 0 * * *", "timezone": "UTC"},
                }
            },
        },
        timeout=15.0,
    )
    gql_resp.raise_for_status()
    gql_data = gql_resp.json()
    if "errors" in gql_data:
        pytest.skip(
            f"createIngestionSource (secret-ref) GraphQL error: {gql_data['errors']}. "
            "DataHub GMS may not support Managed Ingestion in this dev-env."
        )
    urn = gql_data.get("data", {}).get("createIngestionSource")
    assert urn, f"createIngestionSource returned no URN: {gql_data}"

    # ── Step 2: Poll sync sweep until the source URN appears in DataSpoke ─────
    # DataHub eventual consistency: listIngestionSources may not return brand-new
    # sources immediately; subsequent sync calls pick them up once DataHub indexes.
    # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s.
    # spec: feature/BACKEND.md §Sync sweep step 1 — sync mirrors all DataHub-managed sources.
    poll_deadline = time.time() + 180.0
    poll_interval = 5.0
    matching: list = []
    found_urns: list = []
    while time.time() < poll_deadline:
        sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
        )
        assert sync_resp.status_code == 200, (
            f"POST /internal/activities/ingestion/sync expected 200, "
            f"got {sync_resp.status_code}: {sync_resp.text}"
        )
        list_resp = await api_client.get(
            "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, list_resp.text
        sources = list_resp.json().get("sources", [])
        matching = [s for s in sources if s.get("datahub_source_urn") == urn]
        found_urns = [s.get("datahub_source_urn") for s in sources]
        if matching:
            break
        await asyncio.sleep(poll_interval)

    assert len(matching) >= 1, (
        f"Expected DATAHUB_MANAGED source (secret-ref) with "
        f"datahub_source_urn={urn!r} after ≤180s polling; "
        f"found {found_urns}. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — sync mirrors DataHub-managed sources; "
        "spec: project_es_indexing_lag_after_reset_seed — DataHub eventual consistency."
    )

    source_id = matching[0]["id"]

    managed = _ManagedSource(
        id=source_id,
        urn=urn,
        secret_urn=secret_urn,
    )

    try:
        yield managed
    finally:
        # Guaranteed cleanup even on mid-test failure.
        # Delete the DataHub IngestionSource and the secret so subsequent runs
        # see a clean slate.
        # spec: TESTING.md §Integration Testing — deterministic isolation.
        delete_mutation = """
        mutation deleteIngestionSource($urn: String!) {
            deleteIngestionSource(urn: $urn)
        }
        """
        delete_secret_mutation = """
        mutation deleteSecret($urn: String!) {
            deleteSecret(urn: $urn)
        }
        """
        try:
            httpx.post(
                f"{datahub_gms_url}/api/graphql",
                headers=gql_headers,
                json={
                    "query": delete_mutation,
                    "variables": {"urn": urn},
                },
                timeout=10.0,
            )
        except Exception:
            pass

        try:
            httpx.post(
                f"{datahub_gms_url}/api/graphql",
                headers=gql_headers,
                json={
                    "query": delete_secret_mutation,
                    "variables": {"urn": secret_urn},
                },
                timeout=10.0,
            )
        except Exception:
            pass

        # Re-run sync to remove the mirrored DataSpoke row
        try:
            await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
        except Exception:
            pass


@pytest.mark.asyncio
async def test_uc1_datahub_managed_sync_and_readonly(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    _managed_source_setup: _ManagedSource,
) -> None:
    """UC1 Case 1 — DataHub-managed source is synced down and read-only in DataSpoke.

    Narrative from USE_CASE_en.md §UC1 Case 1:
      "The Imazon team creates a DataHub Managed Ingestion source at
       http://datahub.<domain>/ingestion. DataSpoke's sync sweep pulls the
       definition down and exposes it read-only."

    The source recipe uses password = "${UC1_POSTGRES_PASSWORD}" — a DataHub Secret
    reference. The sync sweep must preserve the reference verbatim: it is not a secret
    value, so it is not masked and not resolved.
    spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references are
          preserved as-is (not masked, not resolved)."

    UC1 invariants verified:
      - credential-handling: password reference preserved verbatim as "${UC1_POSTGRES_PASSWORD}"
      - read-only enforcement: PUT / PATCH → 409 INGESTION_SOURCE_READONLY
      - method/run → 409 INGESTION_RUN_NOT_APPLICABLE
      - schedule == '0 0 * * *'; schedule_tier NOT in API response
      - recipe source.type == 'postgres'
      - /sources/{id}/datasets: non-empty, valid derivation enum, non-catalog URNs, ≥1 matched

    spec: USE_CASE_en.md §UC1 Case 1
    spec: API.md §Ingestion — DATAHUB_MANAGED read-only: 409 INGESTION_SOURCE_READONLY
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 1 (source defs)
    spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier internal; never in API
    """
    managed = _managed_source_setup

    # Re-fetch the source for assertions
    get_resp = await api_client.get(
        f"/api/v1/spoke/ingestion/sources/{managed.id}",
        headers=admin_headers,
    )
    assert get_resp.status_code == 200, get_resp.text
    source = get_resp.json()

    # ── Credential-handling: secret-ref path ──────────────────────────────────
    # spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references are
    #   preserved as-is (not masked, not resolved)."
    # The recipe is stored with the reference verbatim; the reference is NOT a
    # credential value and must NOT be replaced with "********".
    password = source.get("recipe", {}).get("source", {}).get("config", {}).get("password")
    assert password == _SECRET_REF, (
        f"recipe.source.config.password must equal "
        f"{_SECRET_REF!r} (reference preserved verbatim); got {password!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — '${...} secret references are "
        "preserved as-is (not masked, not resolved)'."
    )

    # Cheap regression guard: on the secret-ref path DataHub returns only the reference
    # string to DataSpoke — the secret value never reaches DataSpoke at all.
    # Asserting the value is absent confirms nothing was inadvertently resolved or injected.
    assert _PLAINTEXT_PW_IN_FIXTURE not in get_resp.text, (
        f"The secret value '{_PLAINTEXT_PW_IN_FIXTURE}' must not appear anywhere in the "
        f"GET response (on the secret-ref path DataHub returns the reference, not the value). "
        "spec: API.md §Ingestion §Source body shape."
    )

    # ── Schedule round-trips + wire-shape invariant ───────────────────────────
    # spec: USE_CASE_en.md §UC1 Case 1 — "scheduled daily" with cron '0 0 * * *'
    assert source.get("schedule") == "0 0 * * *", (
        f"Synced DATAHUB_MANAGED source must carry schedule='0 0 * * *' (mirrored from DataHub); "
        f"got {source.get('schedule')!r}. "
        "spec: USE_CASE_en.md §UC1 Case 1 — schedule mirrored from DataHub IngestionSource."
    )
    # spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier is internal; never in the API.
    assert "schedule_tier" not in source, (
        f"schedule_tier must NOT appear in the API response for DATAHUB_MANAGED source. "
        f"spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier is internal, never exposed. "
        f"Body keys: {list(source.keys())}"
    )

    # Recipe must be present and have source.type = 'postgres'
    assert source.get("recipe", {}).get("source", {}).get("type") == "postgres", (
        "Synced recipe must preserve source.type='postgres'. "
        "spec: USE_CASE_en.md §UC1 Case 1 — recipe mirrored from DataHub"
    )

    # ── Read-only enforcement ─────────────────────────────────────────────────
    # spec: API.md §Ingestion — PUT / PATCH on DATAHUB_MANAGED → 409 INGESTION_SOURCE_READONLY
    put_resp = await api_client.put(
        f"/api/v1/spoke/ingestion/sources/{managed.id}",
        headers=admin_headers,
        json={
            "mode": "DATAHUB_MANAGED",
            "name": "attempted overwrite",
            "schedule": None,
            "recipe": {"source": {"type": "postgres", "config": {}}},
        },
    )
    assert put_resp.status_code == 409, (
        f"PUT on DATAHUB_MANAGED source must return 409; got {put_resp.status_code}. "
        "spec: API.md §Ingestion — DATAHUB_MANAGED is read-only"
    )
    assert put_resp.json().get("error_code") == "INGESTION_SOURCE_READONLY", (
        f"error_code must be 'INGESTION_SOURCE_READONLY'; "
        f"got {put_resp.json().get('error_code')!r}. "
        "spec: API.md §Ingestion — 409 INGESTION_SOURCE_READONLY"
    )

    patch_resp = await api_client.patch(
        f"/api/v1/spoke/ingestion/sources/{managed.id}",
        headers=admin_headers,
        json={"name": "attempted patch"},
    )
    assert patch_resp.status_code == 409, (
        f"PATCH on DATAHUB_MANAGED source must return 409; got {patch_resp.status_code}. "
        "spec: API.md §Ingestion — DATAHUB_MANAGED is read-only"
    )
    assert patch_resp.json().get("error_code") == "INGESTION_SOURCE_READONLY"

    run_resp = await api_client.post(
        f"/api/v1/spoke/ingestion/sources/{managed.id}/method/run",
        headers=admin_headers,
    )
    assert run_resp.status_code == 409, (
        f"method/run on DATAHUB_MANAGED must return 409; got {run_resp.status_code}. "
        "spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for non-ACTIVE_CUSTOM_MANAGED"
    )
    assert run_resp.json().get("error_code") == "INGESTION_RUN_NOT_APPLICABLE", (
        f"error_code must be 'INGESTION_RUN_NOT_APPLICABLE'; "
        f"got {run_resp.json().get('error_code')!r}. "
        "spec: USE_CASE_en.md §UC1 API Mapping"
    )

    # ── Poll /sources/{id}/datasets until non-catalog URNs appear ─────────────
    # The sync sweep uses DataHub ES search to find URNs matching the recipe's
    # filter. ES indexing lags ~2-3 min after reset-seed; the poll budget of
    # 180s covers the full lag window.
    # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s.
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets lists mapped datasets.
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset —
    #   derivation: emitted | pipeline_name | matched
    datasets_body: dict = {}
    mapped_datasets: list = []
    # ES-gated assertion: dataset existence search indexes lag 2-3 min after seed.
    # Each sync iteration re-runs the matcher so new indexed URNs surface on each call.
    poll_deadline = time.time() + 180.0  # ≥180s per ES lag budget
    poll_interval = 5.0
    while time.time() < poll_deadline:
        # Re-trigger the sync sweep to pick up any newly-indexed DataHub URNs
        try:
            await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
        except Exception:
            pass  # transient; outer deadline handles retry

        datasets_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{managed.id}/datasets",
            headers=admin_headers,
        )
        assert datasets_resp.status_code == 200, (
            f"GET /sources/{managed.id}/datasets expected 200, "
            f"got {datasets_resp.status_code}: {datasets_resp.text}"
        )
        datasets_body = datasets_resp.json()
        mapped_datasets = datasets_body.get("datasets", [])

        # Check if any non-catalog dataset URNs have appeared
        non_catalog_mapped = [
            d
            for d in mapped_datasets
            if _EXPECTED_URN_INFIX in d.get("dataset_urn", "")
            and f"{PG_INSTANCE}.catalog." not in d.get("dataset_urn", "")
        ]
        if non_catalog_mapped:
            break
        await asyncio.sleep(poll_interval)

    # Core assertion: mapped set must be NON-EMPTY after the sync + ES settle period.
    # Vacuous passes (empty list → all() returns True) are eliminated.
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets must list the
    #   covered datasets once the ES index catches up.
    assert mapped_datasets, (
        f"GET /sources/{managed.id}/datasets must return at least one mapped dataset "
        f"within 180s after sync (ES lag budget). "
        f"The recipe covers example_db excluding catalog; DataHub should have seeded "
        f"orders/customers/reviews/shipping URNs. "
        f"Got empty datasets list after {180}s. "
        "spec: USE_CASE_en.md §UC1 Case 1 — /sources/{id}/datasets lists the mapping. "
        "spec: project_es_indexing_lag_after_reset_seed — ES lag budget is 2-3 min."
    )

    # All returned rows must carry both derivation and authority fields.
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation: emitted|pipeline_name|matched.
    # spec: API.md ~line 283 — GET /sources/{id}/datasets rows expose authority + derivation.
    for d in mapped_datasets:
        assert "dataset_urn" in d, f"Mapping row missing dataset_urn: {d}"
        assert "derivation" in d, f"Mapping row missing derivation: {d}"
        assert "authority" in d, f"Mapping row missing authority: {d}"
        assert d["derivation"] in ("emitted", "pipeline_name", "matched"), (
            f"derivation must be one of emitted/pipeline_name/matched; got {d['derivation']!r}. "
            "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation enum."
        )
        assert d["authority"] in ("high", "medium"), (
            f"authority must be high or medium; got {d['authority']!r}. "
            "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — authority derived from derivation."
        )
        # Authority/derivation pairing invariant.
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset —
        #   emitted/pipeline_name→high, matched→medium.
        if d["derivation"] in ("emitted", "pipeline_name"):
            assert d["authority"] == "high", (
                f"derivation={d['derivation']!r} must have authority='high'; "
                f"got {d['authority']!r}. "
                "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted/pipeline_name→high."
            )
        elif d["derivation"] == "matched":
            assert d["authority"] == "medium", (
                f"derivation='matched' must have authority='medium'; "
                f"got {d['authority']!r}. "
                "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — matched→medium."
            )
        # All URNs must be from example_db (the PG_INSTANCE)
        assert _EXPECTED_URN_INFIX in d["dataset_urn"], (
            f"Mapped URN '{d['dataset_urn']}' must contain '{_EXPECTED_URN_INFIX}'. "
            "spec: TESTING.md §Manual REST API Testing — URN format: "
            "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.<schema>.<table>,DEV)"
        )
        # None of the mapped URNs should be from catalog (the denied schema)
        assert f"{PG_INSTANCE}.catalog." not in d["dataset_urn"], (
            f"Catalog URN '{d['dataset_urn']}' must not appear in the mapped datasets. "
            "The recipe denies catalog via schema_pattern.deny. "
            "spec: USE_CASE_en.md §UC1 Case 1 — recipe denies catalog schema."
        )

    # UC1 Case 1 maps via the sync matcher — at least one mapped row must have
    # derivation='matched' (DATAHUB_MANAGED sync path).
    # (pipeline_name is also valid if DataHub stamps systemMetadata, but matched is the
    # primary UC1-Case-1 path since DataSpoke is not the ingestor.)
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets "lists the covered datasets"
    # spec: feature/BACKEND.md §Sync sweep step 2 — DATAHUB_MANAGED uses filter-matcher.
    matched_rows = [d for d in mapped_datasets if d.get("derivation") == "matched"]
    assert matched_rows, (
        f"At least one mapped row must have derivation='matched' for a DATAHUB_MANAGED sync; "
        f"derivations seen: {[d.get('derivation') for d in mapped_datasets]}. "
        "spec: feature/BACKEND.md §Sync sweep step 2 — DATAHUB_MANAGED uses filter-matcher; "
        "derivation=matched is the primary mapping path before pipeline_name enrichment."
    )

    # ── Regression guard: system sources must never appear as DATAHUB_MANAGED rows ──
    # DataHub bootstraps `datahub-gc` (optional: false) and `datahub-documents`
    # (optional: true) as sourceType=SYSTEM ingestion sources.  The sync sweep must
    # mirror only non-system sources (sourceType != SYSTEM, plus a deny-list on the
    # reserved system source types `datahub-gc` and `datahub-documents` since their
    # CLI wrappers are not tagged SYSTEM).
    #
    # The guard is three-part:
    #   1. Precondition check — confirm DataHub's unfiltered listIngestionSources
    #      contains AT LEAST ONE source whose type ∈ _SYSTEM_SOURCE_TYPES (covers
    #      bare system URNs AND any `[CLI] datahub-documents`-style wrapper whose urn
    #      contains a hash suffix).  This proves a system-typed source exists in the
    #      dev DataHub, making the subsequent absence assertions non-vacuous: if the
    #      sweep drops the type deny-list the row would appear in step 2 / step 3.
    #      Skip the guard (not fail) if GMS is unreachable, returns a GraphQL error,
    #      or contains no system-typed source at all.
    #   2. Bare-URN absence check — assert neither `datahub-gc` nor
    #      `datahub-documents` appears in DataSpoke's DATAHUB_MANAGED
    #      datahub_source_urn set.
    #   3. Platform-type absence check — assert no DATAHUB_MANAGED row has
    #      platform ∈ _SYSTEM_SOURCE_TYPES.  The sweep stores platform = source type,
    #      so a `datahub-documents`-typed row means a system pipeline (or its CLI
    #      wrapper) leaked through the deny-list.
    #
    # spec: feature/BACKEND.md §Sync sweep step 1 — "the sweep mirrors only non-system
    #   sources (sourceType != SYSTEM) … datahub-gc and datahub-documents are excluded."
    _SYSTEM_SOURCE_URNS = {
        "urn:li:dataHubIngestionSource:datahub-gc",
        "urn:li:dataHubIngestionSource:datahub-documents",
    }
    _GC_URN = "urn:li:dataHubIngestionSource:datahub-gc"
    # Reserved DataHub system pipeline types — same set the spec names as excluded.
    # spec: feature/BACKEND.md §Sync sweep step 1 — deny-list covers datahub-gc and
    #   datahub-documents (CLI wrappers share the same type but have hash-suffixed URNs).
    _SYSTEM_SOURCE_TYPES = {"datahub-gc", "datahub-documents"}

    # Reuse the GMS-access pattern from _managed_source_setup: same env vars + gql_headers.
    datahub_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")
    gql_headers_guard: dict[str, str] = {"Content-Type": "application/json"}
    if datahub_token:
        gql_headers_guard["Authorization"] = f"Bearer {datahub_token}"

    # Select both urn and type so the precondition can key on source type (catching
    # CLI wrappers like `[CLI] datahub-documents` with hash-suffixed URNs).
    list_sources_query = """
    query listIngestionSources($input: ListIngestionSourcesInput!) {
        listIngestionSources(input: $input) {
            ingestionSources {
                urn
                type
            }
        }
    }
    """
    try:
        gms_resp = httpx.post(
            f"{datahub_gms_url}/api/graphql",
            headers=gql_headers_guard,
            json={
                "query": list_sources_query,
                "variables": {"input": {"start": 0, "count": 100}},
            },
            timeout=15.0,
        )
        gms_resp.raise_for_status()
        gms_data = gms_resp.json()
    except Exception as exc:
        pytest.skip(
            f"Could not reach DataHub GMS to confirm system-source precondition: {exc}. "
            "Skipping system-source guard to avoid a vacuous absence assertion."
        )

    if "errors" in gms_data:
        pytest.skip(
            f"listIngestionSources GraphQL error: {gms_data['errors']}. "
            "DataHub GMS may not support Managed Ingestion — "
            "skipping system-source guard to avoid a vacuous absence assertion."
        )

    gms_sources = (
        gms_data.get("data", {}).get("listIngestionSources", {}).get("ingestionSources", [])
    )
    gms_urns = {src["urn"] for src in gms_sources}
    gms_system_typed_urns = {
        src["urn"] for src in gms_sources if src.get("type") in _SYSTEM_SOURCE_TYPES
    }

    # Broadened precondition: guard runs when GMS contains at least one source
    # whose type ∈ _SYSTEM_SOURCE_TYPES — this covers both the bare system URNs
    # (datahub-gc, datahub-documents) and any [CLI] wrapper with a hash-suffixed URN.
    # Fall back to the bare-URN check so the guard still runs on DataHub builds
    # where type is absent from the response (older schema).
    has_system_typed_source = bool(gms_system_typed_urns) or (_GC_URN in gms_urns)
    if not has_system_typed_source:
        pytest.skip(
            f"No system-typed source (type ∈ {_SYSTEM_SOURCE_TYPES}) found in DataHub's "
            f"unfiltered listIngestionSources (returned {len(gms_urns)} source(s)) "
            f"and bare {_GC_URN!r} is also absent. "
            "Cannot confirm the system-source precondition — "
            "skipping guard to avoid a vacuous absence assertion."
        )

    # Precondition confirmed: at least one system-typed source exists in DataHub's
    # unfiltered list.  Now assert the sweep's deny-list works end-to-end.
    managed_list_resp = await api_client.get(
        "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100",
        headers=admin_headers,
    )
    assert managed_list_resp.status_code == 200, managed_list_resp.text
    all_managed_sources = managed_list_resp.json().get("sources", [])
    all_managed_urns = {s.get("datahub_source_urn") for s in all_managed_sources}
    all_managed_platforms = {s.get("platform") for s in all_managed_sources}

    # Assertion 1 — bare URN absence (original guard).
    system_urns_present = _SYSTEM_SOURCE_URNS & all_managed_urns
    assert not system_urns_present, (
        f"System-internal DataHub ingestion sources must NOT appear as DATAHUB_MANAGED rows "
        f"in DataSpoke; found datahub_source_urn(s): {system_urns_present}. "
        f"Precondition verified: system-typed source(s) present in DataHub's unfiltered list "
        f"({gms_system_typed_urns or {_GC_URN}}), so a dropped deny-list would surface them. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — the sweep mirrors only non-system "
        "sources; datahub-gc and datahub-documents are excluded."
    )

    # Assertion 2 — platform-type absence (catches CLI wrappers with hash-suffixed URNs).
    # The sweep stores platform = source type, so any row with platform ∈ _SYSTEM_SOURCE_TYPES
    # means a system pipeline or its CLI wrapper (e.g. `[CLI] datahub-documents`) leaked
    # through the deny-list despite having a non-bare URN.
    # spec: feature/BACKEND.md §Sync sweep step 1 — deny-list on reserved system source
    #   types datahub-gc and datahub-documents excludes CLI wrappers that share the same type.
    system_platforms_present = _SYSTEM_SOURCE_TYPES & all_managed_platforms
    assert not system_platforms_present, (
        f"No DATAHUB_MANAGED row may have platform ∈ {_SYSTEM_SOURCE_TYPES}; "
        f"found platform(s): {system_platforms_present}. "
        "The sweep stores platform = source type, so a matching row means a system pipeline "
        "(or its CLI wrapper, e.g. `[CLI] datahub-documents`) leaked through the deny-list "
        "even though its URN has a hash suffix and would not appear in the bare-URN check. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — the sweep mirrors only non-system "
        "sources; datahub-gc and datahub-documents are excluded."
    )


@pytest.mark.asyncio
async def test_uc1_datahub_managed_execute_and_reflect(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    _managed_source_setup: _ManagedSource,
) -> None:
    """UC1 Case 1 step 8 — execute source in DataHub; DataSpoke reflects the run.

    Narrative from USE_CASE_en.md §UC1 Case 1:
      "When the source runs in DataHub — on its daily schedule or a run triggered
       manually — DataSpoke's next sync mirrors that execution into …/event as an
       INGESTION.COMPLETE event and upgrades the covered datasets from matcher-mapped
       (derivation=matched, authority=medium) to run-observed (derivation=pipeline_name,
       authority=high), because DataHub stamps the source's identity on the aspects
       the run emits."

    Mechanism (ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
    and ref/github/datahub/smoke-test/tests/managed_ingestion/managed_ingestion_test.py):
      - Trigger: createIngestionExecutionRequest(input: {ingestionSourceUrn}) → exec URN
      - Poll:    ingestionSource(urn){ executions(start:0,count:5){
                     executionRequests { urn result { status } } } }
                 until result.status ∈ {SUCCESS, SUCCEEDED} (≤180s budget)
      - Re-sync: POST /internal/activities/ingestion/sync
      - PRIMARY:   GET /sources/{id}/event → INGESTION.COMPLETE with
                   detail.execution_request_urn present and detail.source='datahub_sync'
      - SECONDARY: GET /sources/{id}/datasets → ≥1 row with derivation='pipeline_name'
                   and authority='high'

    Tolerant: if createIngestionExecutionRequest errors (executor unavailable) or the
    execution does not reach SUCCESS/SUCCEEDED within the budget, the test is skipped with
    a clear message — mirroring the executor-unavailable skip-guard style used in the
    fixture setup.

    spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors the run as
          INGESTION.COMPLETE and upgrades datasets from matched/medium to pipeline_name/high
    spec: feature/BACKEND.md §Sync sweep step 3 — observed enrichment writes
          derivation='pipeline_name' / authority='high' when DataHub stamps pipelineName
    spec: feature/BACKEND.md §Sync sweep step 4 — run events: _mirror_execution_requests
          inserts INGESTION.COMPLETE with detail.execution_request_urn + source='datahub_sync'
    spec: BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name→high derivation/authority
    spec: API.md §Ingestion — GET /sources/{id}/event, GET /sources/{id}/datasets
    """
    managed = _managed_source_setup

    datahub_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping execution step")

    gql_headers: dict[str, str] = {"Content-Type": "application/json"}
    if datahub_token:
        gql_headers["Authorization"] = f"Bearer {datahub_token}"

    # ── Step 8a: Trigger the execution in DataHub ─────────────────────────────
    # spec: ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
    #   createIngestionExecutionRequest(input: CreateIngestionExecutionRequestInput!)
    #   input field: ingestionSourceUrn: String!
    #   return: String (execution request URN)
    # spec: ref/github/datahub/smoke-test/tests/managed_ingestion/managed_ingestion_test.py
    #   test_create_list_get_ingestion_execution_request — confirmed mutation shape
    exec_mutation = """
    mutation createIngestionExecutionRequest($input: CreateIngestionExecutionRequestInput!) {
        createIngestionExecutionRequest(input: $input)
    }
    """
    try:
        exec_resp = httpx.post(
            f"{datahub_gms_url}/api/graphql",
            headers=gql_headers,
            json={
                "query": exec_mutation,
                "variables": {"input": {"ingestionSourceUrn": managed.urn}},
            },
            timeout=20.0,
        )
        exec_resp.raise_for_status()
        exec_data = exec_resp.json()
    except Exception as exc:
        pytest.skip(
            f"createIngestionExecutionRequest HTTP error: {exc}. "
            "DataHub executor may not be available in this dev-env."
        )

    if "errors" in exec_data:
        pytest.skip(
            f"createIngestionExecutionRequest GraphQL error: {exec_data['errors']}. "
            "DataHub executor may not be available or ready in this dev-env."
        )
    execution_request_urn: str = exec_data.get("data", {}).get(
        "createIngestionExecutionRequest"
    ) or ""
    if not execution_request_urn:
        pytest.skip(
            f"createIngestionExecutionRequest returned no URN: {exec_data}. "
            "Skipping execution-and-reflect step."
        )

    # ── Step 8b: Poll the execution to terminal SUCCESS/SUCCEEDED (≤180s) ─────
    # spec: ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
    #   ingestionSource(urn: String!) { executions(start:0, count:5) {
    #       total executionRequests { urn result { status } } } }
    #   result.status: String! — terminal values per service.py:
    #     SUCCESS / SUCCEEDED → INGESTION_COMPLETE (→ test succeeds)
    #     every other terminal value (FAILURE, CANCELLED, ABORTED, TIMEOUT, …) →
    #       INGESTION_FAIL (executor ran but source errored → skip, not fail)
    #     PENDING / RUNNING → in-progress → keep polling
    #     SKIPPED / UP_FOR_RETRY → non-terminal ambiguous → keep polling
    #     None / absent result → still running → keep polling
    poll_query = """
    query ingestionSource($urn: String!) {
        ingestionSource(urn: $urn) {
            executions(start: 0, count: 5) {
                total
                executionRequests {
                    urn
                    result {
                        status
                    }
                }
            }
        }
    }
    """
    # Terminal statuses per _DATAHUB_SUCCESS_STATUSES / _DATAHUB_SKIP_STATUSES in service.py.
    # Anything not in skip set and not None is a terminal outcome (success or failure).
    _SUCCESS_STATUSES = frozenset({"SUCCESS", "SUCCEEDED"})
    # In-progress (PENDING/RUNNING) and ambiguous (SKIPPED/UP_FOR_RETRY) statuses are not
    # terminal — keep polling. Only SUCCESS/SUCCEEDED or a hard-failure status ends the loop.
    _NON_TERMINAL_STATUSES = frozenset({"PENDING", "RUNNING", "SKIPPED", "UP_FOR_RETRY"})

    poll_deadline = time.time() + 180.0
    poll_interval = 8.0
    exec_status: str | None = None

    while time.time() < poll_deadline:
        try:
            poll_resp = httpx.post(
                f"{datahub_gms_url}/api/graphql",
                headers=gql_headers,
                json={"query": poll_query, "variables": {"urn": managed.urn}},
                timeout=15.0,
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()
        except Exception:
            await asyncio.sleep(poll_interval)
            continue

        exec_requests = (
            poll_data.get("data", {})
            .get("ingestionSource", {})
            .get("executions", {})
            .get("executionRequests", [])
        )
        # Find the execution request we triggered by URN
        for req in exec_requests:
            if req.get("urn") == execution_request_urn:
                result = req.get("result") or {}
                status = result.get("status") or None
                if status and status not in _NON_TERMINAL_STATUSES:
                    # Terminal: either success or failure
                    exec_status = status
                    break
        if exec_status is not None:
            break
        await asyncio.sleep(poll_interval)

    if exec_status is None:
        pytest.skip(
            f"Execution {execution_request_urn!r} did not reach a terminal status "
            f"within 180s (last poll: no terminal result seen). "
            "DataHub executor may be slow or unavailable in this dev-env. "
            "spec: TESTING.md — tolerant skip when executor unavailable."
        )

    if exec_status not in _SUCCESS_STATUSES:
        pytest.skip(
            f"Execution {execution_request_urn!r} completed with non-success status "
            f"{exec_status!r} (executor ran but source errored — likely a connectivity "
            "issue in this dev-env, not a DataSpoke bug). "
            "spec: TESTING.md — tolerant skip when executor completes with failure."
        )

    # ── Step 8c: Re-run DataSpoke sync to mirror the completed execution ──────
    # spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests mirrors
    #   terminal execution requests for DATAHUB_MANAGED sources as INGESTION.COMPLETE events.
    sync_resp = await api_client.post(
        "/internal/activities/ingestion/sync",
        headers=internal_headers,
    )
    assert sync_resp.status_code == 200, (
        f"POST /internal/activities/ingestion/sync after execution expected 200, "
        f"got {sync_resp.status_code}: {sync_resp.text}"
    )

    # ── Step 8d: PRIMARY — GET /sources/{id}/event → INGESTION.COMPLETE ──────
    # spec: USE_CASE_en.md §UC1 Case 1 — "DataSpoke's next sync mirrors that execution
    #   into …/event as an INGESTION.COMPLETE event"
    # spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests inserts
    #   Event(event_type=INGESTION_COMPLETE, status='success',
    #         detail={execution_request_urn: ..., source: 'datahub_sync'})
    # Poll briefly to let the event row settle (sync is synchronous but DB may lag).
    event_body: dict = {}
    found_event: dict | None = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        event_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{managed.id}/event",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET /sources/{managed.id}/event expected 200, "
            f"got {event_resp.status_code}: {event_resp.text}"
        )
        event_body = event_resp.json()
        for evt in event_body.get("events", []):
            if evt.get("event_type") == "INGESTION.COMPLETE":
                detail = evt.get("detail") or {}
                if detail.get("execution_request_urn") == execution_request_urn:
                    found_event = evt
                    break
        if found_event is not None:
            break
        await asyncio.sleep(2.0)

    assert found_event is not None, (
        f"Expected an INGESTION.COMPLETE event with "
        f"detail.execution_request_urn={execution_request_urn!r} in "
        f"GET /sources/{managed.id}/event within 30s after sync. "
        f"Events returned: {event_body.get('events', [])}. "
        "spec: USE_CASE_en.md §UC1 Case 1 — sync mirrors run as INGESTION.COMPLETE event. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests."
    )

    # Verify the event's status and detail fields.
    # spec: feature/BACKEND.md §Sync sweep step 4 — Event carries status='success',
    #   detail.source='datahub_sync', detail.execution_request_urn=<urn>
    assert found_event.get("status") == "success", (
        f"INGESTION.COMPLETE event must carry status='success'; "
        f"got {found_event.get('status')!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests."
    )
    event_detail = found_event.get("detail") or {}
    assert event_detail.get("source") == "datahub_sync", (
        f"INGESTION.COMPLETE event detail.source must be 'datahub_sync'; "
        f"got {event_detail.get('source')!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests: "
        "detail.source='datahub_sync' identifies the sync origin."
    )
    assert event_detail.get("execution_request_urn"), (
        f"INGESTION.COMPLETE event detail.execution_request_urn must be present and non-empty; "
        f"got {event_detail.get('execution_request_urn')!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests: "
        "detail carries execution_request_urn for traceability."
    )

    # ── Step 8e: SECONDARY — GET /sources/{id}/datasets → pipeline_name / high ─
    # spec: USE_CASE_en.md §UC1 Case 1 — "upgrades the covered datasets from
    #   matcher-mapped (derivation=matched, authority=medium) to run-observed
    #   (derivation=pipeline_name, authority=high), because DataHub stamps the source's
    #   identity on the aspects the run emits."
    # spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets upserts
    #   derivation='pipeline_name' where systemMetadata.pipelineName == datahub_source_urn.
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name→authority='high'.
    #
    # NOTE: pipeline_name enrichment requires that DataHub has stamped pipelineName on
    # the aspects emitted by the run.  In the dev-env the executor targets the
    # example-postgres instance which is also the DataHub sink; systemMetadata.pipelineName
    # is stamped by the DataHub ingestion framework to the source URN.  We give the ES
    # index a brief settle window via the same sync+poll pattern.
    datasets_body: dict = {}
    pipeline_name_rows: list = []
    deadline = time.time() + 60.0
    while time.time() < deadline:
        # Re-trigger sync so any freshly-indexed pipelineName aspects are picked up.
        try:
            await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
        except Exception:
            pass

        ds_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{managed.id}/datasets",
            headers=admin_headers,
        )
        assert ds_resp.status_code == 200, (
            f"GET /sources/{managed.id}/datasets expected 200, "
            f"got {ds_resp.status_code}: {ds_resp.text}"
        )
        datasets_body = ds_resp.json()
        pipeline_name_rows = [
            d
            for d in datasets_body.get("datasets", [])
            if d.get("derivation") == "pipeline_name" and d.get("authority") == "high"
        ]
        if pipeline_name_rows:
            break
        await asyncio.sleep(8.0)

    assert pipeline_name_rows, (
        f"Expected ≥1 dataset row with derivation='pipeline_name' and authority='high' "
        f"in GET /sources/{managed.id}/datasets after a successful DataHub execution "
        f"and re-sync (within 60s). "
        f"Datasets returned: {datasets_body.get('datasets', [])}. "
        "spec: USE_CASE_en.md §UC1 Case 1 — execution upgrades datasets from matched/medium "
        "to pipeline_name/high via DataHub systemMetadata.pipelineName stamping. "
        "spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets upserts "
        "pipeline_name rows where pipelineName matches datahub_source_urn."
    )
