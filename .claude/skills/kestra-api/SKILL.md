---
name: kestra-api
description: Reference and coding guide for Kestra REST API integration in workflow development. Use when implementing or debugging code that touches Kestra — flow CRUD, execution triggering/polling, logs, KV store, triggers, namespaces, or the KestraClient wrapper. Also answers questions about flow YAML structure, task types, retry/concurrency configuration, and choosing the right API endpoint. Trigger this skill whenever a task involves Kestra workflow orchestration, flow management, execution lifecycle, or any Kestra API surface.
argument-hint: <task>
allowed-tools: Read, Grep, Glob, Bash(curl *), Bash(ls *), Bash(python3 *)
---

## Phase 1 — Understand the Task & Select Mode

**Step 0 — Check reference materials:**

```bash
[ -d "ref/github/kestra" ] && echo "ref present" || echo "ref MISSING"
```

If `ref/github/kestra` is **missing**, stop immediately and tell the user:

> `ref/github/kestra` is not present. Run `/ref-setup` and select **kestra** to download
> the Kestra source (shallow clone, ~1-3 min). Retry this task after it completes.

**Step 1 — Select operating mode:**

| If the task looks like... | Mode |
|---|---|
| "How do I trigger a flow?", "What endpoint lists executions?", "How does the KV store work?" | **Q&A** — research and answer, no execution |
| "Write a Kestra flow YAML", "Add a method to KestraClient", "Build a utility to sync flows" | **Code Writer** — write, execute, verify |
| Mixed ("explain and write the code") | Q&A first, then Code Writer |

**Step 2 — Identify the API domain:**

| Task | API group |
|---|---|
| Create, update, delete, search, validate flows | Flows API |
| Trigger executions, poll status, kill/restart, labels | Executions API |
| Read execution logs, stream logs | Logs API |
| Namespace-scoped key-value storage | KV Store API |
| Manage schedule/polling triggers, backfill | Triggers API |
| Check Kestra health, list plugins | Misc / Plugins API |

**Step 3 — Identify whether this is about the Kestra REST API itself or the DataSpoke KestraClient wrapper:**

| Context | Where to look |
|---|---|
| Raw Kestra REST API | `ref/github/kestra/openapi.yml`, controller source in `ref/github/kestra/webserver/` |
| DataSpoke KestraClient | `src/workflows/kestra/client.py`, `src/workflows/kestra/models.py`, `src/workflows/kestra/errors.py` |
| Flow YAML definitions | `src/workflows/flows/*.yaml` |
| Activity endpoints (called by Kestra) | `src/workflows/activities/`, `spec/feature/BACKEND.md` |

After Phase 1:
- **Q&A** routes to Phase 2 → answer → done.
- **Code Writer** continues through Phases 2–5.

---

## Phase 2 — Reference Navigation

The full Kestra source lives in `ref/github/kestra/`. Use the reference lookup table in [reference.md](reference.md) §Reference Lookup Table to find the right files.

**For Q&A mode**: read the relevant reference files, then write a clear answer with:
- Exact endpoint paths, HTTP methods, parameters, request/response formats
- Source file citations from `ref/` or `src/`
- Recommendation and rationale
- If a DataSpoke wrapper exists for the operation: point to it

**For Code Writer mode**: continue to Phase 3.

---

## Phase 3 — Check Prerequisites

Run these checks before executing any code:

```bash
# 1. Check Kestra is reachable (port-forward must be running)
curl -s http://localhost:8080/api/v1/configs \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Kestra ok')" \
  || echo "ERROR: Kestra not reachable. Check port-forward or dev-env status."

# 2. Check KestraClient dependencies
python3 -c "import httpx; print('httpx', httpx.__version__)" 2>/dev/null \
  || echo "ERROR: httpx not installed"

# 3. Check existing flows in dataspoke namespace
curl -s http://localhost:8080/api/v1/flows/dataspoke \
  | python3 -c "import sys,json; flows=json.load(sys.stdin); print(f'{len(flows)} flows in dataspoke namespace')" \
  2>/dev/null || echo "No flows found or Kestra unreachable"
```

If any prerequisite fails, stop and inform the user with the fix instructions.

---

## Phase 4 — Explore Live API Documentation (optional)

Only use this if the static `ref/` files don't answer the question.

| Resource | URL | Notes |
|---|---|---|
| Kestra UI | `http://localhost:8080` | Full web UI with flow editor, execution viewer |
| OpenAPI spec (static) | `ref/github/kestra/openapi.yml` | Complete API schema |
| Health check | `curl http://localhost:8080/api/v1/configs` | No auth needed in dev |

---

## Phase 5 — Write Code, Execute, Verify

### 5.1 KestraClient Setup (DataSpoke pattern)

```python
from src.workflows.kestra.client import KestraClient

client = KestraClient(
    base_url="http://localhost:8080",
    namespace="dataspoke",
)
```

For direct HTTP (without the wrapper):
```python
import httpx

client = httpx.Client(base_url="http://localhost:8080", timeout=30.0)
```

### 5.2 Common Operations

**Trigger a flow:**
```python
execution = await client.trigger_execution(
    flow_id="ingestion",
    inputs={"dataset_urn": "urn:li:dataset:...", "run_id": "abc123"},
    labels={"dataset_urn": "urn:li:dataset:..."},
)
```

**Check for duplicate runs (concurrency guard):**
```python
await client.check_no_duplicate(
    flow_id="ingestion",
    label_key="dataset_urn",
    label_value="urn:li:dataset:...",
    error_code="INGESTION_RUNNING",
)
```

**Deploy a flow from YAML:**
```python
flow_yaml = open("src/workflows/flows/ingestion.yaml").read()
result = await client.create_or_update_flow(flow_yaml)
```

### 5.3 Execution Loop

For each operation:

1. **Read the relevant API reference** from [reference.md](reference.md)
2. **Check the existing KestraClient** in `src/workflows/kestra/client.py` for wrappers
3. **Write the code** — extend KestraClient or use direct HTTP
4. **Execute** and verify the response
5. **Iterate** on failure — diagnose error, fix, re-run. Stop after 3 consecutive failures and report the blocker.
6. **Report**: final code + what was done + verification output

### 5.4 Flow YAML Conventions (DataSpoke)

All DataSpoke flows follow this structure:
```yaml
id: <flow_name>
namespace: dataspoke
description: "<one-line description>"

inputs:
  - id: callback_base_url
    type: STRING
  - id: <entity_key>       # e.g., dataset_urn, metric_id
    type: STRING
  - id: run_id
    type: STRING

tasks:
  - id: <step_name>
    type: io.kestra.plugin.core.http.Request
    uri: "{{ inputs.callback_base_url }}/api/v1/internal/activities/<activity>"
    method: POST
    contentType: application/json
    body: |
      {"key": "{{ inputs.value }}"}
    retry:
      type: constant
      maxAttempt: 3
      interval: PT10S
```

Key conventions:
- Namespace is always `dataspoke`
- All tasks use `io.kestra.plugin.core.http.Request` to call DataSpoke activity endpoints
- `callback_base_url` is passed as input so flows work in both host and in-cluster modes
- Retry: 3 attempts, 10s interval (constant)
- Flow-level concurrency limits prevent duplicate runs per entity

---

## Constraints

1. **Never use `kubectl exec`** to interact with Kestra — use the REST API.
2. **Never run ad-hoc `kubectl port-forward`** for Kestra — if port-forwarding is needed, use the dev-env tooling.
3. **Always read the existing KestraClient** (`src/workflows/kestra/client.py`) before adding new API calls — avoid duplicating existing wrappers.
4. **Prefer static `ref/` lookup over live API exploration** for speed — only fall back to the live UI when the static ref is ambiguous.
5. **Flow YAML must follow DataSpoke conventions** — HTTP Request tasks calling internal activity endpoints, not inline scripts.

---

See [reference.md](reference.md) for the API endpoint reference, response schemas, and known patterns.
