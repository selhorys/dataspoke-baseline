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

DataSpoke is a **sidecar extension** to DataHub. DataHub is the Hub (metadata SSOT); DataSpoke reads from and writes to DataHub without modifying its core. This document defines the integration patterns, SDK usage conventions, and aspect catalog that all DataSpoke features must follow.

**Key principles**:

1. **DataHub is the Hub** — DataHub stores metadata; DataSpoke computes on top of it. DataSpoke tries not to duplicate metadata that DataHub already persists.
2. **DataSpoke features primarily fill the gaps** — DataSpoke features are designed for use cases that cannot be fulfilled by DataHub alone (e.g., deep ingestion, predictive SLA, NL search).
3. **DataSpoke API can redefine DataHub functions for convenience** — in some cases DataSpoke may re-expose DataHub's basic functions (e.g., dataset registration, metadata browsing) through its own API and UI layer. It is a **blended API/UI** that combines DataHub-native metadata with DataSpoke-specific metadata in a single call for user convenience. For example, when a user needs both basic dataset properties (stored in DataHub) and deep-ingestion annotations (stored in DataSpoke's backend) at the same time, a single DataSpoke endpoint can aggregate both sources instead of requiring two separate calls. The same applies to creation and modification flows — a DataSpoke "create dataset" API could write core metadata to DataHub while simultaneously initializing DataSpoke-side records. These redefined features are **not the primary focus** of this project; architecture and use-case specs do not cover them in detail. However, future versions of DataSpoke may include baseline redefined features (e.g., dataset creation, unified metadata views).

All integration code uses the `acryl-datahub` Python SDK. Three communication channels exist:

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
    │  3. Kafka (events)                           │
    │     MetadataChangeEvent / MetadataAuditEvent │
    │                                              │
    └──────────────────────────────────────────────┘
```

## Goals & Non-Goals

### Goals

- Define a single, consistent set of SDK patterns for all DataSpoke features
- Catalog every DataHub aspect that DataSpoke reads or writes
- Establish error handling and resilience conventions
- Provide copy-paste-ready code patterns for feature implementers
- Enable possible redefinition of DataHub's basic functions (e.g., dataset registration) in DataSpoke's API and UI layer for blended user experiences

### Non-Goals

- Modifying DataHub core (custom aspects may be considered — see [Open Questions](#open-questions))
- Defining DataSpoke's own data model (see individual feature specs)
- Covering DataHub admin operations (ingestion recipes, user management)

## Integration Model

### Read vs Write Boundary

Each DataSpoke feature has a clear integration direction:

| Feature | User Group | Direction | Primary Operations |
|---------|-----------|-----------|-------------------|
| Deep Ingestion | DE | **Write** | Emit enriched metadata (properties, lineage, tags, ownership) |
| Online Validator | DE/DA | **Read** | Query profiles, operations, lineage, assertions |
| Predictive SLA | DE | **Read** | Query timeseries profiles, lineage for anomaly detection |
| Doc Generation | DE | **Read + Write** | Read schemas for clustering; write deprecation, tags |
| NL Search | DA | **Read** | Read properties, tags, lineage, usage for vector index |
| Metrics Dashboard | DG | **Read** | Read properties, ownership, schemas, tags for health scoring |
| Redefined DataHub Functions *(TBD)* | All | **Read + Write** | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data |

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

Read-only features (Validator, Predictive SLA, NL Search, Metrics Dashboard) use `DataHubGraph` only. Features that write back (Deep Ingestion, Doc Generation) additionally use `DatahubRestEmitter`. Redefined DataHub functions would use both clients to blend DataHub and DataSpoke data in a single API call.

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

Regular aspects represent the current state of an entity. Read via `get_aspect()`, write via `emit_mcp()`.

| Aspect | SDK Class | Key Fields | REST Read Path | REST Write Path |
|--------|----------|------------|---------------|----------------|
| `datasetProperties` | `DatasetPropertiesClass` | `description`, `customProperties` | `GET /aspects/{urn}?aspect=datasetProperties` | `POST /openapi/v3/entity/dataset` |
| `schemaMetadata` | `SchemaMetadataClass` | `fields[].fieldPath`, `fields[].nativeDataType`, `fields[].description` | `GET /aspects/{urn}?aspect=schemaMetadata` | `POST /openapi/v3/entity/dataset` |
| `ownership` | `OwnershipClass` | `owners[].owner` (URN), `owners[].type` | `GET /aspects/{urn}?aspect=ownership` | `POST /openapi/v3/entity/dataset` |
| `globalTags` | `GlobalTagsClass` | `tags[].tag` (URN) | `GET /aspects/{urn}?aspect=globalTags` | `POST /openapi/v3/entity/dataset` |
| `upstreamLineage` | `UpstreamLineageClass` | `upstreams[].dataset` (URN), `upstreams[].type` | `GET /aspects/{urn}?aspect=upstreamLineage` | `POST /openapi/v3/entity/dataset` |
| `deprecation` | `DeprecationClass` | `deprecated` (bool), `note`, `replacement` (URN), `decommissionTime` | `GET /aspects/{urn}?aspect=deprecation` | `POST /openapi/v3/entity/dataset` |

### Timeseries Aspects

Timeseries aspects store point-in-time measurements. Read via `get_timeseries_values()`. They are append-only — DataHub retains history.

| Aspect | SDK Class | Key Fields | REST Read Path |
|--------|----------|------------|---------------|
| `datasetProfile` | `DatasetProfileClass` | `rowCount`, `columnCount`, `fieldProfiles`, `sizeInBytes` | `POST /aspects?action=getTimeseriesAspectValues` |
| `operation` | `OperationClass` | `lastUpdatedTimestamp`, `operationType`, `actor` | `POST /aspects?action=getTimeseriesAspectValues` |
| `datasetUsageStatistics` | `DatasetUsageStatisticsClass` | `uniqueUserCount`, `totalSqlQueries`, `topSqlQueries`, `userCounts`, `fieldCounts` | `POST /aspects?action=getTimeseriesAspectValues` |
| `assertionRunEvent` | `AssertionRunEventClass` | `status` (pass/fail), `timestampMillis`, `assertionUrn` | `POST /aspects?action=getTimeseriesAspectValues` |

### Assertion Aspects

Assertions are stored on `assertion` entities (not `dataset` entities):

| Aspect | SDK Class | Entity Type | REST Write Path |
|--------|----------|-------------|----------------|
| `assertionInfo` | `AssertionInfoClass` | `assertion` | `POST /openapi/v3/entity/assertion` |
| `assertionRunEvent` | `AssertionRunEventClass` | `assertion` | `POST /openapi/v3/entity/assertion` |

### Aspect Usage by Feature

Which features read (R) or write (W) each aspect:

| Aspect | Deep Ingestion | Validator | Predictive SLA | Doc Generation | NL Search | Metrics Dashboard |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| `datasetProperties` | W | R | — | R | R | R |
| `schemaMetadata` | W | R | — | R | R | R |
| `ownership` | W | — | — | — | R | R |
| `globalTags` | W | — | — | W | R | R |
| `upstreamLineage` | W | R | R | R | R | — |
| `deprecation` | — | R | — | W | — | — |
| `datasetProfile` | — | R | R | — | — | — |
| `operation` | — | R | R | — | — | — |
| `datasetUsageStatistics` | — | — | — | — | R | R |
| `assertionRunEvent` | W | R | — | — | — | — |

## SDK Patterns

All imports come from these three packages:

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mce_builder import make_dataset_urn
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    DatasetProfileClass,
    DatasetPropertiesClass,
    DatasetUsageStatisticsClass,
    DeprecationClass,
    GlobalTagsClass,
    OperationClass,
    OwnershipClass,
    SchemaMetadataClass,
    UpstreamClass,
    UpstreamLineageClass,
)
```

### Pattern A: Read Regular Aspect

```python
# Read the current state of a regular aspect
# REST equivalent: GET /aspects/{urn}?aspect=<aspectName>
aspect = graph.get_aspect(dataset_urn, DatasetPropertiesClass)

if aspect is not None:
    description = aspect.description
    custom_props = aspect.customProperties
```

Returns `None` if the aspect does not exist for the given URN.

### Pattern B: Read Timeseries Aspect

```python
# Read historical values of a timeseries aspect
# REST equivalent: POST /aspects?action=getTimeseriesAspectValues
profiles = graph.get_timeseries_values(
    dataset_urn,
    DatasetProfileClass,
    filter={},
    limit=30,
)

for profile in profiles:
    row_count = profile.rowCount
    timestamp = profile.timestampMillis
```

The `filter` parameter accepts a dict for field-level filtering. Use `limit` to control history depth. Results are ordered newest-first.

### Pattern C: Write Regular Aspect via MCP

```python
# Write (create or update) a regular aspect
# REST equivalent: POST /openapi/v3/entity/dataset  (aspect: datasetProperties)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=DatasetPropertiesClass(
        description="Master catalog of all book titles...",
        customProperties={"genre_taxonomy": "4-level"},
    ),
))
```

`emit_mcp()` is an upsert — it creates the aspect if missing, or overwrites if present.

### Pattern D: Write Lineage

```python
# Emit upstream lineage edges
# REST equivalent: POST /openapi/v3/entity/dataset  (aspect: upstreamLineage)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=UpstreamLineageClass(
        upstreams=[UpstreamClass(
            dataset=make_dataset_urn(platform="oracle", name="publishers.feed_raw", env="PROD"),
            type=DatasetLineageTypeClass.TRANSFORMED,
        )],
    ),
))
```

### Pattern E: Write Deprecation

```python
# Mark a dataset as deprecated with a replacement pointer
# REST equivalent: POST /openapi/v3/entity/dataset  (aspect: deprecation)
emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=dataset_urn,
    aspect=DeprecationClass(
        deprecated=True,
        note="Migrated to catalog.product_master per ontology reconciliation",
        replacement=make_dataset_urn(platform="oracle", name="catalog.product_master", env="PROD"),
    ),
))
```

### Pattern F: Enumerate Datasets

```python
# Iterate over all datasets matching a filter
# REST equivalent: GraphQL scrollAcrossEntities
dataset_urns = list(graph.get_urns_by_filter(entity_types=["dataset"]))

for urn in dataset_urns:
    props = graph.get_aspect(urn, DatasetPropertiesClass)
    ownership = graph.get_aspect(urn, OwnershipClass)
```

Use `get_urns_by_filter()` for bulk enumeration (e.g., Metrics Dashboard health scan). Supports `platform`, `env`, and `query` filters.

## GraphQL Patterns

GraphQL is used when the REST API lacks an equivalent — primarily for **downstream lineage** and **cross-entity search**.

### Downstream Lineage

The REST API only exposes `upstreamLineage` (which upstream feeds into this dataset). To find **downstream consumers** (what depends on this dataset), use GraphQL:

```python
# Find downstream datasets affected by an SLA miss or deprecation
result = graph.execute_graphql("""
    query {
        searchAcrossLineage(input: {
            urn: "%s",
            direction: DOWNSTREAM,
            types: [DATASET],
            query: "*"
        }) {
            searchResults {
                entity { urn }
                degree
            }
        }
    }
""" % dataset_urn)
```

**Used by**: Predictive SLA (downstream impact), Doc Generation (shared consumers), NL Search (marketing lineage).

### Entity Enumeration by Domain

```python
# List all datasets in a domain for health scoring
result = graph.execute_graphql("""
    query {
        scrollAcrossEntities(input: {
            types: [DATASET],
            query: "*",
            count: 100
        }) {
            searchResults { entity { urn } }
            nextScrollId
        }
    }
""")
```

**Used by**: Metrics Dashboard (department-level enumeration).

### When to Use GraphQL vs REST

| Operation | Use | Reason |
|-----------|-----|--------|
| Read a single aspect by URN | REST (`get_aspect`) | Simpler, typed response |
| Read timeseries history | REST (`get_timeseries_values`) | Pagination + filter support |
| Write any aspect | REST (`emit_mcp`) | MCP is the standard write path |
| Downstream lineage traversal | GraphQL (`searchAcrossLineage`) | No REST equivalent |
| Cross-entity search/scroll | GraphQL (`scrollAcrossEntities`) | Pagination across entity types |
| Complex multi-hop queries | GraphQL | Single request for nested data |

## Event Subscription

DataSpoke consumes Kafka events from DataHub to react to metadata changes in real time.

### Kafka Topics

| Topic | Event Type | When Emitted |
|-------|-----------|-------------|
| `MetadataChangeLog_Versioned_v1` | Metadata change log | Any regular aspect changes |
| `MetadataChangeLog_Timeseries_v1` | Timeseries change log | New profile/operation/usage data arrives |

### Consumer Pattern

```python
from confluent_kafka import Consumer

consumer = Consumer({
    "bootstrap.servers": DATASPOKE_DATAHUB_KAFKA_BROKERS,
    "group.id": "dataspoke-consumers",
    "auto.offset.reset": "latest",
})

consumer.subscribe(["MetadataChangeLog_Versioned_v1"])

while True:
    msg = consumer.poll(timeout=1.0)
    if msg is None:
        continue
    event = deserialize_mcl(msg.value())

    # Route by aspect name
    if event.aspectName == "datasetProperties":
        sync_vector_index(event)       # NL Search: update Qdrant
    elif event.aspectName == "datasetProfile":
        trigger_quality_check(event)   # Validator: anomaly detection
    elif event.aspectName == "ownership":
        update_health_score(event)     # Metrics Dashboard: re-score
```

### Event-Driven Feature Triggers

| Event Aspect | Consumer | Action |
|-------------|---------|--------|
| `datasetProperties` | NL Search | Re-generate embedding, update Qdrant |
| `schemaMetadata` | NL Search, Doc Generation | Re-embed schema, detect new clusters |
| `datasetProfile` | Validator, Predictive SLA | Run anomaly detection on new profile |
| `operation` | Predictive SLA | Check freshness against SLA targets |
| `ownership` | Metrics Dashboard | Re-compute department health score |
| `globalTags` | NL Search, Metrics Dashboard | Update PII index, re-score tag coverage |

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
4. **Bulk operations must be batched** — when scanning all datasets (Metrics Dashboard), process in batches of 100 with 100ms delays to avoid overwhelming GMS
5. **Kafka consumer must commit offsets after processing** — use `enable.auto.commit=false` and commit after successful handling

### Circuit Breaker

For features that scan many datasets (Metrics Dashboard, Doc Generation clustering):

```
If 5 consecutive DataHub API calls fail:
  → Open circuit breaker
  → Wait 60 seconds
  → Try one probe request
  → If probe succeeds → close breaker, resume
  → If probe fails → keep breaker open, wait another 60s
```

## Configuration

All DataHub connection parameters are configured via environment variables (in dev, loaded from `dev_env/.env`; in production, injected via Helm values → ConfigMap/Secret):

| Variable | Purpose | Dev Default |
|----------|---------|-------------|
| `DATASPOKE_DATAHUB_GMS_URL` | GMS endpoint for SDK read/write | `http://localhost:9004` |
| `DATASPOKE_DATAHUB_TOKEN` | Personal access token (empty in dev — DataHub doesn't require auth in the dev env) | `""` |
| `DATASPOKE_DATAHUB_KAFKA_BROKERS` | Kafka brokers for MCE/MAE events | `localhost:9005` |

Resilience settings (retry, circuit breaker, bulk batching) are application-level constants defined in `src/shared/config/`:

| Setting | Default |
|---------|---------|
| `retry_max_attempts` | 3 |
| `retry_backoff_base_ms` | 500 |
| `circuit_breaker_threshold` | 5 |
| `circuit_breaker_reset_ms` | 60000 |
| `bulk_batch_size` | 100 |
| `bulk_batch_delay_ms` | 100 |

See [`spec/feature/DEV_ENV.md` §Application Runtime Variables](feature/DEV_ENV.md#application-runtime-variables-dataspoke) for the full variable listing, and [`spec/feature/HELM_CHART.md` §Configuration Flow](feature/HELM_CHART.md#configuration-flow) for production deployment.

## Open Questions

- [ ] Should DataSpoke define custom aspects in DataHub (e.g., `dataSpokeHealthScore`) or keep all computed data in PostgreSQL?
- [ ] What is the optimal Kafka consumer group topology — one group per feature, or a single shared group with internal routing?
- [ ] Should write operations go through a centralized DataHub client wrapper in `src/shared/`, or can features instantiate their own emitters?
- [ ] How to handle DataHub version upgrades that change aspect schemas — do we pin to a specific `acryl-datahub` SDK version?
