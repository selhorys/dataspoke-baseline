"""UC1 — Ingestion Control: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC1` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Tests in this module:
  - test_uc1_active_and_passive_ingestion: Active branch (Postgres, daily schedule),
    Passive branch (Kafka, external ingestor), cross-dataset overview.
"""
# spec: USE_CASE_en.md §UC1

import os
import urllib.parse

import httpx
import pytest

# Active branch: example_db.catalog.title_master (Postgres)
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1 primary dataset
_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")

# spec: SECRET_RESOLUTION.md §Name prefix policy — names must start with dataspoke-source-cred-
_VAULT_NAME = "dataspoke-source-cred-uc1-title-master"
_VAULT_KEY = "password"

_ACTIVE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_ACTIVE_ENCODED = urllib.parse.quote(_ACTIVE_URN, safe="")

# Passive branch: example_kafka.imazon.orders.events (Kafka)
# spec: USE_CASE_en.md §UC1 — Passive: external system ingests, DataSpoke registers mode=passive
# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_KAFKA_BROKERS = os.environ.get("DATASPOKE_EXAMPLE_KAFKA_BROKERS", "localhost:9104")
_PASSIVE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_PASSIVE_ENCODED = urllib.parse.quote(_PASSIVE_URN, safe="")


@pytest.mark.asyncio
async def test_uc1_active_and_passive_ingestion(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC1 narrative: 'register, run, and observe ingestion … one DataSpoke surface
    drives ingestion config, runs, and event history for the whole estate.'

    Steps mirror USE_CASE_en.md §UC1:
      1. Active conf PUT on catalog.title_master (Postgres)
      2. Dry-run connection check on active dataset
      3. Event history query on active dataset
      4. Passive conf PUT on imazon.orders.events (Kafka)
      5. Event history query on passive dataset
      6. Cross-dataset overview — both URNs present
      7. Cleanup — DELETE both confs
    """
    active_conf_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/attr/ingestion/conf"
    active_run_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/method/ingestion/run"
    active_events_url = f"/api/v1/spoke/common/data/{_ACTIVE_ENCODED}/event/ingestion"
    passive_conf_url = f"/api/v1/spoke/common/data/{_PASSIVE_ENCODED}/attr/ingestion/conf"
    passive_events_url = f"/api/v1/spoke/common/data/{_PASSIVE_ENCODED}/event/ingestion"

    try:
        # ── Step 1: Active branch — register ingestion conf ───────────────────
        # UC1 narrative: "DataSpoke is the ingestor. An Airflow tier DAG runs the
        # platform extractor on the configured schedule_tier."
        # spec: USE_CASE_en.md §UC1 L89-L104 (Active — catalog.books, Postgres, daily)
        # spec: SECRET_RESOLUTION.md §Vault-write flow — vault path with force_overwrite=true
        # for idempotent test runs (re-running the test does not collide on an existing key).
        put_active_resp = await api_client.put(
            active_conf_url,
            headers=admin_headers,
            json={
                "mode": "active",
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
        assert put_active_resp.status_code in (200, 201), (
            f"PUT active conf failed: {put_active_resp.status_code} {put_active_resp.text}"
        )
        active_body = put_active_resp.json()
        assert active_body["dataset_urn"] == _ACTIVE_URN
        assert active_body["mode"] == "active"
        assert active_body["platform"] == "postgres"
        assert active_body["schedule_tier"] == "daily"
        # spec: USE_CASE_en.md §UC1 L82 — is_enabled survives round-trip
        assert active_body["is_enabled"] is False
        # spec: USE_CASE_en.md §UC1 L82 — identifier survives round-trip
        assert active_body["identifier"]["table"] == "title_master"
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 4-5 — response carries reference
        # shape only: {username, secret_ref: {name, key}}. Password dropped before DB write.
        assert active_body["auth"]["username"] == _PG_USER
        assert "password" not in active_body["auth"], (
            "Response auth must not expose the plaintext password. "
            "spec: SECRET_RESOLUTION.md §Vault-write flow step 4"
        )
        assert active_body["auth"]["secret_ref"] == {"name": _VAULT_NAME, "key": _VAULT_KEY}, (
            f"Response secret_ref must be the reference shape {{name, key}}; "
            f"got {active_body['auth'].get('secret_ref')!r}. "
            "spec: SECRET_RESOLUTION.md §Vault-write flow step 5"
        )

        # ── Step 2: Dry-run connection check ─────────────────────────────────
        # UC1 narrative: "A coding agent verifies connectivity before turning the
        # schedule on: POST .../method/ingestion/run { 'dry_run': true }"
        # spec: USE_CASE_en.md §UC1 L106-L110
        run_resp = await api_client.post(
            active_run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert run_resp.status_code == 200, (
            f"POST dry-run failed: {run_resp.status_code} {run_resp.text}"
        )
        run_body = run_resp.json()
        # impl-shape (no spec enumeration of run response keys)
        assert "run_id" in run_body, "dry-run response must carry run_id"
        assert "status" in run_body, "dry-run response must carry status"
        # spec: BACKEND.md L210/L217 (INGESTION.FAIL event tail; dry-run against reachable
        # Postgres must not produce a fail-tail run status)
        assert isinstance(run_body["status"], str) and run_body["status"], (
            f"dry-run status must be a non-empty string; got {run_body['status']!r}"
        )
        _fail_tail = {"fail", "failed", "failure", "error", "errored"}
        assert run_body["status"].lower() not in _fail_tail, (
            "dry-run connection check unexpectedly returned fail-tail status "
            f"{run_body['status']!r}"
        )

        # ── Step 3: Event history query on active dataset ────────────────────
        # UC1 narrative: "After the daily Airflow tier DAG runs, the team reads
        # the per-dataset event history."
        # spec: USE_CASE_en.md §UC1 L113-L115
        events_active_resp = await api_client.get(
            f"{active_events_url}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert events_active_resp.status_code == 200, (
            f"GET active events failed: {events_active_resp.status_code}"
        )
        events_active_body = events_active_resp.json()
        # spec: API.md §Standard Envelope — paginated response keys
        assert "events" in events_active_body
        assert "offset" in events_active_body
        assert "limit" in events_active_body
        assert "total_count" in events_active_body
        assert isinstance(events_active_body["events"], list)

        # ── Step 4: Passive branch — register ingestion conf ─────────────────
        # UC1 narrative: "Passive — an external system ingests directly into DataHub.
        # DataSpoke does not run the extractor; it only marks the dataset's ingestion
        # config as mode: passive."
        # spec: USE_CASE_en.md §UC1 L118-L131
        put_passive_resp = await api_client.put(
            passive_conf_url,
            headers=admin_headers,
            json={
                "mode": "passive",
                "platform": "kafka",
                "locator": {"bootstrap_servers": _KAFKA_BROKERS},
                "identifier": {
                    "topic": "imazon.orders.events",
                    "cluster": "example_kafka",
                },
                "is_enabled": True,
            },
        )
        assert put_passive_resp.status_code in (200, 201), (
            f"PUT passive conf failed: {put_passive_resp.status_code} {put_passive_resp.text}"
        )
        passive_body = put_passive_resp.json()
        assert passive_body["dataset_urn"] == _PASSIVE_URN
        assert passive_body["mode"] == "passive"
        assert passive_body["platform"] == "kafka"
        # spec: USE_CASE_en.md §UC1 L133 — passive mode carries no schedule_tier
        assert passive_body.get("schedule_tier") is None, (
            "Passive conf must not carry schedule_tier; got "
            f"{passive_body.get('schedule_tier')!r}. spec: USE_CASE_en.md §UC1 L133"
        )
        # spec: USE_CASE_en.md §UC1 L82 — is_enabled survives round-trip
        assert passive_body["is_enabled"] is True

        # ── Step 5: Event history on passive dataset (may be empty) ──────────
        # UC1 narrative: "Every hour, DataSpoke's ingestion-passive-hourly DAG polls
        # DataHub for ingestion runs of all passive-marked datasets and writes one row
        # per run to the events table."
        # spec: USE_CASE_en.md §UC1 L133-L140
        events_passive_resp = await api_client.get(
            f"{passive_events_url}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert events_passive_resp.status_code == 200, (
            f"GET passive events failed: {events_passive_resp.status_code}"
        )
        events_passive_body = events_passive_resp.json()
        assert "events" in events_passive_body
        assert "offset" in events_passive_body
        assert "limit" in events_passive_body
        assert "total_count" in events_passive_body
        assert isinstance(events_passive_body["events"], list)
        # Emptiness is acceptable — hourly passive DAG may not have run yet

        # ── Step 6: Cross-dataset overview — both URNs present ───────────────
        # UC1 narrative: "Cross-dataset overview. Returns one row per dataset with
        # its full attr/ingestion/* aggregate."
        # spec: USE_CASE_en.md §UC1 L142-L149
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/ingestion?limit=100",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200, (
            f"GET cross-dataset ingestion failed: {overview_resp.status_code}"
        )
        overview_body = overview_resp.json()
        # spec: API.md §Standard Envelope
        assert "configs" in overview_body
        assert "total_count" in overview_body
        assert isinstance(overview_body["configs"], list)

        # Both registered URNs must appear in the configs list
        configs_by_urn = {c["dataset_urn"]: c for c in overview_body["configs"]}
        assert _ACTIVE_URN in configs_by_urn, (
            f"Active URN {_ACTIVE_URN!r} not found in GET /spoke/common/ingestion configs. "
            "spec: USE_CASE_en.md §UC1 L142-L149"
        )
        assert _PASSIVE_URN in configs_by_urn, (
            f"Passive URN {_PASSIVE_URN!r} not found in GET /spoke/common/ingestion configs. "
            "spec: USE_CASE_en.md §UC1 L142-L149"
        )
        # spec: USE_CASE_en.md §UC1 L150 — cross-dataset list reflects correct mode and
        # schedule_tier per URN
        active_row = configs_by_urn[_ACTIVE_URN]
        passive_row = configs_by_urn[_PASSIVE_URN]
        assert active_row["mode"] == "active", (
            f"Active URN mode expected 'active'; got {active_row['mode']!r}. "
            "spec: USE_CASE_en.md §UC1 L150"
        )
        assert active_row["schedule_tier"] == "daily", (
            f"Active URN schedule_tier expected 'daily'; got {active_row.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC1 L150"
        )
        assert passive_row["mode"] == "passive", (
            f"Passive URN mode expected 'passive'; got {passive_row['mode']!r}. "
            "spec: USE_CASE_en.md §UC1 L150"
        )

    finally:
        # ── Step 7: Cleanup — DELETE both confs ──────────────────────────────
        await api_client.delete(active_conf_url, headers=admin_headers)
        await api_client.delete(passive_conf_url, headers=admin_headers)
