"""Spot tests for internal activity endpoints.

Concerns covered — for each domain (ingestion, validation, metagen, metrics, ontogen):
- POST /internal/activities/{domain}/list-active — returns list of URNs/IDs for given tier
- POST /internal/activities/{domain}/run — executes for a dataset URN (or metric_id)

Auth: X-Internal-Token header (internal_headers fixture).
Internal routes are mounted WITHOUT the /api/v1 prefix (see src/api/main.py line 271).
"""
# spec: BACKEND.md §Tier-DAG selection
# spec: BACKEND.md §Ingestion Service / §Validation Service / §Metrics Service

import os
import urllib.parse

import httpx
import pytest

# Dummy-data Postgres: spec/TESTING.md L312-313 — example_db on the dev-env host.
_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")
_VAULT_NAME = "dataspoke-conf-spot-pg"
_VAULT_KEY = "password"

# Per-module dummy-data seed: re-seed catalog schema in PG and ingest into DataHub
# before this module's tests run (autoused by tests/integration/conftest.py).
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Imazon dataset that is guaranteed to exist in DataHub after reset
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# Second URN for tier-isolation cross-check (seeded in a different tier)
_TEST_URN_2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_ENCODED_URN_2 = urllib.parse.quote(_TEST_URN_2, safe="")


@pytest.mark.asyncio
async def test_ingestion_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/list-active returns URNs for the requested tier
    and excludes URNs assigned to a different tier.

    spec: BACKEND.md §Tier-DAG selection — "the periodic DAG that runs at a given tier
    fetches only the configs whose schedule_tier matches the DAG's tier"
    """
    # spec: BACKEND.md §Tier-DAG selection
    conf_hourly = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"
    conf_daily = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/ingestion/conf"

    # Seed an enabled config in the target tier (hourly)
    await api_client.put(
        conf_hourly,
        headers=admin_headers,
        json={
            "mode": "active",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "title_master"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": _VAULT_NAME,
                    "key": _VAULT_KEY,
                    "force_overwrite": True,
                },
            },
            "is_enabled": True,
            "schedule_tier": "hourly",
        },
    )

    # Seed an enabled config in a DIFFERENT tier (daily) — must NOT appear in hourly results
    await api_client.put(
        conf_daily,
        headers=admin_headers,
        json={
            "mode": "active",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {"database": _PG_DB, "schema_name": "catalog", "table": "editions"},
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": _VAULT_NAME,
                    "key": _VAULT_KEY,
                    "force_overwrite": True,
                },
            },
            "is_enabled": True,
            "schedule_tier": "daily",
        },
    )

    resp = await api_client.post(
        "/internal/activities/ingestion/list-active",
        headers=internal_headers,
        json={"tier": "hourly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Hourly-tier URN must appear
    assert _TEST_URN in body, (
        f"Expected {_TEST_URN} in hourly list, got: {body}"
    )
    # Daily-tier URN must NOT appear in hourly results
    assert _TEST_URN_2 not in body, (
        f"Tier isolation violated: {_TEST_URN_2} (daily tier) appeared in hourly list"
    )

    # Cleanup
    await api_client.delete(conf_hourly, headers=admin_headers)
    await api_client.delete(conf_daily, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/run executes ingestion for a dataset URN.

    Pre-condition: an active ingestion config must exist. Creates one, runs activity,
    then cleans up.

    spec: BACKEND.md §Active run pipeline L195-L204
    """
    # spec: BACKEND.md §Active run pipeline — shape: {run_id, status, ...}
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/ingestion/conf"

    # Create active ingestion config
    await api_client.put(
        conf_url,
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
            "is_enabled": True,
            "schedule_tier": "daily",
        },
    )

    resp = await api_client.post(
        "/internal/activities/ingestion/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN, "dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Active run pipeline — response must carry both run_id and status
    assert "run_id" in body and "status" in body, (
        f"Expected both 'run_id' and 'status' in ingestion run response, got: {list(body.keys())}"
    )

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_list_active_daily(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/validation/list-active returns URNs for the requested
    tier and excludes URNs assigned to a different tier.

    spec: BACKEND.md §Tier-DAG selection — tier filter applies to validation_configs
    """
    # spec: BACKEND.md §Tier-DAG selection
    conf_daily = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    conf_weekly = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/validation/conf"

    _rule = {
        "rule_id": "spot-list-vol",
        "type": "volume",
        "metric": "row_count",
        "condition": {"type": "between", "min": 1, "max": 100000},
    }

    # Seed an enabled config in the target tier (daily)
    await api_client.put(
        conf_daily,
        headers=admin_headers,
        json={
            "rules": [_rule],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-list@imazon.com",
        },
    )

    # Seed an enabled config in a DIFFERENT tier (weekly) — must NOT appear in daily results
    await api_client.put(
        conf_weekly,
        headers=admin_headers,
        json={
            "rules": [{**_rule, "rule_id": "spot-list-vol-2"}],
            "schedule_tier": "weekly",
            "is_enabled": True,
            "owner": "spot-list@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/validation/list-active",
        headers=internal_headers,
        json={"tier": "daily"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Daily-tier URN must appear
    assert _TEST_URN in body, (
        f"Expected {_TEST_URN} in daily validation list, got: {body}"
    )
    # Weekly-tier URN must NOT appear in daily results
    assert _TEST_URN_2 not in body, (
        f"Tier isolation violated: {_TEST_URN_2} (weekly tier) appeared in daily list"
    )

    # Cleanup
    await api_client.delete(conf_daily, headers=admin_headers)
    await api_client.delete(conf_weekly, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/validation/run executes validation for a dataset URN.

    spec: BACKEND.md §Validation Run Pipeline — response shape carries run_id + status.
    """
    # spec: BACKEND.md §Validation Service — response: {run_id, status}
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    # Create validation config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-activity-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/validation/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Validation Run Pipeline — response must carry both run_id and status
    assert "run_id" in body and "status" in body, (
        f"Expected both 'run_id' and 'status' in validation run response, got: {list(body.keys())}"
    )

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_list_active_weekly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metagen/list-active returns URNs for the requested tier
    and excludes URNs assigned to a different tier.

    spec: BACKEND.md §Tier-DAG selection — tier filter applies to metagen_configs
    """
    # spec: BACKEND.md §Tier-DAG selection
    conf_weekly = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    conf_hourly = f"/api/v1/spoke/common/data/{_ENCODED_URN_2}/attr/metagen/conf"

    # Seed an enabled config in the target tier (weekly)
    await api_client.put(
        conf_weekly,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": True,
            "schedule_tier": "weekly",
            "owner": "spot-list@imazon.com",
        },
    )

    # Seed an enabled config in a DIFFERENT tier (hourly) — must NOT appear in weekly results
    await api_client.put(
        conf_hourly,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": True,
            "schedule_tier": "hourly",
            "owner": "spot-list@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/metagen/list-active",
        headers=internal_headers,
        json={"tier": "weekly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Weekly-tier URN must appear
    assert _TEST_URN in body, (
        f"Expected {_TEST_URN} in weekly metagen list, got: {body}"
    )
    # Hourly-tier URN must NOT appear in weekly results
    assert _TEST_URN_2 not in body, (
        f"Tier isolation violated: {_TEST_URN_2} (hourly tier) appeared in weekly list"
    )

    # Cleanup
    await api_client.delete(conf_weekly, headers=admin_headers)
    await api_client.delete(conf_hourly, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metagen/run executes metagen for a dataset URN.

    spec: BACKEND.md §Generation Pipeline — response carries status field.
    """
    # spec: BACKEND.md §Metadata Generation Service
    conf_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    # Create metagen config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": True,
            "schedule_tier": "weekly",
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.post(
        "/internal/activities/metagen/run",
        headers=internal_headers,
        json={"dataset_urn": _TEST_URN, "dry_run": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Generation Pipeline — response must contain a status indicator
    # and dataset context; 'id' or 'dataset_urn' are acceptable alongside 'status'
    assert "status" in body, (
        f"Expected 'status' in metagen run response, got: {list(body.keys())}"
    )

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metrics_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/list-active returns metric IDs for the requested
    tier and excludes metric IDs assigned to a different tier.

    spec: BACKEND.md §Tier-DAG selection — tier filter applies to metric_definitions
    """
    # spec: BACKEND.md §Tier-DAG selection
    metric_id_hourly = "spot-list-freshness-hourly"
    metric_id_daily = "spot-list-freshness-daily"
    conf_hourly = f"/api/v1/spoke/dg/metric/{metric_id_hourly}/attr/conf"
    conf_daily = f"/api/v1/spoke/dg/metric/{metric_id_daily}/attr/conf"

    _common_conf = {
        "title": "Spot List Freshness",
        "description": "Spot test metric for tier-selection.",
        "theme": "freshness",
        "measurement_query": {
            "aggregation": "pct_fresh",
            "dataset_filter": {"dataset_urns": [_TEST_URN]},
        },
        "is_enabled": True,
    }

    # Seed an enabled metric in the target tier (hourly)
    await api_client.put(
        conf_hourly,
        headers=admin_headers,
        json={**_common_conf, "schedule_tier": "hourly"},
    )

    # Seed an enabled metric in a DIFFERENT tier (daily) — must NOT appear in hourly results
    await api_client.put(
        conf_daily,
        headers=admin_headers,
        json={**_common_conf, "schedule_tier": "daily"},
    )

    resp = await api_client.post(
        "/internal/activities/metrics/list-active",
        headers=internal_headers,
        json={"tier": "hourly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Hourly-tier metric must appear
    assert metric_id_hourly in body, (
        f"Expected metric '{metric_id_hourly}' in hourly metrics list, got: {body}"
    )
    # Daily-tier metric must NOT appear in hourly results
    assert metric_id_daily not in body, (
        f"Tier isolation violated: '{metric_id_daily}' (daily tier) appeared in hourly list"
    )

    # Cleanup
    await api_client.delete(conf_hourly, headers=admin_headers)
    await api_client.delete(conf_daily, headers=admin_headers)


@pytest.mark.asyncio
async def test_metrics_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/run executes measurement for a metric_id.

    spec: BACKEND.md §Metrics Service — response carries run_id + status.
    """
    # spec: BACKEND.md §Metrics Service
    metric_id = "spot-activity-freshness"
    conf_url = f"/api/v1/spoke/dg/metric/{metric_id}/attr/conf"

    # Create metric config
    await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "title": "Spot Activity Freshness",
            "description": "Spot test metric description.",
            "theme": "freshness",
            "measurement_query": {
                "aggregation": "pct_fresh",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
            },
            "schedule_tier": "hourly",
            "is_enabled": True,
        },
    )

    resp = await api_client.post(
        "/internal/activities/metrics/run",
        headers=internal_headers,
        json={"metric_id": metric_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Metrics Service — response must carry both run_id and status
    assert "run_id" in body and "status" in body, (
        f"Expected both 'run_id' and 'status' in metrics run response, got: {list(body.keys())}"
    )

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_ontogen_run_activity_dry(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ontogen/run executes ontogen inference (dry_run).

    spec: BACKEND.md §Ontogen Inference Pipeline — response carries status.
    """
    # spec: BACKEND.md §Ontology Generation Service
    resp = await api_client.post(
        "/internal/activities/ontogen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Ontogen Inference Pipeline — response must carry status
    assert "status" in body, (
        f"Expected 'status' in ontogen run response, got: {list(body.keys())}"
    )
