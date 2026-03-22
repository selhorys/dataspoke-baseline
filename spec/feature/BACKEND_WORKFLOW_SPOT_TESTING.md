# Workflow Spot Testing — Ingestion

API-wired spot integration tests for the ingestion workflow. Tests exercise the full request path (HTTP → service → infrastructure) using real dev-env peripherals, with LLM responses mocked.

> Other workflows (validation, generation, metrics, embedding-sync, sla-monitor, ontology-rebuild) will be added later.

## Prerequisites

- PostgreSQL port-forwarded to `localhost:9201`
- DataHub GMS port-forwarded to `localhost:9004`
- Kestra port-forwarded to `localhost:9205`
- Dummy data ingested (Imazon `catalog` schema)

## Test File

`tests/integration/api_wired/spot/test_ingestion_workflow.py`

Separate from `test_ingestion_service.py` (which tests config CRUD). This file focuses on workflow orchestration: the run endpoint, the `ingestion-config-sync` flow's activity endpoint (`sync-periodic-ingestion-flows`), periodic flow generation, and the sync lifecycle.

## Fixtures

### Shared (inherited from conftest)

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `activity_server` | session | Real HTTP server; provides `mock_llm` for patching |
| `async_session` | function | PostgreSQL session for assertions and cleanup |
| `auth_headers` | function | JWT headers (`de`, `da`, `dg` groups) |

### Module-level

| Fixture | Purpose |
|---------|---------|
| `http_client` | `httpx.AsyncClient` pointing at `activity_server.port` |
| `kestra_client` | `KestraClient` instance for verifying flow registration |

```python
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])

# Shared location for all test ingestion configs (example-postgres in dev-env)
EXAMPLE_PG_LOCATION = {
    "host": "localhost",
    "port": 9201,
    "database": "example_db",
    "username": "postgres",
    "secret_ref": "dev/example-postgres-password",
}
```

### Datasets

The `module_dummy_data` fixture (triggered by `DUMMY_DATA_DATAHUB_SCHEMAS`) pre-registers the Imazon `catalog` schema datasets in DataHub with `SchemaMetadata`, `DatasetProperties`, and `Status` aspects. Tests use these already-registered datasets:

| Dataset URN | Rows | Used for |
|-------------|------|----------|
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)` | 30 | Run tests (test 1, 2, 7) |
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)` | 40 | Same-cron group (tests 4, 5, 6) |
| `urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.genre_hierarchy,DEV)` | 15 | Same-cron group (tests 4, 5, 6) |

Transient test URNs via `make_test_urn("ingestion", "<suffix>")` for config-only lifecycle tests (test 3) where DataHub metadata is not needed.

## Test Cases

### 1. `test_run_ingestion_via_public_api`

Verify that `POST .../method/run` executes the full pipeline directly.

**Setup**: PUT ingestion config for `title_master` (`source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, `periodic=false`)

**Action**: `POST /api/v1/spoke/common/data/{urn}/attr/ingestion/method/run` with `{"dry_run": false}`

**Assertions**:
- 200 response with `run_id` and `status == "success"`
- `GET .../attr/ingestion/event` returns `total_count >= 1`

**Cleanup**: DELETE config + events

### 2. `test_run_ingestion_dry_run`

**Setup**: Same as test 1

**Action**: `POST .../method/run` with `{"dry_run": true}`

**Assertions**:
- 200, `status == "success"`
- Event detail contains `"dry_run": true`

**Cleanup**: DELETE config + events

### 3. `test_list_periodic_datasets`

Verify `POST /internal/activities/list-periodic-datasets` returns correct URNs.

**Setup** (transient URNs — config-only, no DataHub metadata needed; all `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`):
- PUT config A: `periodic=true`, `schedule="0 2 * * *"`
- PUT config B: `periodic=true`, `schedule="0 2 * * *"`
- PUT config C: `periodic=true`, `schedule="0 6 * * *"`
- PUT config D: `periodic=false`

**Action**: `POST /internal/activities/list-periodic-datasets` with `{"schedule": "0 2 * * *"}`

**Assertions**:
- Returns list containing URN A and URN B
- Does not contain URN C or URN D

**Cleanup**: DELETE all test configs

### 4. `test_sync_creates_flows_per_schedule`

Verify the sync endpoint generates one Kestra flow per unique schedule.

**Setup** (3 real catalog datasets, all `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, same schedule for 2):
- PUT config for `title_master`: `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `editions`: `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `genre_hierarchy`: `periodic=true`, `schedule="0 6 * * *"`

**Action**: Call `POST /internal/activities/sync-periodic-ingestion-flows`

**Assertions**:
- Two flows in Kestra: `ingestion-periodic-{hash("0 2 * * *")}` and `ingestion-periodic-{hash("0 6 * * *")}`
- Both retrievable via `kestra_client.get_flow()`

**Cleanup**: Delete generated flows + test configs

### 5. `test_sync_removes_stale_flows`

**Setup**:
- PUT config for `title_master`: `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, `periodic=true`, `schedule="0 3 * * *"`
- Call `POST /internal/activities/sync-periodic-ingestion-flows` — flow created

**Action**:
- DELETE the config
- Call `POST /internal/activities/sync-periodic-ingestion-flows`

**Assertions**: The flow for `0 3 * * *` no longer exists in Kestra

**Cleanup**: DELETE any remaining test configs

### 6. `test_sync_updates_on_schedule_change`

**Setup**:
- PUT config for `title_master`: `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `editions`: `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, `periodic=true`, `schedule="0 2 * * *"`
- PUT config for `genre_hierarchy`: `source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`, `periodic=true`, `schedule="0 2 * * *"`
- Call `POST /internal/activities/sync-periodic-ingestion-flows` — one flow with 3 datasets

**Action**:
- PATCH `genre_hierarchy` config: `{"schedule": "0 6 * * *"}`
- Call `POST /internal/activities/sync-periodic-ingestion-flows`

**Assertions**:
- Flow for `0 2 * * *` still exists (`title_master` + `editions` remain)
- New flow for `0 6 * * *` exists
- `list-periodic-datasets` for `0 2 * * *` returns 2 URNs (not `genre_hierarchy`)
- `list-periodic-datasets` for `0 6 * * *` returns only `genre_hierarchy`

**Cleanup**: Delete generated flows + test configs

### 7. `test_concurrency_guard_prevents_duplicate`

Verify that concurrent runs for the same dataset URN are rejected.

**Setup**: PUT ingestion config for `title_master` (`source_type="postgres"`, `location=EXAMPLE_PG_LOCATION`)

**Action**:
- Start a run (`POST .../method/run` with `dry_run=false`) — this may take time
- Immediately POST another run for the same URN

**Assertions**: Second request returns `409` with error code `INGESTION_RUNNING`

**Cleanup**: Wait for first run to complete, DELETE config + events

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
    ├─ extract_metadata()
    ├─ emit_metadata_to_datahub()   [skip if dry_run]
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
Task: POST /internal/activities/sync-periodic-ingestion-flows
      ◄── {"created": [...], "deleted": [...], "unchanged": [...]}
```

Generates/updates/deletes `ingestion-periodic-*` flows in Kestra based on current configs.

### Periodic trigger (Kestra `ingestion-periodic-*` cron)

```
Kestra (ingestion-periodic-{hash} cron fires for schedule group)
  │
  ▼
Task 1: POST /internal/activities/list-periodic-datasets
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
