# DataHub Integration Patterns

## Table of Contents

1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Integration Model](#integration-model)
4. [Aspect Reference](#aspect-reference)
5. [Custom Ingestor Guide](#custom-ingestor-guide)
6. [SDK Patterns](#sdk-patterns)
7. [GraphQL Patterns](#graphql-patterns)
8. [Event Subscription](#event-subscription)
9. [Error Handling & Resilience](#error-handling--resilience)
10. [Configuration](#configuration)
11. [Open Questions](#open-questions)

## Overview

DataSpoke is a **sidecar extension** to DataHub. DataHub is the Hub (metadata SSOT); DataSpoke
reads from and writes to DataHub without modifying its core. This document defines the
integration patterns, SDK usage conventions, and aspect catalog that all DataSpoke features must
follow.

**Key principles**:

1. **DataHub is the SSOT (Single Source of Truth) — the most important principle in this
   project.** All metadata that *can* live in DataHub *must* live in DataHub; DataSpoke
   computes on top without duplicating what DataHub already persists. DataSpoke's PostgreSQL
   storage is reserved for state DataHub does not model natively (validation result timeseries
   for ML training data, the ontology graph, metadata-generation proposal history,
   dataset/metric registries) and always references DataHub URNs as the canonical identifier.
2. **Write to editable aspects, never to connector-owned aspects** — DataHub maintains paired
   aspects for descriptions: a non-editable one populated by ingestion connectors and an
   editable counterpart for human/agent edits. DataSpoke writes only to the editable aspect.
   See [Editable vs Non-Editable Description Aspects](#editable-vs-non-editable-description-aspects).
3. **Use DataHub's standard taxonomy when one exists** — when DataHub already defines a
   vocabulary for a domain (assertion types via the
   [Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec),
   ownership types, tag/glossary semantics, lineage edge types), DataSpoke adopts it directly
   rather than inventing a parallel taxonomy. DataSpoke extensions are added as additional
   values (e.g., `assertionInfo.type=CUSTOM` with a DataSpoke `subtype`) rather than
   replacements.
4. **DataSpoke features primarily fill the gaps** — DataSpoke features are designed for use
   cases that cannot be fulfilled by DataHub alone (e.g., deep ingestion, predictive SLA, NL
   search).
5. **DataSpoke API can redefine DataHub functions for convenience** — in some cases DataSpoke
   may re-expose DataHub's basic functions (e.g., dataset registration, metadata browsing)
   through its own API and UI layer. It is a **blended API/UI** that combines DataHub-native
   metadata with DataSpoke-specific metadata in a single call for user convenience. For example,
   when a user needs both basic dataset properties (stored in DataHub) and deep-ingestion
   annotations (stored in DataSpoke's backend) at the same time, a single DataSpoke endpoint can
   aggregate both sources instead of requiring two separate calls. The same applies to creation
   and modification flows — a DataSpoke "create dataset" API could write core metadata to
   DataHub while simultaneously initializing DataSpoke-side records. These redefined features
   are **not the primary focus** of this project; architecture and use-case specs do not cover
   them in detail. However, future versions of DataSpoke may include baseline redefined features
   (e.g., dataset creation, unified metadata views).

All integration code uses the `acryl-datahub` Python SDK — **never the `datahub` CLI**, which is
limited to Python ≤ 3.11 and incompatible with the project's Python 3.13 runtime. For any task
that would traditionally use the CLI (ingestion, metadata emission, dataset operations), write a
Python script using the SDK instead. Three communication channels exist:

```
DataSpoke ──────────────────────────────────── DataHub
    │                                              │
    │  1. Python SDK (read)                        │
    │     DataHubGraph.get_aspect()                │
    │     DataHubGraph.get_timeseries_values()     │
    │     DataHubGraph.execute_graphql()           │
    │                                              │
    │  2. Python SDK (write)                       │
    │     DatahubRestEmitter.emit_mcp()            │
    │                                              │
    │  3. Kafka (events) — optional                │
    │     MetadataChangeEvent / MetadataAuditEvent │
    │     Reserved for future event-driven         │
    │     extensions; baseline UC1–UC5 are         │
    │     schedule-driven via Airflow.             │
    │                                              │
    └──────────────────────────────────────────────┘
```

## Goals & Non-Goals

### Goals

- Define a single, consistent set of SDK patterns for all DataSpoke features
- Catalog every DataHub aspect that DataSpoke reads or writes
- Establish error handling and resilience conventions
- Provide copy-paste-ready code patterns for feature implementers
- Enable possible redefinition of DataHub's basic functions (e.g., dataset registration) in
  DataSpoke's API and UI layer for blended user experiences

### Non-Goals

- Modifying DataHub core (custom aspects may be considered — see
  [Open Questions](#open-questions))
- Defining DataSpoke's own data model (see individual feature specs)
- Covering DataHub admin operations (ingestion recipes, user management)

## Integration Model

### Read vs Write Boundary

Each MANIFESTO feature has a clear integration direction:

| Feature | UC | Direction | Primary Operations |
|---------|----|-----------|-------------------|
| Ingestion Control (`active-custom`) | UC1 | **Write** | Emit dataset metadata (`Status`, `DatasetProperties`, `SchemaMetadata`) plus per-run `DataProcessInstance` aspects. Applies to `mode: active-custom` configs only. |
| Ingestion Control (`passive`) | UC1 | **Read** | The hourly `ingestion-passive-hourly` DAG polls DataHub for `DataProcessInstance` runs of `mode: passive` configs and mirrors status into `event/ingestion`. No aspect writes by DataSpoke. |
| Validation | UC2 | **Read + Write** | Query profiles, operations, lineage; register `assertionInfo`, emit `assertionRunEvent` |
| Ontology Generation | UC3 | **Read + Write** | Read schemas, descriptions, tags, lineage, usage; UC4-approved editable variants (`editableDatasetProperties`, `editableSchemaMetadata`, `dataProductProperties`); and DataHub Query entities (`queryProperties` + `querySubjects`) — both highlighted (`source = MANUAL`) and auto-discovered joins (`source = SYSTEM`, `len(querySubjects) ≥ 2`), capped per dataset. Ontology is modelled as a subject / predicate / object triple set (nodes / edges / triples). On node approval, attach a glossary term derived from the node ID to each member dataset (`glossaryTerms` only — not `globalTags`). On triple approval, create a glossary-term relationship between the subject and object terms using the edge label. |
| Metadata Generation | UC4 | **Read + Write (editable only)** | Read non-editable descriptions and schemas as context; write reviewer-approved table/column descriptions to the *editable* aspect counterparts; create / modify / split / retitle `dataProduct` entities. Tag / glossary-term proposals are future scope and not part of the baseline. |
| Governance | UC5 | **Read** | Aggregate pre-existing metadata (properties, ownership, tags) and DataSpoke validation / ontology state |
| Redefined DataHub Functions *(TBD)* | — | **Read + Write** | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data |

### Client Initialization

Two SDK clients serve different purposes:

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.rest_emitter import DatahubRestEmitter

# Read client — queries aspects and GraphQL
graph = DataHubGraph(DatahubClientConfig(
    server=DATASPOKE_DATAHUB_GMS_URL,
    token=DATASPOKE_DATAHUB_TOKEN,
))

# Write client — emits MCPs
emitter = DatahubRestEmitter(
    gms_server=DATASPOKE_DATAHUB_GMS_URL,
    token=DATASPOKE_DATAHUB_TOKEN,
)
```

Read-only features (Governance) use `DataHubGraph` only. Features that write back (Ingestion
Control `active-custom` mode, Validation, Ontology Generation, Metadata Generation) additionally
use `DatahubRestEmitter`. Redefined DataHub functions would use both clients to blend DataHub
and DataSpoke data in a single API call.

### URN Construction

Always use the builder function — never construct URN strings manually:

```python
from datahub.emitter.mce_builder import make_dataset_urn

# Correct
dataset_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")

# Wrong — do not use string literals
dataset_urn = "urn:li:dataset:(urn:li:dataPlatform:oracle,catalog.title_master,PROD)"
```

## Aspect Reference

### Regular Aspects

Regular aspects represent the current state of an entity. Read via `get_aspect()`, write via
`emit_mcp()`.

| Aspect | SDK Class | Key Fields | REST Read Path | REST Write Path |
|--------|----------|------------|---------------|----------------|
| `datasetProperties` | `DatasetPropertiesClass` | `description`, `customProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | `POST /openapi/v3/entity/dataset` |
| `editableDatasetProperties` | `EditableDatasetPropertiesClass` | `description` | `GET /aspects/{urn}?aspect=editableDatasetProperties` | `POST /openapi/v3/entity/dataset` |
| `schemaMetadata` | `SchemaMetadataClass` | `fields[].fieldPath`, `fields[].nativeDataType`, `fields[].description` | `GET /aspects/{urn}?aspect=schemaMetadata` | `POST /openapi/v3/entity/dataset` |
| `editableSchemaMetadata` | `EditableSchemaMetadataClass` | `editableSchemaFieldInfo[].fieldPath`, `editableSchemaFieldInfo[].description` | `GET /aspects/{urn}?aspect=editableSchemaMetadata` | `POST /openapi/v3/entity/dataset` |
| `ownership` | `OwnershipClass` | `owners[].owner` (URN), `owners[].type` | `GET /aspects/{urn}?aspect=ownership` | `POST /openapi/v3/entity/dataset` |
| `globalTags` | `GlobalTagsClass` | `tags[].tag` (URN) | `GET /aspects/{urn}?aspect=globalTags` | `POST /openapi/v3/entity/dataset` |
| `glossaryTerms` | `GlossaryTermsClass` | `terms[].urn`, `terms[].context` | `GET /aspects/{urn}?aspect=glossaryTerms` | `POST /openapi/v3/entity/dataset` |
| `upstreamLineage` | `UpstreamLineageClass` | `upstreams[].dataset` (URN), `upstreams[].type` | `GET /aspects/{urn}?aspect=upstreamLineage` | `POST /openapi/v3/entity/dataset` |
| `status` | `StatusClass` | `removed` (bool) | `GET /aspects/{urn}?aspect=status` | `POST /openapi/v3/entity/dataset` |
| `deprecation` | `DeprecationClass` | `deprecated` (bool), `note`, `replacement` (URN), `decommissionTime` | `GET /aspects/{urn}?aspect=deprecation` | `POST /openapi/v3/entity/dataset` |

#### Editable vs Non-Editable Description Aspects

DataHub maintains *paired* aspects for descriptions: a non-editable variant populated by
ingestion connectors, and an editable counterpart for human or agent edits. **DataSpoke
writes only to the editable aspect.** Writing to the non-editable variant risks the next
connector run silently overwriting reviewer-approved text.

| Scope | Non-editable (connector-owned, DataSpoke read-only) | Editable (DataSpoke-writable on approval) |
|---|---|---|
| Table description | `datasetProperties.description` | `editableDatasetProperties.description` |
| Column description | `schemaMetadata.fields[].description` | `editableSchemaMetadata.editableSchemaFieldInfo[].description` (keyed by `fieldPath`) |

DataHub renders both editable description fields as Markdown in the UI. Metadata Generation
(UC4) reads the non-editable variants as context for generation but writes proposals only
to the editable variants on reviewer approval.

### Timeseries Aspects

Timeseries aspects store point-in-time measurements. Read via `get_timeseries_values()`. They
are append-only — DataHub retains history.

| Aspect | SDK Class | Key Fields | REST Read Path |
|--------|----------|------------|---------------|
| `datasetProfile` | `DatasetProfileClass` | `rowCount`, `columnCount`, `fieldProfiles`, `sizeInBytes` | `POST /aspects?action=getTimeseriesAspectValues` |
| `operation` | `OperationClass` | `lastUpdatedTimestamp`, `operationType`, `actor` | `POST /aspects?action=getTimeseriesAspectValues` |
| `datasetUsageStatistics` | `DatasetUsageStatisticsClass` | `uniqueUserCount`, `totalSqlQueries`, `topSqlQueries`, `userCounts`, `fieldCounts` | `POST /aspects?action=getTimeseriesAspectValues` |
| `assertionRunEvent` | `AssertionRunEventClass` | `status` (pass/fail), `timestampMillis`, `assertionUrn` | `POST /aspects?action=getTimeseriesAspectValues` |

### Assertion Aspects

Assertions are stored on `assertion` entities (not `dataset` entities). DataSpoke's
Validation feature (UC2) adopts the DataHub
[Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec)
YAML schema as the **binding contract** for its on-disk rule grammar (`type`,
`condition`, `last_modified_field`, `filter`, `failure_threshold`, `schedule`).
DataSpoke extensions (`rule_id`, `source`, `partition`, `order`, `ml_validation`)
are supersets — never replacements. Note: the OSS `datahub assertions` CLI /
compiler is deprecated in v1.5 and is not invoked; DataSpoke writes both
`assertionInfo` and `assertionRunEvent` aspects directly via MCP emission while
keeping the YAML grammar OAS-conformant.

Six `assertionInfo.type` values cover the primary data quality dimensions:

| `assertionInfo.type` | Quality dimension | DataSpoke `rules[].type` | Required typed sub-aspect (SDK class) | Notes |
|---|---|---|---|---|
| `FRESHNESS` | Timeliness | `freshness` | `freshnessAssertion` (`FreshnessAssertionInfoClass`) | Native |
| `VOLUME` | Completeness | `volume` | `volumeAssertion` (`VolumeAssertionInfoClass`) | Native |
| `FIELD` | Accuracy / validity | `field` | `fieldAssertion` (`FieldAssertionInfoClass`) — `FIELD_VALUES` or `FIELD_METRIC` | Native |
| `DATA_SCHEMA` | Conformance | `schema` | `schemaAssertion` (`SchemaAssertionInfoClass`) | PDL constant is `DATA_SCHEMA` (not `SCHEMA` — reserved-word workaround) |
| `SQL` | Custom SQL | `sql` | `sqlAssertion` (`SqlAssertionInfoClass`) | Native |
| `CUSTOM` | Anything else | `custom` | `customAssertion` (`CustomAssertionInfoClass`) — `entity=<dataset_urn>`, `type=<subtype>` | DataSpoke uses `subtype: "sql_timeseries"` for partition-aware SQL with optional ML-based anomaly detection |

Mandatory conventions (see also
[datahub-api skill §Pattern D](../.claude/skills/datahub-api/reference.md#known-pattern-d--dataspoke-validation-authoring-custom--typed-assertions)):

1. **Typed sub-aspect required.** Setting `assertionInfo.type=…` alone with no
   matching sub-aspect leaves the assertion blank in DataHub's UI and returns
   `null` from `assertionInfo.{freshness,volume,…}Assertion` over GraphQL. The
   sub-aspect carries the actual check definition (entity URN, schedule, field,
   compatibility, statement, etc.).
2. **`source.type = EXTERNAL`** on every DataSpoke-emitted `AssertionInfo`.
   Marks "DataSpoke runs this, DataHub stores results"; DataHub will not try to
   execute it. Never use `NATIVE` (reserved for the DataHub Cloud runner).
3. **Deterministic URN.** `urn:li:assertion:<datahub_guid({"entity": dataset_urn, "rule": rule_id})>`,
   so re-emit on config edit is idempotent.
4. **`lastUpdated` audit stamp.** Populate `AssertionInfoClass.lastUpdated` with
   the DataSpoke service-user URN; otherwise the DataHub UI history card shows
   "unknown actor".
5. **Shared `runId` per validation run.** All rules in one run write the same
   `assertionRunEvent.runId` so the DataHub timeline groups them correctly.
6. **Registration timing.** `assertionInfo` is emitted at config upsert
   (`PUT/PATCH /attr/validation/conf`), not lazily on first run — see
   [BACKEND §Validation Service](feature/BACKEND.md#validation-service-srcbackendvalidation).
   A DataHub error during registration surfaces as 502/503; DataHub is the SSOT
   for assertion definitions and config save is coupled to its availability by
   design.
7. **Run-event emission is best-effort but not silent.** Failures of
   `assertionRunEvent` emission produce an `ERROR` result on the affected rule
   (visible in the run summary), never a swallowed log warning.

| Aspect | SDK Class | Entity Type | REST Write Path |
|--------|----------|-------------|----------------|
| `assertionInfo` | `AssertionInfoClass` | `assertion` | `POST /openapi/v3/entity/assertion` |
| `assertionRunEvent` | `AssertionRunEventClass` | `assertion` | `POST /openapi/v3/entity/assertion` |

### Data Product Aspects

Data products group related datasets under a topic-level concept. UC4 (Metadata
Generation) `cross_data.md` proposals may create, modify, split, or retitle
`dataProduct` entities to organize cross-dataset documentation. The generator chooses
a descriptive title (a topic phrase) for new data products — the URN is **not** keyed
off any UC3 node, edge, or triple ID.

| Aspect | SDK Class | Entity Type | Key Fields | REST Write Path |
|--------|----------|-------------|------------|----------------|
| `dataProductProperties` | `DataProductPropertiesClass` | `dataProduct` | `name`, `description` (Markdown), `assets[]` (dataset URNs) | `POST /openapi/v3/entity/dataproduct` |

### Query Aspects

DataHub's "Queries" feature stores SQL queries as standalone `query` entities,
surfaced on the dataset Queries tab in three lists: **highlighted** (`source = MANUAL`,
human-curated via the UI), **recent** (`source = SYSTEM`, auto-discovered by
crawlers), and **popular** (ranked by usage). The `source` field is **immutable
after creation** — DataHub's `UpdateQueryInput` exposes only `name`, `description`,
`statement`, and `subjects`. A SYSTEM query cannot be promoted to MANUAL in place;
the only path is "copy the SQL and create a new MANUAL query".

UC3 reads both sources as ontology evidence, capped per dataset by
`ontogen_config.max_manual_queries_per_dataset` and
`ontogen_config.max_system_queries_per_dataset` (see
[BACKEND §Ontology Generation](feature/BACKEND.md#ontology-generation-service-srcbackendontogen)).

| Aspect | SDK Class | Entity Type | Key Fields | REST Read Path |
|--------|----------|-------------|------------|----------------|
| `queryProperties` | `QueryPropertiesClass` | `query` | `statement.value` (SQL), `name`, `description`, `source` (`MANUAL` \| `SYSTEM`), `lastModified` | `GET /aspects/{urn}?aspect=queryProperties` |
| `querySubjects` | `QuerySubjectsClass` | `query` | `subjects[].entity` (dataset URN; optional schema field URN) | `GET /aspects/{urn}?aspect=querySubjects` |

**Listing per dataset.** Use the `listQueries` GraphQL with an entity-URN filter and
optional `source` filter (same pattern as the DataHub frontend's
`useHighlightedQueries`, `useRecentQueries`). The `count` parameter caps the result
set server-side, matching the per-dataset config caps.

### Aspect Usage by Feature

Which features read (R) or write (W) each aspect. *Ingestion Control writes apply to
`mode: active-custom` configs only (Status, DatasetProperties, SchemaMetadata, plus
per-run DataProcessInstance aspects per the [Custom Ingestor Guide](#custom-ingestor-guide));
`passive` mode reads `DataProcessInstance` run history out-of-band via the
`ingestion-passive-hourly` DAG and writes no aspects.*

| Aspect | Ingestion Control | Validation | Ontology Generation | Metadata Generation | Governance |
|--------|:---:|:---:|:---:|:---:|:---:|
| `datasetProperties` | W | R | R | R (context only) | R |
| `editableDatasetProperties` | — | — | — | W (on approval) | R |
| `schemaMetadata` | W | R | R | R (context only) | R |
| `editableSchemaMetadata` | — | — | — | W (on approval) | R |
| `ownership` | W | — | R | — | R |
| `globalTags` | W | — | R | — *(future scope)* | R |
| `glossaryTerms` | — | — | R + W (term per approved node, attached to member datasets; glossary-term relationships per approved triple) | — *(future scope)* | R |
| `upstreamLineage` | W | R | R | R | R |
| `status` | W | — | — | — | — |
| `deprecation` | — | R | — | — | — |
| `datasetProfile` | — | R | — | — | R |
| `operation` | — | R | — | — | R |
| `datasetUsageStatistics` | — | — | R | R | R |
| `assertionInfo` | — | W | — | — | — |
| `assertionRunEvent` | — | W | — | — | R |
| `dataProductProperties` | — | — | R | W (create / modify / split / retitle on approval) | R |
| `queryProperties` | — | — | R (per-dataset cap, MANUAL + SYSTEM-with-joins) | — | — |
| `querySubjects` | — | — | R (joins-first sort key) | — | — |

## Custom Ingestor Guide

**Audience**: anyone writing an ingestor that emits dataset metadata to DataHub —
in-house custom extractors, external scripts using `acryl-datahub`, or third-party
pipelines. This guide describes the generic DataHub-side contract (aspects to
emit, ordering, identity). DataSpoke-side consumption (how DataSpoke turns these
emissions into `event/ingestion` rows) is documented in
[BACKEND §Custom Ingestor Authoring Contract](feature/BACKEND.md#custom-ingestor-authoring-contract).

**Why this contract exists**: DataHub's `DataProcessInstance` (DPI) is the universal
"this is an ingestion run" entity. Without DPI emission, runs are invisible to any
DataHub consumer that wants per-run drill-down (DataSpoke's hourly poll, the DataHub
UI's run history, downstream lineage tools). Without correct `systemMetadata`,
DataHub's `dataset.lastIngested` field stays `null` and the UI's "Synced X ago from
\<Platform\>" badge does not render.

### DPI emission contract — required aspects per run

In the listed order:

| # | Aspect | Notes |
|---|--------|-------|
| 1 | `DataProcessInstanceProperties` | `name` describes the run (e.g. `"<author>-<platform>-<run_id>"`); `type = BATCH_SCHEDULED` |
| 2a | `DataProcessInstanceRelationships` | `parentTemplate = null`, `upstreamInstances = []` for standalone ingestion runs (DPI-to-DPI lineage; no dataset linkage on this aspect) |
| 2b | `DataProcessInstanceOutput` | `outputs = [<dataset_urn>]` — the dataset(s) this DPI ingested into. This is what makes the DPI surface in DataHub's `dataset(urn).runs` GraphQL query. |
| 3 | `DataProcessInstanceRunEvent` (`status = STARTED`) | Emitted **before** any schema/property aspect work begins on the dataset |
| 4 | `StatusClass`, `DatasetPropertiesClass`, `SchemaMetadataClass`, … | The actual ingested metadata. Emit whatever aspects are appropriate for the source — DPI does not constrain the metadata shape. |
| 5 | `DataProcessInstanceRunEvent` (`status = COMPLETE`) | Emitted **after** all aspect work is finished. Carry `result.resultType = SUCCESS` for happy-path; `result.resultType = FAILURE` and `result.nativeResultType = <author-specific code>` for failures. |

### DPI URN convention

`urn:li:dataProcessInstance:<deterministic-id>`. Recommend `<platform>-<run_id>` so
retries on the same logical run remain addressable.

### Failure semantics

A failed run still emits the COMPLETE `RunEvent`, not a missing event. A run that
emits STARTED but never emits a terminal `RunEvent` is treated as in-flight by
consumers (and never surfaced) until a terminal event arrives or a human cleans it
up via DataHub's UI.

### Ordering guarantee

The STARTED event must precede schema/property emission on the dataset; the
terminal event must follow all aspect work. Out-of-order emission produces
non-deterministic ordering for any consumer that polls run history.

### systemMetadata requirement

DataHub's `dataset.lastIngested` GraphQL field is computed by scanning each
aspect's `systemMetadata.runId`. Any aspect whose `runId` equals
`"no-run-id-provided"` (the default sentinel set when no `systemMetadata` is
supplied to `MetadataChangeProposalWrapper`) is excluded from the computation.
When all aspects carry the default sentinel, `lastIngested` stays `null`.

**Contract**: every aspect emission within a custom-ingestor run MUST carry a
non-default `systemMetadata`. Reuse the same `sysmeta` for all aspects of a run
(DPI lifecycle aspects + dataset aspects):

```python
import time
from datahub.metadata.schema_classes import SystemMetadataClass

sysmeta = SystemMetadataClass(
    runId=f"<author>-{platform}-{run_id}",   # non-default, unique per run
    lastObserved=int(time.time() * 1000),    # epoch ms
)
await datahub_client.emit_aspect(dataset_urn, aspect, system_metadata=sysmeta)
```

### Authoring checklist

Self-verify before treating an ingestor as "done":

- [ ] DPI URN is deterministic per logical run (retries reuse the same URN)
- [ ] `Properties`, `Relationships`, and `Output` aspects are emitted before the first `RunEvent`
- [ ] STARTED `RunEvent` is emitted before any dataset aspect emission
- [ ] Terminal `RunEvent` (COMPLETE/FAILED) is emitted after all aspect work, with `result.resultType` set
- [ ] Failures emit a terminal event (do not let the run hang in STARTED)
- [ ] Every emit carries non-default `systemMetadata.runId`

### Conventions adopted by DataSpoke

DataSpoke's in-house extractors implement this contract with these specific
choices, useful as a reference template:

- `runId = "dataspoke-{platform}-{run_id}"`, with `run_id = uuid4()` per
  `IngestionService._run_inner` invocation.
- DPI URN = `urn:li:dataProcessInstance:{platform}-{run_id}`, matching the
  `runId` suffix so dataset aspects and the DPI cross-reference cleanly.
- One `SystemMetadataClass` instance per run, reused across all 11 emissions
  (5 DPI + 3 dataset for postgres, 5 DPI + 3 dataset for kafka).

Reference implementation: `src/backend/ingestion/service.py::_run_inner`,
`src/backend/ingestion/extractors.py`.

## SDK Patterns

SDK imports resolve from three packages: `datahub.ingestion.graph.client` (for `DataHubGraph`,
`DatahubClientConfig`), `datahub.emitter.{rest_emitter,mcp,mce_builder}` (for
`DatahubRestEmitter`, `MetadataChangeProposalWrapper`, `make_dataset_urn`), and
`datahub.metadata.schema_classes` (for aspect classes — `DatasetPropertiesClass`,
`SchemaMetadataClass`, `OwnershipClass`, `GlobalTagsClass`, `UpstreamLineageClass`,
`UpstreamClass`, `DeprecationClass`, `DatasetProfileClass`, `OperationClass`,
`DatasetUsageStatisticsClass`, `DatasetLineageTypeClass`).

| Pattern | Call | REST equivalent | Notes |
|---------|------|-----------------|-------|
| A. Read regular aspect | `graph.get_aspect(urn, AspectClass)` | `GET /aspects/{urn}?aspect=<name>` | Returns `None` if absent — always null-check. |
| B. Read timeseries aspect | `graph.get_timeseries_values(urn, AspectClass, filter={}, limit=30)` | `POST /aspects?action=getTimeseriesAspectValues` | Results newest-first; `filter` takes a field-level dict. |
| C. Write regular aspect | `emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=AspectClass(...)))` | `POST /openapi/v3/entity/dataset` | Upsert semantics — creates or overwrites. |
| D. Write lineage | Same as C with `UpstreamLineageClass(upstreams=[UpstreamClass(dataset=upstream_urn, type=...)])` | `POST /openapi/v3/entity/dataset` (aspect `upstreamLineage`) | Use `make_dataset_urn()` for upstream URN. |
| E. Write deprecation | Same as C with `DeprecationClass(deprecated=True, note=..., replacement=...)` | `POST /openapi/v3/entity/dataset` (aspect `deprecation`) | Replacement is a dataset URN. |
| F. Enumerate datasets | `list(graph.get_urns_by_filter(entity_types=["dataset"], ...))` | GraphQL `scrollAcrossEntities` | Supports `platform`, `env`, `query` filters; used by Governance bulk scan. |

For live examples see `src/shared/datahub/client.py` (DataSpoke's wrapper) and per-feature
service files under `src/backend/`.

## GraphQL Patterns

GraphQL is used when the REST API lacks an equivalent — primarily for **downstream lineage**
and **cross-entity search**.

### Downstream Lineage

The REST API only exposes `upstreamLineage` (what this dataset reads from). To find
**downstream consumers** (what depends on this dataset), call `graph.execute_graphql(...)` with
a `searchAcrossLineage` query (`direction: DOWNSTREAM`, `types: [DATASET]`) and read
`searchResults[].entity.urn` + `degree`. Used by Validation (downstream impact of failing
rules), Metadata Generation (shared consumers informing descriptions), Ontology Generation
(node and triple inference), Governance (ownership topology).

### Entity Enumeration by Domain

For cross-entity enumeration (e.g., listing all datasets for health scoring), call
`graph.execute_graphql(...)` with `scrollAcrossEntities` (`types: [DATASET]`) and paginate via
`nextScrollId`. Used by Governance (department-level enumeration).

### When to Use GraphQL vs REST

| Operation | Use | Reason |
|-----------|-----|--------|
| Read a single aspect by URN | REST (`get_aspect`) | Simpler, typed response |
| Read timeseries history | REST (`get_timeseries_values`) | Pagination + filter support |
| Write any aspect | REST (`emit_mcp`) | MCP is the standard write path |
| Downstream lineage traversal | GraphQL (`searchAcrossLineage`) | No REST equivalent |
| Cross-entity search/scroll | GraphQL (`scrollAcrossEntities`) | Pagination across entity types |
| Complex multi-hop queries | GraphQL | Single request for nested data |

## Event Subscription *(not used by baseline)*

The baseline UC1–UC5 flows are schedule-driven via Airflow tier DAGs and do not subscribe
to DataHub's Kafka topics (`MetadataChangeLog_Versioned_v1`,
`MetadataChangeLog_Timeseries_v1`). Organisations adding event-driven extensions can
consume those topics via `confluent_kafka.Consumer` and route by `event.aspectName`; no
spec is provided for this in the baseline contract.

## Error Handling & Resilience

### SDK Error Categories

| Error | Cause | Handling |
|-------|-------|---------|
| `ConnectionError` | DataHub GMS unreachable | Retry with exponential backoff (max 3 attempts) |
| `HttpError 404` | URN does not exist | Return `None` / skip — not all datasets have all aspects |
| `HttpError 401/403` | Token expired or insufficient permissions | Fail fast, log, alert — do not retry |
| `HttpError 429` | Rate limited | Retry after `Retry-After` header value |
| `HttpError 5xx` | DataHub internal error | Retry with backoff; circuit-break after 5 consecutive failures |

### Resilience Conventions

1. **Aspect reads may return `None`** — always check before accessing fields
2. **Timeseries queries may return empty lists** — handle gracefully (e.g., skip scoring)
3. **Write operations are idempotent** — `emit_mcp` is safe to retry on transient failures
4. **Bulk operations must be batched** — when scanning all datasets (Governance), process in
   batches of 100 with 100ms delays to avoid overwhelming GMS
5. **Kafka consumer must commit offsets after processing** *(only if event-driven extensions
   are enabled — see [Event Subscription](#event-subscription-optional-not-used-by-baseline))* —
   use `enable.auto.commit=false` and commit after successful handling
6. **Error responses for DataHub-availability faults must carry a generic message.**
   When `DataHubUnavailableError` propagates to a 502/503 response, the body must NOT
   include the inner exception text (GMS URLs, hostnames, endpoint paths, stack
   traces) — these are logged server-side only. The user-facing `message` is a
   stable, generic string; the underlying detail is correlated via the trace_id in
   server logs.

### Circuit Breaker

For features that scan many datasets (Governance, Metadata Generation clustering):

```
If 5 consecutive DataHub API calls fail:
  → Open circuit breaker
  → Wait 60 seconds
  → Try one probe request
  → If probe succeeds → close breaker, resume
  → If probe fails → keep breaker open, wait another 60s
```

## Configuration

All DataHub connection parameters are configured via environment variables (in dev, loaded from
`dev_env/.env`; in production, injected via Helm values → ConfigMap/Secret):

| Variable | Purpose | Dev Default |
|----------|---------|-------------|
| `DATASPOKE_DATAHUB_GMS_URL` | GMS endpoint for SDK read/write | `http://datahub.<INGRESS_DOMAIN>/gms` |
| `DATASPOKE_DATAHUB_TOKEN` | Personal access token; required because dev GMS runs with `METADATA_SERVICE_AUTH_ENABLED=true`. Generated by `dev_env/datahub/install.sh` via the frontend `/logIn` + `createAccessToken` flow and written back to `.env`. | generated PAT |
| `DATASPOKE_DATAHUB_KAFKA_BROKERS` | Kafka brokers for MCE/MAE events. **Optional** — only required when an organisation enables event-driven extensions; the baseline UC1–UC5 flows are schedule-driven via Airflow and do not subscribe to Kafka. | `<INGRESS_IP>:9005` |

Resilience settings (retry, circuit breaker, bulk batching) are application-level constants
defined in `src/shared/config/`. See
[`BACKEND.md §Configuration`](feature/BACKEND.md#configuration) for the full settings table, and
[`HELM_CHART.md §Configuration Flow`](feature/HELM_CHART.md#configuration-flow) for production
deployment.

## Open Questions

- [ ] Should DataSpoke define custom aspects in DataHub (e.g., `dataSpokeHealthScore`) or keep
  all computed data in PostgreSQL?
- [ ] If event-driven extensions are added on top of the baseline, what is the optimal Kafka
  consumer group topology — one group per feature, or a single shared group with internal
  routing?
- [ ] Should write operations go through a centralized DataHub client wrapper in `src/shared/`,
  or can features instantiate their own emitters?
- [ ] How to handle DataHub version upgrades that change aspect schemas — do we pin to a
  specific `acryl-datahub` SDK version?
