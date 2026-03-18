# Kestra API Skill — Reference

## Reference Lookup Table

| Question type | Where to look |
|---|---|
| "What REST endpoints exist?" | `ref/github/kestra/openapi.yml` (complete OpenAPI 3.0 spec) |
| "How does the Flow API work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/FlowController.java` |
| "How does execution triggering work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/ExecutionController.java` |
| "How does the KV store work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/KVController.java` |
| "How do triggers work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/TriggerController.java` |
| "How do logs work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/LogController.java` |
| "How do namespaces work?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/NamespaceController.java` |
| "What task types are available?" | `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/PluginController.java` |
| "How does DataSpoke call Kestra?" | `src/workflows/kestra/client.py` (KestraClient wrapper) |
| "What flows does DataSpoke define?" | `src/workflows/flows/*.yaml` |
| "What models does the client use?" | `src/workflows/kestra/models.py` (ExecutionResponse, ExecutionStatus, FlowResponse) |
| "How are errors handled?" | `src/workflows/kestra/errors.py` |
| "How do activity endpoints work?" | `spec/feature/BACKEND.md` §Kestra Workflows |

### Controller Source Files

```
ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/
  FlowController.java           — Flow CRUD, search, import/export, validation, graph
  ExecutionController.java      — Trigger, poll, kill, restart, replay, labels, webhooks
  LogController.java            — Log search, streaming (SSE), download
  KVController.java             — Namespace KV store CRUD, TTL, inheritance
  TriggerController.java        — Trigger management, backfill, enable/disable
  NamespaceController.java      — Namespace search, dependencies
  TaskRunController.java        — Task run details within executions
  PluginController.java         — Plugin listing and introspection
  MiscController.java           — Instance config, health
```

---

## API Endpoint Reference

> **Note on tenancy**: In OSS Kestra, `{tenant}` in paths is empty or omitted. DataSpoke's dev environment uses OSS Kestra, so paths are `/api/v1/flows/...` not `/api/v1/{tenant}/flows/...`. The KestraClient in `src/workflows/kestra/client.py` already handles this.

### 1. Flows API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/flows` | Create a flow from YAML (`Content-Type: application/x-yaml`) |
| `GET` | `/api/v1/flows/{namespace}/{id}` | Get flow by namespace + ID. Query: `?source=true` for YAML, `?revision=N` |
| `PUT` | `/api/v1/flows/{namespace}/{id}` | Update flow from YAML source |
| `DELETE` | `/api/v1/flows/{namespace}/{id}` | Delete a flow |
| `GET` | `/api/v1/flows/{namespace}` | List all flows in a namespace |
| `GET` | `/api/v1/flows/search` | Search flows. Query: `page`, `size`, `sort`, `namespace`, `q`, `labels` |
| `GET` | `/api/v1/flows/distinct-namespaces` | List all namespaces that contain flows |
| `POST` | `/api/v1/flows/validate` | Validate flow YAML without creating it |
| `POST` | `/api/v1/flows/import` | Import flows from YAML file |
| `POST` | `/api/v1/flows/export/by-ids` | Export flows as ZIP. Body: array of `{namespace, id}` |
| `POST` | `/api/v1/flows/export/by-query` | Export flows matching query as ZIP |
| `POST` | `/api/v1/flows/bulk` | Batch create/update flows. Query: `?delete=true` removes missing |
| `DELETE` | `/api/v1/flows/delete/by-ids` | Bulk delete. Body: array of `{namespace, id}` |
| `POST` | `/api/v1/flows/disable/by-ids` | Bulk disable. Body: array of `{namespace, id}` |
| `POST` | `/api/v1/flows/enable/by-ids` | Bulk enable. Body: array of `{namespace, id}` |
| `GET` | `/api/v1/flows/{namespace}/{id}/graph` | Flow DAG graph (nodes/edges) |
| `GET` | `/api/v1/flows/{namespace}/{id}/dependencies` | Flow dependency graph |
| `GET` | `/api/v1/flows/{namespace}/{id}/revisions` | Revision history. Query: `page`, `size` |
| `GET` | `/api/v1/flows/{namespace}/{id}/tasks/{taskId}` | Get specific task definition |

**Request/Response notes:**
- Create/update accept `Content-Type: application/x-yaml` with raw YAML body
- Search returns `PagedResults`: `{ results: [...], total: N }`
- Validation returns errors array (empty = valid)

### 2. Executions API

#### Triggering

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/executions/{namespace}/{id}` | Trigger execution. Body: `multipart/form-data` with input fields. Query: `?labels=key:value`, `?wait=true` |
| `POST` | `/api/v1/executions/webhook/{namespace}/{id}/{key}` | Trigger via webhook (any content type) |

#### Status and Search

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/executions/{executionId}` | Get execution by ID |
| `GET` | `/api/v1/executions/search` | Search executions. Query: `namespace`, `flowId`, `state`, `labels=key:value`, `startDate`, `endDate`, `page`, `size` |
| `GET` | `/api/v1/executions/latest` | Get latest executions |
| `GET` | `/api/v1/executions/{executionId}/state` | Get execution state only |
| `GET` | `/api/v1/executions/{executionId}/graph` | Get execution graph with task run states |
| `GET` | `/api/v1/executions/{executionId}/flow` | Get flow definition used by this execution |
| `GET` | `/api/v1/executions/{executionId}/follow` | SSE stream — real-time execution status updates |

#### Labels

| Method | Path | Description |
|---|---|---|
| `PUT` | `/api/v1/executions/{executionId}/labels` | Set labels. Body: `[{"key":"k","value":"v"}]` |
| `POST` | `/api/v1/executions/labels/by-ids` | Bulk set labels |

#### State Management

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/executions/{executionId}/kill` | Kill a running execution |
| `POST` | `/api/v1/executions/kill/by-ids` | Bulk kill. Body: array of execution IDs |
| `POST` | `/api/v1/executions/{executionId}/pause` | Pause execution |
| `POST` | `/api/v1/executions/{executionId}/resume` | Resume paused execution |
| `POST` | `/api/v1/executions/{executionId}/restart` | Restart failed execution |
| `POST` | `/api/v1/executions/{executionId}/replay` | Replay execution from start |
| `POST` | `/api/v1/executions/{executionId}/force-run` | Force re-run |
| `POST` | `/api/v1/executions/{executionId}/change-status` | Change state. Query: `?status=FAILED` |

#### Files

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/executions/{executionId}/file` | Download file. Query: `?path=<uri>` |
| `GET` | `/api/v1/executions/{executionId}/file/metas` | File metadata. Query: `?path=<uri>` |
| `GET` | `/api/v1/executions/{executionId}/file/preview` | Preview file. Query: `?path=<uri>&maxRows=100` |

#### Deletion

| Method | Path | Description |
|---|---|---|
| `DELETE` | `/api/v1/executions/{executionId}` | Delete execution. Query: `?deleteLogs=true&deleteMetrics=true&deleteStorage=true` |
| `DELETE` | `/api/v1/executions/by-ids` | Bulk delete. Body: array of execution IDs |
| `DELETE` | `/api/v1/executions/by-query` | Delete by query filters |

#### Debugging

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/executions/{executionId}/eval/{taskRunId}` | Evaluate Pebble expression in task run context. Body: expression as `text/plain` |

**Execution states**: `CREATED`, `RUNNING`, `PAUSED`, `SUCCESS`, `WARNING`, `FAILED`, `KILLING`, `KILLED`, `RESTARTED`, `QUEUED`, `RETRYING`, `RETRIED`, `CANCELLED`

**Terminal states**: `SUCCESS`, `WARNING`, `FAILED`, `KILLED`

### 3. Logs API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/logs/search` | Search logs. Query: `namespace`, `flowId`, `minLevel`, `page`, `size` |
| `GET` | `/api/v1/logs/{executionId}` | Get logs for execution. Query: `?minLevel=INFO&taskRunId=X&taskId=Y&attempt=N` |
| `GET` | `/api/v1/logs/{executionId}/download` | Download logs as text file |
| `GET` | `/api/v1/logs/{executionId}/follow` | SSE stream — real-time log entries. Query: `?minLevel=INFO`. Timeout: 1h idle |
| `DELETE` | `/api/v1/logs/{executionId}` | Delete logs for execution |
| `DELETE` | `/api/v1/logs/{namespace}/{flowId}` | Delete logs for a flow |

**Log levels**: `ERROR`, `WARN`, `INFO`, `DEBUG`, `TRACE`

### 4. KV Store API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/namespaces/{namespace}/kv` | List KV entries in namespace |
| `GET` | `/api/v1/namespaces/{namespace}/kv/{key}` | Get KV value. Returns `{type, value, revision, updated}` |
| `PUT` | `/api/v1/namespaces/{namespace}/kv/{key}` | Set KV value. Body: `text/plain`. Headers: `description`, `ttl` (ISO-8601 duration, e.g. `PT1H`) |
| `DELETE` | `/api/v1/namespaces/{namespace}/kv/{key}` | Delete KV entry |
| `DELETE` | `/api/v1/namespaces/{namespace}/kv` | Bulk delete. Body: `{keys: ["k1","k2"]}` |
| `GET` | `/api/v1/namespaces/{namespace}/kv/inheritance` | Get inherited KV entries from parent namespaces |

**KV types**: `STRING`, `JSON`, `BYTES`, `INTEGER`, `FLOAT`, `BOOLEAN`, `INSTANT`

**Notes:**
- JSON values are auto-detected when body is valid JSON
- TTL causes automatic expiry — useful for caching
- Child namespaces inherit parent KV values; child values override parents

### 5. Triggers API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/triggers/search` | Search triggers. Query: `namespace`, `flowId`, `page`, `size` |
| `GET` | `/api/v1/triggers/{namespace}/{flowId}` | Get triggers for a specific flow |
| `PUT` | `/api/v1/triggers` | Update trigger state |
| `POST` | `/api/v1/triggers/{namespace}/{flowId}/{triggerId}/unlock` | Unlock a stuck trigger |
| `POST` | `/api/v1/triggers/{namespace}/{flowId}/{triggerId}/restart` | Restart a trigger |
| `DELETE` | `/api/v1/triggers/{namespace}/{flowId}/{triggerId}` | Delete trigger |
| `POST` | `/api/v1/triggers/set-disabled/by-triggers` | Enable/disable. Body: `{triggers: [...], disabled: bool}` |
| `PUT` | `/api/v1/triggers/backfill/pause` | Pause backfill on a trigger |
| `PUT` | `/api/v1/triggers/backfill/unpause` | Resume paused backfill |
| `POST` | `/api/v1/triggers/backfill/delete` | Delete backfill configuration |

### 6. Namespaces API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/namespaces/search` | Search namespaces. Query: `q`, `page`, `size`, `existing` |
| `GET` | `/api/v1/namespaces/{id}` | Get namespace details |
| `GET` | `/api/v1/namespaces/{namespace}/dependencies` | Flow dependency graph in namespace |

### 7. Misc / System API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/configs` | Instance configuration (no auth in dev) |
| `GET` | `/api/v1/plugins` | List installed plugins |
| `GET` | `/api/v1/plugins/{cls}` | Plugin details by class name |

---

## Pagination Convention

All search endpoints return `PagedResults`:
```json
{
  "results": [...],
  "total": 42
}
```

Standard query parameters: `page` (default=1, 1-indexed), `size` (default=10), `sort` (array).

---

## DataSpoke KestraClient Coverage

The `KestraClient` in `src/workflows/kestra/client.py` wraps these operations:

| KestraClient method | Kestra API endpoint |
|---|---|
| `create_or_update_flow(yaml)` | `PUT /api/v1/flows/{ns}/{id}` → fallback `POST /api/v1/flows` |
| `get_flow(flow_id)` | `GET /api/v1/flows/{ns}/{id}` |
| `trigger_execution(flow_id, inputs, labels)` | `POST /api/v1/executions/{ns}/{id}` (multipart) |
| `get_execution(execution_id)` | `GET /api/v1/executions/{id}` |
| `wait_for_execution(execution_id, ...)` | Polls `GET /api/v1/executions/{id}` |
| `find_running_executions(flow_id, label)` | `GET /api/v1/executions/search?state=RUNNING&...` |
| `trigger_and_wait(flow_id, ...)` | `trigger_execution` + `wait_for_execution` |
| `check_no_duplicate(flow_id, label, ...)` | `find_running_executions` → raises `ConflictError` if found |
| `_set_labels(execution_id, labels)` | `POST /api/v1/executions/{id}/labels` |

**Not yet wrapped** (use direct HTTP if needed):
- Flow search, validation, import/export, bulk operations
- Execution kill, restart, replay, pause/resume
- Logs API (search, streaming)
- KV Store API
- Triggers API
- Namespaces API

---

## Known Pattern A — Trigger Flow and Wait for Result

**Question**: How do I trigger a Kestra flow from DataSpoke and wait for the result?

### Using KestraClient (preferred)

```python
from src.workflows.kestra.client import KestraClient

client = KestraClient(base_url="http://localhost:8080", namespace="dataspoke")

# One-shot: trigger + wait
result = await client.trigger_and_wait(
    flow_id="ingestion",
    inputs={
        "callback_base_url": "http://host.docker.internal:8000",
        "dataset_urn": "urn:li:dataset:...",
        "run_id": "run-001",
    },
    labels={"dataset_urn": "urn:li:dataset:..."},
    timeout_seconds=300,
)
print(result.status)  # ExecutionStatus.SUCCESS
```

### Using direct HTTP

```python
import httpx

async with httpx.AsyncClient(base_url="http://localhost:8080") as client:
    # Trigger (multipart form data)
    resp = await client.post(
        "/api/v1/executions/dataspoke/ingestion",
        files={
            "callback_base_url": (None, "http://host.docker.internal:8000"),
            "dataset_urn": (None, "urn:li:dataset:..."),
            "run_id": (None, "run-001"),
        },
    )
    execution = resp.json()

    # Poll until done
    while True:
        resp = await client.get(f"/api/v1/executions/{execution['id']}")
        state = resp.json()["state"]["current"]
        if state in ("SUCCESS", "FAILED", "KILLED", "WARNING"):
            break
        await asyncio.sleep(1)
```

### Decision

| Approach | Verdict |
|---|---|
| `KestraClient.trigger_and_wait()` | **Preferred** — handles polling, timeout, error parsing |
| Direct HTTP polling | Use when you need custom polling logic or KestraClient doesn't wrap the operation |
| SSE follow (`/follow` endpoint) | Use for real-time UI updates, not backend services |

---

## Known Pattern B — Concurrency Guard (Deduplication)

**Question**: How do I prevent duplicate flow runs for the same entity?

### Using KestraClient

```python
# Before triggering, check for running duplicates
await client.check_no_duplicate(
    flow_id="ingestion",
    label_key="dataset_urn",
    label_value="urn:li:dataset:...",
    error_code="INGESTION_RUNNING",
)
# If no exception, safe to trigger
execution = await client.trigger_execution(
    flow_id="ingestion",
    inputs={...},
    labels={"dataset_urn": "urn:li:dataset:..."},
)
```

### How it works

1. Trigger sets a label (`dataset_urn=<value>`) on the execution
2. Before triggering, `check_no_duplicate` searches for `RUNNING` executions with the same label
3. If found, raises `ConflictError` with `INGESTION_RUNNING` → API returns `409 Conflict`

### Alternative: Kestra-native concurrency

Kestra also supports flow-level concurrency limits in YAML:
```yaml
concurrency:
  limit: 1
  behavior: CANCEL  # or QUEUE, FAIL
```

DataSpoke uses the label-based approach for finer-grained control (per-entity, not per-flow).

---

## Known Pattern C — Deploy Flows on Startup

**Question**: How do I sync flow YAML definitions to Kestra when the app starts?

### Pattern

The flow registry (`src/workflows/kestra/registry.py`) handles deploying flow YAML files from `src/workflows/flows/` to Kestra on application startup.

```python
from src.workflows.kestra.client import KestraClient
from pathlib import Path

async def deploy_all_flows(client: KestraClient):
    flows_dir = Path("src/workflows/flows")
    for yaml_file in sorted(flows_dir.glob("*.yaml")):
        flow_yaml = yaml_file.read_text()
        result = await client.create_or_update_flow(flow_yaml)
        print(f"Deployed {result['id']} (revision {result.get('revision', '?')})")
```

### How `create_or_update_flow` works

1. Parses YAML to extract `namespace` and `id`
2. Tries `PUT /api/v1/flows/{namespace}/{id}` (update)
3. On 404 → falls back to `POST /api/v1/flows` (create)
4. Idempotent — safe to call on every startup

---

## Error Handling

### Kestra HTTP error codes

| Status | Meaning | Action |
|---|---|---|
| `200` | Success | — |
| `404` | Flow/execution not found | Check namespace and ID |
| `409` | Conflict (disabled flow, constraint violation) | Check flow state |
| `422` | Validation error (invalid YAML, bad inputs) | Fix the payload |
| `500` | Internal server error | Check Kestra logs |

### DataSpoke error types (`src/workflows/kestra/errors.py`)

| Error | When raised |
|---|---|
| `KestraExecutionFailedError` | Execution reached `FAILED` state |
| `KestraTimeoutError` | `wait_for_execution` exceeded timeout |

---

## Reference Files

| File | Purpose |
|---|---|
| `ref/github/kestra/openapi.yml` | Complete OpenAPI 3.0 specification |
| `ref/github/kestra/webserver/src/main/java/io/kestra/webserver/controllers/api/` | All REST controller implementations |
| `ref/github/kestra/core/` | Core execution engine, models, state machine |
| `ref/github/kestra/model/` | Domain models (Flow, Execution, Task, Trigger) |
| `src/workflows/kestra/client.py` | DataSpoke KestraClient wrapper |
| `src/workflows/kestra/models.py` | Pydantic models: ExecutionResponse, ExecutionStatus, FlowResponse |
| `src/workflows/kestra/errors.py` | Custom error types |
| `src/workflows/kestra/registry.py` | Flow deployment registry |
| `src/workflows/flows/*.yaml` | DataSpoke flow definitions |
| `spec/feature/BACKEND.md` §Kestra Workflows | Architecture and conventions |
