---
name: datahub-api
description: Reference and coding guide for DataHub integration in backend development. Use when implementing or debugging code that touches DataHub — entities (datasets, dashboards, charts), aspects (ownership, tags, domains, glossary terms, structured properties), lineage, assertions, URNs, ingestion/emission, GraphQL, REST, or the acryl-datahub Python SDK. Also answers data-modeling questions and helps choose native DataHub features vs custom extensions. Trigger this skill whenever a task involves DataHub metadata, lineage, ingestion, or any DataHub API surface, even without explicit mention of "API".
argument-hint: <task>
allowed-tools: Read, Grep, Glob, Bash(python3 *), Bash(pip3 *), Bash(curl *)
---

## Phase 1 — Understand the Task & Select Mode

**Step 0 — Check reference materials:**

```bash
[ -d "ref/github/datahub" ] && echo "ref present" || echo "ref MISSING"
```

If `ref/github/datahub` is **missing**, stop immediately and tell the user:

> `ref/github/datahub` is not present. Run `/ref-setup` to download
> the DataHub v1.4.0 source (shallow clone, ~1-3 min). Retry this task after it completes.

**Step 1 — Select operating mode:**

| If the task looks like... | Mode |
|---|---|
| "Does DataHub have X?", "What's the best way to model Y?", "Should I use Z or build custom?" | **Q&A** — research and answer, no execution |
| "Write a Python module to do X", "Build a utility for Y", "Test whether Z works" | **Code Writer** — write, execute, verify |
| Mixed ("explain and write the code") | Q&A first, then Code Writer |

**Step 2 — Identify the API layer** (both modes):

| Task | API to use |
|---|---|
| Search, entity reads, lineage queries (UI-facing) | GraphQL |
| Emit lineage, custom aspects, bulk ingestion | Python SDK (`DatahubRestEmitter`) |
| Token management, soft delete, batch operations | GraphQL mutation |
| Advanced admin, index management | OpenAPI REST |

**Step 3 — Identify entity type** (both modes): dataset, dashboard, chart, tag, lineage, assertion, structured property, etc.

After Phase 1:
- **Q&A** routes to Phase 2 → answer → done.
- **Code Writer** continues through Phases 2–5.

---

## Phase 2 — Reference Navigation

The full DataHub source (v1.4.0) lives in `ref/github/datahub/`. Use the reference lookup table in [reference.md](reference.md) §Reference Lookup Table to find the right files.

**For Q&A mode**: read the relevant reference files, then write a clear answer with:
- Exact field names, aspect names, URN formats
- Source file citations from `ref/`
- Recommendation and rationale
- If no native solution exists: propose the minimal custom approach (Structured Properties before Glossary before fully custom aspects)

Follow the **Decision Protocol** in [reference.md](reference.md) §Decision Protocol to determine whether DataHub supports the concept natively.

**For Code Writer mode**: continue to Phase 3.

---

## Phase 3 — Check Prerequisites

Run these checks before executing any code:

```bash
# 1. Check acryl-datahub is installed
python3 -c "import datahub; print('acryl-datahub', datahub.__version__)" 2>/dev/null \
  || pip3 install acryl-datahub --quiet

# 2. Check GMS is reachable (port-forward must be running)
curl -s http://localhost:9004/config \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('GMS ok, version:', d['versions']['acryldata/datahub']['version'])" \
  || echo "ERROR: GMS not reachable. Run: dev_env/datahub-port-forward.sh"

# 3. Check token
if [ -z "${DATASPOKE_DATAHUB_TOKEN:-}" ]; then
  echo "DATASPOKE_DATAHUB_TOKEN not set — generate via: http://localhost:9002 → Settings → Access Tokens"
fi
```

If any prerequisite fails, stop and inform the user with the fix instructions.

---

## Phase 4 — Explore Live API Documentation (optional)

Only use this if the static `ref/` files don't answer the question.

| Resource | URL | Notes |
|---|---|---|
| Swagger UI (REST/OpenAPI) | `http://localhost:9004/openapi/swagger-ui/index.html` | Set Bearer token in Authorize dialog |
| Raw OpenAPI spec | `curl -s -H "Authorization: Bearer $DATASPOKE_DATAHUB_TOKEN" http://localhost:9004/openapi/v3/api-docs` | JSON, pipe to `python3 -m json.tool` |
| GraphiQL | `http://localhost:9002/api/graphiql` | Browser only, uses session cookie |
| Unauthenticated health | `http://localhost:9004/config`, `http://localhost:9004/health` | No token needed |

---

## Phase 5 — Write Code, Execute, Verify

### 5.1 SDK Setup Pattern

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
import os

graph = DataHubGraph(DatahubClientConfig(
    server="http://localhost:9004",
    token=os.environ.get("DATASPOKE_DATAHUB_TOKEN", ""),
))
```

For bulk emission:
```python
from datahub.emitter.rest_emitter import DatahubRestEmitter
import os

emitter = DatahubRestEmitter(
    "http://localhost:9004",
    token=os.environ.get("DATASPOKE_DATAHUB_TOKEN", ""),
)
```

### 5.2 Execution Loop

For each operation:

1. **Read the relevant tutorial** from `ref/github/datahub/docs/api/tutorials/`
2. **Find the matching SDK example** in `ref/github/datahub/metadata-ingestion/examples/library/`
3. **Write the code** to a temp file
4. **Execute**: `python3 /tmp/datahub_test_script.py`
5. **Verify** by reading back the written entity:

```python
# Read back to confirm the write succeeded
aspect = graph.get_aspect(entity_urn=urn, aspect_type=AspectClass)
assert aspect is not None, "Entity not found after write"
print("Verified:", aspect)
```

6. **Iterate** on failure — diagnose error, fix, re-run. Stop after 3 consecutive failures and report the blocker.
7. **Report**: final code + what was written + verification output

### 5.3 Common URN Builders

```python
from datahub.emitter.mce_builder import (
    make_dataset_urn,        # urn:li:dataset:(urn:li:dataPlatform:<p>,<name>,<env>)
    make_data_platform_urn,  # urn:li:dataPlatform:<name>
    make_schema_field_urn,   # urn:li:schemaField:(<dataset_urn>,<field>)
    make_tag_urn,            # urn:li:tag:<name>
    make_user_urn,           # urn:li:corpuser:<username>
    make_group_urn,          # urn:li:corpGroup:<name>
    make_domain_urn,         # urn:li:domain:<uuid>
)
```

### 5.4 Emitter Behavior

- **Retry**: 3 attempts on `[429, 500, 502, 503, 504]` with exponential backoff
- **Payload limit**: ~15 MB per request; split large batches
- **Rate limiting**: exponential back-off on 429 with 2x multiplier

---

## Constraints

1. **Never use `kubectl exec`** to interact with DataHub — it bypasses the API surface and doesn't reflect production behavior.
2. **Never run ad-hoc `kubectl port-forward`** — if a new port is needed, add it to `dev_env/datahub-port-forward.sh` and propose the change.
3. **Always read the matching tutorial/example first** before writing API code.
4. **Prefer static `ref/` lookup over live API exploration** for speed — only fall back to Swagger/GraphiQL when the static ref is ambiguous.

---

See [reference.md](reference.md) for the reference lookup table, decision protocol, and known patterns.
