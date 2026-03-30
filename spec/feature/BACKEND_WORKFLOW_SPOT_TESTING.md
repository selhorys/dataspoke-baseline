# Workflow Spot Testing — Ingestion

API-wired spot integration tests for the ingestion workflow. Tests exercise the full request path (HTTP → service → real source extraction → DataHub emission) using real dev-env peripherals, with LLM responses mocked.

> Other workflows (validation, generation, metrics, embedding-sync, ontology-rebuild) will be added later.

## Prerequisites

- Host-mode DataSpoke server running with test stubs: `./dev_env/dataspoke-test-mode.sh`
- DataSpoke PostgreSQL port-forwarded to `localhost:9201`
- Example PostgreSQL (dummy data) port-forwarded to `localhost:9102`
- Example Kafka port-forwarded to `localhost:9104`
- DataHub GMS port-forwarded to `localhost:9004`
- Kestra port-forwarded to `localhost:9205`
- Redis port-forwarded to `localhost:9202`
- Dummy data ingested (Imazon `catalog` schema + `imazon.orders.events` topic)

## Test File

`tests/integration/api_wired/spot/test_ingestion_workflow.py`

Separate from `test_ingestion_service.py` (which tests config CRUD). This file focuses on workflow orchestration: the run endpoint (real extraction + DataHub emission), the `ingestion-config-sync` flow's activity endpoint (`ingestion/sync-periodic-flows`), periodic flow generation, and the sync lifecycle.

## Fixtures

### Shared (inherited from conftest)

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `require_server` | session (autouse) | Fails fast if `DATASPOKE_TEST_MODE` not set, server not running, or `ingestion-config-sync` flow not registered |
| `async_session` | function | PostgreSQL session for assertions and cleanup |
| `auth_headers` | function | JWT headers (`de`, `da`, `dg` groups) |
| `datahub_client` | function | `DataHubClient` for verifying aspects landed in DataHub |

### Module-level

| Fixture | Purpose |
|---------|---------|
| `http_client` | `httpx.AsyncClient` pointing at host-mode server (`localhost:{DATASPOKE_API_PORT}`) |
| `kestra_client` | `KestraClient` instance for verifying flow registration |

```python
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])

# PostgreSQL connection — resolved from dev_env/.env
EXAMPLE_PG_LOCATOR = {"host": "localhost", "port": <DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT>}
EXAMPLE_PG_IDENTIFIER = {"database": <DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB>, "schema_name": "catalog"}
EXAMPLE_PG_AUTH = {"username": <DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER>, "password": <DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD>}

# Kafka connection — resolved from dev_env/.env
EXAMPLE_KAFKA_LOCATOR = {"bootstrap_servers": <DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS>}
EXAMPLE_KAFKA_IDENTIFIER = {"topic": "imazon.orders.events", "cluster": <DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_INSTANCE>}
```

### Datasets

The `module_dummy_data` fixture (triggered by `DUMMY_DATA_DATAHUB_SCHEMAS` and `DUMMY_DATA_TOPICS`) pre-registers the Imazon `catalog` schema datasets in DataHub and seeds Kafka topic messages. Tests use these already-registered datasets:

| Dataset URN | Source | Used for |
|-------------|--------|----------|
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)` | PostgreSQL | Run tests (test 1, 2, 7) |
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)` | PostgreSQL | Same-cron group (tests 4, 5, 6) |
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.genre_hierarchy,DEV)` | PostgreSQL | Same-cron group (tests 4, 5, 6) |
| `urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)` | Kafka | Kafka ingestion test (test 8) |

Transient test URNs via `make_test_urn("ingestion", "<suffix>")` for config-only lifecycle tests (test 3, 9) where DataHub metadata is not needed.

## Test Cases

### 1. `test_run_ingestion_via_public_api`

Verify that `POST .../method/run` executes the full pipeline: connects to example-postgres, discovers schema, emits aspects to DataHub.

**Setup**: PUT ingestion config for `title_master` (`source_type="POSTGRESQL"`, `periodic=false`)

**Action**: `POST /api/v1/spoke/common/data/{urn}/attr/ingestion/method/run` with `{"dry_run": false}`

**Assertions**:
- 200 response with `run_id` and `status == "success"`
- `entities_ingested >= 1`
- `GET .../attr/ingestion/event` returns `total_count >= 1`
- DataHub `SchemaMetadataClass` exists with `fields` count > 0
- DataHub `DatasetPropertiesClass` has `customProperties.source == "dataspoke-ingestion"`

**Cleanup**: DELETE config + events

### 2. `test_run_ingestion_dry_run`

**Setup**: Same as test 1

**Action**: `POST .../method/run` with `{"dry_run": true}`

**Assertions**:
- 200, `status == "success"`
- Event detail contains `"dry_run": true`

**Cleanup**: DELETE config + events

### 3. `test_list_periodic_datasets`

Verify `POST /internal/activities/ingestion/list-periodic` returns correct URNs.

**Setup** (transient URNs — config-only, no DataHub metadata needed; all `source_type="POSTGRESQL"`):
- PUT config A: `periodic=true`, `schedule="0 2 * * *"`
- PUT config B: `periodic=true`, `schedule="0 2 * * *"`
- PUT config C: `periodic=true`, `schedule="0 6 * * *"`
- PUT config D: `periodic=false`

**Action**: `POST /internal/activities/ingestion/list-periodic` with `{"schedule": "0 2 * * *"}`

**Assertions**:
- Returns list containing URN A and URN B
- Does not contain URN C or URN D

**Cleanup**: DELETE all test configs

### 4. `test_sync_creates_flows_per_schedule`

Verify the sync endpoint generates one Kestra flow per unique schedule.

**Setup** (3 real catalog datasets, all `source_type="POSTGRESQL"`, same schedule for 2):
- PUT config for `title_master`: `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `editions`: `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `genre_hierarchy`: `periodic=true`, `schedule="0 6 * * *"`

**Action**: Call `POST /internal/activities/ingestion/sync-periodic-flows`

**Assertions**:
- Two flows in Kestra: `ingestion-periodic-{hash("0 2 * * *")}` and `ingestion-periodic-{hash("0 6 * * *")}`
- Both retrievable via `kestra_client.get_flow()`

**Cleanup**: Delete generated flows + test configs

### 5. `test_sync_removes_stale_flows`

**Setup**:
- PUT config for `title_master`: `source_type="POSTGRESQL"`, `periodic=true`, `schedule="0 3 * * *"`
- Call `POST /internal/activities/ingestion/sync-periodic-flows` — flow created

**Action**:
- DELETE the config
- Call `POST /internal/activities/ingestion/sync-periodic-flows`

**Assertions**: The flow for `0 3 * * *` no longer exists in Kestra

**Cleanup**: DELETE any remaining test configs

### 6. `test_sync_updates_on_schedule_change`

**Setup**:
- PUT config for `title_master`: `source_type="POSTGRESQL"`, `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `editions`: `source_type="POSTGRESQL"`, `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `genre_hierarchy`: `source_type="POSTGRESQL"`, `periodic=true`, `schedule="0 2 * * *"`
- Call `POST /internal/activities/ingestion/sync-periodic-flows` — one flow with 3 datasets

**Action**:
- PATCH `genre_hierarchy` config: `{"schedule": "0 6 * * *"}`
- Call `POST /internal/activities/ingestion/sync-periodic-flows`

**Assertions**:
- Flow for `0 2 * * *` still exists (`title_master` + `editions` remain)
- New flow for `0 6 * * *` exists
- `ingestion/list-periodic` for `0 2 * * *` returns 2 URNs (not `genre_hierarchy`)
- `ingestion/list-periodic` for `0 6 * * *` returns only `genre_hierarchy`

**Cleanup**: Delete generated flows + test configs

### 7. `test_concurrency_guard_prevents_duplicate`

Verify that concurrent runs for the same dataset URN are rejected.

**Setup**: PUT ingestion config for `title_master` (`source_type="POSTGRESQL"`)

**Action**:
- Start a run (`POST .../method/run` with `dry_run=false`) — this may take time
- Immediately POST another run for the same URN

**Assertions**: Second request returns `409` with error code `INGESTION_RUNNING`

**Cleanup**: Wait for first run to complete, DELETE config + events

### 8. `test_run_kafka_ingestion`

Verify end-to-end KAFKA ingestion: schema inference from messages and DataHub emission.

**Setup**: PUT ingestion config for `imazon.orders.events` (`source_type="KAFKA"`, `auth=null`, `periodic=false`)

**Action**: `POST .../method/run` with `{"dry_run": false}`

**Assertions**:
- 200, `status == "success"`, `entities_ingested >= 1`
- GET config confirms KAFKA shape: `locator.bootstrap_servers`, `identifier.topic`, `auth == null`
- DataHub `SchemaMetadataClass` exists with inferred fields (from polled messages)
- DataHub `DatasetPropertiesClass.name == "imazon.orders.events"`

**Cleanup**: DELETE config + events

### 9. `test_mixed_source_types_in_periodic_sync`

Verify periodic sync groups configs by schedule regardless of source_type.

**Setup**:
- PUT POSTGRESQL config for `title_master`: `periodic=true`, `schedule="0 4 * * *"`
- PUT KAFKA config (transient URN): `periodic=true`, `schedule="0 4 * * *"`

**Action**: `POST /internal/activities/ingestion/sync-periodic-flows`

**Assertions**:
- One flow created for `schedule="0 4 * * *"`
- `ingestion/list-periodic` returns both the PG and Kafka URNs

**Cleanup**: Delete flow + configs

## Message Flow Summary

### Manual trigger

```
Client
  POST /api/v1/spoke/common/data/{urn}/attr/ingestion/method/run
  Body: {"dry_run": false}
    │
    ▼
DataSpoke API (data router)
  concurrency guard (per dataset URN)
  IngestionService.run(dataset_urn)
    ├─ connect to source (asyncpg / confluent_kafka)
    ├─ discover schema (information_schema / message polling)
    ├─ emit MCPs to DataHub (StatusClass, DatasetPropertiesClass, SchemaMetadataClass)
    │    [skip if dry_run]
    └─ record_ingestion_event()
    │
    ▼
Client ◄── {"run_id": "...", "status": "success", "detail": {...}}
```

### Config sync (Kestra `ingestion-config-sync`, default `*/10 * * * *`)

```
Kestra (ingestion-config-sync cron fires)
  │
  ▼
Task: POST /internal/activities/ingestion/sync-periodic-flows
      ◄── {"created": [...], "deleted": [...], "unchanged": [...]}
```

Generates/updates/deletes `ingestion-periodic-*` flows in Kestra based on current configs.

### Periodic trigger (Kestra `ingestion-periodic-*` cron)

```
Kestra (ingestion-periodic-{hash} cron fires for schedule group)
  │
  ▼
Task 1: POST /internal/activities/ingestion/list-periodic
         Body: {"schedule": "0 2 * * *"}
         ◄── ["urn:...:title_master", "urn:...:editions", "urn:...:genre_hierarchy"]
  │
  ▼
Task 2: EachParallel (concurrency: 5)
  ├─ POST /api/v1/spoke/common/data/{title_master}/attr/ingestion/method/run
  │  Body: {"dry_run": false}   (service account auth)
  │  ◄── {"run_id": "...", "status": "success", ...}
  │
  ├─ POST /api/v1/spoke/common/data/{editions}/attr/ingestion/method/run
  │  Body: {"dry_run": false}   (service account auth)
  │  ◄── {"run_id": "...", "status": "success", ...}
  │
  └─ POST /api/v1/spoke/common/data/{genre_hierarchy}/attr/ingestion/method/run
     Body: {"dry_run": false}   (service account auth)
     ◄── {"run_id": "...", "status": "success", ...}
```

## Assertion Rules

Per `spec/TESTING.md`:

- Never hardcode row counts — query actual counts within the test
- Never hardcode surrogate IDs — look up by stable natural key (dataset URN)
- Never assert on wall-clock timestamps — assert on relative ordering or freshness windows
