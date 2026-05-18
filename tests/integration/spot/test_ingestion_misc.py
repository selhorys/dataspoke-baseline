"""Spot tests — Ingestion Control: miscellaneous edge cases.

Concerns covered:
- GET /spoke/common/ingestion — paginated collection envelope (list)
- GET /data/{urn}/attr/ingestion/conf — 404 for unknown URN
- Secret collision: second PUT with same (name, key) without force_overwrite → 422
- Invalid secret-ref prefix (not starting with dataspoke-source-cred-) → 422
- Reference-path: PUT with no password pointing at existing Secret succeeds
- Reference to non-existent key on existing Secret → 422
- GET /data/{urn}/event/ingestion — paginated event envelope
- PUT with schedule_tier="monthly" → 422 (Pydantic Literal boundary)
"""
# spec: API.md §Standard Envelope
# spec: SECRET_RESOLUTION.md §Name prefix policy, §Vault-write flow, §Reference-path verify flow
# spec: BACKEND.md §Ingestion Service

import os
import urllib.parse
import uuid

import httpx
import pytest

# Per-module dummy-data seed — catalog schema triggers PG reset + DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

_TEST_URN_2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_ENCODED_URN_2 = urllib.parse.quote(_TEST_URN_2, safe="")


@pytest.mark.asyncio
async def test_ingestion_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ingestion returns a paginated collection envelope with required keys.

    Seeding N=2 configs verifies total_count >= N and that limit=1 trims the page.
    spec: API.md §Standard Envelope — paginated response carries configs[], total_count, offset, limit
    spec: BACKEND.md §Ingestion Service — list_configs paginates ingestion_configs rows
    """
    vault_name = f"dataspoke-source-cred-spot-misc-list-{uuid.uuid4().hex[:8]}"
    base_1 = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_2 = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/ingestion/conf"

    common_auth = {
        "username": _PG_USER,
        "password": _PG_PASSWORD,
        "secret_ref": {
            "name": vault_name,
            "key": "password",
            "force_overwrite": True,
        },
    }

    await api_client.put(
        base_1,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
            "auth": common_auth,
            "is_enabled": False,
        },
    )
    await api_client.put(
        base_2,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "editions"},
            "auth": {
                **common_auth,
                "secret_ref": {**common_auth["secret_ref"], "key": "pw2"},
            },
            "is_enabled": False,
        },
    )

    resp_all = await api_client.get(
        "/api/v1/spoke/common/ingestion?offset=0&limit=10",
        headers=admin_headers,
    )
    assert resp_all.status_code == 200
    body_all = resp_all.json()
    # spec: API.md §Standard Envelope — required keys
    assert "configs" in body_all
    assert "offset" in body_all
    assert "limit" in body_all
    assert "total_count" in body_all
    assert isinstance(body_all["configs"], list)
    assert body_all["total_count"] >= 2, (
        f"Expected total_count >= 2 after seeding 2 configs; got {body_all['total_count']}"
    )

    # limit=1 must trim page to 1 item, total_count must still reflect full count
    resp_paged = await api_client.get(
        "/api/v1/spoke/common/ingestion?offset=0&limit=1",
        headers=admin_headers,
    )
    assert resp_paged.status_code == 200
    body_paged = resp_paged.json()
    assert len(body_paged["configs"]) == 1, (
        f"Expected page size 1 (limit=1); got {len(body_paged['configs'])}"
    )
    assert body_paged["total_count"] >= 2

    await api_client.delete(base_1, headers=admin_headers)
    await api_client.delete(base_2, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET ingestion conf for an unknown URN returns 404.

    spec: API.md §Error responses — 404 for not-found resource
    """
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/ingestion/conf",
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_conf_put_secret_collision_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Second PUT with same (name, key) without force_overwrite → 422 (SecretCollision).

    spec: SECRET_RESOLUTION.md §Validation matrix row 3 — SecretCollision → 422
    spec: SECRET_RESOLUTION.md §Vault-write flow step 2 — collision detection
    """
    collision_secret = f"dataspoke-source-cred-spot-collision-{uuid.uuid4().hex[:8]}"
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

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
            f"Second PUT without force_overwrite expected 422 SecretCollision; "
            f"got {put2.status_code}: {put2.text}"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_put_invalid_secret_ref_prefix_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """secret_ref.name without 'dataspoke-source-cred-' prefix → 422 SecretRefNameForbidden.

    spec: SECRET_RESOLUTION.md §Name prefix policy — name must start with dataspoke-source-cred-
    spec: SECRET_RESOLUTION.md §Validation matrix last row — forbidden prefix → 422
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
        f"PUT with non-prefix secret_ref.name expected 422; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: SECRET_RESOLUTION.md §Name prefix policy"
    )


@pytest.mark.asyncio
async def test_ingestion_conf_put_reference_path_to_existing_secret(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Reference path: vault first on URN1, then PUT URN2 pointing at the same Secret — succeeds.

    spec: SECRET_RESOLUTION.md §Validation matrix row 4 — reference path allowed
    spec: SECRET_RESOLUTION.md §Reference-path verify flow — key must exist
    """
    ref_secret = f"dataspoke-source-cred-spot-ref-{uuid.uuid4().hex[:8]}"
    base_conf_1 = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_conf_2 = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/ingestion/conf"

    # Vault path on URN1 to establish the Secret
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
        # Reference path on URN2 — no password, point at the just-vaulted Secret
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
            f"Reference-path response leaked password key: {ref_body['auth']}"
        )

        # Reference to a NON-existent key on the same Secret → 422
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
            f"Reference path to nonexistent key expected 422; "
            f"got {bad_key_resp.status_code}. "
            "spec: SECRET_RESOLUTION.md §Reference-path verify flow"
        )
    finally:
        await api_client.delete(base_conf_1, headers=admin_headers)
        await api_client.delete(base_conf_2, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/ingestion returns paginated event envelope with required keys.

    spec: API.md §Standard Envelope — events[], total_count, offset, limit
    """
    vault_name = f"dataspoke-source-cred-spot-misc-events-{uuid.uuid4().hex[:8]}"
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/ingestion"

    await api_client.put(
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
                    "name": vault_name,
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
        },
    )

    resp = await api_client.get(base_events, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_conf_put_invalid_schedule_tier_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with schedule_tier='monthly' returns 422 at the Pydantic boundary.

    schedule_tier for active-custom ingestion is
    Literal["hourly","daily","weekly"] | None; "monthly" is not a member of
    that union so Pydantic rejects it with 422 before the service layer is
    reached.  This pins the Pydantic boundary so that a regression back to a
    service-layer string comparison would surface via a different error shape.

    spec: BACKEND.md §UC1 Ingestion Control — schedule_tier ∈ {"hourly","daily","weekly"};
      Pydantic Literal auto-422
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    resp = await api_client.put(
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
                    "name": f"dataspoke-source-cred-spot-sched-tier-{uuid.uuid4().hex[:8]}",
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "schedule_tier": "monthly",
            "is_enabled": False,
        },
    )

    # spec: BACKEND.md §UC1 Ingestion Control — schedule_tier ∈ {"hourly","daily","weekly"};
    # Pydantic Literal auto-422; "monthly" is not a valid member.
    assert resp.status_code == 422, (
        f"PUT with schedule_tier='monthly' must return 422; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: BACKEND.md §UC1 Ingestion Control — schedule_tier Pydantic Literal auto-422"
    )
