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
  8. Cleanup: deleteIngestionSource, deleteSecret, re-run sync to remove mirrored rows.

spec: USE_CASE_en.md §UC1 Case 1
spec: API.md §Ingestion — DATAHUB_MANAGED, read-only invariant (409 INGESTION_SOURCE_READONLY)
spec: feature/BACKEND.md §Ingestion Service §Sync sweep
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
        pytest.skip(
            "DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping DATAHUB_MANAGED UC1 test"
        )

    gql_headers: dict[str, str] = {"Content-Type": "application/json"}
    if datahub_token:
        gql_headers["Authorization"] = f"Bearer {datahub_token}"

    # Clean slate before test — spec: TESTING.md §Integration Testing §Per-Module reset
    await dataspoke_db.reset_ingestion_sources()

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
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation: emitted | pipeline_name | matched
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
            d for d in mapped_datasets
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
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — emitted/pipeline_name→high, matched→medium.
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
    # mirror only non-system sources (sourceType != SYSTEM).
    #
    # The guard is two-part:
    #   1. Precondition check — confirm `datahub-gc` IS present in DataHub's unfiltered
    #      listIngestionSources (no sourceType filter).  This proves the system source
    #      exists in the dev DataHub, making the subsequent absence assertion non-vacuous:
    #      if the sweep drops the SYSTEM filter the URN would appear in step 2.
    #      Skip the guard entirely (rather than false-pass) if GMS cannot confirm it.
    #   2. Absence check — assert neither `datahub-gc` nor `datahub-documents` appears in
    #      DataSpoke's DATAHUB_MANAGED list.
    #
    # spec: feature/BACKEND.md §Sync sweep step 1 — "the sweep mirrors only non-system
    #   sources (sourceType != SYSTEM) … datahub-gc and datahub-documents are excluded."
    _SYSTEM_SOURCE_URNS = {
        "urn:li:dataHubIngestionSource:datahub-gc",
        "urn:li:dataHubIngestionSource:datahub-documents",
    }
    _GC_URN = "urn:li:dataHubIngestionSource:datahub-gc"

    # Reuse the GMS-access pattern from _managed_source_setup: same env vars + gql_headers.
    datahub_gms_url = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")
    gql_headers_guard: dict[str, str] = {"Content-Type": "application/json"}
    if datahub_token:
        gql_headers_guard["Authorization"] = f"Bearer {datahub_token}"

    list_sources_query = """
    query listIngestionSources($input: ListIngestionSourcesInput!) {
        listIngestionSources(input: $input) {
            ingestionSources {
                urn
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
            f"Could not reach DataHub GMS to confirm datahub-gc precondition: {exc}. "
            "Skipping system-source guard to avoid a vacuous absence assertion."
        )

    if "errors" in gms_data:
        pytest.skip(
            f"listIngestionSources GraphQL error: {gms_data['errors']}. "
            "DataHub GMS may not support Managed Ingestion — "
            "skipping system-source guard to avoid a vacuous absence assertion."
        )

    gms_urns = {
        src["urn"]
        for src in gms_data.get("data", {})
        .get("listIngestionSources", {})
        .get("ingestionSources", [])
    }
    if _GC_URN not in gms_urns:
        pytest.skip(
            f"{_GC_URN!r} not found in DataHub's unfiltered listIngestionSources "
            f"(returned {len(gms_urns)} source(s)). "
            "Cannot confirm the system-source precondition — "
            "skipping guard to avoid a vacuous absence assertion."
        )

    # Precondition confirmed: datahub-gc IS in DataHub's unfiltered list.
    # Now assert the sweep's SYSTEM filter works: neither system source must appear
    # in DataSpoke's DATAHUB_MANAGED mirror.
    managed_list_resp = await api_client.get(
        "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100",
        headers=admin_headers,
    )
    assert managed_list_resp.status_code == 200, managed_list_resp.text
    all_managed_sources = managed_list_resp.json().get("sources", [])
    all_managed_urns = {s.get("datahub_source_urn") for s in all_managed_sources}
    system_urns_present = _SYSTEM_SOURCE_URNS & all_managed_urns
    assert not system_urns_present, (
        f"System-internal DataHub ingestion sources must NOT appear as DATAHUB_MANAGED rows "
        f"in DataSpoke; found: {system_urns_present}. "
        f"Precondition verified: {_GC_URN!r} IS present in DataHub's unfiltered source list, "
        f"so a dropped sourceType filter would surface it here. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — 'the sweep mirrors only non-system "
        "sources (sourceType != SYSTEM) … datahub-gc and datahub-documents are excluded'."
    )
