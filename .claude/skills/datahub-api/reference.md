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

## Known Pattern D — DataSpoke Validation Authoring (Custom + Typed Assertions)

**Question**: How do I push DataSpoke-run validation results into DataHub so they appear in the native assertion UI alongside any DataHub-emitted assertions?

### Scope

OSS DataHub does **not** run assertions natively — the runner is DataHub Cloud only. DataSpoke owns execution: it computes the metric, decides pass / fail, and emits both the assertion definition (`assertionInfo`) and each run outcome (`assertionRunEvent`) directly via MCPs.

**Open Assertions YAML is the binding schema for DataSpoke's on-disk rule grammar.** DataSpoke conforms to OAS field names (`type`, `condition`, `last_modified_field`, `filter`, `failure_threshold`, `schedule`); DataSpoke-original additions (`rule_id`, `source`, `partition`, `order`, `ml_validation`) are supersets, not replacements. Note: the OSS `datahub assertions` CLI / compiler is deprecated in v1.5 (`metadata-ingestion/src/datahub/cli/specific/assertions_cli.py` prints a runtime deprecation warning and is slated for removal) — DataSpoke writes through the SDK / MCP instead, but the YAML schema OAS defines remains the contract.

### Mandatory conventions

| Concern | Requirement |
|---|---|
| URN | `urn:li:assertion:<datahub_guid({"entity": dataset_urn, "rule": rule_id})>` — deterministic across re-runs so re-emit is idempotent |
| `assertionInfo.type` | One of `FRESHNESS` / `VOLUME` / `FIELD` / `DATA_SCHEMA` / `SQL` / `CUSTOM`. Note: the schema type is **`DATA_SCHEMA`**, not `SCHEMA` (PDL reserved-word workaround — see `AssertionType.pdl`) |
| Typed sub-aspect | **Required**. Setting `type=...` alone leaves the assertion blank in DataHub's UI and renders empty `assertionInfo.{freshness,volume,…}Assertion` over GraphQL. Populate the matching sub-aspect — see table below |
| `assertionInfo.source` | `AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL)`. EXTERNAL marks "DataSpoke runs this, DataHub stores results" — DataHub will not attempt to execute it |
| `assertionInfo.lastUpdated` | `AuditStampClass(time=ts_ms, actor=<urn of dataspoke service user>)` |
| `assertionRunEvent.runId` | A workflow execution ID (Airflow run UUID is fine — plain string, no URN). All rules in one validation run **must share the same runId** for timeline grouping in DataHub |
| `assertionRunEvent.partitionSpec` | `PartitionTypeClass.PARTITION` with serialized partition dict for partitioned runs; `FULL_TABLE` otherwise |
| `assertionRunEvent.result.nativeResults` | `map[string,string]` of computed metric → string value. Numeric values must be stringified; `None` becomes `"null"` |

### Typed sub-aspects (one per `assertionInfo.type`)

| `type` | Required field on `AssertionInfoClass` | SDK class | Key fields |
|---|---|---|---|
| `FRESHNESS` | `freshnessAssertion` | `FreshnessAssertionInfoClass` | `type=DATASET_CHANGE`, `entityUrn`, `schedule` (`FreshnessAssertionScheduleClass`), optional `filter` |
| `VOLUME` | `volumeAssertion` | `VolumeAssertionInfoClass` | `type=ROW_COUNT_TOTAL`/`ROW_COUNT_CHANGE`/`INCREMENTING_SEGMENT_*`, `entityUrn`, `rowCountTotal` or `rowCountChange` |
| `FIELD` | `fieldAssertion` | `FieldAssertionInfoClass` | `type=FIELD_VALUES` or `FIELD_METRIC`, `entityUrn`, `fieldValuesAssertion` or `fieldMetricAssertion` |
| `DATA_SCHEMA` | `schemaAssertion` | `SchemaAssertionInfoClass` | `entityUrn`, `compatibility=EXACT_MATCH`/`SUPERSET`/`SUBSET`, `schema.fields[]` |
| `SQL` | `sqlAssertion` | `SqlAssertionInfoClass` | `type=METRIC` or `METRIC_CHANGE`, `entityUrn`, `statement`, `operator`, `parameters` |
| `CUSTOM` | `customAssertion` | `CustomAssertionInfoClass` | `type=<your_subtype>` (e.g. `"sql_timeseries"`), `entity=<dataset_urn>`, optional `field`, optional `logic` |

### Reference Files

| File | Purpose |
|---|---|
| `metadata-models/.../assertion/AssertionInfo.pdl` | Top-level aspect — `type` + sub-aspect union |
| `metadata-models/.../assertion/{Freshness,Volume,Field,Schema,Sql,Custom}AssertionInfo.pdl` | Typed sub-aspect schemas |
| `metadata-models/.../assertion/AssertionRunEvent.pdl` | Run-event aspect with `result` + `partitionSpec` |
| `metadata-ingestion/src/datahub/emitter/mcp_builder.py` (`datahub_guid`) | URN GUID derivation |
| `docs/assertions/open-assertions-spec.md` | YAML schema reference (CLI is deprecated; do not call) |

### Anti-patterns

| Pattern | Why it's wrong |
|---|---|
| `assertionInfo.type=FRESHNESS` with `freshnessAssertion=None` | Assertion has no detail in UI; GraphQL `assertionInfo.freshnessAssertion` returns null. Equivalent to a write-only stub |
| `source.type=NATIVE` for DataSpoke-run checks | Tells DataHub it owns execution; the Cloud runner is the only thing that should claim NATIVE |
| Best-effort error swallowing on `emit_mcp` for assertion **definitions** | Hides integration breakage. Definition emission must propagate failures (502/503 on the upsert API) so users learn DataHub is unhealthy at config-save time, not weeks later when they wonder where the assertions went |
| Best-effort silence on `emit_mcp` for assertion **run events** | Should surface as `ERROR` on the affected rule in the run summary, not buried in logs |
| Calling `datahub assertions upsert -f …` from any scaffolded code | Deprecated CLI path; will be removed |
| `runId = uuid.uuid4()` regenerated per rule emit within a single run | Breaks "all rules of one run share a runId" grouping in DataHub assertion timeline |

---

## Note — "Smart DataHub" / AI Auto-Documentation Is Not in OSS

AI-driven auto-documentation and intelligent SQL-parsing features (sometimes marketed as "Smart DataHub") are **DataHub Cloud / Acryl-hosted** offerings and are **not present in the OSS `ref/github/datahub/` v1.5.0.2 source**. There is no public SDK, GraphQL, or REST surface in OSS for:

- LLM-generated dataset/column descriptions
- Automatic glossary term suggestions
- AI-assisted lineage repair

In OSS the `Documents` entity (`metadata-service/.../DocumentService.java`) is a **manual-authoring** wiki/knowledge-base feature and does not produce generated content.

If a DataSpoke task requires these capabilities, route it through DataHub Cloud APIs (outside this skill's scope) or implement the generation client-side and write the resulting text via the regular `description` / `documentation` / structured-property aspects.
