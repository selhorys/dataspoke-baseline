"""Spot tests — Ingestion Control: per-source model.

Concerns covered (one per test):
1. Create ACTIVE_CUSTOM_MANAGED postgres catalog source with ${dummy-data-pg__password}
   → 201; GET returns {mode, name, schedule, recipe} with ${...} intact; no schedule_tier on wire.
2. Create / update / delete DATAHUB_MANAGED source → 409 INGESTION_SOURCE_READONLY.
3. POST .../method/run on PASSIVE source → 409 INGESTION_RUN_NOT_APPLICABLE.
4. GET /spoke/ingestion/secrets lists refs (name__key, secret_name, key) with NO values;
   writer-gated (reader token → 403).
5. Create with ${nope__missing} (nonexistent secret) → 422 SECRET_REF_NOT_FOUND.
6. Reverse-lookup GET /data/{urn}/attr/ingestion shape: {dataset_urn, source_id, mode, name,
   latest_run} — null/unmapped case.
7. Sync-sweep matcher mapping: PASSIVE kafka source with topic_patterns.allow over imazon.*
   → POST /internal/activities/ingestion/sync → GET .../datasets returns URNs with
   derivation='matched'. Proves the sync matcher independently of api-wired UC1 Case 3.
8. Real run emit + derivation=emitted: ACTIVE_CUSTOM_MANAGED postgres catalog source →
   POST .../method/run (no dry_run param) → ≥2 catalog URNs in .../datasets with
   derivation='emitted'. Skipped if dummy-data-pg secret absent from cluster.
9. Populated reverse-lookup: after real run in #8, GET /data/{catalog_title_urn}/attr/ingestion
   returns source_id, mode=ACTIVE_CUSTOM_MANAGED, latest_run.status=success. Complements
   test 6 (null case).

Spec: spec/USE_CASE_en.md §UC1
Spec: spec/API.md §Ingestion
Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/SECRET_RESOLUTION.md
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source
"""

import os
import urllib.parse
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import pool as sa_pool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.util import dataspoke_db

# ── Dummy-data seed ───────────────────────────────────────────────────────────
# catalog schema triggers PG reset + DataHub ingest.
# Kafka topics are needed for spot test 7 (sync-sweep matcher).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({
    "imazon.orders.events",
    "imazon.shipping.updates",
})

# ── Test constants ─────────────────────────────────────────────────────────────
# Imazon catalog URN — only schema DataHub seeds in dev
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# In-cluster cluster-DNS address of the dummy-data postgres. The recipe is
# consumed by the API pod IN-CLUSTER, so it must be the cluster-DNS host:port,
# NOT the laptop-side ingress/port-forward address. Populated by install.sh;
# required (no default) so an unset env fails loud rather than guessing.
_PG_HOST_PORT = os.environ["DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT"]
_PG_DB = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")

# The K8s Secret that holds the dummy-data PG password.
# Secret name follows the dataspoke-source-cred-<name> prefix policy;
# <name> must be DNS-label-safe (hyphens, no underscores).
# ref = "dummy-data-pg__password" → Secret dataspoke-source-cred-dummy-data-pg, key password.
# spec: SECRET_RESOLUTION.md §Name prefix policy
# spec: SECRET_RESOLUTION.md §Admin authoring guide
_DUMMY_DATA_SECRET_NAME = "dataspoke-source-cred-dummy-data-pg"
_DUMMY_DATA_SECRET_REF = "dummy-data-pg__password"

# Kafka URNs for imazon.* topics (seeded by DUMMY_DATA_DATAHUB_TOPICS above).
# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topics
_KAFKA_ORDERS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_KAFKA_SHIPPING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)"
)

# API base paths
_SOURCES_BASE = "/api/v1/spoke/ingestion/sources"
_SECRETS_BASE = "/api/v1/spoke/ingestion/secrets"


def _unique_email(prefix: str = "spot-ingestion") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@test.dataspoke.example.com"


@pytest_asyncio.fixture(scope="module")
async def reader_headers(integration_db_url: str) -> AsyncGenerator[dict[str, str]]:
    """Module-scoped fixture: seed a Reader user directly in the DB and return auth headers.

    Uses the same proven pattern as the reader_token fixture in test_auth_privilege.py:
    DB-seeded user (google_sub, no password_hash) + issue_access_token, so the JWT is
    signed with the same secret the in-cluster API uses.  The server looks up role from
    the DB on every request; the seeded 'Reader' row ensures the gate evaluates to Reader.

    spec: API.md §Authentication — method × role gate (Reader GET only; writes → 403)
    spec: feature/AUTH.md §Privilege Model — Reader on /spoke/* write routes → 403 READ_ONLY_ROLE
    spec: TESTING.md §Spot vs Api-Wired Integration Tests — fixture may use util for teardown
    """
    from src.backend.auth.tokens import issue_access_token

    user_id = uuid.uuid4()
    email = _unique_email("reader-ingestion")
    google_sub = f"test-sub-{uuid.uuid4()}"

    engine = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO dataspoke.users (id, email, name, google_sub, role)"
                    " VALUES (:id, :email, :name, :google_sub, 'Reader')"
                ),
                {
                    "id": str(user_id),
                    "email": email,
                    "name": "Spot Reader",
                    "google_sub": google_sub,
                },
            )
    finally:
        await engine.dispose()

    # issue_access_token signs with DATASPOKE_JWT_SECRET_KEY, which conftest promotes
    # from DATASPOKE_TEST_JWT_SECRET_KEY so it matches the in-cluster API pod.
    # spec: feedback_test_runtime_env_promotion — conftest must promote JWT secret.
    token, _ = issue_access_token(user_id, email)

    yield {"Authorization": f"Bearer {token}"}

    engine2 = create_async_engine(integration_db_url, poolclass=sa_pool.NullPool)
    try:
        async with engine2.begin() as conn:
            await conn.execute(
                text("DELETE FROM dataspoke.users WHERE id = :id"),
                {"id": str(user_id)},
            )
    finally:
        await engine2.dispose()


async def _dummy_pg_secret_is_provisioned(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> bool:
    """Return True if the dummy-data-pg secret ref is listed by GET /spoke/ingestion/secrets.

    Used as a pre-condition skip guard for real-run tests that require the
    dataspoke-source-cred-dummy-data-pg K8s Secret to be provisioned in the cluster.

    The tests are REST-only and cannot read K8s directly; we probe the API's own
    secret-list endpoint (which reads k8s Secrets under the dataspoke-source-cred- prefix)
    as the lightweight check.

    spec: SECRET_RESOLUTION.md §Reference discovery (list flow)
    spec: TESTING.md §Spot vs Api-Wired Integration Tests — REST-only guard
    """
    resp = await api_client.get(_SECRETS_BASE, headers=admin_headers)
    if resp.status_code != 200:
        return False
    secrets_body = resp.json()
    return any(
        item.get("ref") == _DUMMY_DATA_SECRET_REF
        for item in secrets_body.get("secrets", [])
    )


def _catalog_recipe(secret_ref: str = _DUMMY_DATA_SECRET_REF) -> dict:
    """DataHub-compatible recipe for the Imazon catalog postgres source."""
    return {
        "source": {
            "type": "postgres",
            "config": {
                "host_port": _PG_HOST_PORT,
                "database": _PG_DB,
                "username": _PG_USER,
                "password": f"${{{secret_ref}}}",
                "schema_pattern": {"allow": ["^catalog$"]},
                "env": "DEV",
            },
        }
    }


# ── Test 1: Create ACTIVE_CUSTOM_MANAGED, GET round-trip, schedule_tier not on wire ──


@pytest.mark.asyncio
async def test_create_active_custom_managed_source(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Create ACTIVE_CUSTOM_MANAGED postgres catalog source → 201; GET returns
    {mode, name, schedule, recipe} with ${...} intact; schedule_tier absent from wire.

    spec: BACKEND.md §Ingestion Service — 'API body shape: {mode, name, schedule, recipe}'.
    spec: BACKEND_SCHEMA.md §ingestion_source — schedule_tier internal; never in API.
    spec: SECRET_RESOLUTION.md §Data Model — plaintext never stored; ${name__key} intact in GET.
    spec: USE_CASE_en.md §UC1 — Imazon catalog postgres source.
    """
    payload = {
        "mode": "ACTIVE_CUSTOM_MANAGED",
        "name": f"imazon catalog pg spot {uuid.uuid4().hex[:6]}",
        "schedule": "0 0 * * *",  # daily
        "recipe": _catalog_recipe(),
    }

    # POST /spoke/ingestion/sources
    resp = await api_client.post(_SOURCES_BASE, headers=admin_headers, json=payload)
    assert resp.status_code == 201, (
        f"Expected 201 on source create; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    source_id = body["id"]
    assert body["mode"] == "ACTIVE_CUSTOM_MANAGED"
    assert body["schedule"] == "0 0 * * *"
    # spec: BACKEND_SCHEMA.md — schedule_tier never on wire
    assert "schedule_tier" not in body, (
        f"schedule_tier must not appear in the API response. "
        f"spec: BACKEND_SCHEMA.md §ingestion_source. Body keys: {list(body.keys())}"
    )
    # spec: SECRET_RESOLUTION.md — ${...} placeholder kept intact in response
    config = body["recipe"]["source"]["config"]
    assert config.get("password") == f"${{{_DUMMY_DATA_SECRET_REF}}}", (
        "Secret ref must be returned as ${...} placeholder, not plaintext. "
        "spec: SECRET_RESOLUTION.md §Data Model."
    )

    # GET /spoke/ingestion/sources/{id} — round-trip check
    get_resp = await api_client.get(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["mode"] == "ACTIVE_CUSTOM_MANAGED"
    assert get_body["name"] == payload["name"]
    assert get_body["schedule"] == "0 0 * * *"
    assert "schedule_tier" not in get_body
    # ${...} placeholder still intact
    get_config = get_body["recipe"]["source"]["config"]
    assert "${" in get_config.get("password", "")

    # Teardown
    await api_client.delete(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)


# ── Test 2: DATAHUB_MANAGED create/update/delete → 409 INGESTION_SOURCE_READONLY ─


@pytest.mark.asyncio
async def test_datahub_managed_source_is_readonly(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Create / update / delete on DATAHUB_MANAGED source returns 409 INGESTION_SOURCE_READONLY.

    DATAHUB_MANAGED rows are read-only in DataSpoke — DataHub is SSOT.
    spec: BACKEND.md §Ingestion Service §Editability.
    spec: BACKEND_SCHEMA.md §ingestion_source — 'DATAHUB_MANAGED rows created only by sync sweep'.
    """
    payload = {
        "mode": "DATAHUB_MANAGED",
        "name": "datahub-managed source",
        "schedule": None,
        "recipe": {"source": {"type": "postgres", "config": {}}},
    }

    resp = await api_client.post(_SOURCES_BASE, headers=admin_headers, json=payload)
    assert resp.status_code == 409, (
        f"Expected 409 INGESTION_SOURCE_READONLY for DATAHUB_MANAGED create; "
        f"got {resp.status_code}: {resp.text}"
    )
    error_body = resp.json()
    assert error_body.get("error_code") == "INGESTION_SOURCE_READONLY", (
        f"Expected error_code=INGESTION_SOURCE_READONLY; got {error_body}. "
        "spec: BACKEND.md §Ingestion Service §Editability."
    )


# ── Test 3: POST .../method/run on PASSIVE → 409 INGESTION_RUN_NOT_APPLICABLE ─


@pytest.mark.asyncio
async def test_run_passive_source_returns_not_applicable(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST .../method/run on a PASSIVE source returns 409 INGESTION_RUN_NOT_APPLICABLE.

    spec: BACKEND.md §Active-custom run pipeline — 'reject if mode != ACTIVE_CUSTOM_MANAGED;
          409 INGESTION_RUN_NOT_APPLICABLE'.
    spec: USE_CASE_en.md §UC1 — PASSIVE sources are externally run.
    """
    # Create a PASSIVE source first
    create_resp = await api_client.post(
        _SOURCES_BASE,
        headers=admin_headers,
        json={
            "mode": "PASSIVE",
            "name": f"passive kafka spot {uuid.uuid4().hex[:6]}",
            "schedule": None,
            "recipe": {
                "source": {
                    "type": "kafka",
                    "config": {"schema_pattern": {"allow": ["^imazon\\.orders\\.events$"]}},
                }
            },
        },
    )
    assert create_resp.status_code == 201, (
        f"Expected 201 for PASSIVE create; got {create_resp.status_code}: {create_resp.text}"
    )
    source_id = create_resp.json()["id"]

    # POST .../method/run — must be rejected
    run_resp = await api_client.post(
        f"{_SOURCES_BASE}/{source_id}/method/run",
        headers=admin_headers,
    )
    assert run_resp.status_code == 409, (
        f"Expected 409 INGESTION_RUN_NOT_APPLICABLE on PASSIVE run; "
        f"got {run_resp.status_code}: {run_resp.text}"
    )
    error_body = run_resp.json()
    assert error_body.get("error_code") == "INGESTION_RUN_NOT_APPLICABLE", (
        f"Expected error_code=INGESTION_RUN_NOT_APPLICABLE; got {error_body}. "
        "spec: BACKEND.md §Active-custom run pipeline step 1."
    )

    # Teardown
    await api_client.delete(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)


# ── Test 4: GET /spoke/ingestion/secrets lists refs; reader → 403 ──────────────


@pytest.mark.asyncio
async def test_list_secrets_shape_and_reader_forbidden(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    reader_headers: dict[str, str],
) -> None:
    """GET /spoke/ingestion/secrets lists refs (name__key, secret_name, key) with NO values.
    Writer-gated: reader token returns 403.

    spec: SECRET_RESOLUTION.md §Reference discovery (list flow) — 'ref, secret_name, key';
          values are never returned.
    spec: API.md §Ingestion — GET /spoke/ingestion/secrets requires Editor or Admin.
    """
    # Admin/editor can list
    resp = await api_client.get(_SECRETS_BASE, headers=admin_headers)
    assert resp.status_code == 200, (
        f"Expected 200 from admin on secrets list; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "secrets" in body, "Response must have 'secrets' key"
    for item in body["secrets"]:
        assert "ref" in item, "Each secret item must have 'ref' field"
        assert "secret_name" in item, "Each secret item must have 'secret_name' field"
        assert "key" in item, "Each secret item must have 'key' field"
        assert "value" not in item, (
            "Secret values must NEVER be included in the list response. "
            "spec: SECRET_RESOLUTION.md §Reference discovery."
        )
        # ref must have __ to be pasteable as ${name__key}
        assert "__" in item["ref"], (
            f"ref '{item['ref']}' must contain '__' (the name__key separator). "
            "spec: SECRET_RESOLUTION.md §Reference syntax."
        )
        # secret_name must start with the required prefix
        assert item["secret_name"].startswith("dataspoke-source-cred-"), (
            f"secret_name '{item['secret_name']}' must start with 'dataspoke-source-cred-'. "
            "spec: SECRET_RESOLUTION.md §Name prefix policy."
        )

    # Reader is forbidden
    reader_resp = await api_client.get(_SECRETS_BASE, headers=reader_headers)
    assert reader_resp.status_code == 403, (
        f"Expected 403 for reader on secrets list; got {reader_resp.status_code}. "
        "spec: API.md §Ingestion — GET /spoke/ingestion/secrets requires Editor or Admin."
    )


# ── Test 5: Create with ${nope__missing} → 422 SECRET_REF_NOT_FOUND ────────────


@pytest.mark.asyncio
async def test_create_source_with_nonexistent_secret_ref_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Creating a source with a ${name__key} ref that doesn't exist → 422 SECRET_REF_NOT_FOUND.

    spec: SECRET_RESOLUTION.md §Reference verify flow — 'Secret missing → 422 SECRET_REF_NOT_FOUND'.
    spec: BACKEND.md §Ingestion Service — 'Verify all secret refs at save time.'
    """
    resp = await api_client.post(
        _SOURCES_BASE,
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": f"missing secret source {uuid.uuid4().hex[:6]}",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": _PG_DB,
                        "username": _PG_USER,
                        "password": "${nope__missing}",  # this K8s secret does not exist
                    },
                }
            },
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 for nonexistent secret ref; got {resp.status_code}: {resp.text}"
    )
    error_body = resp.json()
    assert error_body.get("error_code") == "SECRET_REF_NOT_FOUND", (
        f"Expected error_code=SECRET_REF_NOT_FOUND; got {error_body}. "
        "spec: SECRET_RESOLUTION.md §Reference verify flow."
    )


# ── Test 6: Reverse-lookup GET /data/{urn}/attr/ingestion shape ────────────────


@pytest.mark.asyncio
async def test_reverse_lookup_shape_for_unmapped_dataset(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /data/{urn}/attr/ingestion for an unmapped dataset returns the correct shape
    with all nullable fields as null.

    spec: API.md §Ingestion — 'Returns the owning source for a dataset, or null if unmapped'.
    spec: API.md §Ingestion — response shape: {dataset_urn, source_id, mode, name, latest_run}.
    """
    reverse_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion"
    resp = await api_client.get(reverse_url, headers=admin_headers)
    assert resp.status_code == 200, (
        f"Expected 200 on reverse-lookup; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # spec: API.md §Ingestion — required shape fields
    assert "dataset_urn" in body, "Response must have 'dataset_urn' field"
    assert body["dataset_urn"] == _TEST_URN, (
        f"dataset_urn must reflect queried URN; got {body['dataset_urn']!r}"
    )
    # When no source maps this dataset, all ownership fields are null
    for nullable_field in ("source_id", "mode", "name", "latest_run"):
        assert nullable_field in body, f"Response must have '{nullable_field}' field"
    # spec: BACKEND_SCHEMA.md — schedule_tier is internal, never in API
    assert "schedule_tier" not in body, (
        "schedule_tier must not appear in the reverse-lookup response. "
        "spec: BACKEND_SCHEMA.md §ingestion_source."
    )


# ── Test 7: Sync-sweep matcher mapping (PASSIVE kafka, imazon.* topics) ──────────


@pytest.mark.asyncio
async def test_sync_sweep_passive_kafka_matcher_maps_topics(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """Sync-sweep maps imazon.* Kafka topics to a PASSIVE source via the AllowDenyPattern matcher.

    Independent spot-level proof of the sync matcher pipeline:
    1. Create a PASSIVE kafka source with topic_patterns.allow: ["^imazon\\..*$"]
    2. POST /internal/activities/ingestion/sync to trigger the sweep
    3. GET /sources/{id}/datasets and assert imazon.* URNs appear with derivation='matched'

    This test stands alone from UC1 Case 3 (api-wired): the spot set must cover this
    surface even when api-wired tests are skipped.

    spec: USE_CASE_en.md §UC1 Case 3 — PASSIVE source declares allow/deny scope; sync maps topics.
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 2 — derivation=matched.
    spec: TESTING.md §Coverage rule — spot set must catch backend regressions independently.
    """
    # Clean slate: remove any leftover ingestion_source rows.
    # spec: TESTING.md §spot independence — each test resets/cleans its own state.
    await dataspoke_db.reset_ingestion_sources()

    source_id: str | None = None
    try:
        # Step 1: Create PASSIVE kafka source with the UC1 Case 3 recipe.
        # spec: USE_CASE_en.md §UC1 Case 3 — topic_patterns.allow: ["^imazon\\..*$"]
        create_resp = await api_client.post(
            _SOURCES_BASE,
            headers=admin_headers,
            json={
                "mode": "PASSIVE",
                "name": f"imazon kafka passive spot {uuid.uuid4().hex[:6]}",
                "schedule": None,
                "recipe": {
                    "source": {
                        "type": "kafka",
                        "config": {
                            "topic_patterns": {
                                "allow": ["^imazon\\..*$"]
                            }
                        },
                    }
                },
            },
        )
        assert create_resp.status_code == 201, (
            f"Expected 201 for PASSIVE kafka source create; "
            f"got {create_resp.status_code}: {create_resp.text}"
        )
        source_id = create_resp.json()["id"]

        # Step 2: Trigger the sync sweep (activity endpoint used by the hourly DAG).
        # spec: feature/BACKEND.md §Ingestion Service §Sync sweep — sync() reconciles all modes.
        sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
        )
        assert sync_resp.status_code == 200, (
            f"POST /internal/activities/ingestion/sync expected 200; "
            f"got {sync_resp.status_code}: {sync_resp.text}"
        )

        # Step 3: GET /sources/{id}/datasets — imazon.* topics must be mapped.
        # spec: API.md §Ingestion — GET /sources/{id}/datasets returns the mapping.
        datasets_resp = await api_client.get(
            f"{_SOURCES_BASE}/{source_id}/datasets",
            headers=admin_headers,
        )
        assert datasets_resp.status_code == 200, (
            f"GET /sources/{source_id}/datasets expected 200; "
            f"got {datasets_resp.status_code}: {datasets_resp.text}"
        )
        datasets_body = datasets_resp.json()
        assert "datasets" in datasets_body, "Response must have 'datasets' key"
        dataset_urns = {d["dataset_urn"] for d in datasets_body["datasets"]}

        # Both seeded imazon.* topics must be mapped by the pattern matcher.
        # spec: TESTING.md §Imazon Dummy-Data Reference — seeded via DUMMY_DATA_DATAHUB_TOPICS
        assert _KAFKA_ORDERS_URN in dataset_urns, (
            f"imazon.orders.events URN must be mapped after sync; "
            f"mapped URNs: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 3 — declared allow scope maps topics."
        )
        assert _KAFKA_SHIPPING_URN in dataset_urns, (
            f"imazon.shipping.updates URN must be mapped after sync; "
            f"mapped URNs: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 3 — declared allow scope maps topics."
        )

        # All mapped rows must have derivation='matched' (PASSIVE sync path).
        # spec: feature/BACKEND.md §Sync sweep step 2 — PASSIVE: derivation=matched.
        for d in datasets_body["datasets"]:
            if d["dataset_urn"] in (_KAFKA_ORDERS_URN, _KAFKA_SHIPPING_URN):
                assert d["derivation"] == "matched", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have "
                    f"derivation='matched'; got {d['derivation']!r}. "
                    "spec: feature/BACKEND.md §Sync sweep step 2 — PASSIVE uses matched derivation."
                )

    finally:
        if source_id is not None:
            await api_client.delete(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)
        # Reset so subsequent tests start clean.
        await dataspoke_db.reset_ingestion_sources()


# ── Test 8: Real-run emit + derivation=emitted (ACTIVE_CUSTOM_MANAGED) ──────────


@pytest.mark.asyncio
async def test_real_run_emits_catalog_datasets_with_derivation_emitted(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """ACTIVE_CUSTOM_MANAGED postgres catalog source: POST .../method/run (real run)
    → ≥2 catalog URNs in .../datasets with derivation='emitted'.

    Skipped when the dummy-data-pg K8s Secret is not provisioned in the cluster
    (checked via GET /spoke/ingestion/secrets).

    spec: USE_CASE_en.md §UC1 Case 2 — real run emits dataset aspects + records emitted URNs.
    spec: feature/BACKEND.md §Active-custom run pipeline — emitted URNs recorded with
    derivation=emitted.
    spec: SECRET_RESOLUTION.md §Reference discovery — skip guard via list endpoint.
    spec: TESTING.md §Coverage rule — spot must prove emit surface independently.
    """
    # Skip-guard: probe the API's own secret-list endpoint.
    # REST-only tests cannot read k8s directly; the list endpoint is the correct gate.
    # spec: SECRET_RESOLUTION.md §Reference discovery (list flow)
    if not await _dummy_pg_secret_is_provisioned(api_client, admin_headers):
        pytest.skip(
            f"Secret ref '{_DUMMY_DATA_SECRET_REF}' not listed by GET /spoke/ingestion/secrets. "
            f"Pre-create K8s Secret {_DUMMY_DATA_SECRET_NAME!r} with key 'password' "
            "to enable real-run tests. spec: SECRET_RESOLUTION.md §Admin authoring guide."
        )

    await dataspoke_db.reset_ingestion_sources()

    source_id: str | None = None
    try:
        # Create ACTIVE_CUSTOM_MANAGED source — catalog schema only.
        # spec: USE_CASE_en.md §UC1 Case 2 — schema_pattern.allow: ["^catalog$"]
        create_resp = await api_client.post(
            _SOURCES_BASE,
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": f"imazon catalog pg spot real {uuid.uuid4().hex[:6]}",
                "schedule": "0 0 * * *",
                "recipe": _catalog_recipe(),
            },
        )
        assert create_resp.status_code == 201, (
            f"Expected 201 on source create; got {create_resp.status_code}: {create_resp.text}"
        )
        source_id = create_resp.json()["id"]

        # Real run: dry_run omitted (defaults to false).
        # spec: feature/BACKEND.md §Active-custom run pipeline — emit dataset aspects (not dry_run)
        run_resp = await api_client.post(
            f"{_SOURCES_BASE}/{source_id}/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"POST .../method/run (real run) expected 200; "
            f"got {run_resp.status_code}: {run_resp.text}"
        )
        run_body = run_resp.json()
        assert "status" in run_body
        _fail_tail = {"fail", "failed", "failure", "error", "errored"}
        assert run_body["status"].lower() not in _fail_tail, (
            f"Real run returned failure status {run_body['status']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — catalog schema must be reachable."
        )

        # ≥2 catalog datasets must have been emitted (title_master + editions).
        # spec: feature/BACKEND.md §Active-custom run pipeline — non-dry run that ingests
        #   zero entities is treated as failure.
        run_detail = run_body.get("detail", {}) or {}
        emitted_count = run_detail.get("emitted_urns_count", 0)
        assert emitted_count >= 2, (
            f"Real run must emit ≥2 catalog datasets; got emitted_urns_count={emitted_count}. "
            "spec: USE_CASE_en.md §UC1 Case 2 — catalog schema produces title_master + editions."
        )

        # GET .../datasets — emitted URNs must be present with derivation='emitted'.
        # spec: feature/BACKEND.md §Active-custom run pipeline — derivation=emitted (authoritative).
        datasets_resp = await api_client.get(
            f"{_SOURCES_BASE}/{source_id}/datasets",
            headers=admin_headers,
        )
        assert datasets_resp.status_code == 200, (
            f"GET .../datasets expected 200; got {datasets_resp.status_code}: {datasets_resp.text}"
        )
        datasets_body = datasets_resp.json()
        assert "datasets" in datasets_body
        dataset_rows = datasets_body["datasets"]
        assert len(dataset_rows) >= 2, (
            f"At least 2 catalog datasets must appear in the mapping; "
            f"got {len(dataset_rows)}: {[d['dataset_urn'] for d in dataset_rows]}. "
            "spec: USE_CASE_en.md §UC1 Case 2."
        )
        # All emitted rows must carry derivation='emitted'.
        # spec: feature/BACKEND.md §Active-custom run pipeline — derivation=emitted for real runs.
        for row in dataset_rows:
            assert row.get("derivation") == "emitted", (
                f"Dataset {row.get('dataset_urn')!r} must have derivation='emitted' after "
                f"a real run; got {row.get('derivation')!r}. "
                "spec: feature/BACKEND.md §Active-custom run pipeline — derivation=emitted."
            )

    finally:
        if source_id is not None:
            await api_client.delete(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)
        await dataspoke_db.reset_ingestion_sources()


# ── Test 9: Populated reverse-lookup after real run ───────────────────────────────


@pytest.mark.asyncio
async def test_populated_reverse_lookup_after_real_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /data/{catalog_title_urn}/attr/ingestion returns source_id, mode=ACTIVE_CUSTOM_MANAGED,
    and latest_run.status='success' after a real catalog ingestion run.

    Complements test 6 (null/unmapped case): proves the populated shape.
    Skipped when the dummy-data-pg K8s Secret is not provisioned in the cluster.

    spec: USE_CASE_en.md §UC1 API Mapping — reverse-lookup: source, mode, latest run.
    spec: API.md §Ingestion — GET /spoke/common/data/{urn}/attr/ingestion response shape.
    spec: feature/BACKEND.md §Active-custom run pipeline — run records emitted URNs in
          ingestion_source_dataset.
    spec: SECRET_RESOLUTION.md §Reference discovery — skip guard via list endpoint.
    """
    # Skip-guard: same logic as test 8.
    # spec: SECRET_RESOLUTION.md §Reference discovery (list flow)
    if not await _dummy_pg_secret_is_provisioned(api_client, admin_headers):
        pytest.skip(
            f"Secret ref '{_DUMMY_DATA_SECRET_REF}' not listed by GET /spoke/ingestion/secrets. "
            f"Pre-create K8s Secret {_DUMMY_DATA_SECRET_NAME!r} with key 'password' "
            "to enable real-run tests. spec: SECRET_RESOLUTION.md §Admin authoring guide."
        )

    await dataspoke_db.reset_ingestion_sources()

    source_id: str | None = None
    try:
        # Create source and run — same recipe as test 8.
        # spec: USE_CASE_en.md §UC1 Case 2 — schema_pattern.allow: ["^catalog$"]
        create_resp = await api_client.post(
            _SOURCES_BASE,
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": f"imazon catalog pg reverse {uuid.uuid4().hex[:6]}",
                "schedule": "0 0 * * *",
                "recipe": _catalog_recipe(),
            },
        )
        assert create_resp.status_code == 201, (
            f"Expected 201 on source create; got {create_resp.status_code}: {create_resp.text}"
        )
        source_id = create_resp.json()["id"]

        run_resp = await api_client.post(
            f"{_SOURCES_BASE}/{source_id}/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"Real run expected 200; got {run_resp.status_code}: {run_resp.text}"
        )
        run_body = run_resp.json()
        _fail_tail = {"fail", "failed", "failure", "error", "errored"}
        assert run_body.get("status", "").lower() not in _fail_tail, (
            f"Real run returned failure status; details: {run_body}"
        )

        # Reverse-lookup on the catalog.title_master URN.
        # spec: API.md §Ingestion — GET /spoke/common/data/{urn}/attr/ingestion
        reverse_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion",
            headers=admin_headers,
        )
        assert reverse_resp.status_code == 200, (
            f"GET /data/{{urn}}/attr/ingestion expected 200; "
            f"got {reverse_resp.status_code}: {reverse_resp.text}"
        )
        rev_body = reverse_resp.json()

        # source_id must match the source we just created.
        # spec: API.md §Ingestion — source_id populated when dataset is mapped.
        assert rev_body.get("source_id") == source_id, (
            f"Reverse-lookup source_id must equal the created source {source_id!r}; "
            f"got {rev_body.get('source_id')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping — reverse-lookup returns owning source."
        )

        # mode must reflect the source's mode.
        # spec: API.md §Ingestion — mode in reverse-lookup response.
        assert rev_body.get("mode") == "ACTIVE_CUSTOM_MANAGED", (
            f"Reverse-lookup mode must be 'ACTIVE_CUSTOM_MANAGED'; "
            f"got {rev_body.get('mode')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping."
        )

        # latest_run must be present and reflect a successful run.
        # spec: API.md §Ingestion — IngestionReverseLookupResponse.latest_run shape.
        latest_run = rev_body.get("latest_run")
        assert latest_run is not None, (
            "Reverse-lookup must include latest_run after a real run. "
            "spec: API.md §Ingestion — IngestionReverseLookupResponse.latest_run."
        )
        assert latest_run.get("status") == "success", (
            f"latest_run.status must be 'success'; got {latest_run.get('status')!r}. "
            "spec: USE_CASE_en.md §UC1 — INGESTION.COMPLETE event status='success'."
        )

    finally:
        if source_id is not None:
            await api_client.delete(f"{_SOURCES_BASE}/{source_id}", headers=admin_headers)
        await dataspoke_db.reset_ingestion_sources()
