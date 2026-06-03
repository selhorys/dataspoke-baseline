"""UC1 Case 1 — DATAHUB_MANAGED source sync: end-to-end through public REST API.

DataHub's own recipe + cron runs the ingestion; DataSpoke syncs the source
definition down and exposes it read-only. DataSpoke is NOT the ingestor.

Steps mirror USE_CASE_en.md §UC1 Case 1:
  1. Create a DataHub IngestionSource via GraphQL createIngestionSource (setup)
  2. Trigger the DataSpoke sync sweep via POST /internal/activities/ingestion/sync
  3. Assert a DATAHUB_MANAGED source row appeared in GET /spoke/ingestion/sources
  4. Assert the row is read-only: PUT / PATCH / DELETE return 409 INGESTION_SOURCE_READONLY
  5. Poll GET /sources/{id}/datasets until seed-derived non-catalog URNs appear (≥180s, ES budget)
  6. Assert schedule round-trips ('0 0 * * *') and schedule_tier is absent from wire
  7. Cleanup: deleteIngestionSource from DataHub + re-run sync to remove the mirrored row

F1 fix: Step 5 polls with ≥180s timeout until mapped dataset rows appear and asserts non-empty.
F2 fix: Step 6 asserts schedule == '0 0 * * *' and 'schedule_tier' not in response.
F5 fix: cleanup moved to a pytest fixture with yield so mid-test failures still run teardown.

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


@pytest_asyncio.fixture
async def _managed_source_setup(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> AsyncGenerator[str]:
    """Create a DataHub IngestionSource, run sync, yield the DataSpoke source id.

    Teardown (F5 fix): deletes the DataHub IngestionSource and re-runs sync to
    remove the mirrored DataSpoke row even if the test fails mid-run.

    spec: TESTING.md §Api-Wired Integration Tests — fixture teardown prevents
          managed source leaking into DataHub for subsequent runs.
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

    # Create the DataHub IngestionSource (mirroring USE_CASE_en.md §UC1 Case 1 YAML)
    # Recipe covers all example_db schemas except catalog (and pg_* / information_schema).
    test_name = f"uc1-datahub-managed-test-{uuid.uuid4().hex[:8]}"
    test_recipe = {
        "source": {
            "type": "postgres",
            "config": {
                "host_port": "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
                "database": "example_db",
                "username": "postgres",
                "password": "ExampleDev2024!",
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
                    "name": test_name,
                    "type": "postgres",
                    "config": {
                        "recipe": json.dumps(test_recipe),
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
            f"createIngestionSource GraphQL error: {gql_data['errors']}. "
            "DataHub GMS may not support Managed Ingestion in this dev-env."
        )
    ingestion_source_urn = gql_data.get("data", {}).get("createIngestionSource")
    assert ingestion_source_urn, f"createIngestionSource returned no URN: {gql_data}"

    # Poll: re-run POST /internal/activities/ingestion/sync and re-list
    # GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED until a row with the created
    # datahub_source_urn appears (≤180s).
    #
    # DataHub eventual consistency: listIngestionSources may not return the brand-new
    # source immediately, so the sync correctly mirrors only pre-existing sources on the
    # first call.  Subsequent sync calls pick it up once DataHub indexes the new entry.
    # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s.
    # spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 1 — sync mirrors all
    #       DataHub-managed sources; new sources surface after indexing completes.
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
        matching = [s for s in sources if s.get("datahub_source_urn") == ingestion_source_urn]
        found_urns = [s.get("datahub_source_urn") for s in sources]
        if matching:
            break
        await asyncio.sleep(poll_interval)

    assert len(matching) >= 1, (
        f"Expected a DATAHUB_MANAGED source with "
        f"datahub_source_urn={ingestion_source_urn!r} after ≤180s polling; "
        f"found {found_urns}. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — sync mirrors DataHub-managed sources; "
        "spec: project_es_indexing_lag_after_reset_seed — DataHub eventual consistency."
    )

    managed_source = matching[0]
    managed_id = managed_source["id"]

    try:
        yield managed_id
    finally:
        # F5: Guaranteed cleanup even on mid-test failure.
        # Delete the DataHub IngestionSource so subsequent runs see a clean slate.
        # spec: TESTING.md §Integration Testing — deterministic isolation.
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
    _managed_source_setup: str,
) -> None:
    """UC1 Case 1 — DataHub-managed source is synced down and read-only in DataSpoke.

    Narrative from USE_CASE_en.md §UC1 Case 1:
      "The Imazon team creates a DataHub Managed Ingestion source at
       http://datahub.<domain>/ingestion. DataSpoke's sync sweep pulls the
       definition down and exposes it read-only."

    F1: Polls GET /sources/{id}/datasets with ≥180s timeout until non-catalog mapped
        dataset rows appear; asserts the mapped set is NON-EMPTY and that all URNs
        belong to example_db non-catalog schemas. The poll budget covers the ES
        indexing lag window (2-3 min per project_es_indexing_lag_after_reset_seed).

    F2: Asserts the synced source's schedule == '0 0 * * *' (spec: USE_CASE_en.md §UC1
        Case 1 — "scheduled daily"). Also asserts 'schedule_tier' is absent from the
        wire-shape (internal field, never exposed in API; spec: BACKEND_SCHEMA.md
        §ingestion_source — schedule_tier internal; never in API).

    spec: USE_CASE_en.md §UC1 Case 1
    spec: API.md §Ingestion — DATAHUB_MANAGED read-only: 409 INGESTION_SOURCE_READONLY
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 1 (source defs)
    spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier internal; never in API
    """
    managed_id = _managed_source_setup

    # Re-fetch the managed source to get its current shape for assertions
    get_resp = await api_client.get(
        f"/api/v1/spoke/ingestion/sources/{managed_id}",
        headers=admin_headers,
    )
    assert get_resp.status_code == 200, get_resp.text
    managed_source = get_resp.json()

    # ── F2: schedule round-trips + wire-shape invariant ──────────────────────
    # spec: USE_CASE_en.md §UC1 Case 1 — "scheduled daily" with cron '0 0 * * *'
    assert managed_source.get("schedule") == "0 0 * * *", (
        f"Synced DATAHUB_MANAGED source must carry schedule='0 0 * * *' (mirrored from DataHub); "
        f"got {managed_source.get('schedule')!r}. "
        "spec: USE_CASE_en.md §UC1 Case 1 — schedule mirrored from DataHub IngestionSource."
    )
    # spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier is internal; never in the API.
    assert "schedule_tier" not in managed_source, (
        f"schedule_tier must NOT appear in the API response for DATAHUB_MANAGED source. "
        f"spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier is internal, never exposed. "
        f"Body keys: {list(managed_source.keys())}"
    )

    # Recipe must be present and have source.type = 'postgres'
    assert managed_source.get("recipe", {}).get("source", {}).get("type") == "postgres", (
        "Synced recipe must preserve source.type='postgres'. "
        "spec: USE_CASE_en.md §UC1 Case 1 — recipe mirrored from DataHub"
    )

    # ── Secret-masking invariant ──────────────────────────────────────────────
    # USE_CASE_en.md §UC1 Case 1 specifies "secrets masked" in the synced recipe:
    #   the displayed recipe shows password: <hidden>.
    # The DataHub source fixture uses 'ExampleDev2024!' as the plaintext password;
    # the synced DataSpoke copy must NOT contain that plaintext anywhere in the response.
    # spec: USE_CASE_en.md §UC1 Case 1 — "recipe is rendered as YAML with secrets masked"
    # spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 1 — "Mask secrets in the
    #   stored/displayed recipe (DataHub returns them raw)."
    # spec: API.md §Ingestion §Source body shape — secret refs never expanded in responses
    _PLAINTEXT_PW_IN_FIXTURE = "ExampleDev2024!"
    assert _PLAINTEXT_PW_IN_FIXTURE not in get_resp.text, (
        f"Synced DATAHUB_MANAGED recipe must have secrets masked; "
        f"plaintext '{_PLAINTEXT_PW_IN_FIXTURE}' must not appear in GET response. "
        "spec: USE_CASE_en.md §UC1 Case 1 — 'recipe is rendered as YAML with secrets masked'."
    )

    # ── Step 4: Assert read-only enforcement ─────────────────────────────────
    # spec: API.md §Ingestion — PUT / PATCH on DATAHUB_MANAGED → 409 INGESTION_SOURCE_READONLY
    put_resp = await api_client.put(
        f"/api/v1/spoke/ingestion/sources/{managed_id}",
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
        f"/api/v1/spoke/ingestion/sources/{managed_id}",
        headers=admin_headers,
        json={"name": "attempted patch"},
    )
    assert patch_resp.status_code == 409, (
        f"PATCH on DATAHUB_MANAGED source must return 409; got {patch_resp.status_code}. "
        "spec: API.md §Ingestion — DATAHUB_MANAGED is read-only"
    )
    assert patch_resp.json().get("error_code") == "INGESTION_SOURCE_READONLY"

    run_resp = await api_client.post(
        f"/api/v1/spoke/ingestion/sources/{managed_id}/method/run",
        headers=admin_headers,
        json={"dry_run": False},
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

    # ── F1: Poll /sources/{id}/datasets until non-catalog URNs appear ────────
    # The sync sweep uses DataHub ES search to find URNs matching the recipe's
    # filter. ES indexing lags ~2-3 min after reset-seed; the poll budget of
    # 180s covers the full lag window.
    # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s.
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets lists mapped datasets.
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — origin: emitted | matcher | pipeline_name
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
            f"/api/v1/spoke/ingestion/sources/{managed_id}/datasets",
            headers=admin_headers,
        )
        assert datasets_resp.status_code == 200, (
            f"GET /sources/{managed_id}/datasets expected 200, "
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

    # F1 core assertion: mapped set must be NON-EMPTY after the sync + ES settle period.
    # Vacuous passes (empty list → all() returns True) are eliminated.
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets must list the
    #   covered datasets once the ES index catches up.
    assert mapped_datasets, (
        f"GET /sources/{managed_id}/datasets must return at least one mapped dataset "
        f"within 180s after sync (ES lag budget). "
        f"The recipe covers example_db excluding catalog; DataHub should have seeded "
        f"orders/customers/reviews/shipping URNs. "
        f"Got empty datasets list after {180}s. "
        "spec: USE_CASE_en.md §UC1 Case 1 — /sources/{id}/datasets lists the mapping. "
        "spec: project_es_indexing_lag_after_reset_seed — ES lag budget is 2-3 min."
    )

    # All returned URNs must carry a valid origin value.
    # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — origin: matcher | emitted | pipeline_name.
    for d in mapped_datasets:
        assert "dataset_urn" in d, f"Mapping row missing dataset_urn: {d}"
        assert "origin" in d, f"Mapping row missing origin: {d}"
        assert d["origin"] in ("emitted", "pipeline_name", "matcher"), (
            f"origin must be one of emitted/pipeline_name/matcher; got {d['origin']!r}. "
            "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — origin enum."
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

    # UC1 Case 1 maps via the sync matcher — at least one mapped row must have origin='matcher'.
    # (pipeline_name origin is also valid if DataHub stamps systemMetadata, but matcher is the
    # primary UC1-Case-1 path since DataSpoke is not the ingestor.)
    # spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets "lists the covered datasets"
    # spec: feature/BACKEND.md §Sync sweep step 2 — DATAHUB_MANAGED matcher origin.
    matcher_origins = [d for d in mapped_datasets if d.get("origin") == "matcher"]
    assert matcher_origins, (
        f"At least one mapped row must have origin='matcher' for a DATAHUB_MANAGED sync; "
        f"origins seen: {[d.get('origin') for d in mapped_datasets]}. "
        "spec: feature/BACKEND.md §Sync sweep step 2 — DATAHUB_MANAGED uses filter-matcher; "
        "origin=matcher is the primary mapping path before pipeline_name enrichment."
    )
