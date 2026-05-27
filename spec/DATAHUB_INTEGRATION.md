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
   served as the historical-baseline cache, the ontology graph, metadata-generation proposal
   history, dataset/metric registries) and always references DataHub URNs as the canonical
   identifier.
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
| Validation | UC2 | **Write** | Emit `assertionInfo` on conf upsert (variable list joined as `customAssertion.logic`); emit `assertionRunEvent` per pipeline-posted result (timestamped to `data_time`); emit `status.removed` on DELETE / clear on resurrection. Validation logic lives in the data pipeline. |
| Ontology Generation | UC3 | **Read** | Read `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`, and `documentInfo` on `document` entities whose `relatedAssets` reference an in-scope dataset. Ontology is modelled as a subject / predicate / object triple set (nodes / edges / triples) and stored entirely in DataSpoke (PostgreSQL relational + pgvector). |
| Metadata Generation | UC4 | **Read + Write (editable description only)** | Read the same DataHub aspect set as UC3 (`datasetProperties`, `schemaMetadata`, `editableDatasetProperties`, `editableSchemaMetadata`, `glossaryTerms`, `documentInfo`) plus UC3-approved nodes/triples from DataSpoke storage. On reviewer approval of a candidate, write only to the *editable* description aspects — `editableDatasetProperties.description` for `dataset.description` items, `editableSchemaMetadata.editableSchemaFieldInfo[].description` for `column.<fieldPath>.description` items. Tag and glossary-term proposals are future scope. |
| Governance | UC5 | **Read** | Aggregate pre-existing metadata (properties, ownership, tags) and DataSpoke validation / ontology state |
| Redefined DataHub Functions *(TBD)* | — | **Read + Write** | Blended API/UI that proxies DataHub reads/writes alongside DataSpoke-specific data |

### Client Initialization

Two SDK clients serve different purposes. The app pod reads `gms_url` and `token` from the DB `peripheral_config` table (populated via `/api/v1/admin/peripherals/datahub`); test/dev tooling reads them from `DATASPOKE_TEST_DATAHUB_{GMS_URL,TOKEN}` in `helm-charts/.env`.

```python
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.rest_emitter import DatahubRestEmitter

# Read client — queries aspects and GraphQL
graph = DataHubGraph(DatahubClientConfig(
    server=gms_url,      # peripheral_config.datahub.gms_url (app) | $DATASPOKE_TEST_DATAHUB_GMS_URL (tests)
    token=token,         # peripheral_config.datahub.token   (app) | $DATASPOKE_TEST_DATAHUB_TOKEN   (tests)
))

# Write client — emits MCPs
emitter = DatahubRestEmitter(
    gms_server=gms_url,
    token=token,
)
```

Read-only features (Governance, Ontology Generation) use `DataHubGraph` only. Features that
write back (Ingestion Control `active-custom` mode, Validation, Metadata Generation)
additionally use `DatahubRestEmitter`. Redefined DataHub functions would use both clients
to blend DataHub and DataSpoke data in a single API call.

### Service Credential Model

DataSpoke→DataHub calls use a single pre-configured admin-level Personal
Access Token stored in the dedicated K8s Secret `dataspoke-datahub-secret`
(key `token`). The token is bound to a DataHub corpuser at creation time —
in dev, `helm-charts/bin/peripherals/datahub.sh` binds it to
`urn:li:corpuser:datahub` (DataHub's built-in admin); in prod, the operator
chooses the corpuser, which must have sufficient privileges to emit every
aspect in the [Aspect Usage by Feature](#aspect-usage-by-feature) write
column.

This service token is the **only** credential used for upstream calls —
every code path (request handler, Airflow DAG, Kafka consumer, the new
user-mirror writes from [feature/AUTH.md](feature/AUTH.md)) reads it via
RBAC and reuses it. DataSpoke user identity does **not** propagate to the
upstream call. Implications:

- DataHub's aspect-level audit attribution (`systemMetadata.actor` /
  `lastUpdated.actor`) points to the service-token's corpuser URN, not the
  DataSpoke user who triggered the write. User-level audit lives only in
  the DataSpoke `events` table.
- DataSpoke user privileges (DataHub `Admin` / `Editor` / `Reader`, plus
  workspace tier in the JWT `groups` claim) gate access only to DataSpoke
  routes. They do not constrain what the upstream call can write — that
  is bounded by the service-token's own DataHub role.
- Per-user impersonation against DataHub (user-bound PATs, token exchange,
  on-behalf-of writes) is out of scope for the baseline. Organisations
  needing DataHub-side per-user attribution add it as an extension.

The same model applies to Langfuse: the project-level `secret_key` in
`dataspoke-langfuse-secret` is shared by every LLM trace from every code
path. See [BACKEND_LLM.md §Observability](feature/BACKEND_LLM.md) for the
Langfuse client setup.

### URN Construction

Always use the builder function — never construct URN strings manually:

```python
from datahub.emitter.mce_builder import make_dataset_urn

# Correct
dataset_urn = make_dataset_urn(platform="oracle", name="catalog.title_master", env="PROD")

# Wrong — do not use string literals
dataset_urn = "urn:li:dataset:(urn:li:dataPlatform:oracle,catalog.title_master,PROD)"
```

### Container URN Construction

Container URNs for database/schema hierarchies must be **byte-identical** to those
emitted by DataHub's upstream managed-source plugins (e.g. the managed PostgreSQL
source). Without parity, Browse v2 renders two folders for the same logical
database — one container-backed (from managed ingestion), one path-text-derived
(from container-less emission) — and users see duplicates.

Use the upstream SDK key classes; never compute container GUIDs manually:

```python
from datahub.emitter.mcp_builder import DatabaseKey, SchemaKey, gen_containers

db_key = DatabaseKey(
    database="example_db",
    platform="postgres",
    instance=None,
    env="DEV",
    backcompat_env_as_instance=True,  # required for parity with upstream PG source
)
schema_key = SchemaKey(
    database="example_db", schema="catalog",
    platform="postgres", instance=None, env="DEV",
    backcompat_env_as_instance=True,
)
```

Invariants:
- `backcompat_env_as_instance=True` is mandatory — upstream sources set it, so omitting
  it produces a different GUID and a sibling-duplicate folder.
- Database container is emitted with `sub_types=["Database"]`, schema container with
  `sub_types=["Schema"]` and `parent_container_key=db_key`.
- Each dataset emits `ContainerClass(container=schema_key.as_urn())` so it nests under
  its schema container.
- Re-emission is idempotent (DataHub merges by URN).

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
| `container` | `ContainerClass` | `container` (container URN) | `GET /aspects/{urn}?aspect=container` | `POST /openapi/v3/entity/dataset` |
| `browsePathsV2` | `BrowsePathsV2Class` | `path[].id`, `path[].urn` (container URNs) | `GET /aspects/{urn}?aspect=browsePathsV2` | `POST /openapi/v3/entity/dataset` |
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
Validation feature (UC2) is a **passive result store** — external pipelines compute
results and POST them; DataSpoke writes three aspects:
`assertionInfo` (versioned, on `PUT/PATCH /attr/validation/conf`),
`assertionRunEvent` (timeseries, per `POST /attr/validation/result`), and `status`
(versioned, on `DELETE` and on resurrection via `PUT`-after-`DELETE`). The full
contract — URN derivation, aspect contents, `customAssertion.logic` format,
`result.type` mapping, `nativeResults` serialization, and intentionally omitted
aspects — lives in [`spec/feature/VALIDATION.md` §DataHub Aspect Mapping](feature/VALIDATION.md#datahub-aspect-mapping).

Mandatory conventions for the DataSpoke emission path:

1. **`assertionInfo.type = CUSTOM`.** The `customAssertion` sub-aspect carries
   `type: "DATASPOKE_VALIDATION"` (Quality-tab categorization label),
   `entity: <dataset_urn>`, and `logic: "<comma-joined variable names>"`.
   Variable names follow the regex `[a-z][a-z0-9_]{0,99}` so `,` is unambiguous
   on read.
2. **`source.type = EXTERNAL`.** Marks "the data pipeline runs this, DataHub
   stores results". `NATIVE` is reserved for the DataHub Cloud runner.
3. **Deterministic URN.**
   `urn:li:assertion:<datahub_guid({"platform": "dataspoke-validation", "entity": dataset_urn})>`.
   Recomputable from `dataset_urn` alone — one slot per dataset. PUT/PATCH is
   idempotent. The soft-delete / resurrection cycle reuses the same URN: `DELETE`
   emits `status.removed = true`; subsequent `PUT` emits `status.removed = false`
   together with `assertionInfo`. DataSpoke is authoritative for the assertion
   lifecycle — out-of-band tombstones (e.g. a DataHub UI admin manually setting
   `status.removed=true`) are reverted on the next config PUT/PATCH. Operators
   who want to durably hide a DataSpoke assertion use `DELETE /attr/validation/conf`.
4. **`lastUpdated` audit stamp.** Populate `AssertionInfoClass.lastUpdated` with
   the DataSpoke service-user URN; otherwise the DataHub UI history card shows
   "unknown actor".
5. **`assertionRunEvent.timestampMillis = data_time`.** The pipeline-supplied
   `data_time` is the timeseries axis — it aligns DataHub's chart axis with the
   user mental model ("when the data is for"). Server ingest time is preserved
   separately in `runtimeContext.ingestion_time` for audit.
6. **`result.type` mapping.** `SUCCESS` if `score == 1.0`, `FAILURE` otherwise.
   The raw `score` is preserved in `actualAggValue` and `nativeResults["score"]`
   so partial-success semantics can be introduced later without losing fidelity.
7. **`nativeResults` carries variables.** `Map<string,string>` of variable name →
   `repr(float)` (round-trip safe under IEEE 754); parsed back as float on read.
8. **Append-only timeseries.** Multiple POSTs with the same `data_time` become
   distinct `assertionRunEvent` rows; the GET endpoint returns
   last-write-wins per distinct `data_time`. This matches DataHub's
   timeseries aspect being fundamentally append-only.
9. **Registration timing.** `assertionInfo` is emitted at config upsert
   (`PUT/PATCH /attr/validation/conf`). A DataHub error during registration
   surfaces as 502/503; DataHub is the SSOT for assertion definitions and config
   save is coupled to its availability by design.

| Aspect | SDK Class | Entity Type | REST Write Path |
|--------|----------|-------------|----------------|
| `assertionInfo` | `AssertionInfoClass` | `assertion` | `POST /openapi/v3/entity/assertion` |
| `assertionRunEvent` | `AssertionRunEventClass` | `assertion` | `POST /openapi/v3/entity/assertion` |
| `status` | `StatusClass` | `assertion` | `POST /openapi/v3/entity/assertion` |

### Document Aspects

DataHub's `document` entity (URN `urn:li:document:<id>`, server-assigned) is a
knowledge-base entity for prose attached to data assets — Markdown notes,
runbooks, design memos, and ingested third-party docs (Confluence, Notion, Slack).
DataSpoke reads documents whose `relatedAssets` overlap an in-scope dataset as
context for UC3 ontology inference; baseline DataSpoke does not write document
entities. (Generative document-entity authoring is future scope for UC4
metadata generation.)

#### `documentInfo` aspect fields

| Field | Type | Notes |
|---|---|---|
| `title` | optional string | Document title (searchable, autocomplete-enabled) |
| `source` | optional `DocumentSource` | `{sourceType: NATIVE \| EXTERNAL, externalUrl?, externalId?}`. DataSpoke-authored documents emit `NATIVE`; ingested third-party documents arrive as `EXTERNAL` with the source URL/ID populated by the ingestion connector |
| `status` | enum `DocumentStatus` | `published \| unpublished` (do not confuse with the separate `Status` aspect used for soft-delete) |
| `contents.text` | string | Document body. **DataSpoke convention:** treated as Markdown; the DataHub UI renders it as Markdown |
| `created` | `AuditStamp` | Creation actor + timestamp |
| `lastModified` | `AuditStamp` | Last-edit actor + timestamp |
| `relatedAssets[]` | optional array of URN | Outbound links to data assets (datasets, dashboards, etc.) — see entityType whitelist below |
| `relatedDocuments[]` | optional array of document URN | Outbound links to other documents |
| `parentDocument` | optional document URN | Hierarchical parent (doc-to-doc only) |

#### DataSpoke conventions

- **Body format.** `documentInfo.contents.text` is Markdown by convention.
- **Discovery.** Find documents that reference a given dataset via the GraphQL `searchAcrossEntities` query, filtering on `entityType: DOCUMENT` and `relatedAssets` containing the dataset URN. Sort by `lastModified` descending and apply a per-dataset cap when feeding documents to the LLM as evidence.

#### Aspect reference (read-only in baseline)

| Aspect | SDK Class | Entity Type | Key Fields | REST Read Path |
|--------|----------|-------------|------------|----------------|
| `documentInfo` | `DocumentInfoClass` | `document` | `title`, `contents.text` (Markdown), `relatedAssets[]`, `source`, `status`, `created`, `lastModified` | `GET /openapi/v3/entity/document/{urn}` |
| `status` | `StatusClass` | `document` | `removed` (bool) | `GET /openapi/v3/entity/document/{urn}` |

#### `relatedAssets` entityType whitelist

A `relatedAssets[].asset` URN must point at one of the entity types accepted by
the `document` entity registry — see
[`RelatedAsset.pdl`](https://github.com/datahub-project/datahub/blob/v1.5.0.2/metadata-models/src/main/pegasus/com/linkedin/knowledge/RelatedAsset.pdl)
in the DataHub source for the authoritative list. DataSpoke populates
`relatedAssets` with dataset URNs in the baseline.

#### Linkage caveats

- The relationship is non-exclusive: many documents can reference the same dataset, and one document can reference many datasets.
- Hierarchy is doc-to-doc only via `parentDocument`; documents cannot declare a parent of any other entity type.
- `relatedAssets` carries no edge label beyond "related" — the relationship type is implicit in the document's body.

### Aspect Usage by Feature

Which features read (R) or write (W) each aspect. *Ingestion Control writes apply to
`mode: active-custom` configs only (Status, DatasetProperties, SchemaMetadata, plus
per-run DataProcessInstance aspects per the [Custom Ingestor Guide](#custom-ingestor-guide);
postgres datasets additionally receive `Container` and `BrowsePathsV2` aspects so they
nest under the same database → schema hierarchy as DataHub's managed-PG source);
`passive` mode reads `DataProcessInstance` run history out-of-band via the
`ingestion-passive-hourly` DAG and writes no aspects.*

| Aspect | Ingestion Control | Validation | Ontology Generation | Metadata Generation | Governance |
|--------|:---:|:---:|:---:|:---:|:---:|
| `datasetProperties` | W | R | R | R | R *(doc-health table description)* |
| `editableDatasetProperties` | — | — | R | R + W (W on approval) | R *(doc-health table description overlay)* |
| `schemaMetadata` | W | R | R | R | R *(doc-health column descriptions)* |
| `editableSchemaMetadata` | — | — | R | R + W (W on approval) | R *(doc-health column descriptions overlay)* |
| `ownership` | W | — | — | — | — |
| `globalTags` | W | — | — | — *(future scope)* | R *(dataset_filter.tags)* |
| `glossaryTerms` | — | — | R | R | R *(dataset_filter.glossary_terms)* |
| `upstreamLineage` | W | R | — | — | — |
| `status` | W | W (assertion entity, on DELETE / resurrect) | — | — | — |
| `deprecation` | — | — | — | — | — |
| `datasetProfile` | — | — | — | — | — |
| `operation` | — | — | — | — | — |
| `datasetUsageStatistics` | — | — | — | — | — |
| `assertionInfo` | — | W | — | — | — |
| `assertionRunEvent` | — | W | — | — | — |
| `documentInfo` | — | — | R (documents whose `relatedAssets` overlap in-scope datasets) | R (read as generation context only) | — |

## User & Role Management

DataSpoke mirrors its own user accounts into DataHub as `corpuser` entities so
DataHub can attach metadata (ownership, group membership, role) to them.
**DataSpoke is the SSOT for role**; the DataHub-side role assignment is a
one-way mirror used by the DataHub UI for its own authorisation decisions.
Roles (`Admin` / `Editor` / `Reader`) propagate DataSpoke→DataHub via the
`batchAssignRole` GraphQL mutation; a nightly reconciliation DAG corrects
drift — see [Nightly Role Reconciliation](#nightly-role-reconciliation).
The full feature spec — lifecycle, OAuth, password reset, admin surface,
failure modes — lives in [feature/AUTH.md](feature/AUTH.md). This section
catalogues the DataHub-side primitives that AUTH consumes.

### URN Conventions

| Entity | URN | Notes |
|--------|-----|-------|
| User | `urn:li:corpuser:<email>` | Email-as-id form. Aligns with DataHub `AUTH_OIDC_USER_ID_CLAIM=email` so OIDC login resolves to the same URN DataSpoke wrote. |
| Group | `urn:li:corpGroup:<name>` | The marker-group name comes from `/admin/conf.auth_datahub_corp_group` (default `dataspoke-users`). |
| Role | `urn:li:dataHubRole:<Admin\|Editor\|Reader>` | Built-in DataHub role URNs. DataSpoke does not define custom roles. |

### Aspects DataSpoke Writes

| Entity | Aspect | When |
|--------|--------|------|
| corpuser | `corpUserInfo` | On user create; on display-name change. |
| corpGroup | `corpGroupInfo` | On marker-group lazy-create (first user registration if missing). |

DataSpoke **never** writes the `corpUserCredentials` aspect. DataHub native
password login is deliberately unused — DataHub UI access uses Google OIDC
SSO configured with the same Google client as DataSpoke (see
[feature/HELM_CHART.md §DataHub OIDC](feature/HELM_CHART.md)).

### Operations

#### Create corpuser

```python
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import CorpUserInfoClass

emitter.emit_mcp(MetadataChangeProposalWrapper(
    entityUrn=f"urn:li:corpuser:{email}",
    aspect=CorpUserInfoClass(active=True, email=email, displayName=name),
))
```

Idempotent — re-emit with the same data overwrites in place. The marker
corpGroup is re-asserted on every user registration by emitting both
`StatusClass(removed=False)` and `CorpGroupInfoClass(displayName=name,
members=[], admins=[], groups=[])`. Both aspects are required: a previous
partial bootstrap that committed only `Status` would leave the group
unresolvable to `addGroupMembers`, so DataSpoke always re-emits the pair.

#### Group membership

Add and remove via the GraphQL `addGroupMembers` / `removeGroupMembers`
mutations:

```python
graph.execute_graphql(
    "mutation($g: String!, $u: [String!]!) { addGroupMembers(input: {groupUrn: $g, userUrns: $u}) }",
    {"g": group_urn, "u": [corpuser_urn]},
)
```

Membership writes are idempotent.

#### Role assignment

```python
graph.execute_graphql(
    "mutation($r: String!, $u: [String!]!) { batchAssignRole(input: {roleUrn: $r, actors: $u}) }",
    {"r": "urn:li:dataHubRole:Reader", "u": [corpuser_urn]},
)
```

Default role on user creation is `Reader`. Admin role changes via
`PATCH /admin/users/{id}/role` use the same mutation with the requested role
URN.

#### Role read

DataHub stores role membership as the `RoleMembership` aspect on the
corpuser (atomic single-role per DataHub `RoleService`). Read it via the
SDK aspect read:

```python
graph.get_aspect(corpuser_urn, RoleMembershipClass)
```

The `IsMemberOfRole` GraphQL relationship index is **not** used —
it lags MCL→ES indexing and transiently shows roles that were already
overwritten in the aspect.

**Not used on the hot path.** Per-request privilege gating reads role from
DataSpoke `users.role` (see [feature/AUTH.md §Privilege Model](feature/AUTH.md#privilege-model)).
The aspect read here is used only by the nightly reconciliation DAG (see
below) to detect DataHub-side drift from the DataSpoke SSOT.

#### Hard delete

DataSpoke hard-deletes corpusers via the SDK's `hard_delete_entity` — removes
the entity together with all incoming and outgoing references (group
memberships, role assignments, ownership entries) from the DataHub metadata
graph. Reference: [DataHub delete-metadata.md §SDK and APIs](https://docs.datahub.com/docs/how/delete-metadata#deletes-using-the-sdk-and-apis).

```python
graph.hard_delete_entity(corpuser_urn)
```

Corp groups are long-lived and are not hard-deleted in the baseline flow;
soft-delete via `status.removed=true` is available if an operator needs to
retire a managed group.

### Nightly Role Reconciliation

DataSpoke is the SSOT for role. The DataHub-side mirror can drift if an
operator changes a corpuser's role directly in the DataHub UI rather than
via `PATCH /admin/users/{id}/role`. The Airflow DAG `auth-role-sync-daily`
detects and corrects this:

1. For each row in `users` (managed identities), read the corresponding
   corpuser's `RoleMembership` aspect directly (atomic single-role per
   DataHub `RoleService`). The `IsMemberOfRole` GraphQL relationship index
   is not used — it lags MCL→ES indexing and transiently shows roles that
   were already overwritten in the aspect.
2. If the observed role differs from DataSpoke `users.role`, re-assert
   `users.role` to DataHub via `batchAssignRole` (DataSpoke wins).
3. Emit an `AUTH.ROLE_SYNC_FIXED` event recording the divergence and the
   correction (event_type, user_id, datahub_role_observed,
   dataspoke_role_authoritative, occurred_at).

The auto-fix is intentional: any DataHub-side drift is by definition a
mistake to be corrected. The DAG iterates only rows in DataSpoke's
`users` table; DataHub-only corpusers (e.g., a super-admin not managed
by DataSpoke) are out of scope. Operators who need a DataHub-only role
assignment keep that corpuser out of the DataSpoke `users` table.

For large deployments, the per-user GraphQL fan-out is bounded (one query
per managed corpuser per day). A batched variant via `scrollAcrossEntities`
is an organisation-specific optimisation.

### Failure Handling

| Failure | DataSpoke behaviour |
|---------|---------------------|
| `emit_mcp` (corpuser create) | Compensating hard-delete of the DataSpoke `users` row; `503 DATAHUB_SYNC_FAILED` returned to the caller. |
| `batchAssignRole` during user create / role change | DataSpoke `users.role` write succeeds first; if the DataHub propagation fails, the DataSpoke side stays correct and the nightly DAG re-asserts. The admin caller is informed via a warning log; the API call returns `200` because DataSpoke-side state is intact. |
| `hard_delete_entity` (post-DataSpoke-row deletion) | DataSpoke row is already gone; orphan corpuser remains in DataHub for operator cleanup. Admin call returns `200`. |
| GraphQL role read during the reconciliation DAG | Skip-and-log the affected user; next nightly run retries. No user-facing impact (DataSpoke `users.role` is authoritative). |

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
| F. Enumerate datasets | `list(graph.get_urns_by_filter(entity_types=["dataset"], ...))` | GraphQL `scrollAcrossEntities` | Supports `platform`, `origin` (DataHub `FabricType` — `PROD`/`DEV`/`CORP`/`EI`/`STG`/`NON_PROD`/…), `tags`, `glossaryTerms`, and `query` filters; used by Governance `dataset_filter` resolution and Ontology Generation. |

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
rules), Metadata Generation (shared consumers informing descriptions), and Ontology
Generation (node and triple inference).

### Entity Enumeration by Domain

For cross-entity enumeration, call `graph.execute_graphql(...)` with
`scrollAcrossEntities` (`types: [DATASET]`) and paginate via `nextScrollId`. Used by
Ontology Generation, Metadata Generation, and Governance to resolve `dataset_filter`
(origin / tags / glossary_terms / explicit URNs) into the dataset URN list scanned
by each pipeline.

#### Origin filter group

`origin` in DataSpoke is the same `origin` field DataHub carries on every dataset —
defined on the `DatasetUrn` key (`li-utils/.../common/DatasetUrn.pdl`) with type
`com.linkedin.common.FabricType`, encoded into the dataset URN itself as
`urn:li:dataset:(<platform>,<name>,<origin>)`. Example:
`urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)`
has `origin="DEV"`.

DataSpoke accepts any value DataHub's `FabricType` enum accepts and forwards it
verbatim — no DataSpoke-side allow-list. The DataHub enum currently includes
`DEV`, `TEST`, `QA`, `UAT`, `EI`, `PRE`, `STG`, `NON_PROD`, `PROD`, `CORP`, `RVW`,
`PRD`, `TST`, `SIT`, `SBX`, `SANDBOX` (see `FabricType.pdl`). Unknown values are
rejected by DataHub at query time rather than by DataSpoke at PUT/PATCH time.

The resolver in `DataHubClient.enumerate_datasets` emits `origin` as its own
AND-clause within each `scrollAcrossEntities` `or` group so that DataHub combines
`origin` with the OR-ed `tags` / `glossaryTerms` / explicit-URN groups:

```
or: [
  { and: [{ field: "origin", value: "PROD" }, { field: "tags", value: "urn:li:tag:PII" }] },
  { and: [{ field: "origin", value: "PROD" }, { field: "glossaryTerms", value: "urn:li:glossaryTerm:..." }] },
  ...
]
```

When `origin` is absent the clause is omitted. When the OR-group dimensions are all
empty, `origin` becomes the single AND-clause and the enumeration returns every
dataset with that origin. Explicit `dataset_urns` are validated separately via
`get_aspect` and AND-ed against `origin` by checking the URN's third segment before
resolving the aspect.

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

DataHub connection parameters are stored in the DB `peripheral_config` table on the app pod
side, and as `DATASPOKE_TEST_DATAHUB_*` env vars in `helm-charts/.env` on the test/dev tooling
side (integration tests run only against dev clusters).

### App pod — `peripheral_config.datahub`

Updated via `PATCH /api/v1/admin/peripherals/datahub` (and the unattended mirror
`/internal/admin/peripherals/datahub`, used by the install script's
`helm-charts/bin/post-install/seed-peripheral-config.sh`). Fields: `gms_url`, `token`,
`kafka_brokers`. The pod reads from the DB at request time — no Helm-managed env var.

### Test / dev tooling — `helm-charts/.env`

| Variable | Purpose | Dev Source |
|----------|---------|------------|
| `DATASPOKE_TEST_DATAHUB_GMS_URL` | GMS endpoint read by integration tests, `tests/integration/util/datahub.py`, and the `datahub-api` skill | Written by `helm-charts/bin/peripherals/datahub.sh` (`http://datahub.<INGRESS_DOMAIN>/gms`) |
| `DATASPOKE_TEST_DATAHUB_TOKEN` | Personal access token; required because dev GMS runs with `METADATA_SERVICE_AUTH_ENABLED=true`. Generated by `helm-charts/bin/peripherals/datahub.sh` via the frontend `/logIn` + `createAccessToken` flow and written back to `.env`. | generated PAT |
| `DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS` | Kafka brokers for MCE/MAE events. **Optional** — only required when an organisation enables event-driven extensions; the baseline UC1–UC5 flows are schedule-driven via Airflow and do not subscribe to Kafka. | `<INGRESS_IP>:9005` |

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
