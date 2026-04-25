# DataSpoke: Use Case Scenarios

> **Note on Document Purpose**
> This document presents conceptual scenarios for ideation and to seed integration test
> cases. Scenarios illustrate the intended capabilities of DataSpoke — they are not
> implementation specifications. Technical architecture and feature prioritization are
> defined in separate specs (`ARCHITECTURE.md`, `feature/*.md`). Where a scenario
> introduces concepts not yet reflected in lower-priority specs, a `(Lower-priority specs
> need follow-up)` note marks the gap.

This document demonstrates how DataSpoke realises the five features defined in
`MANIFESTO_en.md` §2.1: **Ingestion Control**, **Validation**, **Ontology**,
**Doc Generation**, and **Governance**. All scenarios share a single imaginary company —
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
| UC1 | Ingestion Control | [Passive and Polling Ingestion](#uc1-ingestion-control) |
| UC2 | Validation | [Rule Registration, Scheduled and Dry-Run](#uc2-validation) |
| UC3 | Ontology | [Concept Inference Across Imazon Datasets](#uc3-ontology) |
| UC4 | Doc Generation | [Description and MD Doc Proposals](#uc4-doc-generation) |
| UC5 | Governance | [Ingestion Freshness and Validation Score](#uc5-governance) |

---

## UC1: Ingestion Control

**MANIFESTO §2.1 feature**: *Ingestion Control — convenience functions for configuring,
controlling, and managing data ingestion in one place.*

### User Story

> *As a* data team member,
> *I want to* register, run, and observe ingestion for any dataset I care about — whether
> DataSpoke ingests it directly or an external pipeline ingests it into DataHub —
> *so that* one DataSpoke surface drives ingestion config, runs, and event history for
> the whole estate.

Two ingestion modes are supported:

- **Polling** — DataSpoke is the ingestor. An Airflow tier DAG runs the platform
  extractor on the configured `schedule_tier` (`hourly` / `daily` / `weekly`) and emits
  results to DataHub. Manual and dry-run runs are also supported.
- **Passive** — an external system ingests directly into DataHub. DataSpoke does not
  run the extractor; it only marks the dataset's ingestion config as `mode: passive`. A
  `datahub-ingestion-status-sync` Airflow DAG runs **hourly**, polls DataHub for
  ingestion run history of all passive-marked datasets, and writes the resulting status
  as rows on `event/ingestion`. The DataSpoke API surface therefore looks the same to
  clients regardless of mode.

> *(Lower-priority specs need follow-up)* The `mode: passive | polling` field on
> `attr/ingestion/conf` and the `datahub-ingestion-status-sync` DAG are introduced here.
> `feature/API.md`, `feature/BACKEND.md`, and `feature/BACKEND_SCHEMA.md` need follow-up
> edits to model the `mode` field on `ingestion_configs`, register the sync DAG, and
> describe how DataHub run history is mapped onto `event/ingestion`.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | Register, read, update, remove ingestion conf (`mode`, `platform`, `locator`, `identifier`, `auth`, `is_active`, `schedule_tier` for polling) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | Manual run (`dry_run: true` for connection check) — polling configs only |
| `GET /spoke/common/data/{urn}/event/ingestion` | Per-dataset ingestion event history (polling: written by DataSpoke runs; passive: written by the hourly DataHub status sync) |
| `GET /spoke/common/ingestion` | Cross-dataset list view aggregating per-dataset `attr/ingestion/*` |

### Imazon Example

**Polling — `catalog.books` (Postgres, daily).**

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "polling",
  "platform": "postgres",
  "locator": {"host": "pg-oltp.imazon.internal", "port": 5432},
  "identifier": {"database": "imazon", "schema_name": "catalog", "table": "books"},
  "auth": {"username": "spoke_reader", "secret_ref": "vault://imazon/pg/spoke_reader"},
  "is_active": true,
  "schedule_tier": "daily"
}
```

A coding agent verifies connectivity before turning the schedule on:

```http
POST .../method/ingestion/run    { "dry_run": true }
```

After the daily Airflow tier DAG runs, the team reads the per-dataset event history:

```http
GET .../event/ingestion?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

**Passive — `orders.shipments` (Kafka, externally ingested).**

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:kafka,orders.shipments,PROD)/attr/ingestion/conf
```
```json
{
  "mode": "passive",
  "platform": "kafka",
  "locator": {"bootstrap_servers": "kafka.imazon.internal:9092"},
  "identifier": {"topic": "orders.shipments", "cluster": "PROD"},
  "is_active": true
}
```

No `schedule_tier`. DataSpoke does not run an extractor. The external fulfillment-service
pipeline (an Airflow DAG outside DataSpoke) emits the schema and properties to DataHub
directly.

Every hour, DataSpoke's `datahub-ingestion-status-sync` DAG polls DataHub for ingestion
runs of all passive-marked datasets and writes one row per run to the events table.
Imazon reads them through the same API:

```http
GET .../event/ingestion?from=…&to=…
```

**Cross-dataset overview.**

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

Returns one row per dataset with its full `attr/ingestion/*` aggregate (mode, schedule,
last event status). Useful for dashboards and bulk audit.

---

## UC2: Validation

**MANIFESTO §2.1 feature**: *Validation — registration, execution, and management of
validation rules. Supports dry-run, point-in-time historical validation, and real-time
APIs.*

### User Story

> *As a* data team member,
> *I want to* register rules per dataset, run them on schedule or on demand, dry-run
> them from a coding agent before shipping a pipeline, and query historical results,
> *so that* data quality is observable and verifiable without building bespoke checks.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/validation/conf` | Register / read / update / remove the rule set (DataHub Open Assertions Spec compatible) |
| `POST /spoke/common/data/{urn}/method/validation/run` | Manual run; `dry_run: true` for the Online Verifier (no result write) |
| `GET /spoke/common/data/{urn}/attr/validation/result?from=…&to=…&partition=…` | Historical results (timeseries) |
| `GET /spoke/common/data/{urn}/event/validation` | Per-dataset validation event history |
| `WS /spoke/common/data/{urn}/stream/validation` | Real-time progress stream during a run |
| `GET /spoke/common/validation` | Cross-dataset list with conf + latest result |

### Imazon Example

The orders team registers two rules on `orders.line_items`:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)/attr/validation/conf
```
```json
{
  "is_active": true,
  "schedule_tier": "daily",
  "rules": [
    {"rule_id": "qty_positive", "type": "field", "column": "quantity",
     "condition": "between", "min": 1, "max": 100},
    {"rule_id": "daily_volume", "type": "volume",
     "comparison": "ratio", "threshold": 0.8, "window": "7d",
     "partition": "event_date"}
  ]
}
```

**Scheduled run.** The daily Airflow validation DAG executes both rules and writes
`assertionRunEvent` aspects to DataHub plus rows to `validation_results`.

**Dry-run from a coding agent.** While a developer ships a new fulfillment pipeline, an
AI coding agent calls:

```http
POST .../method/validation/run    { "dry_run": true, "partition": {"event_date": "2026-04-25"} }
```

to verify the rules pass against yesterday's data before merging.

**Historical query.** A week later, an analyst checks last week's results:

```http
GET .../attr/validation/result?from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z
```

**Live progress.** The portal opens

```
WS .../stream/validation
```

and renders rule-by-rule progress as the run proceeds.

**Cross-dataset overview.** Ops teams browse `GET /spoke/common/validation` to see
per-dataset latest pass/fail.

---

## UC3: Ontology

**MANIFESTO §2.1 feature**: *Ontology — analyses source code, SQL logs, external
documents, and more to autonomously construct an ontology, maintained in a graph DB and
a vector DB.*

### User Story

> *As an* analyst or governance member,
> *I want* DataSpoke to autonomously infer the business concepts that exist across my
> datasets and the relationships between them,
> *so that* I can navigate datasets by concept, and review low-confidence proposals
> before they are accepted.

The baseline ontology is **single-level** — concepts are peers, not nested. Relationships
are edges between concepts; member datasets are listed under each concept.

### API Mapping

| Endpoint | Used for |
|---|---|
| `GET /spoke/common/ontology` | List concepts (with confidence and status) |
| `GET /spoke/common/ontology/{concept_id}` | Concept detail incl. member datasets and outgoing relationships |
| `GET /spoke/common/ontology/{concept_id}/attr` | Concept attributes (confidence, source evidence) |
| `GET /spoke/common/ontology/{concept_id}/event` | Change history (proposed → approved / rejected, member additions) |
| `POST /spoke/common/ontology/{concept_id}/method/review` | Approve or reject a pending concept proposal |

### Imazon Example

**Inputs.** DataSpoke reads DataHub aspects (`schemaMetadata`, `datasetProperties`,
`upstreamLineage`) for the three OLTP tables, plus SQL query logs and a few
`imazon/order-service` GitHub repos.

**Inferred output.** Three peer concepts with two relationships:

```
Concept: BOOK                       confidence 0.96   status: approved
  members:
    catalog.books                   (primary)

Concept: CUSTOMER                   confidence 0.94   status: approved
  members:
    customers.profiles              (primary)

Concept: ORDER_LINE                 confidence 0.71   status: pending_review
  members:
    orders.line_items               (primary)
  evidence:
    - foreign key book_id → catalog.books.book_id (schema)
    - join with customers.profiles appears in 84% of order-service queries (SQL logs)

Relationships:
  ORDER_LINE  --references-->  BOOK         (FK book_id,     confidence 0.95)
  ORDER_LINE  --placed_by-->   CUSTOMER     (FK customer_id, confidence 0.87)
```

`ORDER_LINE` is below the auto-approval threshold (LLM disambiguating between "order"
and "line item"), so it lands in the review queue:

```http
GET /api/v1/spoke/common/ontology
```

A governance reviewer fetches detail and event history:

```http
GET /api/v1/spoke/common/ontology/order_line
GET /api/v1/spoke/common/ontology/order_line/event
```

…and approves the proposal:

```http
POST /api/v1/spoke/common/ontology/order_line/method/review
```
```json
{ "verdict": "approve", "reason": "Confirmed FK structure; rename later if needed." }
```

After approval, concept membership is reflected back to DataHub as a glossary term
attachment on the member dataset.

---

## UC4: Doc Generation

**MANIFESTO §2.1 feature**: *Doc Generation — based on the ontology, inspects the state
of data documentation and proposes documents via generative AI, including APIs and a
review process.*

This feature proposes values for documentation fields that already exist in DataHub
metadata. It does **not** propose ontology structure (UC3 owns that).

### User Story

> *As a* dataset owner or governance reviewer,
> *I want* DataSpoke to propose documentation for under-documented datasets, and let me
> approve, edit, or reject proposals field-by-field,
> *so that* documentation coverage improves without me writing every description by
> hand.

**Supported documentation fields in baseline**

DataSpoke writes only to **editable** DataHub aspects. The non-editable counterparts
(`datasetProperties.description`, `schemaMetadata.fields[].description`) are reserved
for ingestion connectors; writing to them risks the next connector run overwriting
human-approved text. DataHub treats both editable description fields as rich text and
the UI renders Markdown.

| Scope | Field | Format | DataHub target |
|---|---|---|---|
| Per-data | Table description | Markdown | `editableDatasetProperties.description` |
| Per-data | Column description | Markdown | `editableSchemaMetadata.editableSchemaFieldInfo[].description` (keyed by `fieldPath`) |
| Cross-data | Cross-data documentation | Markdown | `dataProductProperties.description` on a `dataProduct` entity whose `assets` list the related datasets |

Future scope (mentioned, not modelled here): proposals for `domains` and `globalTags`.

> *(Lower-priority specs need follow-up)* The `targets` enum on `attr/gen/conf` needs
> three concrete values — `dataset.description`, `column.description`, `cross_data.md`
> — mapped to the editable aspects above and to a `dataProduct` write on the cross-data
> case. Propagate to `feature/BACKEND.md` and `DATAHUB_INTEGRATION.md`.

> *(Open design question)* When a `cross_data.md` proposal is approved, how is the
> target `dataProduct` resolved? Two sub-questions: (a) URN scheme — propose
> `urn:li:dataProduct:<concept_id>` keyed off UC3 so a concept owns at most one Data
> Product; (b) create-vs-update — does approval always upsert the Data Product's
> description, or do we distinguish "create new Data Product" from "edit existing
> description" as separate proposal types? Resolve before implementing UC4.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/gen/conf` | Configure target fields, period, status |
| `POST /spoke/common/data/{urn}/method/gen/run` | Trigger a generation run |
| `GET /spoke/common/data/{urn}/attr/gen/result?latest=true` | Get the latest proposal for a dataset |
| `PATCH /spoke/common/data/{urn}/attr/gen/result/{result_id}` | Approve / partial-approve / reject — body `{ "verdict": "approve"\|"reject", "fields": [...], "reason": "…" }`. Approval writes the chosen subset to DataHub. |
| `GET /spoke/common/data/{urn}/event/gen` | Per-dataset generation event history |
| `GET /spoke/common/gen` | Cross-dataset list with conf + latest result |

### Imazon Example

The catalog team enables doc generation on `catalog.books`:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/gen/conf
```
```json
{
  "targets": ["dataset.description", "column.description", "cross_data.md"],
  "period": "weekly",
  "is_active": true
}
```

**Run.**

```http
POST .../method/gen/run
```

**Latest proposal.**

```http
GET .../attr/gen/result?latest=true
```

Returns:

```
result_id: 7e8b…
status:    pending_review

dataset.description (markdown, confidence 0.92):
  "# Books\n\nMaster catalog of every title Imazon offers...\n## Notes\n- Primary key: `book_id`."

column.description proposals (markdown):
  book_id   — "Stable, opaque identifier for a book."
  title     — "Display title shown to customers."
  author    — "Free-text author / creator name."
  isbn      — "ISBN-13 string; '0000000000000' when unknown."
  price     — "List price in USD, two decimal places."

cross_data.md (markdown, confidence 0.81, scope: BOOK + ORDER_LINE):
  "# How orders reference books\n\n`orders.line_items.book_id` joins to `catalog.books.book_id` ..."
```

**Review.** The reviewer approves the table description and 4 of 5 columns, then issues
follow-up calls to edit `author` and reject the cross-data MD:

```http
PATCH .../attr/gen/result/7e8b…
```
```json
{
  "verdict": "approve",
  "fields": ["dataset.description",
             "column.description.book_id",
             "column.description.title",
             "column.description.isbn",
             "column.description.price"],
  "reason": "Approved as generated."
}
```

A second PATCH approves an edited `author` description; a third PATCH rejects
`cross_data.md` with a reason. DataSpoke writes each approved subset to DataHub on the
same call.

The team can then watch the proposal lifecycle:

```http
GET .../event/gen
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

**Baseline metrics (exactly two)**

| Metric ID | Definition |
|---|---|
| `ingestion-freshness` | Percentage of active ingestion configs whose latest successful `event/ingestion` falls within the configured freshness window (per `schedule_tier` for polling; per a fixed window for passive). |
| `validation-score` | Percentage of validation rules with `assertion_result = SUCCESS` in the latest run, averaged across all datasets that have at least one rule. |

**Baseline overview (one)**

A single dashboard returns the latest value of both metrics plus a per-dataset
breakdown — which datasets are stale, which datasets have failing rules.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/dg/metric/{metric_id}/attr/conf` | Define / update / read a metric (title, theme, query, schedule_tier, active flag) |
| `POST /spoke/dg/metric/{metric_id}/method/run` | Trigger a measurement run |
| `GET /spoke/dg/metric/{metric_id}/attr/result?from=…&to=…` | Numeric timeseries of past measurements |
| `GET /spoke/dg/metric/{metric_id}/event` | Run completion / definition change events |
| `WS /spoke/dg/metric/stream` | Real-time updates when measurement runs complete |
| `GET /spoke/dg/metric` | List all metrics |
| `GET /spoke/dg/overview` | Snapshot — both metric values + per-dataset breakdown |
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
  "is_active": true
}
```

```http
PUT /api/v1/spoke/dg/metric/validation-score/attr/conf
```
```json
{
  "title": "Validation score",
  "theme": "quality",
  "measurement_query": {"dataset_filter": {}, "aggregation": "pct_rules_passing"},
  "schedule_tier": "hourly",
  "is_active": true
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
their freshness and validation status.
