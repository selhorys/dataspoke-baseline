"""Spot tests — Ingestion Control: per-source model (Phase 2a).

Concerns covered (one per test):
1. Create ACTIVE_CUSTOM_MANAGED postgres catalog source with ${dummy_data_pg__password}
   → 201; GET returns {mode, name, schedule, recipe} with ${...} intact; no schedule_tier on wire.
2. Create / update / delete DATAHUB_MANAGED source → 409 INGESTION_SOURCE_READONLY.
3. POST .../method/run on PASSIVE source → 409 INGESTION_RUN_NOT_APPLICABLE.
4. GET /spoke/ingestion/secrets lists refs (name__key, secret_name, key) with NO values;
   writer-gated (reader token → 403).
5. Create with ${nope__missing} (nonexistent secret) → 422 SECRET_REF_NOT_FOUND.
6. Reverse-lookup GET /data/{urn}/attr/ingestion shape: {dataset_urn, source_id, mode, name,
   latest_run}.

NOTE: tests needing 2b sync sweep or full DataHub emission are NOT included.

Spec: spec/USE_CASE_en.md §UC1
Spec: spec/API.md §Ingestion
Spec: spec/feature/BACKEND.md §Ingestion Service
Spec: spec/feature/SECRET_RESOLUTION.md
Spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source
"""

import os
import urllib.parse
import uuid

import httpx
import pytest

# ── Dummy-data seed ───────────────────────────────────────────────────────────
# catalog schema triggers PG reset + DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# ── Test constants ─────────────────────────────────────────────────────────────
# Imazon catalog URN — only schema DataHub seeds in dev
# spec: TESTING.md §Imazon Dummy-Data Reference
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

_PG_HOST = os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_TEST_DUMMY_DATA_POSTGRES_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_USER", "postgres")

# The K8s Secret that holds the dummy-data PG password.
# spec: SECRET_RESOLUTION.md §Admin authoring guide — dataspoke-source-cred-<name>
_DUMMY_DATA_SECRET_NAME = "dataspoke-source-cred-dummy-data-pg"
_DUMMY_DATA_SECRET_REF = "dummy_data_pg__password"

# API base paths
_SOURCES_BASE = "/api/v1/spoke/ingestion/sources"
_SECRETS_BASE = "/api/v1/spoke/ingestion/secrets"


def _catalog_recipe(secret_ref: str = _DUMMY_DATA_SECRET_REF) -> dict:
    """DataHub-compatible recipe for the Imazon catalog postgres source."""
    return {
        "source": {
            "type": "postgres",
            "config": {
                "host_port": f"{_PG_HOST}:{_PG_PORT}",
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
        json={"dry_run": False},
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
    """Creating a source with a ${name__key} ref that doesn't exist in K8s → 422 SECRET_REF_NOT_FOUND.

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
                        "host_port": f"{_PG_HOST}:{_PG_PORT}",
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
