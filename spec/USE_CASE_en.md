# DataSpoke: Use Case Scenarios

> **Note on Document Purpose**
> This document presents conceptual scenarios for ideation and to seed integration test
> cases. Scenarios illustrate the intended capabilities of DataSpoke — they are not
> implementation specifications. Technical architecture and feature prioritization are
> defined in separate specs (`ARCHITECTURE.md`, `feature/*.md`). Where a scenario
> introduces concepts not yet reflected in lower-priority specs, a `(Lower-priority specs
> need follow-up)` note marks the gap.

This document demonstrates how DataSpoke realises the five features defined in
`MANIFESTO_en.md` §2.1: **Ingestion Control**, **Validation**, **Ontology Generation**,
**Metadata Generation**, and **Governance**. All scenarios share a single imaginary company —
**Imazon**, an online bookstore — so use cases coexist and reinforce each other.

User-group framing (Data Engineering / Data Analysis / Data Governance) remains as a
UI and API extensibility surface, but features are not partitioned by user group.

---

## Imaginary Company Profile: Imazon

Imazon is an online bookstore. Its data estate is small and healthy: it already runs on
DataHub. Imazon adopts DataSpoke not to recover from a legacy mess, but to gain more
visibility and one-place manageability over ingestion, quality, documentation, and
governance.

**Data sources used throughout this document**

- **PostgreSQL OLTP**
  - `catalog.books` — book catalog (one row per book)
  - `orders.line_items` — order items (one row per book in an order)
  - `customers.profiles` — registered customer profiles
- **Kafka topics**
  - `orders.shipments` — shipment events emitted by an external fulfillment service
  - `orders.events` — order state-change events emitted by the order service

Some datasets are ingested into DataHub by DataSpoke; others are ingested by external
pipelines that Imazon already operates. DataSpoke covers both modes.

**Feature mapping**

| # | MANIFESTO Feature | Use Case |
|---|---|---|
| UC1 | Ingestion Control | [Active-Custom and Passive Ingestion](#uc1-ingestion-control) |
| UC2 | Validation | [Single-rule Slot, Pipeline-Posted Results, Historical Baseline](#uc2-validation) |
| UC3 | Ontology Generation | [Node, Edge, and Triple Inference Across Imazon Datasets](#uc3-ontology-generation) |
| UC4 | Metadata Generation | [Per-Item Description Proposals](#uc4-metadata-generation) |
| UC5 | Governance | [Ingestion Freshness and Validation Score](#uc5-governance) |

---

## UC1: Ingestion Control

**MANIFESTO §2.1 feature**: *Ingestion Control — convenience functions for configuring,
controlling, and managing data ingestion in one place.*

### User Story

> *As a* data team member,
> *I want to* register, run, and observe ingestion for any dataset I care about — whether
> DataSpoke ingests it itself or some external system does —
> *so that* one DataSpoke surface drives ingestion config, runs, and event history for
> the whole estate.

Two ingestion modes are supported:

- **`active-custom`** — DataSpoke is the ingestor. An Airflow tier DAG runs an
  in-house extractor on the configured `schedule_tier` (`hourly` / `daily` /
  `weekly`) and emits results to DataHub. Manual runs and dry runs are also
  supported.
- **`passive`** — DataSpoke does not run the extractor. The user wires up
  extraction externally (DataHub Managed Ingestion, a one-off `acryl-datahub` SDK
  script, or any existing pipeline). DataSpoke registers the URN and observes
  per-run state through DataHub.

Supported `active-custom` platforms, the DataProcessInstance emission contract that
external ingestors must satisfy for per-run observability, and DataSpoke's hourly
passive-observation pipeline are specified in
[`BACKEND.md §Ingestion Service`](feature/BACKEND.md#ingestion-service-srcbackendingestion)
and
[`DATAHUB_INTEGRATION.md §Custom Ingestor Guide`](DATAHUB_INTEGRATION.md#custom-ingestor-guide).

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | Register, read, update, remove ingestion conf (`mode`, `platform`, `identifier`, plus `locator`/`auth`/`schedule_tier` for `active-custom`) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | Manual run (`dry_run: true` for connection check) — **`active-custom` configs only**; passive configs return `409 INGESTION_NOT_APPLICABLE` |
| `GET /spoke/common/data/{urn}/event/ingestion` | Per-dataset ingestion event history (active-custom: written by DataSpoke runs; passive: written by the hourly poll observing DataProcessInstance records in DataHub) |
| `GET /spoke/common/ingestion` | Cross-dataset list view aggregating per-dataset `attr/ingestion/*` |

Each `event/ingestion` row carries an `event_type` (`INGESTION.COMPLETE` on success,
`INGESTION.FAIL` on failure) and a matching `status` (`success` / `failure`).

### Imazon Examples

#### Case 1 — Active-custom, Postgres `catalog.title_master` (daily)

DataSpoke owns the extraction. An Airflow `ingestion-active-daily` DAG calls the
in-house Postgres extractor every day; manual runs are also possible.

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.title_master,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "active-custom",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "title_master"},
  "auth": {"username": "spoke_reader", "secret_ref": {"name": "dataspoke-source-cred-title-master", "key": "password"}},
  "is_enabled": true,
  "schedule_tier": "daily"
}
```

A coding agent verifies connectivity before turning the schedule on:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

Dry-run is also the only way to exercise `method/ingestion/run` while `is_enabled=false`; non-dry-run calls return `409 INGESTION_DISABLED`.

After the daily Airflow tier DAG runs, the team reads the per-dataset event history:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

Each row is backed by a `DataProcessInstance` aspect that DataSpoke's extractor emitted
to DataHub during the run, so the same record is also visible in DataHub's UI.

#### Case 2 — Passive, Postgres `catalog.reviews` via DataHub Managed Ingestion

The team wants column-level lineage and profile statistics that DataSpoke's in-house
extractor doesn't produce. They configure DataHub Managed Ingestion directly:
**at `http://datahub.<domain>/ingestion`**, create a postgres recipe targeting
`catalog.reviews` with a daily cron, and let DataHub's executor run it. DataSpoke does
not touch this configuration.

To make the dataset appear on DataSpoke's surface and pick up event history, register
it as passive:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.reviews,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "postgres",
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "reviews"},
  "is_enabled": true
}
```

No `locator`, `auth`, or `schedule_tier` — those belong to the external ingestor.
`POST .../method/ingestion/run` returns `409 INGESTION_NOT_APPLICABLE` for this URN.

After DataHub's executor finishes a run, DataSpoke's hourly poll surfaces an event:

```http
GET .../event/ingestion?from=…&to=…
```
```json
{
  "events": [
    {
      "event_type": "INGESTION.COMPLETE",
      "status": "success",
      "occurred_at": "2026-04-25T03:14:00Z",
      "detail": {"source": "passive", "datahub_status": "SUCCEEDED", "run_id": "..."}
    }
  ]
}
```

#### Case 3 — Passive, Kafka `imazon.orders.events` via custom one-time script

Imazon needs to load metadata for a Kafka topic from a one-off context: a developer
runs a Python script using the `acryl-datahub` SDK that emits Status, SchemaMetadata,
and a `DataProcessInstance` per invocation. The script lives outside DataSpoke and is
not scheduled.

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "identifier": {"topic": "orders.events", "cluster": "PROD"},
  "is_enabled": true
}
```

When the script runs and emits a DPI, the next hourly poll surfaces a row in
`event/ingestion` exactly as in Case 2.

#### Cross-dataset overview

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

Returns one row per dataset with its full `attr/ingestion/*` aggregate (mode, schedule
where applicable, last event status). Useful for dashboards and bulk audit.

---

## UC2: Validation

**MANIFESTO §2.1 feature**: *Validation — one validation slot per dataset (description +
declared variable names) plus ingestion of pipeline-emitted timeseries results.
Validation logic lives in the data pipeline; DataSpoke stores the configuration and the
result timeseries, emits the matching DataHub assertion aspects, and serves historical
results as a baseline cache.*

### User Story

> *As a* data team member,
> *I want to* configure one validation rule per dataset (a free-form description and the
> set of variables my pipeline will report), have my pipeline POST results to DataSpoke
> after each partition write, and query historical results as a baseline for future runs,
> *so that* data quality results are centralized and surfaced in DataHub without
> DataSpoke needing production-engine credentials.

A `validation/conf` is a small fixed-shape document — a free-form `description` and a
list of `variables` (named scalars the pipeline will report). The configuration carries
**no** rule logic; the data pipeline runs the check, computes a `score` (0..1) plus
the named variables, and POSTs them. DataSpoke stores the result, emits a DataHub
`assertionRunEvent`, and serves the historical timeseries.

Teams that need multiple distinct checks per dataset (separate freshness / volume /
field assertions, per-column validators, multi-team ownership) use **DataHub's native
assertion APIs** directly — DataSpoke is the opinionated single-rule shortcut for the
80% case, not the only path. The full contract — conf pre-conditions, result row
shape, soft-delete / resurrect semantics, and DataHub assertion-aspect emission —
lives in [`spec/feature/VALIDATION.md`](feature/VALIDATION.md).

### API Mapping

| Endpoint | Used for |
|---|---|
| `GET/PUT/PATCH/DELETE /spoke/common/data/{urn}/attr/validation/conf` | Read / create-or-replace / partial-update / soft-delete the validation slot (`description` + `variables`). PUT for a URN absent from DataHub returns `422 DATASET_NOT_IN_DATAHUB` |
| `POST /spoke/common/data/{urn}/attr/validation/result` | Append a result `{data_time, score, variables}`. Unknown variable keys → `422 UNKNOWN_VARIABLE`; `score` outside `[0,1]` → `422 INVALID_SCORE` |
| `GET /spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…` | Historical results filtered by `data_time` (RFC 3339, `from` inclusive, `until` exclusive). Default `limit=1000`, server cap `10000` |
| `GET /spoke/common/data/{urn}/event/validation` | Per-dataset validation event history |
| `GET /spoke/common/validation` | Cross-dataset list with conf (description + variable names) + latest result (data_time, score) |

### Imazon Example

The orders team configures one validation slot on `orders.line_items` declaring the
variables their daily quality task will report:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)/attr/validation/conf
```
```json
{
  "description": "Daily fitness check: row count, quantity sanity, key column nulls",
  "variables": ["row_cnt", "qty_negative_cnt", "qty_total", "user_id_null_cnt"]
}
```

**Pipeline-emitted result.** The same Airflow DAG that writes the daily partition runs
the team's quality task immediately after, computes the four variables, and POSTs:

```http
POST .../attr/validation/result
```
```json
{
  "data_time": "2026-05-08T00:00:00Z",
  "score": 1.0,
  "variables": {
    "row_cnt": 12480.0,
    "qty_negative_cnt": 0.0,
    "qty_total": 38712.0,
    "user_id_null_cnt": 0.0
  }
}
```

The result appears in the DataHub Quality tab as an `assertionRunEvent` timestamped to
`data_time`. A failed score (anything `< 1.0`) flips the assertion to `FAILURE` in
DataHub's UI; the raw score is preserved in `actualAggValue` for partial-success
semantics later.

A second slot is configured on the Kafka topic `orders.events` (different platform,
different variables), so the same surface covers both relational and streaming
sources.

**Historical baseline cache.** Tomorrow's quality task computes today's row-count
anomaly against a 14-day rolling baseline. Instead of re-aggregating
`orders.line_items`, it issues:

```http
GET .../attr/validation/result?from=2026-04-24T00:00:00Z&until=2026-05-08T00:00:00Z
```

and uses the prior `row_cnt` series directly. Results are returned newest first
(descending `data_time`).

**Retire and resurrect.** `DELETE attr/validation/conf` soft-deletes the slot
(returns `204`; subsequent `GET conf` returns `404`). Re-issuing `PUT` on the same
URN reinstates it (returns `201`) and the resurrected slot may carry a new
description and variable set.

**Cross-dataset overview.** `GET /spoke/common/validation` lists each dataset's
`description`, `variable_count`, `latest_data_time`, `latest_score`, and
`is_removed`. The list accepts `?removed=true|false` to include or exclude
soft-deleted slots.

---

## UC3: Ontology Generation

**MANIFESTO §2.1 feature**: *Ontology Generation — autonomously constructs an ontology
from DataHub-resident metadata, maintained in a graph DB and a vector DB inside
DataSpoke.*

### User Story

> *As an* analyst or governance member,
> *I want* DataSpoke to autonomously infer the business concepts (subjects and objects),
> the relationship types (predicates), and the specific facts (triples) that connect them
> across my datasets,
> *so that* I can navigate datasets by concept, browse meaningful relationships, and
> review each layer before it is accepted.

The baseline ontology follows the **subject / predicate / object triple model**, with
three independently reviewable result types:

- **Node** — a *subject* or *object*: a business concept rooted in one or more datasets
  (e.g., `BOOK`, `CUSTOMER`).
- **Edge** — a *predicate*: a relationship type (e.g., `references`, `placed_by`).
- **Triple** — a `(subject_node, edge, object_node)` fact. A triple may only be
  composed of pre-approved nodes and edges, so the conceptual vocabulary is approved
  once and reused across many specific facts.

Node and edge IDs are slugs (`book`, `placed_by`); a triple ID is the composite
slug `subject_node_id__edge_id__object_node_id` (e.g.,
`order_line__references__book`), so the ID itself encodes the fact.

The ontology is a global artifact. A singleton operational conf at
`/spoke/common/ontogen/attr/conf` controls when the inference DAG runs and which
datasets are in scope. Human-authored Markdown **seeds** (prompts, domain hints,
naming conventions) steer the LLM alongside the data sources, and a manual
`POST /method/run` may carry an inline Markdown body as a one-shot prompt for that
single run.

A triple may only be human-approved once both its endpoint nodes and its edge are
themselves human-approved, so reviewers typically process **nodes → edges →
triples**.

Conf field semantics, seed lifecycle, the inference pipeline and its incremental
reuse rules, run semantics (`dry_run`, concurrency, prompt fallback to
`default_run_prompt`), and the triple review-dependency contract are specified in
[`BACKEND.md §Ontology Generation Service`](feature/BACKEND.md#ontology-generation-service-srcbackendontogen).
The producer / reviewer adversarial-debate inference loop is in
[`BACKEND_LLM.md §Adversarial Debate Framework`](feature/BACKEND_LLM.md#adversarial-debate-framework).

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/ontogen/attr/conf` | Singleton operational conf — see field table above |
| `GET /spoke/common/ontogen/attr/seed` | List seeds — `[{seed_id, updated_at, preview}]` (Markdown bodies fetched per-seed below) |
| `POST /spoke/common/ontogen/attr/seed` | Create an inference seed — body is a raw Markdown document (`Content-Type: text/markdown`); server assigns `seed_id` |
| `GET/PATCH/DELETE /spoke/common/ontogen/attr/seed/{seed_id}` | Read, refine, or retire a seed |
| `POST /spoke/common/ontogen/method/run` | Trigger a manual re-inference. Optional `Content-Type: text/markdown` body acts as a one-shot prompt for this run; `?dry_run=true` evaluates without persisting. Concurrent runs return `409 ONTOGEN_RUNNING` |
| `GET /spoke/common/ontogen/event` | Global inference-run history (`ONTOGEN.RUN_COMPLETE`, `ONTOGEN.RUN_FAILED`) |
| `GET /spoke/common/ontogen/result/node` | List nodes (subjects / objects) with confidence and status |
| `GET /spoke/common/ontogen/result/node/{node_id}` | Node detail incl. member datasets |
| `GET /spoke/common/ontogen/result/node/{node_id}/attr` | Node attributes (confidence, source evidence) |
| `GET /spoke/common/ontogen/result/node/{node_id}/event` | Node-level change history (proposed → approved / rejected, member additions) |
| `POST /spoke/common/ontogen/result/node/{node_id}/method/review` | Approve or reject a pending node |
| `GET /spoke/common/ontogen/result/edge` | List edges (predicates) with confidence and status |
| `GET /spoke/common/ontogen/result/edge/{edge_id}` | Edge detail |
| `GET /spoke/common/ontogen/result/edge/{edge_id}/attr` | Edge attributes (confidence, source evidence) |
| `GET /spoke/common/ontogen/result/edge/{edge_id}/event` | Edge-level change history |
| `POST /spoke/common/ontogen/result/edge/{edge_id}/method/review` | Approve or reject a pending edge |
| `GET /spoke/common/ontogen/result/triple` | List triples — `(subject_node_id, edge_id, object_node_id)` facts — with confidence and status |
| `GET /spoke/common/ontogen/result/triple/{triple_id}` | Triple detail (resolved subject node, edge, object node) |
| `GET /spoke/common/ontogen/result/triple/{triple_id}/attr` | Triple attributes (confidence, source evidence) |
| `GET /spoke/common/ontogen/result/triple/{triple_id}/event` | Triple-level change history |
| `POST /spoke/common/ontogen/result/triple/{triple_id}/method/review` | Approve or reject a pending triple — returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` if any of subject node, edge, or object node is not yet approved |

### Imazon Example

**Conf.** The governance team enables ontology generation:

```http
PUT /api/v1/spoke/common/ontogen/attr/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "dataset_filter": {"tags": ["urn:li:tag:env:PROD"]}
}
```

**Seed.** They post a domain seed (Markdown) to steer the LLM toward bookstore-friendly names:

```http
POST /api/v1/spoke/common/ontogen/attr/seed
Content-Type: text/markdown
```
```markdown
# Imazon Bookstore Domain

Imazon is an online bookstore. Treat *order* as a header concept and *order line* as
the per-book row. Prefer business-friendly names over table names.
```

**Inferred output.** Three nodes, two edges, two triples — each row's `status` is
either `llm_approved` (high confidence) or `llm_pending` (awaiting human review):

```
Nodes (subjects / objects):
  BOOK         confidence 0.96   member: catalog.books         (primary)
  CUSTOMER     confidence 0.94   member: customers.profiles    (primary)
  ORDER_LINE   confidence 0.71   member: orders.line_items     (primary)
    evidence:
      - foreign key book_id → catalog.books.book_id (schemaMetadata)
      - column-level FK customer_id → customers.profiles.customer_id (schemaMetadata)

Edges (predicates):
  references   confidence 0.95   semantics: foreign-key reference
  placed_by    confidence 0.87   semantics: agent / actor

Triples (subject — predicate — object):
  ORDER_LINE  --references--> BOOK       confidence 0.95
  ORDER_LINE  --placed_by --> CUSTOMER   confidence 0.87
```

**Review flow — nodes first.** `ORDER_LINE` has the lowest node confidence (0.71, due
to LLM ambiguity between "order" and "line item"), so the reviewer starts with nodes:

```http
GET /api/v1/spoke/common/ontogen/result/node
GET /api/v1/spoke/common/ontogen/result/node/order_line
GET /api/v1/spoke/common/ontogen/result/node/order_line/event
POST /api/v1/spoke/common/ontogen/result/node/order_line/method/review
```
```json
{ "verdict": "approve", "reason": "Confirmed FK structure; rename later if needed." }
```

**Edges next.** With the nodes approved, the reviewer moves to edges:

```http
GET /api/v1/spoke/common/ontogen/result/edge
POST /api/v1/spoke/common/ontogen/result/edge/references/method/review
POST /api/v1/spoke/common/ontogen/result/edge/placed_by/method/review
```

**Triples last.** Once both endpoint nodes and the edge of a triple are approved, the
triple becomes eligible for review:

```http
GET /api/v1/spoke/common/ontogen/result/triple
POST /api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review
```

Approval marks the entry as approved in DataSpoke storage.

---

## UC4: Metadata Generation

**MANIFESTO §2.1 feature**: *Metadata Generation — based on the ontology, inspects the
state of data documentation and proposes metadata via generative AI, including APIs and
a review process.*

This feature proposes values for **editable description aspects** that already exist
in DataHub metadata — one for the dataset, one per column. It does **not** propose
ontology structure (UC3 owns that). Generation is grounded in the same proofread
DataHub aspect set UC3 reads, plus the UC3-approved ontology as additional context.

### User Story

> *As a* dataset owner or governance reviewer,
> *I want* DataSpoke to propose documentation candidates for under-documented
> datasets and let me browse several alternatives per slot, approve one, and reject
> the ones that miss,
> *so that* documentation coverage improves without me writing every description
> by hand and I can still steer the wording.

**Supported documentation fields in baseline**

DataSpoke writes only to **editable** DataHub aspects. The non-editable counterparts
(`datasetProperties.description`, `schemaMetadata.fields[].description`) are reserved
for ingestion connectors; writing to them risks the next connector run overwriting
human-approved text. DataHub treats both editable description fields as rich text and
the UI renders Markdown.

| Item kind | Format | DataHub target |
|---|---|---|
| `dataset.description` | Markdown | `editableDatasetProperties.description` |
| `column.<fieldPath>.description` | Markdown | `editableSchemaMetadata.editableSchemaFieldInfo[].description` keyed by `fieldPath` |

Future scope (mentioned, not modelled here): proposals for `domains` and
`globalTags`.

A **global** operational conf at `/spoke/common/metagen/attr/conf` controls when the
generation DAG runs and which datasets are in scope. A **per-dataset** boundary at
`/spoke/common/data/{urn}/attr/metagen/conf` is the opt-in switch — datasets without
an `is_enabled=true` boundary row are excluded regardless of the global filter.

For each in-scope (dataset, item) pair the generator accumulates up to
`result_limit` candidates across runs (default `3`). The reviewer browses
candidates, approves one (which emits the value to the editable DataHub aspect
and locks the item), and rejects the misses. **Approval is mutable**: approving
a different sibling atomically demotes the previously-approved candidate, so the
reviewer can change their mind at any time. **On subsequent runs**, items with
an `approved` candidate are skipped entirely; rejected candidates are cleared
at the start of the next run so the item is re-proposed from scratch.

Conf field semantics, candidate status lifecycle, per-item eviction policy, run
pipeline, and the producer / reviewer adversarial debate are specified in
[`BACKEND.md §Metadata Generation Service`](feature/BACKEND.md#metadata-generation-service-srcbackendmetagen)
and
[`BACKEND_LLM.md §Metagen Adversarial Debate`](feature/BACKEND_LLM.md#metagen-adversarial-debate).

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/metagen/attr/conf` | Singleton operational conf — see field table above |
| `POST /spoke/common/metagen/method/run` | Trigger a manual generation run. Optional body `{"dataset_urns": [...], "dry_run": bool}`. Concurrent runs return `409 METAGEN_RUNNING`; disabled-conf non-dry-run returns `409 METAGEN_DISABLED` |
| `GET /spoke/common/metagen/event` | Global generation-run event history (`METAGEN.RUN_COMPLETE`, `METAGEN.RUN_FAILED`) |
| `GET /spoke/common/metagen/item` | List items across datasets (paginated; filterable by `dataset_urn`, `kind`, `status`) |
| `GET /spoke/common/metagen/item/{composite_id}` | Item detail by composite id `{dataset_urn}::{item_id}`, including every candidate |
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/metagen/conf` | Per-dataset boundary (`is_enabled`, `allowed`) |
| `GET /spoke/common/data/{urn}/attr/metagen/item` | List items for one dataset |
| `GET /spoke/common/data/{urn}/attr/metagen/item/{item_id}` | One item with all candidates |
| `POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review` | Approve or reject one candidate — body `{ "verdict": "approve"\|"reject", "reason": "…" }`. Approve emits to DataHub and locks the item |
| `GET /spoke/common/data/{urn}/event/metagen` | Per-dataset metagen events (`METAGEN.CANDIDATE_APPROVE`, `METAGEN.CANDIDATE_REJECT`) |

### Imazon Example

**Conf.** The governance team enables metagen globally:

```http
PUT /api/v1/spoke/common/metagen/attr/conf
```
```json
{
  "is_enabled": true,
  "schedule_tier": "daily",
  "dataset_filter": {"tags": ["urn:li:tag:env:PROD"]},
  "result_limit": 3,
  "overwrite_pending": true
}
```

**Boundary.** The catalog team opts `catalog.books` in for both kinds:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/metagen/conf
```
```json
{
  "is_enabled": true,
  "allowed": ["dataset.description", "column.description"]
}
```

**Run.** The daily Airflow DAG fires, or a reviewer triggers an immediate run:

```http
POST /api/v1/spoke/common/metagen/method/run
```

**Browse items.** After the run, the catalog dashboard lists the dataset's items:

```http
GET /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/metagen/item
```

Returns one `dataset.description` item plus one `column.<fieldPath>.description` item
per column. Inspecting the dataset description item:

```http
GET .../attr/metagen/item/dataset.description
```

```
item_id: dataset.description
kind:    dataset.description
status:  pending           # no approved candidate yet
candidates (3 of result_limit=3):
  - candidate_id: c1   status: llm_approved   confidence 0.92
      "# Books\n\nMaster catalog of every title Imazon offers..."
  - candidate_id: c2   status: llm_approved   confidence 0.88
      "# Catalog: Books\n\nThe authoritative book catalog..."
  - candidate_id: c3   status: llm_approved   confidence 0.85
      "Books table — Imazon's primary title catalog..."
```

**Review.** The reviewer approves `c1`, rejects `c3`, and leaves `c2` as-is:

```http
POST .../attr/metagen/item/dataset.description/candidate/c1/method/review
{ "verdict": "approve", "reason": "Best framing of the catalog role." }

POST .../attr/metagen/item/dataset.description/candidate/c3/method/review
{ "verdict": "reject", "reason": "Truncated and lacks the key fact." }
```

On `c1`'s approve call, DataSpoke writes the value to
`editableDatasetProperties.description` on the dataset; the item now reports
`status: approved`. `c2` stays `llm_approved` as visible history, eligible for
later approval if the reviewer changes their mind (approving `c2` would
atomically demote `c1`). `c3` will be deleted at the start of the next run.

**Event history.**

```http
GET .../event/metagen
```

---

## UC5: Governance

**MANIFESTO §2.1 feature**: *Governance — APIs for configuring and monitoring governance
metrics such as documentation coverage and data freshness.*

### User Story

> *As a* governance lead or CDO,
> *I want* a small set of always-on signals — ingestion freshness and validation score —
> and one overview that shows them at a glance,
> *so that* I can monitor health without curating dashboards by hand.

**Baseline metrics**

The baseline ships with two metrics; organisations register additional metrics by
defining new `measurement_query` types via the same `attr/conf` endpoint.

| Metric ID | Definition |
|---|---|
| `ingestion-freshness` | Percentage of enabled ingestion configs whose latest successful `event/ingestion` falls within the configured freshness window (per `schedule_tier` for active-custom mode; per a fixed window for passive). |
| `validation-score` | Percentage of datasets whose latest `attr/validation/result` row has `score == 1.0`, among datasets that have a validation conf. |

Every measurement run persists one `attr/result` row carrying both an aggregate
`value` and a per-dataset `breakdown`, so time-range queries can answer "which
datasets failed last Tuesday" without re-running the metric. Run semantics
(serialization, dry-run, disabled-conf rejection) and breakdown shape are
specified in [`BACKEND.md §Metrics Service`](feature/BACKEND.md#metrics-service-srcbackendmetrics).

**Baseline overview (one)**

A single dashboard returns the latest value of every enabled metric, a per-dataset
breakdown (which datasets are stale, which have failing rules), and **blind spots** —
datasets present in DataHub but not mapped to any UC3 ontology node. Blind spots
are governance signals in their own right: they surface coverage gaps where the
ontology has not yet caught up with the data estate.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/dg/metric/{metric_id}/attr/conf` | Define / update / read a metric (title, theme, query, schedule_tier, enabled flag) |
| `POST /spoke/dg/metric/{metric_id}/method/run` | Trigger a measurement run; `dry_run: true` evaluates without persisting. Concurrent runs on the same metric return `409 METRIC_RUNNING` |
| `GET /spoke/dg/metric/{metric_id}/attr/result?from=…&to=…` | Timeseries of past measurements (each row carries both aggregate `value` and per-dataset `breakdown`) |
| `GET /spoke/dg/metric/{metric_id}/event` | Run completion / definition change events |
| `GET /spoke/dg/metric` | List all metrics |
| `GET /spoke/dg/overview` | Snapshot — every enabled metric value + per-dataset breakdown + blind spots (datasets unmapped to any ontology node) |
| `GET/PATCH /spoke/dg/overview/attr` | Read or update visualization config |

### Imazon Example

The CDO registers both metrics:

```http
PUT /api/v1/spoke/dg/metric/ingestion-freshness/attr/conf
```
```json
{
  "title": "Ingestion freshness",
  "theme": "freshness",
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_fresh"},
  "schedule_tier": "hourly",
  "is_enabled": true
}
```

```http
PUT /api/v1/spoke/dg/metric/validation-score/attr/conf
```
```json
{
  "title": "Validation score",
  "theme": "quality",
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_datasets_passing"},
  "schedule_tier": "hourly",
  "is_enabled": true
}
```

The CDO triggers an immediate first run rather than waiting for the schedule:

```http
POST /api/v1/spoke/dg/metric/ingestion-freshness/method/run
POST /api/v1/spoke/dg/metric/validation-score/method/run
```

A week later, trends are pulled for a board update:

```http
GET /api/v1/spoke/dg/metric/ingestion-freshness/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
GET /api/v1/spoke/dg/metric/validation-score/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

The dashboard view consumes the overview endpoint:

```http
GET /api/v1/spoke/dg/overview
```

…and returns both metric values plus a per-dataset breakdown grouping `catalog.books`,
`orders.line_items`, `customers.profiles`, `orders.shipments`, and `orders.events` by
their freshness and validation status, alongside any blind spots — datasets visible in
DataHub that have not yet been mapped to a UC3 node.
