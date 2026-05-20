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

- **PostgreSQL OLTP** (database `example_db`, fabric `DEV`)
  - `catalog.title_master` — book master, one row per title (ISBN PK)
  - `catalog.editions` — per-format edition rows that join to `title_master` on ISBN
  - `customers.eu_profiles` — EU customer accounts (GDPR PII surface)
  - `reviews.user_ratings` — ratings linking customers to editions
  - `orders.daily_fulfillment_summary` — daily fulfillment quality aggregates
  - `shipping.carrier_status` — carrier scan events keyed by `order_id`
- **Kafka topics** (cluster `example_kafka`)
  - `imazon.orders.events` — order state-change events emitted by the order service
  - `imazon.shipping.updates` — shipment events emitted by the fulfillment service

Some datasets are ingested into DataHub by DataSpoke; others are ingested by external
pipelines that Imazon already operates. DataSpoke covers both modes.

**Feature mapping**

| # | MANIFESTO Feature | Use Case |
|---|---|---|
| UC1 | Ingestion Control | [Active-Custom and Passive Ingestion](#uc1-ingestion-control) |
| UC2 | Validation | [Single-rule Slot, Pipeline-Posted Results, Historical Baseline](#uc2-validation) |
| UC3 | Ontology Generation | [Node, Edge, and Triple Inference Across Imazon Datasets](#uc3-ontology-generation) |
| UC4 | Metadata Generation | [Per-Item Description Proposals](#uc4-metadata-generation) |
| UC5 | Governance | [Active Metrics — Freshness, Validation, Doc Health](#uc5-governance) |

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
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "active-custom",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "example_db", "schema_name": "catalog", "table": "title_master"},
  "auth": {"username": "spoke_reader", "secret_ref": {"name": "dataspoke-source-cred-title-master", "key": "password"}},
  "is_enabled": false,
  "schedule_tier": "daily"
}
```

A coding agent verifies connectivity before turning the schedule on:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

Dry-run is the only way to exercise `method/ingestion/run` while `is_enabled=false`; non-dry-run calls return `409 INGESTION_DISABLED`. Once the dry-run succeeds, the team flips the switch:

```http
PATCH .../attr/ingestion/conf    { "is_enabled": true }
```

After the daily Airflow tier DAG runs, the team reads the per-dataset event history:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

Each row is backed by a `DataProcessInstance` aspect that DataSpoke's extractor emitted
to DataHub during the run, so the same record is also visible in DataHub's UI.

#### Case 2 — Passive, Postgres `catalog.editions` via DataHub Managed Ingestion

The team wants column-level lineage and profile statistics that DataSpoke's in-house
extractor doesn't produce. They configure DataHub Managed Ingestion directly:
**at `http://datahub.<domain>/ingestion`**, create a postgres recipe targeting
`catalog.editions` with a daily cron, and let DataHub's executor run it. DataSpoke does
not touch this configuration.

To make the dataset appear on DataSpoke's surface and pick up event history, register
it as passive:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "postgres",
  "identifier": {"database": "example_db", "schema_name": "catalog", "table": "editions"},
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
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "identifier": {"topic": "imazon.orders.events", "cluster": "example_kafka"},
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

The orders team configures one validation slot on `orders.daily_fulfillment_summary`
declaring the variables their daily quality task will report:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)/attr/validation/conf
```
```json
{
  "description": "Daily order fulfillment quality: row count, fill rate, and anomaly score",
  "variables": ["row_cnt", "fill_rate", "anomaly_score"]
}
```

**Pipeline-emitted result.** The same Airflow DAG that writes the daily partition runs
the team's quality task immediately after, computes the three variables, and POSTs:

```http
POST .../attr/validation/result
```
```json
{
  "data_time": "2026-05-01T00:00:00Z",
  "score": 1.0,
  "variables": {
    "row_cnt": 1250.0,
    "fill_rate": 0.98,
    "anomaly_score": 0.02
  }
}
```

The result appears in the DataHub Quality tab as an `assertionRunEvent` timestamped to
`data_time`. A failed score (anything `< 1.0`) flips the assertion to `FAILURE` in
DataHub's UI; the raw score is preserved in `actualAggValue` for partial-success
semantics later.

A second slot is configured on the Kafka topic `imazon.orders.events` with
`description: "Order events stream quality: message count and lag"` and
`variables: ["msg_cnt", "lag_seconds"]`, so the same surface covers both relational
and streaming sources.

**Historical baseline cache.** Tomorrow's quality task computes today's row-count
anomaly against a 30-day rolling baseline. Instead of re-aggregating
`orders.daily_fulfillment_summary`, it issues:

```http
GET .../attr/validation/result?from=2026-04-01T00:00:00Z&until=2026-05-01T00:00:00Z
```

and uses the prior `row_cnt` series directly. Results are returned newest first
(descending `data_time`).

**Retire and resurrect.** `DELETE attr/validation/conf` soft-deletes the slot
(returns `204`; subsequent `GET conf` returns `404`). Re-issuing `PUT` on the same
URN reinstates it (returns `201`) and the resurrected slot may carry a new
description and variable set — e.g.
`variables: ["row_cnt", "fill_rate", "anomaly_score", "null_rate"]`.

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
  (e.g., `TITLE`, `CUSTOMER`).
- **Edge** — a *predicate*: a relationship type (e.g., `rates`, `is_edition_of`).
- **Triple** — a `(subject_node, edge, object_node)` fact. A triple may only be
  composed of pre-approved nodes and edges, so the conceptual vocabulary is approved
  once and reused across many specific facts.

Node and edge IDs are slugs (`title`, `rates`); a triple ID is the composite
slug `subject_node_id__edge_id__object_node_id` (e.g.,
`edition__is_edition_of__title`), so the ID itself encodes the fact.

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
  "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]}
}
```

**Seed.** They post a domain seed (Markdown) to steer the LLM toward bookstore-friendly names:

```http
POST /api/v1/spoke/common/ontogen/attr/seed
Content-Type: text/markdown
```
```markdown
# Imazon Bookstore Domain

Imazon is an online retailer specialising in books. Each title is identified by an
ISBN-13 and is sold in multiple formats (Hardcover, Paperback, eBook, Audiobook) as
distinct editions. Customers may submit ratings tied to a specific edition. Prefer
business-domain language over warehouse schema names whenever both are available.
```

**Inferred output.** Four nodes, two edges, two triples — each row's `status` is
either `llm_approved` (high confidence) or `llm_pending` (awaiting human review):

```
Nodes (subjects / objects):
  TITLE      confidence 0.96   member: catalog.title_master   (primary)
  EDITION    confidence 0.94   member: catalog.editions       (primary)
  CUSTOMER   confidence 0.93   member: customers.eu_profiles  (primary)
  RATING     confidence 0.72   member: reviews.user_ratings   (primary)
    evidence:
      - foreign key edition_id → catalog.editions.edition_id (schemaMetadata)
      - foreign key user_id → customers.eu_profiles.user_id (schemaMetadata)

Edges (predicates):
  is_edition_of  confidence 0.95   semantics: format-of relationship
  rates          confidence 0.87   semantics: customer-rates-edition

Triples (subject — predicate — object):
  EDITION  --is_edition_of--> TITLE      confidence 0.95
  RATING   --rates         --> EDITION   confidence 0.87
```

**Review flow — nodes first.** `RATING` has the lowest node confidence (0.72, due to
LLM ambiguity between "rating" and "review"), so the reviewer starts with nodes:

```http
GET /api/v1/spoke/common/ontogen/result/node
GET /api/v1/spoke/common/ontogen/result/node/rating
GET /api/v1/spoke/common/ontogen/result/node/rating/event
POST /api/v1/spoke/common/ontogen/result/node/rating/method/review
```
```json
{ "verdict": "approve", "reason": "Confirmed FK structure; rename later if needed." }
```

**Edges next.** With the nodes approved, the reviewer moves to edges:

```http
GET /api/v1/spoke/common/ontogen/result/edge
POST /api/v1/spoke/common/ontogen/result/edge/is_edition_of/method/review
POST /api/v1/spoke/common/ontogen/result/edge/rates/method/review
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
  "dataset_filter": {"tags": ["urn:li:tag:area:fulfillment"]},
  "result_limit": 3,
  "overwrite_pending": true
}
```

**Boundary.** The customer team opts `customers.eu_profiles` in for both kinds, and
the orders team opts `imazon.orders.events` in for column descriptions only:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)/attr/metagen/conf
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

**Browse items.** After the run, the dashboard lists the dataset's items:

```http
GET /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)/attr/metagen/item
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
      "# EU Customer Profiles\n\nGDPR-scoped customer accounts for the EU region..."
  - candidate_id: c2   status: llm_approved   confidence 0.88
      "# Customers (EU)\n\nAuthoritative profile records for EU customers..."
  - candidate_id: c3   status: llm_approved   confidence 0.85
      "EU profiles table — registered customer accounts under EU jurisdiction..."
```

**Review.** The reviewer approves `c1`, rejects `c3`, and leaves `c2` as-is:

```http
POST .../attr/metagen/item/dataset.description/candidate/c1/method/review
{ "verdict": "approve", "reason": "Best framing of the EU/GDPR scope." }

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
> *I want* a small set of always-on metrics — ingestion freshness, validation score, and
> documentation health — that I can schedule, scope, and trend over time,
> *so that* I can monitor estate health without curating dashboards by hand.

### Concept

A governance **metric** is a named, scheduled aggregation over the data estate. Every
measurement run persists a result row whose `values` are a dict of named floats and
whose `breakdown` is a per-dataset list, so time-range queries can answer "which
datasets failed last Tuesday" without re-running the metric.

**Modes.** A metric's `mode` is either `active` or `passive`.

- **`active`** — DataSpoke computes the measurement itself from the metric's
  `metric_type` and `metric_conf`.
- **`passive`** — DataSpoke ingests measurement results emitted by an external system
  (no built-in computation). **Reserved for future work; PUT with `mode: "passive"`
  returns `501 NOT_IMPLEMENTED` in this release.**

### Built-in active metric types

Three `metric_type` values ship in the baseline. All emit floating-point values; ratios
are NOT pre-computed by the server — clients derive them from the named fields.

| `metric_type` | Emitted `values` keys | Meaning of each key |
|---|---|---|
| `ingestion-freshness` | `total`, `ingested_in_time` | `total` = count of datasets matched by `dataset_filter`; `ingested_in_time` = count whose latest `INGESTION.COMPLETE` was less than `metric_conf.time_window_sec` ago |
| `validation-score` | `total`, `validation_score_sum` | `total` = count of datasets matched by `dataset_filter`; `validation_score_sum` = sum of each dataset's latest validation `score` in the time range from `metric_conf.time_window_sec` ago to now (0.0 when the dataset has no validation result inside that window) |
| `doc-health` | `total`, `doc_health` | `total` = count of datasets matched by `dataset_filter`; `doc_health` = sum of per-dataset documentation scores, where a dataset scores `1.0` iff it has a non-empty table description AND every column carries a non-empty description, else `0.0` |

`metric_conf` carries type-specific parameters: `time_window_sec` for
`ingestion-freshness` and `validation-score`; empty `{}` for `doc-health`.

`dataset_filter` carries four optional dimensions: `origin` (the DataHub `FabricType`
value carried as the third URN segment — `PROD` / `DEV` / `CORP` / `EI` / `STG` /
`NON_PROD` / `QA` / `TEST` / `PRE` / `RVW` / `SIT` / `SANDBOX` / …; passed through to
DataHub verbatim), `tags` (DataHub tag URNs), `glossary_terms` (DataHub glossary term
URNs), and `dataset_urns` (explicit `urn:li:dataset:(…)` URNs). The tag / term / URN
dimensions form an OR-group; `origin` is AND-ed with that group. `{}` means all
datasets. URN format is validated at PUT/PATCH (`422 INVALID_DATASET_URN`);
unresolved-at-runtime entries are skipped and reported in the `METRIC.RUN_COMPLETE`
event's `unresolved_urns` field. The GraphQL shape of the resolver is documented in
[`DATAHUB_INTEGRATION.md §Origin filter group`](DATAHUB_INTEGRATION.md#origin-filter-group);
breakdown shape and DAG semantics are in
[`BACKEND.md §Metrics Service`](feature/BACKEND.md#metrics-service-srcbackendmetrics).

### Factory defaults

On first start, DataSpoke seeds one metric of each built-in type (idempotent — only
inserted when the `metric_definitions` row is absent). Defaults are
`mode: "active"`, `is_enabled: false`, `schedule_tier: "daily"`, `dataset_filter: {}`,
type-appropriate `metric_conf`. The seeds ship disabled so the governance lead opts
in explicitly via PATCH `is_enabled: true` (or runs a one-off `method/run` with
`dry_run: true`) before scheduled measurement begins. The user can edit, disable, or
delete any default, and add more metrics of the same three types.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/dg/metric/{metric_id}/attr/conf` | Define / update / read a metric (`mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`) |
| `POST /spoke/dg/metric/{metric_id}/method/run` | Trigger a measurement run; `dry_run: true` evaluates without persisting. Concurrent runs on the same metric return `409 METRIC_RUNNING` |
| `GET /spoke/dg/metric/{metric_id}/attr/result?from=…&to=…` | Timeseries of past measurements (each row carries `values` and per-dataset `breakdown`) |
| `GET /spoke/dg/metric/{metric_id}/event` | Run completion / definition change events |
| `GET /spoke/dg/metric` | List all metrics |

Available `schedule_tier` values: `hourly`, `daily`, `weekly`. When enabled, the
metric is invoked on its tier; on-demand runs always go through
`POST .../method/run`.

### Imazon Example

The CDO replaces the daily doc-health default with a PROD-scoped weekly run:

```http
PUT /api/v1/spoke/dg/metric/doc-health-prod/attr/conf
```
```json
{
  "mode": "active",
  "is_enabled": true,
  "metric_type": "doc-health",
  "title": "Doc Health (PROD)",
  "description": "Weekly documentation-completeness check across PROD datasets",
  "metrics": ["total", "doc_health"],
  "metric_conf": {},
  "schedule_tier": "weekly",
  "dataset_filter": {"origin": "PROD"}
}
```

The CDO triggers an immediate first run rather than waiting for the schedule:

```http
POST /api/v1/spoke/dg/metric/doc-health-prod/method/run
```

A week later, trends are pulled for a board update:

```http
GET /api/v1/spoke/dg/metric/doc-health-prod/attr/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

Each result row carries `values: {"total": 142.0, "doc_health": 119.0}` plus a
per-dataset breakdown listing only the **undocumented** datasets (those that
contributed `0.0`) — e.g. `customers.eu_profiles`, `shipping.carrier_status` — so
the board review focuses on the work still outstanding.
