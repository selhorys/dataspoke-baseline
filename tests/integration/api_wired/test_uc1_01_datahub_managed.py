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
  8. Execute the source in DataHub via createIngestionExecutionRequest; poll the
     execution request DIRECTLY (executionRequest(urn){result{status}}) to terminal
     SUCCESS (≤180s) — the parent's executions relationship is empty by design because
     DataHub books the run on a hidden CLI wrapper. Re-run sync; verify DataSpoke
     reflects the run on the REGULAR source:
       PRIMARY:   GET /sources/{id}/event (the parent) has INGESTION.COMPLETE with
                  wrapper=true and status='success'
                  (detail.execution_request_urn is the spec'd identity key for sync-mirrored
                   DATAHUB_MANAGED events — BACKEND.md §Event Catalogue — used here to locate the
                   row).
       The wrapper source is ABSENT from GET /sources?mode=DATAHUB_MANAGED.
       SECONDARY: GET /sources/{id}/datasets has ≥1 row with derivation='pipeline_name'
                  and authority='high'; attr/ingestion latest_run reflects the run.
     Skip/fail split: the ONLY skip is the pre-trigger precondition probe — the
     acryl-datahub-actions executor reporting no ready replica, i.e. nothing can run
     the request. Every post-trigger outcome FAILS: a terminal non-success status
     (the run completed and the ingestion broke) and an exhausted 180s wait alike.
  9. Cleanup: deleteIngestionSource, deleteSecret, re-run sync to remove mirrored rows.

tests/e2e/use-case/uc1-01-datahub-managed.spec.ts walks the same UC1 Case 1 arc in the
browser and shares this file's skip/fail stance: a skip marks a precondition this run
cannot establish (GMS URL unset, PAT unset, executor unschedulable) and names how to
supply it, while every outcome the arc exists to judge is an assertion. The two files
express that stance through their own layers' gestures, so they are not step-for-step
identical.

spec: USE_CASE_en.md §UC1 Case 1
spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors the run as INGESTION.COMPLETE
      and upgrades datasets from matched/medium to pipeline_name/high
spec: API.md §Ingestion — DATAHUB_MANAGED, read-only invariant (409 INGESTION_SOURCE_READONLY)
spec: feature/BACKEND.md §Ingestion Service §Sync sweep steps 3-4
spec: BACKEND_SCHEMA.md §ingestion_source_dataset — derivation→authority pairing
spec: TESTING.md §Api-Wired Integration Tests
spec: TESTING.md §Assertion Discipline — "Skip only on an absent precondition … A test
      never skips on an outcome it exists to judge: a failed run, an empty result, or a
      wait that exhausts its budget is a failure, not a skip."
"""

import asyncio
import json
import os
import subprocess
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db
from tests.integration.util.datahub import (
    PG_INSTANCE,
    PG_PLATFORM,
    TARGET_SCHEMAS,
    ingest_pg_datasets,
    reset_datasets,
)

# In-cluster cluster-DNS address of the dummy-data postgres; mode-independent
# (the DataHub executor consumes ingestion recipes IN-CLUSTER via cluster DNS).
# Populated by install.sh; required (no default) so an unset env fails loud
# rather than guessing a namespace.
_PG_HOST_PORT = os.environ["DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT"]

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

# Stable, human-readable name for the provisioned DataHub IngestionSource. A fixed
# name (rather than a uuid suffix) lets reruns pre-clean a leftover same-named source
# in DataHub so they don't accumulate, mirroring the secret pre-delete above.
_SOURCE_NAME = "dummy datahub-managed"


def _gql_headers(datahub_token: str) -> dict[str, str]:
    """Headers for a DataHub GMS GraphQL call.

    The PAT is always sent: GMS's authentication filter rejects an unauthenticated call with
    HTTP 401, and the module-scoped fixture establishes a non-empty token as a precondition
    before any caller reaches here, so an omitted Authorization header could only produce a
    masked 401.
    """
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {datahub_token}",
    }


def _datahub_gql(
    gms_url: str,
    headers: dict[str, str],
    query: str,
    variables: dict,
    *,
    timeout: float = 15.0,
) -> dict:
    """POST a GraphQL operation to DataHub GMS and return the parsed response envelope.

    Fails — never skips — when the call does not reach the GraphQL layer at all.

    The **HTTP status** is the load-bearing discriminator. GMS's
    AuthenticationEnforcementFilter rejects a missing or stale PAT with
    ``sendError(SC_UNAUTHORIZED, …)``, which this GMS renders as an ``application/json``
    error body — ``{"timestamp":…,"status":401,"error":"Unauthorized","path":"/api/graphql"}``
    — carrying neither ``data`` nor ``errors``. A status-blind parse therefore succeeds and
    hands back a superficially valid dict whose missing fields resurface far from their cause
    (as "the mutation returned no URN"), so the 401/403 status is what identifies a rejected
    PAT here and what names the credential remedy. The content-type check is belt-and-braces
    for builds that render a non-JSON servlet error page instead; keep both.

    A GraphQL ``errors`` array means the opposite: it arrives on an HTTP-200 JSON envelope,
    so GMS accepted the credential and refused the operation. This helper returns that
    envelope unjudged; each caller judges its own operation's errors.

    spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists to
      judge"; a skip is reserved for an absent precondition whose reason "names the
      precondition and how to supply it".
    """
    resp = httpx.post(
        f"{gms_url}/api/graphql",
        headers=headers,
        json={"query": query, "variables": variables},
        timeout=timeout,
    )
    content_type = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "json" not in content_type.lower():
        credential_hint = ""
        if resp.status_code in (401, 403):
            credential_hint = (
                "This is GMS's authentication filter rejecting the PAT, so "
                "DATASPOKE_DEV_DATAHUB_TOKEN is missing or stale — re-derive it from the "
                "cluster secret and re-source helm-charts/.env.dev (a fragmented "
                "--from-component install skips that env-sync). "
            )
        raise AssertionError(
            f"DataHub GraphQL call failed before reaching the GraphQL layer: "
            f"HTTP {resp.status_code}, content-type {(content_type or 'none')!r}. "
            f"{credential_hint}Body: {resp.text[:300]}"
        )
    return resp.json()


def _probe_datahub_executor() -> Literal["ready", "unavailable", "unknown"]:
    """Pre-trigger precondition probe: is the DataHub ingestion executor schedulable?

    DataHub does not run ingestion in GMS — the ``acryl-datahub-actions`` deployment picks
    execution requests off Kafka and runs them. With no ready replica a triggered request
    never leaves PENDING: the precondition is absent and the step has nothing to judge.
    Probed BEFORE the trigger so the post-trigger wait is free to treat an exhausted budget
    as the failure it is.

    Returns "unavailable" ONLY on positive evidence of zero ready replicas. Anything that
    makes the probe itself untrustworthy — kubectl missing, namespace unset, selector
    matching nothing — returns "unknown", and an "unknown" never skips: an unreliable probe
    must not become a new mask for a real product failure.

    Selected by the chart-canonical label ``app.kubernetes.io/name=acryl-datahub-actions``
    rather than a release-derived deployment name, so a differently-named release matches.
    The NAME prefix in the jsonpath is load-bearing: Kubernetes omits ``readyReplicas``
    entirely when it is zero, so a value-only template renders a 0-ready deployment as a
    blank line — indistinguishable from "the selector matched nothing".

    spec: TESTING.md §Assertion Discipline — "Skip only on an absent precondition … an
      unconfigured dependency".
    spec: TESTING.md §End-to-End (E2E) Testing → Execution discipline — "Cluster-side setup
      reuses the existing tooling. Setup with no REST route … shells out to `kubectl` or to
      `tests/integration/util`." (stated there for E2E; this probe is the same kind of
      cluster-side setup with no REST route).
    """
    namespace = os.environ.get("DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE", "")
    if not namespace:
        return "unknown"
    try:
        completed = subprocess.run(
            [
                "kubectl",
                "get",
                "deployment",
                "-n",
                namespace,
                "-l",
                "app.kubernetes.io/name=acryl-datahub-actions",
                "-o",
                r'jsonpath={range .items[*]}{.metadata.name}={.status.readyReplicas}{"\n"}{end}',
            ],
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    # No deployment matched the label — the executor may be deployed under a shape this
    # probe does not recognise, so this is "unknown", not evidence of absence.
    if not lines:
        return "unknown"
    # `<name>=<readyReplicas>`; the value is empty when Kubernetes omitted the field (zero).
    for line in lines:
        _, _, ready = line.partition("=")
        if ready.isdigit() and int(ready) > 0:
            return "ready"
    return "unavailable"


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


@pytest_asyncio.fixture(scope="module")
async def _managed_source_setup(
    internal_headers: dict[str, str],
) -> AsyncGenerator[_ManagedSource]:
    """Provision a DataHub Secret + one IngestionSource, run sync, yield the DataSpoke id.

    Module-scoped: the provisioning (create secret + source, then poll the sync sweep
    for up to 180s until DataHub indexes the new source) runs once and is shared by
    both UC1-Case-1 tests. Neither test deletes or mutates the provisioned source's
    identity — the sync-and-readonly test only proves mutations are rejected (409),
    and the execute-and-reflect test runs the source (additive) — so a single shared
    provision is correct and halves the module's ~180s indexing wait.

    Because it is module-scoped, this fixture cannot depend on the function-scoped
    ``api_client`` / ``admin_headers`` fixtures; it opens its own module-lifetime
    async client and mints its own admin headers. Session-scoped ``internal_headers``
    is compatible and reused. The yielded ``_ManagedSource`` is an inert dataclass,
    safe to hand to the function-scoped async tests.

    The source recipe uses password = "${UC1_POSTGRES_PASSWORD}" — the DataHub-recommended
    best practice of referencing a pre-created DataHub Secret rather than embedding a
    plaintext credential.

    Teardown (guaranteed on mid-test failure): deleteIngestionSource, deleteSecret for the
    secret URN, then re-run sync to remove the mirrored DataSpoke row.

    spec: TESTING.md §Api-Wired Integration Tests — fixture teardown prevents
          managed sources leaking into DataHub for subsequent runs.
    spec: USE_CASE_en.md §UC1 Case 1 — DataHub-managed source exposed read-only via DataSpoke.
    """
    datahub_gms_url = os.environ.get("DATASPOKE_DEV_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_DEV_DATAHUB_TOKEN", "")

    # Absent preconditions this run cannot establish: the GMS endpoint and the PAT that
    # authenticates every GraphQL call below. Both name how to supply them — an empty PAT
    # would otherwise produce unauthenticated calls that GMS rejects with HTTP 401.
    # spec: TESTING.md §Assertion Discipline — "Skip only on an absent precondition …
    #   an unset credential or env var … and the skip reason names the precondition and
    #   how to supply it."
    if not datahub_gms_url:
        pytest.skip(
            "DATASPOKE_DEV_DATAHUB_GMS_URL is not set, so this UC1 Case 1 module has no "
            "DataHub GMS to provision the managed source in. "
            "Source helm-charts/.env.dev before running this test."
        )
    if not datahub_token:
        pytest.skip(
            "DATASPOKE_DEV_DATAHUB_TOKEN is not set, so every DataHub GraphQL call in "
            "this module would be unauthenticated and rejected by GMS. "
            "Source helm-charts/.env.dev before running this test (a fragmented "
            "--from-component install skips the env-sync that derives this PAT from the "
            "cluster secret; re-run the install to refresh it)."
        )

    gql_headers = _gql_headers(datahub_token)

    # Clean slate before test — spec: TESTING.md §Integration Testing §Per-Module reset
    await dataspoke_db.reset_ingestion_sources()

    # Idempotency: refresh the covered postgres datasets to a clean baseline so no
    # stale systemMetadata.pipelineName from a prior managed run survives into this
    # one. This module is the only api-wired module that WRITES durable DataHub
    # dataset state — the execute-and-reflect test runs the managed source, and
    # DataHub stamps the source URN onto the run-refreshed aspects (in practice only
    # `subTypes`, since the seed pre-populates datasetProperties/schemaMetadata with
    # no pipelineName). DataHub dedups an unchanged aspect on the next run, so a
    # second run of a fresh source URN would re-emit `subTypes="Table"` as a no-op
    # and keep the deleted prior source's pipelineName — the matched→pipeline_name
    # upgrade would then never fire (issue #77). Hard-deleting + re-ingesting the pg
    # datasets clears that pipelineName, restoring the exact post-reset-seed state
    # (Kafka datasets untouched) and making the module re-runnable without an
    # intervening --reset-seed.
    # spec: BACKEND.md §Sync sweep step 3 — pipeline_name derivation reads
    #   systemMetadata.pipelineName; spec: project_es_indexing_lag_after_reset_seed.
    reset_datasets(platform=PG_PLATFORM)
    await ingest_pg_datasets(DUMMY_DATA_DATAHUB_SCHEMAS)

    # Idempotency: drop any leftover DataHub Secret from a prior interrupted run.
    # DataHub Secrets are name-keyed (urn:li:dataHubSecret:<name>) and survive a DataSpoke
    # reset-seed, so without this createSecret below fails with "This Secret already exists!".
    # Best-effort at the GraphQL layer only: the returned envelope is not judged, so GraphQL
    # errors from an absent secret are ignored (mirrors the teardown deleteSecret and the e2e
    # uc1-01 beforeAll pre-delete). Anything that never reached the GraphQL layer raises out of
    # _datahub_gql, so a stale PAT is reported here rather than masked — except a transport
    # blip against the ingress, tolerated the same way as the leftover-source pre-delete
    # below because a genuinely down GMS fails loudly at the provisioning that follows.
    try:
        _datahub_gql(
            datahub_gms_url,
            gql_headers,
            "mutation deleteSecret($urn: String!) { deleteSecret(urn: $urn) }",
            {"urn": f"urn:li:dataHubSecret:{_SECRET_NAME}"},
            timeout=10.0,
        )
    except httpx.TransportError:
        pass

    # Idempotency: drop any leftover DataHub IngestionSource with the same fixed name
    # from a prior interrupted run. The source name is stable (_SOURCE_NAME), so a
    # leftover would otherwise accumulate as a duplicate. Best-effort — list the
    # sources, delete any whose name matches. Mirrors the secret pre-delete above and the
    # e2e uc1-01 beforeAll pre-delete. A transport blip here is tolerated (the provisioning
    # below fails loudly on its own if GMS is really down), but a credential rejection
    # propagates out of _datahub_gql so it is reported at its cause.
    try:
        leftovers_data = _datahub_gql(
            datahub_gms_url,
            gql_headers,
            (
                "query listIngestionSources($input: ListIngestionSourcesInput!) {"
                " listIngestionSources(input: $input) {"
                " ingestionSources { urn name } } }"
            ),
            {"input": {"start": 0, "count": 100}},
        )
        leftover_sources = (
            (leftovers_data.get("data") or {}).get("listIngestionSources") or {}
        ).get("ingestionSources", [])
        for src in leftover_sources:
            if src.get("name") == _SOURCE_NAME and src.get("urn"):
                _datahub_gql(
                    datahub_gms_url,
                    gql_headers,
                    (
                        "mutation deleteIngestionSource($urn: String!) {"
                        " deleteIngestionSource(urn: $urn) }"
                    ),
                    {"urn": src["urn"]},
                    timeout=10.0,
                )
    except httpx.TransportError:
        pass

    # ── Step 1a: Create DataHub Secret ────────────────────────────────────────
    # spec: feature/BACKEND.md §Ingestion Service — sync sweep step 1: ${...} secret
    #   references are preserved as-is (not masked, not resolved). The fixture stores the
    #   credential in a DataHub Secret and references it as ${SECRET_NAME} in the recipe.
    # The secret value itself must NEVER appear in any DataSpoke API response.
    create_secret_mutation = """
    mutation createSecret($input: CreateSecretInput!) {
        createSecret(input: $input)
    }
    """
    secret_data = _datahub_gql(
        datahub_gms_url,
        gql_headers,
        create_secret_mutation,
        {
            "input": {
                "name": _SECRET_NAME,
                "value": _PLAINTEXT_PW_IN_FIXTURE,
                "description": "UC1 test secret: postgres password for DATAHUB_MANAGED fixture",
            }
        },
    )
    # A GraphQL error here is a BROKEN dependency, not an absent one: reaching the GraphQL
    # layer at all means GMS authenticated the caller (_datahub_gql raises before this on an
    # auth rejection), and Managed Ingestion — Secrets included — is provisioned by the dev
    # install. Skipping would report a DataHub that cannot hold a Managed Secret as green.
    # spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists
    #   to judge"; skips are reserved for an absent precondition.
    assert "errors" not in secret_data, (
        f"createSecret returned GraphQL errors: {secret_data['errors']}. "
        "GMS accepted the credential and refused the operation, so two causes produce this: "
        "(1) the authenticated actor is under-privileged — the PAT's actor lacks "
        "MANAGE_SECRETS, so grant it (or use an admin PAT) and refresh "
        "DATASPOKE_DEV_DATAHUB_TOKEN in helm-charts/.env.dev; or (2) Managed Secrets are "
        "broken or absent in this GMS — they are provisioned by the dev install "
        "(./helm-charts/bin/install.sh --profile dev --components datahub). Check the "
        "privilege before reinstalling DataHub. Neither is an absent precondition."
    )
    secret_urn = secret_data.get("data", {}).get("createSecret")
    assert secret_urn, f"createSecret returned no URN: {secret_data}"

    # ── Step 1b: Create IngestionSource — secret-ref recipe ──────────────────
    # spec: feature/BACKEND.md §Sync sweep step 1 — ${...} secret references are
    #   preserved as-is (not masked, not resolved).
    # The password field holds only the reference; the actual credential value is
    # stored in the DataHub Secret and never returned by DataHub to DataSpoke.
    name = _SOURCE_NAME
    recipe = {
        "source": {
            "type": "postgres",
            "config": {
                "host_port": _PG_HOST_PORT,
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
    gql_data = _datahub_gql(
        datahub_gms_url,
        gql_headers,
        create_mutation,
        {
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
    )
    # Same stance as createSecret above: the call authenticated (_datahub_gql raises
    # otherwise) and Managed Ingestion is part of the dev DataHub stack this module's
    # GMS-URL precondition names, so a GraphQL error is a broken dependency and fails.
    # spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists
    #   to judge".
    assert "errors" not in gql_data, (
        f"createIngestionSource (secret-ref) returned GraphQL errors: {gql_data['errors']}. "
        "GMS accepted the credential and refused the operation, so two causes produce this: "
        "(1) the authenticated actor is under-privileged — the PAT's actor lacks the Manage "
        "Ingestion privilege, so grant it (or use an admin PAT) and refresh "
        "DATASPOKE_DEV_DATAHUB_TOKEN in helm-charts/.env.dev; or (2) Managed Ingestion is "
        "broken or absent in this GMS — it is provisioned by the dev install "
        "(./helm-charts/bin/install.sh --profile dev --components datahub). Check the "
        "privilege before reinstalling DataHub. Neither is an absent precondition."
    )
    urn = gql_data.get("data", {}).get("createIngestionSource")
    assert urn, f"createIngestionSource returned no URN: {gql_data}"

    # Module-lifetime API client + admin headers. Created here, past the early
    # skip/assert guards above (so no client leaks on a skipped provision), and
    # closed at the end of the teardown finally below. Minted directly (not via the
    # function-scoped api_client/admin_headers fixtures) so this fixture can be
    # module-scoped. spec: TESTING.md §Api-Wired Integration Tests.
    from tests.integration.util.auth import login_headers

    api_base_url = f"http://api.{os.environ['DATASPOKE_KUBE_INGRESS_DOMAIN']}"
    admin_headers = login_headers(api_base_url, "dataspoke@dataspoke.local", "dataspoke")
    api_client = httpx.AsyncClient(base_url=api_base_url, timeout=30.0)

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
        # Best-effort: a teardown failure must not mask the test's own outcome.
        try:
            _datahub_gql(
                datahub_gms_url,
                gql_headers,
                delete_mutation,
                {"urn": urn},
                timeout=10.0,
            )
        except Exception:
            pass

        try:
            _datahub_gql(
                datahub_gms_url,
                gql_headers,
                delete_secret_mutation,
                {"urn": secret_urn},
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

        # Close the module-lifetime client opened for the provisioning + teardown.
        await api_client.aclose()


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
    # A single blip against the ingress must not fail the run (a laptop→LB drop is transient),
    # so the sweep's own outcome is recorded rather than asserted per iteration — and carried
    # into the post-loop failure below, so a sweep that 500s on every pass is reported as
    # itself instead of being misattributed to ES lag.
    last_sync_outcome: str = "not attempted"
    while time.time() < poll_deadline:
        # Re-trigger the sync sweep to pick up any newly-indexed DataHub URNs
        try:
            sync_poll_resp = await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
            last_sync_outcome = f"HTTP {sync_poll_resp.status_code}: {sync_poll_resp.text[:200]}"
        except httpx.HTTPError as exc:
            last_sync_outcome = f"transport error: {exc!r}"

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
        f"Last POST /internal/activities/ingestion/sync outcome: {last_sync_outcome} — if "
        "that is not a 200, the sweep itself failed and the empty mapping is its symptom, "
        "not ES lag. "
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
    #      Only an empty system-typed set skips — there is then genuinely nothing to
    #      judge. A GMS that is unreachable or refuses the list query fails: the module's
    #      preconditions (GMS URL + PAT) were established by the fixture, so those are
    #      broken-dependency outcomes, not absent ones.
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
    # Both are non-empty here — the module-scoped fixture skips the module when either is
    # unset, so a test body only runs once they are established.
    datahub_gms_url = os.environ["DATASPOKE_DEV_DATAHUB_GMS_URL"]
    datahub_token = os.environ["DATASPOKE_DEV_DATAHUB_TOKEN"]
    gql_headers_guard = _gql_headers(datahub_token)

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
    # _datahub_gql fails (never skips) when the call does not reach the GraphQL layer: a
    # servlet 401/403 names the PAT remedy instead of being misreported as unreachable
    # infrastructure, and any other transport/HTTP outcome fails against a GMS this module
    # already provisioned a source in.
    gms_data = _datahub_gql(
        datahub_gms_url,
        gql_headers_guard,
        list_sources_query,
        {"input": {"start": 0, "count": 100}},
    )

    # Errors on an HTTP-200 envelope mean GMS authenticated the caller and refused the
    # query — Managed Ingestion broken or the actor under-privileged, both outcomes rather
    # than absent preconditions.
    # spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists
    #   to judge".
    assert "errors" not in gms_data, (
        f"listIngestionSources returned GraphQL errors: {gms_data['errors']}. "
        "GMS accepted the credential and refused the query, so either the PAT's actor "
        "lacks the Manage Ingestion privilege (grant it, or use an admin PAT, and refresh "
        "DATASPOKE_DEV_DATAHUB_TOKEN in helm-charts/.env.dev) or Managed Ingestion is "
        "broken in this GMS (./helm-charts/bin/install.sh --profile dev --components "
        "datahub). Neither is an absent precondition."
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
    # An absent precondition: with no system-typed source in DataHub there is nothing whose
    # exclusion could be judged, and the absence assertions below would pass vacuously.
    # spec: TESTING.md §Assertion Discipline — "Absence assertions require injection"; and
    #   "Skip only on an absent precondition … the skip reason names the precondition and
    #   how to supply it."
    has_system_typed_source = bool(gms_system_typed_urns) or (_GC_URN in gms_urns)
    if not has_system_typed_source:
        pytest.skip(
            f"No system-typed source (type ∈ {_SYSTEM_SOURCE_TYPES}) found in DataHub's "
            f"unfiltered listIngestionSources (returned {len(gms_urns)} source(s)) "
            f"and bare {_GC_URN!r} is also absent, so the deny-list guard has nothing to "
            "exclude and its absence assertions would be vacuous. Supply the precondition "
            "by installing a DataHub whose bootstrap provisions the system ingestion "
            "sources (./helm-charts/bin/install.sh --profile dev --components datahub), "
            "which creates datahub-gc and datahub-documents."
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
    """UC1 Case 1 step 8 — execute source in DataHub; DataSpoke reflects the run on the
    regular source via the hidden CLI wrapper linkage.

    Narrative from USE_CASE_en.md §UC1 Case 1:
      "When the source runs in DataHub — on its daily schedule or a run triggered
       manually — DataSpoke's next sync mirrors that execution into …/event as an
       INGESTION.COMPLETE event and upgrades the covered datasets from matcher-mapped
       (derivation=matched, authority=medium) to run-observed (derivation=pipeline_name,
       authority=high), because DataHub stamps the source's identity on the aspects
       the run emits."

    Mechanism — where DataHub books a managed source's run depends on the trigger path.
    An **API-triggered** run (createIngestionExecutionRequest on the source URN, the path
    this test uses) books the execution **directly on the registered source**, so the
    event surfaces on the source itself with ``wrapper=false``. A **CLI/schedule-run**
    path instead books the run on an auto-created CLI wrapper source (own URN …:cli-<hash>)
    linked to the registered parent, so its event surfaces on the parent tagged
    ``wrapper=true``. Either way DataSpoke surfaces the run ON the regular parent the user
    looks at; the ``wrapper`` flag is derived per-event and depends on which source the
    run was booked against, so this test accepts either value.

    We poll the execution request DIRECTLY by its URN
    (executionRequest(urn){ result { status } }) rather than via the parent's executions,
    since the parent's ``executions`` relationship may be empty on the CLI-wrapper path.
    (ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql:
     executionRequest(urn: String!): ExecutionRequest { result { status } };
     ref/github/datahub/smoke-test/tests/managed_ingestion/managed_ingestion_test.py
     _ensure_execution_request_present — confirmed query shape.)

      - Trigger: createIngestionExecutionRequest(input: {ingestionSourceUrn}) → exec URN
      - Poll:    executionRequest(urn){ result { status } }
                 until status ∈ {SUCCESS, SUCCEEDED} (≤180s budget)
      - Re-sync: POST /internal/activities/ingestion/sync
      - PRIMARY:   GET /sources/{id}/event (the REGULAR parent) → exactly ONE
                   INGESTION.COMPLETE with status='success' for this run, and ZERO
                   INGESTION.FAIL — and that holds across repeated syncs (idempotent
                   upsert keyed on the execution-request URN; no spurious FAIL minted)
                   (detail.execution_request_urn is the spec'd identity key for sync-mirrored
                    DATAHUB_MANAGED events per BACKEND.md §Event Catalogue; used to locate the row)
      - The wrapper source is ABSENT from GET /sources?mode=DATAHUB_MANAGED (hidden)
      - SECONDARY: GET /sources/{id}/datasets → ≥1 row with derivation='pipeline_name'
                   and authority='high'
      - attr/ingestion latest_run on a covered dataset reflects the run (status='success')

    Skip/fail split: the ONLY skip is the pre-trigger precondition probe — the
    acryl-datahub-actions executor reporting no ready replica, i.e. nothing in the cluster
    can run the request. Every post-trigger outcome FAILS: GraphQL errors on the trigger,
    a trigger that books no execution request, an exhausted 180s wait, and a terminal
    non-success status (the executor ran the ingestion to completion and it broke — the
    product failure this test exists to detect).
    spec: TESTING.md §Assertion Discipline — "Skip only on an absent precondition … A test
      never skips on an outcome it exists to judge: a failed run, an empty result, or a
      wait that exhausts its budget is a failure, not a skip."

    spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors the run as
          INGESTION.COMPLETE and upgrades datasets from matched/medium to pipeline_name/high
    spec: feature/BACKEND.md §Sync sweep step 1 — wrappers hidden from the list; linked
          to the regular parent via parent_source_id
    spec: feature/BACKEND.md §Sync sweep step 3 — observed enrichment writes
          derivation='pipeline_name' / authority='high' when DataHub stamps pipelineName
    spec: feature/BACKEND.md §Sync sweep step 4 — the regular source aggregates events
          across itself and its linked wrappers; each event carries a derived wrapper flag
    spec: API.md §Ingestion — GET /sources/{id}/event includes linked-wrapper events
          carrying wrapper: bool; the list returns regular sources only;
          attr/ingestion latest_run spans the union
    spec: feature/BACKEND.md §Sync sweep step 4 — one event per execution-request URN,
          upserted (no per-sync growth); occurred_at from startTimeMs/requestedAt; only
          terminal outcomes mirrored (SUCCESS→COMPLETE, no spurious FAIL)
    spec: BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name→high derivation/authority
    """
    managed = _managed_source_setup

    # The GMS URL and the PAT are established by _managed_source_setup, which skips this
    # test's whole module when either is unset — a module-scoped fixture that skips during
    # setup skips every requesting test without running its body. Reading them here is
    # therefore unconditional: both are non-empty by the time this line executes.
    datahub_gms_url = os.environ["DATASPOKE_DEV_DATAHUB_GMS_URL"]
    datahub_token = os.environ["DATASPOKE_DEV_DATAHUB_TOKEN"]

    gql_headers = _gql_headers(datahub_token)

    # ── Precondition (pre-trigger): the DataHub executor can run the request at all ──
    # The ONLY skip past this point. Everything after the trigger is an outcome this test
    # exists to judge and therefore fails rather than skips. An "unknown" probe result does
    # NOT skip: an unreliable probe must not become a new mask for a real product failure.
    # spec: TESTING.md §Assertion Discipline — "Skip only on an absent precondition …
    #   an unconfigured dependency — and the skip reason names the precondition and how to
    #   supply it."
    executor_state = _probe_datahub_executor()
    if executor_state == "unavailable":
        datahub_namespace = os.environ.get("DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE", "")
        pytest.skip(
            "DataHub ingestion executor is not schedulable: the acryl-datahub-actions "
            f"deployment in namespace {datahub_namespace} has 0 ready replicas, so a "
            "triggered execution request would never leave PENDING. Supply the "
            f"precondition with: kubectl -n {datahub_namespace} scale deployment "
            "-l app.kubernetes.io/name=acryl-datahub-actions --replicas=1 (or reinstall: "
            "./helm-charts/bin/install.sh --profile dev --components datahub)."
        )

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
    # _datahub_gql fails on a call that never reaches the GraphQL layer: a servlet 401/403
    # names the PAT remedy rather than being misattributed to executor availability.
    exec_data = _datahub_gql(
        datahub_gms_url,
        gql_headers,
        exec_mutation,
        {"input": {"ingestionSourceUrn": managed.urn}},
        timeout=20.0,
    )

    # Post-trigger: everything from here on is an outcome this test exists to judge. The
    # executor's schedulability — the one absent-precondition case — was settled above, so
    # a GraphQL error on the trigger is DataHub refusing to book a run it is provisioned
    # to book, and fails.
    # spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists
    #   to judge: a failed run, an empty result, or a wait that exhausts its budget is a
    #   failure, not a skip."
    assert "errors" not in exec_data, (
        f"createIngestionExecutionRequest returned GraphQL errors: {exec_data['errors']}. "
        f"GMS accepted the credential and refused the operation; the executor probe "
        f"reported {executor_state!r} before this trigger, so DataHub refused a run it is "
        "provisioned to accept. Check the PAT actor's Manage Ingestion privilege and the "
        "GMS logs before dismissing this as env noise."
    )
    execution_request_urn: str = (
        exec_data.get("data", {}).get("createIngestionExecutionRequest") or ""
    )
    # An errorless mutation that yields no URN is the same class of outcome: the trigger
    # did not take, and there is nothing left for the reflect assertions to observe.
    assert execution_request_urn, (
        f"createIngestionExecutionRequest returned no execution-request URN: {exec_data}. "
        "The mutation reported no error, so DataHub accepted the request and still booked "
        "no run — the execution beat UC1 Case 1 asserts never started. "
        "spec: USE_CASE_en.md §UC1 Case 1 — execution beat."
    )

    # ── Step 8b: Poll the execution request DIRECTLY to terminal SUCCESS (≤180s) ─
    # By design DataHub books a managed source's run on a CLI wrapper source, so the
    # PARENT's `executions` relationship is empty. Query the execution request by its
    # own URN instead (executionRequest(urn){ result { status } }).
    # spec: ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
    #   executionRequest(urn: String!): ExecutionRequest { result { status } }
    # spec: ref/github/datahub/smoke-test/tests/managed_ingestion/managed_ingestion_test.py
    #   _ensure_execution_request_present — confirmed query shape.
    #   result.status: String! — per the spec status→event mapping (BACKEND.md §Sync step 4):
    #     SUCCESS / SUCCEEDED → INGESTION.COMPLETE (→ test succeeds)
    #     FAILURE / TIMEOUT / ABORTED / ROLLBACK_FAILED → INGESTION.FAIL
    #       (the executor ran the ingestion to completion and it broke → test fails)
    #     RUNNING / ROLLING_BACK / UP_FOR_RETRY / PENDING → in-progress → keep polling
    #     CANCELLED / DUPLICATE / ROLLED_BACK → not an ingestion outcome, not mirrored;
    #       terminal all the same, so the loop ends on them and the terminal-outcome
    #       assertion names them at their cause (→ test fails)
    #     None / absent result → still pending → keep polling
    poll_query = """
    query executionRequest($urn: String!) {
        executionRequest(urn: $urn) {
            urn
            result {
                status
            }
        }
    }
    """
    # Success statuses map to INGESTION.COMPLETE per BACKEND.md §Sync step 4 status table.
    _SUCCESS_STATUSES = frozenset({"SUCCESS", "SUCCEEDED"})
    # Only the spec's in-progress tier keeps the loop running: the request has not yet
    # reached an outcome, so waiting is the right response.
    # spec: feature/BACKEND.md §Sync sweep step 4 status table — "RUNNING, ROLLING_BACK,
    #   UP_FOR_RETRY, no result | not mirrored (in-progress / pending)". PENDING is DataHub's
    #   name for the queued state the table describes as "pending", so it belongs here.
    _IN_PROGRESS_STATUSES = frozenset(
        {"PENDING", "RUNNING", "ROLLING_BACK", "UP_FOR_RETRY"}
    )
    # Every other status is terminal and ends the loop: SUCCESS/SUCCEEDED, the hard failures
    # (FAILURE/TIMEOUT/ABORTED/ROLLBACK_FAILED), and the statuses the spec classes as "not an
    # ingestion outcome". The last group is terminal too — the request will never produce an
    # ingestion result — so breaking on it reports the outcome at its cause within seconds
    # rather than burning the whole 180s budget and reporting an exhausted wait instead.
    # spec: feature/BACKEND.md §Sync sweep step 4 status table — "CANCELLED, DUPLICATE,
    #   ROLLED_BACK | not mirrored (not an ingestion outcome)".
    _NOT_AN_INGESTION_OUTCOME = frozenset({"CANCELLED", "DUPLICATE", "ROLLED_BACK"})

    poll_deadline = time.time() + 180.0
    poll_interval = 8.0
    exec_status: str | None = None
    last_poll_errors: list | None = None
    # Only a repeated transport blip is retried. A credential rejection, any other HTTP
    # outcome, and a non-JSON body all raise out of _datahub_gql immediately, so they
    # surface at their cause instead of being retried silently for the whole 180s window.
    consecutive_transport_failures = 0
    _MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3

    while time.time() < poll_deadline:
        try:
            poll_data = _datahub_gql(
                datahub_gms_url,
                gql_headers,
                poll_query,
                {"urn": execution_request_urn},
            )
        except httpx.TransportError as exc:
            consecutive_transport_failures += 1
            if consecutive_transport_failures > _MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                raise AssertionError(
                    f"Polling execution {execution_request_urn!r} hit "
                    f"{consecutive_transport_failures} consecutive transport failures "
                    f"against {datahub_gms_url}; the last was: {exc!r}. DataHub GMS is not "
                    "reachable from this run, so the execution outcome cannot be observed."
                ) from exc
            await asyncio.sleep(poll_interval)
            continue
        consecutive_transport_failures = 0

        # GraphQL errors on the poll query do not end the wait — a request that is not yet
        # readable is indistinguishable here from one that never will be — but the last set
        # is carried into the exhausted-budget failure below so the cause is visible there.
        last_poll_errors = poll_data.get("errors")
        exec_request = (poll_data.get("data", {}) or {}).get("executionRequest") or {}
        result = exec_request.get("result") or {}
        status = result.get("status") or None
        if status and status not in _IN_PROGRESS_STATUSES:
            # Terminal: success, hard failure, or a not-an-ingestion-outcome status.
            exec_status = status
            break
        await asyncio.sleep(poll_interval)

    # The wait exhausted its budget. The executor's schedulability was settled before the
    # trigger, so this is the run failing to complete — an outcome this test judges.
    # spec: TESTING.md §Assertion Discipline — "A test never skips on an outcome it exists
    #   to judge: a failed run, an empty result, or a wait that exhausts its budget is a
    #   failure, not a skip."
    _datahub_namespace = os.environ.get("DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE", "<datahub-ns>")
    assert exec_status is not None, (
        f"Execution {execution_request_urn!r} did not reach a terminal status within 180s. "
        f"The executor probe reported {executor_state!r} before the trigger, so the request "
        f"is stuck in progress ({'/'.join(sorted(_IN_PROGRESS_STATUSES))}, or no result at "
        "all) or unreadable. "
        f"Last GraphQL errors seen while polling: {last_poll_errors}. "
        f"Inspect the executor: kubectl -n {_datahub_namespace} logs "
        "-l app.kubernetes.io/name=acryl-datahub-actions --tail=200, and the request's own "
        "status in DataHub (Ingestion → the source → its runs)."
    )

    # A TERMINAL non-success status is the opposite case: the request will never yield the
    # successful ingestion UC1 Case 1 asserts. That is the product failure this test exists
    # to detect, so it fails — skipping here would report a permanently broken
    # DataHub-managed run as green.
    # spec: USE_CASE_en.md §UC1 Case 1 — "DataSpoke's next sync mirrors that execution into
    #   …/event as an INGESTION.COMPLETE event"; INGESTION.COMPLETE is the success type.
    # spec: feature/BACKEND.md §Sync sweep step 4 — SUCCESS/SUCCEEDED→INGESTION.COMPLETE,
    #   FAILURE/TIMEOUT/ABORTED/ROLLBACK_FAILED→INGESTION.FAIL, and CANCELLED/DUPLICATE/
    #   ROLLED_BACK are "not an ingestion outcome".
    if exec_status in _NOT_AN_INGESTION_OUTCOME:
        _outcome_note = (
            "DataHub classes this status as 'not an ingestion outcome', so the run this "
            "test triggered was superseded or discarded before producing one — check "
            "whether a concurrent run of the same source is racing this test."
        )
    else:
        _outcome_note = (
            "The DataHub executor ran the ingestion to completion and it failed — the run "
            "outcome UC1 Case 1 asserts."
        )
    assert exec_status in _SUCCESS_STATUSES, (
        f"Execution {execution_request_urn!r} reached TERMINAL status {exec_status!r}, not "
        f"one of {sorted(_SUCCESS_STATUSES)}. {_outcome_note} Inspect the "
        "execution request's logs in DataHub (Ingestion → the source → the run) "
        "before dismissing this as env noise."
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

    # ── Step 8d: PRIMARY — the regular parent's event log surfaces the wrapper run ─
    # The run was booked on the hidden CLI wrapper; DataSpoke surfaces it ON the regular
    # parent the user looks at, tagged wrapper=true.
    # spec: USE_CASE_en.md §UC1 Case 1 — "DataSpoke's next sync mirrors that execution
    #   into …/event as an INGESTION.COMPLETE event"
    # spec: feature/BACKEND.md §Sync sweep step 4 — the regular source aggregates events
    #   across itself and its linked wrappers; each carries a derived wrapper flag.
    # spec: API.md §Ingestion — GET /sources/{id}/event includes linked-wrapper events
    #   carrying wrapper: bool.
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
                # Identity key: execution_request_urn is the spec'd identity key for
                # sync-mirrored DATAHUB_MANAGED events (BACKEND.md §Event Catalogue — the
                # sweep upserts at most one event per execution-request URN); used to pick
                # the row for THIS run.
                detail = evt.get("detail") or {}
                if detail.get("execution_request_urn") == execution_request_urn:
                    found_event = evt
                    break
        if found_event is not None:
            break
        await asyncio.sleep(2.0)

    assert found_event is not None, (
        f"Expected an INGESTION.COMPLETE event with "
        f"detail.execution_request_urn={execution_request_urn!r} on the REGULAR parent's "
        f"GET /sources/{managed.id}/event within 30s after sync. "
        f"Events returned: {event_body.get('events', [])}. "
        "spec: USE_CASE_en.md §UC1 Case 1 — sync mirrors run as INGESTION.COMPLETE event. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — the regular source aggregates the "
        "wrapper's run events."
    )

    # The event carries a derived ``wrapper`` flag (bool), but its value depends on which
    # DataHub source the run was booked against. The API-trigger path this test uses
    # (createIngestionExecutionRequest on the source URN) books the run directly on the
    # registered source → wrapper=false; a CLI/schedule run instead books it on a linked
    # CLI wrapper → wrapper=true. Either way the run surfaces on the regular parent, so we
    # assert only that the flag is a present boolean — not a fixed value.
    # spec: API.md §Ingestion — GET /sources/{id}/event rows carry a derived wrapper: bool.
    # spec: feature/BACKEND_SCHEMA.md §events — wrapper derived at read time, never stored.
    assert isinstance(found_event.get("wrapper"), bool), (
        f"The mirrored run event must carry a derived boolean 'wrapper' flag; got "
        f"{found_event.get('wrapper')!r}. The value depends on the trigger path "
        "(API-trigger → false, books on the source; CLI/schedule → true, books on a "
        "linked wrapper). spec: API.md §Ingestion — derived wrapper flag."
    )

    # Verify the event's status.
    # spec: feature/BACKEND.md §Sync sweep step 4 — DataHub execution status mapping:
    #   SUCCESS/SUCCEEDED → INGESTION.COMPLETE → status='success'.
    # (detail.execution_request_urn was used above to locate the right row; it is the
    #  spec'd identity key for sync-mirrored DATAHUB_MANAGED events per
    #  BACKEND.md §Event Catalogue.)
    assert found_event.get("status") == "success", (
        f"INGESTION.COMPLETE event must carry status='success'; "
        f"got {found_event.get('status')!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — SUCCESS→INGESTION.COMPLETE→status='success'."
    )

    # ── Step 8d-ter: idempotency — exactly ONE COMPLETE, ZERO FAIL across re-syncs ─
    # The redesign keys each mirrored event on the execution-request URN and upserts it,
    # so syncing the same completed run repeatedly must NOT mint a second COMPLETE nor any
    # spurious FAIL — the bug this redesign fixes (a fresh FAIL minted on every sync,
    # leaving latest_run permanently 'failure' and events growing unbounded).
    # spec: feature/BACKEND.md §Sync sweep step 4 — 'One DataSpoke event per execution
    #   request, upserted by its URN ... repeated syncs ... are idempotent (no per-sync
    #   event growth)'; only terminal outcomes are mirrored, RUNNING/CANCELLED/etc. produce
    #   no event, so a SUCCESS run never yields a FAIL.
    def _events_for_this_run(events: list) -> tuple[int, int]:
        """Count (COMPLETE, FAIL) events for THIS execution-request URN."""
        completes = sum(
            1
            for e in events
            if e.get("event_type") == "INGESTION.COMPLETE"
            and (e.get("detail") or {}).get("execution_request_urn") == execution_request_urn
        )
        fails = sum(
            1
            for e in events
            if e.get("event_type") == "INGESTION.FAIL"
            and (e.get("detail") or {}).get("execution_request_urn") == execution_request_urn
        )
        return completes, fails

    # Sync three more times; the event count for this run must not grow and no FAIL appears.
    for sync_pass in range(3):
        resync = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
        )
        assert resync.status_code == 200, (
            f"Idempotency re-sync pass {sync_pass} expected 200; got {resync.status_code}: "
            f"{resync.text}"
        )

    event_after_resp = await api_client.get(
        f"/api/v1/spoke/ingestion/sources/{managed.id}/event",
        headers=admin_headers,
    )
    assert event_after_resp.status_code == 200, event_after_resp.text
    events_after = event_after_resp.json().get("events", [])
    complete_count, fail_count = _events_for_this_run(events_after)

    assert complete_count == 1, (
        f"After repeated syncs there must be exactly ONE INGESTION.COMPLETE event for run "
        f"{execution_request_urn!r}; got {complete_count}. "
        f"Events: {events_after}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — upsert keyed on the execution-request "
        "URN; repeated syncs do not duplicate or grow the event set."
    )
    assert fail_count == 0, (
        f"A SUCCESS run must NOT mint any INGESTION.FAIL event for run "
        f"{execution_request_urn!r} on any sync; got {fail_count} FAIL event(s). "
        f"Events: {events_after}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — only terminal outcomes are mirrored "
        "(SUCCESS→COMPLETE); no spurious FAIL is minted per-sync."
    )

    # ── Step 8d-bis: the CLI wrapper is ABSENT from the source list ───────────
    # The wrapper is internal plumbing; the list returns regular DATAHUB_MANAGED sources
    # only. There must be exactly one row carrying our datahub_source_urn (the regular
    # parent), and no row whose URN is a CLI wrapper (…:cli-…).
    # spec: API.md §Ingestion — DataHub CLI wrapper sources are internal and never listed.
    # spec: feature/BACKEND.md §Sync sweep step 1 — list_sources hides wrappers.
    list_after_resp = await api_client.get(
        "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100",
        headers=admin_headers,
    )
    assert list_after_resp.status_code == 200, list_after_resp.text
    listed_after = list_after_resp.json().get("sources", [])
    listed_urns = [s.get("datahub_source_urn") for s in listed_after]
    assert managed.urn in listed_urns, (
        f"The regular DATAHUB_MANAGED parent {managed.urn!r} must remain in the list; "
        f"got {listed_urns}. spec: API.md §Ingestion — regular sources are listed."
    )
    wrapper_urns_listed = [
        u for u in listed_urns if u and "dataHubIngestionSource:cli-" in u
    ]
    assert not wrapper_urns_listed, (
        f"No CLI wrapper source may appear in GET /sources?mode=DATAHUB_MANAGED; found "
        f"{wrapper_urns_listed}. "
        "spec: API.md §Ingestion — DataHub CLI wrapper sources are internal, never listed. "
        "spec: feature/BACKEND.md §Sync sweep step 1 — wrappers hidden from the list."
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
    # index a settle window via the same sync+poll pattern.
    # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s
    #   (matches the 180s budget used by this test's other ES-dependent steps above).
    datasets_body: dict = {}
    pipeline_name_rows: list = []
    deadline = time.time() + 180.0
    # Same stance as the mapping poll in the sync-and-readonly test: record the sweep's
    # outcome per pass instead of asserting it (one transient LB drop must not fail the run)
    # and surface the last one in the post-loop failure, so a sweep that fails every pass is
    # reported as itself rather than as missing pipelineName enrichment.
    last_sync_outcome = "not attempted"
    while time.time() < deadline:
        # Re-trigger sync so any freshly-indexed pipelineName aspects are picked up.
        try:
            sync_poll_resp = await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
            last_sync_outcome = f"HTTP {sync_poll_resp.status_code}: {sync_poll_resp.text[:200]}"
        except httpx.HTTPError as exc:
            last_sync_outcome = f"transport error: {exc!r}"

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
        f"and re-sync (within 180s). "
        f"Datasets returned: {datasets_body.get('datasets', [])}. "
        f"Last POST /internal/activities/ingestion/sync outcome: {last_sync_outcome} — if "
        "that is not a 200, the sweep itself failed and the missing upgrade is its symptom, "
        "not absent pipelineName stamping. "
        "spec: USE_CASE_en.md §UC1 Case 1 — execution upgrades datasets from matched/medium "
        "to pipeline_name/high via DataHub systemMetadata.pipelineName stamping. "
        "spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets upserts "
        "pipeline_name rows where pipelineName matches datahub_source_urn."
    )

    # ── Step 8f: attr/ingestion latest_run on a covered dataset reflects the run ─
    # The per-dataset reverse-lookup aggregates the source's runs and those booked on
    # its internal wrappers, so latest_run on a covered dataset shows the run that just
    # completed (status='success').
    # spec: API.md §Ingestion — attr/ingestion latest_run spans the source's own runs and
    #   those booked on its internal wrappers.
    # spec: feature/BACKEND.md §Sync sweep step 4 — per-dataset latest-run aggregation
    #   unions the parent's own events with its wrappers' events.
    covered_urn = pipeline_name_rows[0]["dataset_urn"]
    attr_resp = await api_client.get(
        f"/api/v1/spoke/common/data/{covered_urn}/attr/ingestion",
        headers=admin_headers,
    )
    assert attr_resp.status_code == 200, (
        f"GET attr/ingestion for {covered_urn} expected 200, "
        f"got {attr_resp.status_code}: {attr_resp.text}"
    )
    attr_body = attr_resp.json()
    # The covered dataset must resolve to our regular parent source.
    assert attr_body.get("source_id") == managed.id, (
        f"attr/ingestion for the covered dataset must resolve to the regular parent "
        f"source_id={managed.id!r}; got {attr_body.get('source_id')!r}. "
        "spec: feature/BACKEND.md §Querying Events — reverse-lookup resolves the covering "
        "source. (The regular-parent-over-wrapper tiebreak asserted here is impl behavior, "
        "not explicitly stated in the spec — flagged.)"
    )
    latest_run = attr_body.get("latest_run")
    assert latest_run is not None, (
        f"attr/ingestion latest_run must reflect the completed run, not null; "
        f"got {attr_body!r}. "
        "spec: API.md §Ingestion — latest_run spans the source's own runs and its wrappers'."
    )
    assert latest_run.get("status") == "success", (
        f"attr/ingestion latest_run.status must be 'success' after the SUCCESS run; "
        f"got {latest_run.get('status')!r}. "
        "spec: feature/BACKEND.md §Sync sweep step 4 — SUCCESS/SUCCEEDED→INGESTION.COMPLETE→"
        "status='success'; latest-run aggregation spans the source + its linked wrappers."
    )
