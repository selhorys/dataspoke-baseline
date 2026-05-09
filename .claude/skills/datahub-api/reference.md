# DataHub API Skill — Reference

## Reference Lookup Table

| Question type | Where to look |
|---|---|
| "What GraphQL query does X?" | `ref/github/datahub/docs/api/graphql/getting-started.md`<br>`ref/github/datahub/docs/api/graphql/graphql-best-practices.md`<br>`ref/github/datahub/datahub-graphql-core/src/main/resources/*.graphql` |
| "How do I emit / write X via SDK?" | `ref/github/datahub/docs/api/tutorials/<topic>.md`<br>`ref/github/datahub/metadata-ingestion/examples/library/` |
| "What is the URN format for X?" | `ref/github/datahub/docs/api/tutorials/datasets.md`<br>`ref/github/datahub/metadata-ingestion/src/datahub/emitter/mce_builder.py` |
| "How does the emitter / client work?" | `ref/github/datahub/metadata-ingestion/src/datahub/emitter/rest_emitter.py`<br>`ref/github/datahub/metadata-ingestion/src/datahub/ingestion/graph/client.py` |
| "What is a MetadataChangeProposal?" | `ref/github/datahub/metadata-ingestion/src/datahub/emitter/mcp_builder.py`<br>`ref/github/datahub/metadata-ingestion/src/datahub/emitter/mcp.py` |
| "What REST endpoints exist?" | `ref/github/datahub/docs/api/openapi/` (static docs) OR live Swagger |
| "What aspects does entity X have?" | `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/` |
| "How to auth / token management?" | `ref/github/datahub/docs/api/graphql/token-management.md` |

### GraphQL Schema Files

The complete GraphQL schema is split into domain files under:
```
ref/github/datahub/datahub-graphql-core/src/main/resources/
  entity.graphql       — all entity types (Dataset, Dashboard, Chart, ...)
  search.graphql       — search and scroll queries
  lineage.graphql      — lineage queries and mutations
  ingestion.graphql    — ingestion sources and runs
  auth.graphql         — token management
  common.graphql       — shared types (Tag, GlossaryTerm, Owner, ...)
```

### SDK Examples

`ref/github/datahub/metadata-ingestion/examples/library/` contains runnable Python scripts, one per operation type. Naming convention: `<entity>_<operation>.py`. Always read the matching example before writing SDK code.

---

## Decision Protocol

When answering "should I use DataHub or build my own?", follow this protocol:

```
1. Identify the metadata type: is it entity metadata, annotations,
   timeseries events, or business classification?

2. Check DataHub's native aspects first:
   - Entity metadata       → DatasetProperties, DataPlatformInstance
   - Typed custom attributes → Structured Properties
   - Quality / operational events → AssertionRunEvent (timeseries)
   - Business vocabulary   → Glossary Terms (only this case)

3. Only propose a custom structure if no native aspect covers the need
   after checking metadata-models/ and docs/api/tutorials/.
```

Preference order for custom attributes:
1. **Structured Properties** (typed, constrained, first-class support since v1.3)
2. **Glossary Terms** (only for business vocabulary — never for technical metadata or events)
3. **Fully custom aspects** (last resort, requires model changes)

---

## Known Pattern A — Dataset Storage / DB Platform Property

**Question**: How do I record which storage or DB platform a dataset uses?

### Native Model

Platform identity is built into the dataset URN:
```
urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<env>)
```

DataHub ships with dozens of recognized platform names (`iceberg`, `parquet`, `postgresql`, `mysql`, `snowflake`, `hive`, ...) and stores extended metadata in the `dataPlatformInfo` aspect.

For named clusters/instances, use the `dataPlatformInstance` aspect:
```
metadata-models/.../common/DataPlatformInstance.pdl
  platform: Urn          → "urn:li:dataPlatform:postgresql"
  instance: optional Urn → "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgresql,prod-us-east-1)"
```

### When the URN Isn't Enough

For secondary platform attributes (e.g., primary platform is `s3` but table format is `iceberg`), use **Structured Properties**:

```python
property_def = {
    "qualifiedName": "io.dataspoke.storage.tableFormat",
    "displayName": "Table Format",
    "valueType": "urn:li:dataType:datahub.string",
    "cardinality": "SINGLE",
    "entityTypes": ["urn:li:entityType:datahub.dataset"],
    "allowedValues": [
        {"value": {"string": "iceberg"}},
        {"value": {"string": "parquet"}},
        {"value": {"string": "delta"}},
        {"value": {"string": "orc"}},
    ],
}
```

### Decision

| Scenario | Recommendation |
|---|---|
| Platform IS the primary storage technology | Encode in dataset URN (`make_dataset_urn(platform="iceberg", ...)`) |
| Named cluster or instance of a platform | Use `dataPlatformInstance` aspect |
| Secondary format attribute alongside a primary platform | Use **Structured Properties** |
| Any of the above | **Never use Glossary Terms** — they are for business vocabulary, not technical platform metadata |

### Reference Files

| File | Purpose |
|---|---|
| `li-utils/.../common/DataPlatformUrn.pdl` | URN format for data platforms |
| `metadata-models/.../common/DataPlatformInstance.pdl` | `dataPlatformInstance` aspect fields |
| `metadata-models/.../structured/StructuredPropertyDefinition.pdl` | Structured property schema |
| `metadata-models/.../structured/StructuredPropertyValueAssignment.pdl` | Attaching values to entities |
| `metadata-ingestion/src/datahub/emitter/mce_builder.py` | `make_dataset_urn()` |
| `docs/api/tutorials/structured-properties.md` | Tutorial |

---

## Known Pattern B — Custom Quality Checker Results with External Experiment Link

**Question**: How do I write a custom data quality checker's result as an event when the result includes a link to an MLflow experiment run?

### Native Model

DataHub models quality checks as **Assertion** entities with **AssertionRunEvent** timeseries aspects. For fully custom checkers, use `CustomAssertionInfo`.

**AssertionInfo** (`assertionInfo` aspect):
```
metadata-models/.../assertion/AssertionInfo.pdl
  type: CUSTOM
  customAssertion: CustomAssertionInfo
    .type: string          → your category, e.g. "mlflow-quality-check"
    .entity: Urn           → the dataset URN being monitored
    .field: optional Urn   → if checking a specific column
    .logic: optional string → description of the check logic
```

**AssertionRunEvent** (`assertionRunEvent` timeseries aspect):
```
metadata-models/.../assertion/AssertionRunEvent.pdl
  timestampMillis: long
  runId: string              → your run identifier (e.g. mlflow run ID)
  asserteeUrn: Urn           → dataset URN
  status: COMPLETE
  result: AssertionResult
    .type: SUCCESS | FAILURE | ERROR
    .externalUrl: string     → ← MLflow experiment/run URL goes here
    .nativeResults: map[string, string]  → arbitrary key-value metrics
```

### Python SDK

```python
# Step 1: Create the assertion (idempotent)
assertion_urn = graph.upsert_custom_assertion(
    urn=None,
    entity_urn="urn:li:dataset:...",
    type="mlflow-quality-check",
    description="Monthly feature drift check",
    platform_name="dataspoke-quality",
    external_url="https://mlflow.company.com/experiments/42",
)

# Step 2: Report each run result
graph.report_assertion_result(
    urn=assertion_urn,
    timestamp_millis=round(time.time() * 1000),
    type="SUCCESS",
    external_url="https://mlflow.company.com/experiments/42/runs/abc123",
    properties=[
        {"key": "precision", "value": "0.95"},
        {"key": "recall",    "value": "0.92"},
        {"key": "f1_score",  "value": "0.935"},
    ],
)
```

### Decision

| Approach | Verdict |
|---|---|
| `CustomAssertionInfo` + `AssertionRunEvent.externalUrl` | **Correct** — native DataHub, queryable timeseries, designed for this |
| `AssertionRunEvent.nativeResults` for metrics | **Correct** — map[string,string] for supporting key-value data |
| Custom Glossary structure | **Wrong** — static annotations, no timeseries, no run status |
| `DataQualityContract` | Not relevant here — contracts define SLAs, not per-run results |

### Reference Files

| File | Purpose |
|---|---|
| `metadata-models/.../assertion/AssertionInfo.pdl` | Assertion entity aspect |
| `metadata-models/.../assertion/CustomAssertionInfo.pdl` | Custom assertion subtype |
| `metadata-models/.../assertion/AssertionRunEvent.pdl` | Timeseries run event (has `externalUrl`) |
| `metadata-models/.../assertion/AssertionResult.pdl` | Result with `externalUrl` + `nativeResults` |
| `metadata-ingestion/src/datahub/ingestion/graph/client.py` | `upsert_custom_assertion()`, `report_assertion_result()` |
| `smoke-test/tests/assertions/custom_assertions_test.py` | End-to-end usage example |
| `docs/api/tutorials/custom-assertions.md` | Tutorial |

---

## Known Pattern C — Kafka Event Listener Async Commits (datahub-actions)

**Question**: How do I raise throughput for a high-volume MCL/PE event listener without risking per-event commit overhead?

### Scope

This applies to the **datahub-actions** event listener service (the process that consumes `MetadataChangeLog_v1` / `PlatformEvent_v1` and runs actions), **not** the `metadata-ingestion` SDK emission path. DataSpoke sidecars that run their own actions pods benefit; direct callers of `DataHubRestEmitter.emit_mcps()` do not.

### Config

`datahub-actions` ships a `KafkaEventSource` with two opt-in fields in `KafkaEventSourceConfig` (actions recipe YAML):

```yaml
source:
  type: kafka
  config:
    async_commit_enabled: true      # default: false
    async_commit_interval: 10000    # ms; default: 10000 (10s)
    commit_retry_count: 5
    commit_retry_backoff: 10.0
```

When enabled, the confluent-kafka consumer flips to librdkafka's background auto-commit mode:
- `enable.auto.offset.store = False` → handler calls `consumer.store_offsets()` after processing
- `enable.auto.commit = True` + `auto.commit.interval.ms = async_commit_interval` → broker commit happens on a background thread

### Tradeoff

| Aspect | Sync (default) | Async (`async_commit_enabled: true`) |
|---|---|---|
| Per-event broker round-trip | Yes — `_commit_offsets` with retry | No — `store_offsets` then batched background commit |
| Throughput | Limited by broker RTT | Much higher for high-volume topics |
| Failure semantics | At-least-once (commit after process) | At-least-once with a wider replay window (up to `async_commit_interval` of processed events may be redelivered on crash) |

Action handlers must be **idempotent** under async mode — they already should be, but the replay window widens.

### Reference Files

| File | Purpose |
|---|---|
| `datahub-actions/src/datahub_actions/plugin/source/kafka/kafka_event_source.py` | `KafkaEventSourceConfig`, `KafkaEventSource`, `ack()`, `_store_offsets()`, `_commit_offsets()` |

---

## Known Pattern D — DataSpoke Validation Emission (Passive Result Store)

**Question**: How does DataSpoke push pipeline-emitted validation results into DataHub so they appear in the native assertion UI?

### Scope

DataSpoke's Validation feature is a **passive result store**: data pipelines run the validation logic and POST results to DataSpoke; DataSpoke stores the configuration (description + declared variable names) plus the result timeseries in PostgreSQL and emits the matching DataHub aspects on the pipeline's behalf. Full contract: `spec/feature/VALIDATION.md`.

DataSpoke writes three aspects on a single `assertion` entity per dataset:

- `assertionInfo` (versioned) — emitted on `PUT/PATCH /attr/validation/conf`. Always `type = CUSTOM`.
- `assertionRunEvent` (timeseries) — emitted on each `POST /attr/validation/result`. `timestampMillis` = the pipeline-supplied `data_time`.
- `status` (versioned) — emitted on `DELETE /attr/validation/conf` (`removed = true`) and on `PUT`-after-`DELETE` resurrection (`removed = false`).

### Mandatory conventions

| Concern | Requirement |
|---|---|
| URN | `urn:li:assertion:<datahub_guid({"platform": "dataspoke-validation", "entity": dataset_urn})>`. Recomputable from `dataset_urn` alone — one validation slot per dataset. PUT/PATCH is idempotent; the soft-delete / resurrection cycle reuses the same URN. |
| `assertionInfo.type` | Always `CUSTOM`. The `customAssertion` sub-aspect carries the DataSpoke schema declaration. |
| `customAssertion` | `type = "DATASPOKE_VALIDATION"` (Quality-tab categorization label), `entity = <dataset_urn>`, `logic = "<comma-joined declared variable names>"`. Variable names follow the regex `[a-z][a-z0-9_]{0,99}` so `,` is unambiguous on read. |
| `assertionInfo.source` | `AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL)`. Marks "the data pipeline runs this, DataHub stores results"; `NATIVE` is reserved for the DataHub Cloud runner. |
| `assertionInfo.lastUpdated` | `AuditStampClass(time=ts_ms, actor=<urn of dataspoke service user>)`. |
| `assertionRunEvent.timestampMillis` | The pipeline-supplied `data_time` (epoch ms, UTC) — typically the partition timestamp. Aligns DataHub's chart axis with "when the data is for". |
| `assertionRunEvent.runId` | `uuid4()` per accepted result POST (plain string, no URN). |
| `result.type` | `SUCCESS` if `score == 1.0`, `FAILURE` otherwise. The raw `score` is preserved in `actualAggValue` and `nativeResults["score"]` for partial-success semantics later. |
| `result.actualAggValue` | The `score` (single float in `[0.0, 1.0]`); DataHub plots it as a timeseries chart. |
| `result.nativeResults` | `map[string,string]` of variable name → `repr(float)` (round-trip safe under IEEE 754). `score` is included as one of the entries. |
| `result.partitionSpec` | Omit (DataHub default). Partition identity already lives in `timestampMillis = data_time`; `PartitionType` is `@deprecated` in `PartitionSpec.pdl`. |
| `runtimeContext.ingestion_time` | Server-side `now()` epoch ms. Audit trail for when DataSpoke accepted the result. |

### Append-only timeseries

Multiple POSTs sharing the same `data_time` become **distinct** `assertionRunEvent` rows in DataHub's timeseries store. The `GET /attr/validation/result` endpoint collapses duplicates with last-write-wins per distinct `data_time`. This matches DataHub's timeseries aspect being fundamentally append-only; forcing replace requires `messageId` workarounds.

### Multi-rule scope-out

DataSpoke deliberately exposes **one** validation slot per dataset (description + flat bag of named scalar variables + single pass/fail score). Teams that need multiple distinct checks per dataset (separate freshness / volume / field assertions, per-column validators, multi-team ownership) use **DataHub's native assertion APIs** directly — `upsertCustomAssertion` (GraphQL) or the REST assertion endpoints. DataSpoke is the opinionated single-rule shortcut for the 80% case.

### Reference Files

| File | Purpose |
|---|---|
| `metadata-models/.../assertion/AssertionInfo.pdl` | Top-level aspect — `type` + sub-aspect union |
| `metadata-models/.../assertion/CustomAssertionInfo.pdl` | The sub-aspect DataSpoke fills (`type`, `entity`, `logic`) |
| `metadata-models/.../assertion/AssertionRunEvent.pdl` | Run-event aspect with `result` + timestamp |
| `metadata-models/.../assertion/AssertionResult.pdl` | `result.type` enum + `actualAggValue` + `nativeResults` |
| `metadata-ingestion/src/datahub/emitter/mcp_builder.py` (`datahub_guid`) | URN GUID derivation |
| `spec/feature/VALIDATION.md` | DataSpoke-side contract (philosophy, scope, API surface, aspect mapping) |

### Anti-patterns

| Pattern | Why it's wrong |
|---|---|
| Picking `assertionInfo.type` from `FRESHNESS / VOLUME / FIELD / DATA_SCHEMA / SQL` | Those types imply rule grammar that DataSpoke does not maintain. Always use `CUSTOM` with `customAssertion`. |
| `source.type = NATIVE` | Tells DataHub it owns execution; the Cloud runner is the only thing that may claim NATIVE. |
| `timestampMillis = now()` (server ingest time) | Misaligns DataHub's chart axis with the user mental model. Use the pipeline-supplied `data_time`; preserve `now()` separately in `runtimeContext.ingestion_time`. |
| Best-effort error swallowing on `emit_mcp` for `assertionInfo` / `status` (config emission) | Hides integration breakage. Config emission must propagate failures as `502/503` so users learn DataHub is unhealthy at config-save time. |
| Best-effort silence on `emit_mcp` for `assertionRunEvent` | The row stays in `validation_results` (the historical-baseline cache is local), but the caller must receive `502/503` so the pipeline can decide whether to retry. |
| Out-of-band `status.removed = true` set in DataHub UI to hide a DataSpoke assertion | Reverted on the next config `PUT/PATCH`. To durably hide, call `DELETE /attr/validation/conf`. |

---

## Note — "Smart DataHub" / AI Auto-Documentation Is Not in OSS

AI-driven auto-documentation and intelligent SQL-parsing features (sometimes marketed as "Smart DataHub") are **DataHub Cloud / Acryl-hosted** offerings and are **not present in the OSS `ref/github/datahub/` v1.5.0.2 source**. There is no public SDK, GraphQL, or REST surface in OSS for:

- LLM-generated dataset/column descriptions
- Automatic glossary term suggestions
- AI-assisted lineage repair

In OSS the `Documents` entity (`metadata-service/.../DocumentService.java`) is a **manual-authoring** wiki/knowledge-base feature and does not produce generated content.

If a DataSpoke task requires these capabilities, route it through DataHub Cloud APIs (outside this skill's scope) or implement the generation client-side and write the resulting text via the regular `description` / `documentation` / structured-property aspects.
