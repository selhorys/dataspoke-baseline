# DataHub Integration Patterns

## Table of Contents

1. [Overview](#overview)
2. [Goals & Non-Goals](#goals--non-goals)
3. [Integration Model](#integration-model)
4. [Aspect Reference](#aspect-reference)
5. [SDK Patterns](#sdk-patterns)
6. [GraphQL Patterns](#graphql-patterns)
7. [Event Subscription](#event-subscription)
8. [Error Handling & Resilience](#error-handling--resilience)
9. [Configuration](#configuration)
10. [Open Questions](#open-questions)

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
| Ingestion Control (active) | UC1 | **Write** | Emit enriched metadata (properties, lineage, tags, ownership). Applies to `mode: active` configs only. |
| Ingestion Control (passive) | UC1 | **Read** | The hourly `datahub-ingestion-status-sync` DAG polls DataHub ingestion run history for `mode: passive` configs and mirrors status into `event/ingestion`. No aspect writes. |
| Validation | UC2 | **Read + Write** | Query profiles, operations, lineage; register `assertionInfo`, emit `assertionRunEvent` |
| Ontology Generation | UC3 | **Read + Write** | Read schemas, descriptions, tags, lineage, usage; on review approval, attach a glossary term to the member dataset (`glossaryTerms` only — not `globalTags`) to reflect concept membership |
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
Control active mode, Validation, Ontology Generation, Metadata Generation) additionally use
`DatahubRestEmitter`. Redefined DataHub functions would use both clients to blend DataHub and
DataSpoke data in a single API call.

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
Validation feature (UC2) builds on DataHub's
[Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec),
which defines six `assertionInfo.type` values covering the primary data quality
dimensions:

| `assertionInfo.type` | Quality dimension | DataSpoke `rules[].type` | Notes |
|---|---|---|---|
| `FRESHNESS` | Timeliness | `freshness` | Native |
| `VOLUME` | Completeness | `volume` | Native |
| `FIELD` | Accuracy / validity | `field` | Native |
| `SCHEMA` | Conformance | `schema` | Native |
| `SQL` | Custom SQL | `sql` | Native |
| `CUSTOM` | Anything else | `custom` | DataSpoke uses `subtype: "sql_timeseries"` for partition-aware SQL with optional ML-based anomaly detection |

DataSpoke registers each rule's `assertionInfo` once at config upsert and reports
execution outcomes via `assertionRunEvent` (`SUCCESS` / `FAILURE` / `ERROR`), so
DataSpoke-managed checks appear in DataHub's native assertion UI alongside DataHub-native
assertions.

| Aspect | SDK Class | Entity Type | REST Write Path |
|--------|----------|-------------|----------------|
| `assertionInfo` | `AssertionInfoClass` | `assertion` | `POST /openapi/v3/entity/assertion` |
| `assertionRunEvent` | `AssertionRunEventClass` | `assertion` | `POST /openapi/v3/entity/assertion` |

### Data Product Aspects

Data products group related datasets under a topic-level concept. UC4 (Metadata
Generation) `cross_data.md` proposals may create, modify, split, or retitle
`dataProduct` entities to organize cross-dataset documentation. The generator chooses
a descriptive title (a topic phrase) for new data products — the URN is **not** keyed
off any UC3 concept ID.

| Aspect | SDK Class | Entity Type | Key Fields | REST Write Path |
|--------|----------|-------------|------------|----------------|
| `dataProductProperties` | `DataProductPropertiesClass` | `dataProduct` | `name`, `description` (Markdown), `assets[]` (dataset URNs) | `POST /openapi/v3/entity/dataproduct` |

### Aspect Usage by Feature

Which features read (R) or write (W) each aspect. *Ingestion Control writes apply to
`mode: active` configs only; passive mode reads ingestion run history out-of-band via
the `datahub-ingestion-status-sync` DAG and writes no aspects.*

| Aspect | Ingestion Control | Validation | Ontology Generation | Metadata Generation | Governance |
|--------|:---:|:---:|:---:|:---:|:---:|
| `datasetProperties` | W | R | R | R (context only) | R |
| `editableDatasetProperties` | — | — | — | W (on approval) | R |
| `schemaMetadata` | W | R | R | R (context only) | R |
| `editableSchemaMetadata` | — | — | — | W (on approval) | R |
| `ownership` | W | — | R | — | R |
| `globalTags` | W | — | R | — *(future scope)* | R |
| `glossaryTerms` | — | — | R + W (concept attachment on approval) | — *(future scope)* | R |
| `upstreamLineage` | W | R | R | R | R |
| `deprecation` | — | R | — | — | — |
| `datasetProfile` | — | R | — | — | R |
| `operation` | — | R | — | — | R |
| `datasetUsageStatistics` | — | — | R | R | R |
| `assertionInfo` | — | W | — | — | — |
| `assertionRunEvent` | — | W | — | — | R |
| `dataProductProperties` | — | — | — | W (create / modify / split / retitle on approval) | R |

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
(cross-concept relationship inference), Governance (ownership topology).

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

## Event Subscription *(optional, not used by baseline)*

> **Baseline UC1–UC5 do not subscribe to Kafka events.** Cross-feature triggers in the
> baseline are schedule-driven via Airflow tier DAGs (see
> [`ARCHITECTURE.md §Cross-Cutting Backend Concerns`](ARCHITECTURE.md#cross-cutting-backend-concerns)
> and [`BACKEND.md §Airflow Workflows`](feature/BACKEND.md#airflow-workflows-srcworkflows)).
> The pattern below is preserved as a reference for organisations that want to add
> event-driven extensions on top of DataSpoke; it is **not enabled in the baseline build**.

### Kafka Topics

| Topic | Event Type | When Emitted |
|-------|-----------|-------------|
| `MetadataChangeLog_Versioned_v1` | Metadata change log | Any regular aspect changes |
| `MetadataChangeLog_Timeseries_v1` | Timeseries change log | New profile/operation/usage data arrives |

### Consumer Pattern *(reference)*

If enabled, a single `confluent_kafka.Consumer` (group `dataspoke-consumers`,
`auto.offset.reset=latest`) subscribes to both topics. Messages are deserialized via
`deserialize_mcl()` and routed by `event.aspectName` to feature-specific handlers.

### Example Event-Driven Triggers *(reference, not part of baseline)*

| Event Aspect | Possible Consumer | Possible Action |
|-------------|-------------------|-----------------|
| `datasetProperties` | Ontology Generation extension | Re-generate embedding; re-classify if description changed |
| `schemaMetadata` | Ontology / Metadata Generation extensions | Re-embed schema; flag metadata-generation candidates |
| `datasetProfile` | Validation extension | Anomaly detection on new profile |
| `operation` | Validation / Governance extension | Freshness check against SLA |
| `ownership` | Governance extension | Re-compute owner-keyed metrics |
| `globalTags` | Ontology / Governance extension | Re-sync coverage metrics |

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
| `DATASPOKE_DATAHUB_KAFKA_BROKERS` | Kafka brokers for MCE/MAE events | `<INGRESS_IP>:9005` |

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
