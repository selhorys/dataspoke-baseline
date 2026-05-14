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
| UC4 | Metadata Generation | [Description and MD Doc Proposals](#uc4-metadata-generation) |
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

- **`active-custom`** — DataSpoke is the ingestor, using its own in-house extractor
  framework. An Airflow tier DAG runs the platform extractor on the configured
  `schedule_tier` (`hourly` / `daily` / `weekly`) and emits results to DataHub. Manual
  and dry-run runs are also supported. Limited to platforms DataSpoke has implemented
  (`postgres`, `kafka` today). Each run emits the standard schema aspects **plus a
  `DataProcessInstance`** that powers `event/ingestion`.
- **`passive`** — DataSpoke does **not** run the extractor and does nothing programmatic
  toward making the run happen. The user sets up extraction however they prefer:
  configuring a recipe in DataHub Managed Ingestion (UI or GraphQL), running a one-off
  Python script with the `acryl-datahub` SDK, or wiring it into any external pipeline.
  DataSpoke registers the URN and observes via the hourly `ingestion-passive-hourly`
  DAG, which polls DataHub for `DataProcessInstance` records and writes one row per
  run to `event/ingestion`. Whatever the external ingestor is, **it must emit a
  `DataProcessInstance` per run** for runs to surface in DataSpoke's events
  ([DATAHUB_INTEGRATION §Custom Ingestor Guide](DATAHUB_INTEGRATION.md#custom-ingestor-guide)).

The same DPI emission contract applies in both modes — DataSpoke's own active-custom
extractors emit DPI just as external passive ingestors must, so observation behavior is
uniform regardless of who ran the job.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/ingestion/conf` | Register, read, update, remove ingestion conf (`mode`, `platform`, `identifier`, plus `locator`/`auth`/`schedule_tier` for `active-custom`) |
| `POST /spoke/common/data/{urn}/method/ingestion/run` | Manual run (`dry_run: true` for connection check) — **`active-custom` configs only**; passive configs return `409 INGESTION_NOT_APPLICABLE` |
| `GET /spoke/common/data/{urn}/event/ingestion` | Per-dataset ingestion event history (active-custom: written by DataSpoke runs; passive: written by the hourly poll observing DataProcessInstance records in DataHub) |
| `GET /spoke/common/ingestion` | Cross-dataset list view aggregating per-dataset `attr/ingestion/*` |

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

Each time DataHub's executor finishes a run, it writes a `DataProcessInstance` for
the dataset. The hourly `ingestion-passive-hourly` DAG picks it up:

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
`event/ingestion` exactly as in Case 2. **If the script emits neither a DPI nor an
ingestion-like `Operation` aspect (INSERT/UPDATE/CREATE/ALTER)**, the events list
stays empty for that URN — the dataset still appears in `GET /spoke/common/ingestion`,
the schema is still in DataHub, and the
[`ingestion-freshness` metric](#uc5-governance) still tracks it via DataHub timestamps;
only per-run drill-down via `event/ingestion` is unavailable. DataHub Managed
Ingestion's standard source plugins emit `Operation` aspects automatically, so passive
URNs ingested via Managed Ingestion are observable without any extra work. Custom
scripts that want full event detail (terminal status, run identity) must follow the
DPI emission contract in
[DATAHUB_INTEGRATION §Custom Ingestor Guide](DATAHUB_INTEGRATION.md#custom-ingestor-guide)
— the same contract that DataSpoke's own active-custom extractors satisfy.

#### Cross-dataset overview

```http
GET /api/v1/spoke/common/ingestion?limit=100
```

Returns one row per dataset with its full `attr/ingestion/*` aggregate (mode, schedule
where applicable, last event status). Useful for dashboards and bulk audit.

### Scope Note

DataSpoke ingestion's responsibility is **source connectivity, schema discovery, and
freshness signals**. Profiling, column-level lineage, and usage analytics are out of
scope for the in-house `active-custom` path; teams that need them should configure
DataHub Managed Ingestion directly and register the dataset with `mode: passive` in
DataSpoke. This keeps DataSpoke's extractor surface small and consistent with the
"DataSpoke is a control surface, DataHub is the SSOT for metadata" principle.

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
80% case, not the only path. See [`spec/feature/VALIDATION.md`](feature/VALIDATION.md)
for the full contract.

**Conf pre-condition.** PUT `validation/conf` requires the dataset to already exist in
DataHub — configuring a slot for a URN that DataHub doesn't track returns
`422 DATASET_NOT_IN_DATAHUB`. Unlike ingestion (which can create the dataset),
validation always operates on a dataset DataHub already knows about.

**Result row shape.** Each pipeline `POST .../attr/validation/result` writes one
timeseries row keyed by `data_time` (typically the partition timestamp) with `score`
and a map of named variables. Multiple POSTs with the same `data_time` are
**append-only**: each becomes a distinct `assertionRunEvent` row in DataHub, and the
GET endpoint returns the most recent (last-write-wins) per distinct `data_time`.

**Soft-delete + resurrect.** `DELETE .../attr/validation/conf` emits
`status.removed = true` on the assertion URN; a subsequent `PUT` resurrects the same
deterministic URN (clears `removed`, overwrites `assertionInfo`).

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

**Historical baseline cache.** Tomorrow's quality task computes today's row-count
anomaly against a 14-day rolling baseline. Instead of re-aggregating
`orders.line_items`, it issues:

```http
GET .../attr/validation/result?from=2026-04-24T00:00:00Z&until=2026-05-08T00:00:00Z
```

and uses the prior `row_cnt` series directly.

**Cross-dataset overview.** Ops teams browse `GET /spoke/common/validation` to see
per-dataset description, variable count, and latest score.

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

Node and edge IDs are slugs (`book`, `placed_by`); node and edge slugs may not
contain `__` (reserved as the triple-ID separator). A triple ID is the composite
slug `subject_node_id__edge_id__object_node_id` (e.g.,
`order_line__references__book`), so the ID itself encodes the fact and is
inherently idempotent across re-inference runs.

**Conf is a singleton.** Unlike the per-dataset configs in UC1 / UC2 / UC4, the
ontology is a global artifact. The operational conf at `/spoke/common/ontogen/attr/conf`
controls when the inference DAG runs and which datasets are in scope.

**Inputs (proofread DataHub boundary).** UC3 reads the same set of DataHub
aspects as UC4: `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
`editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
`document` entities whose `relatedAssets` reference an in-scope dataset
(Markdown body by convention). DataSpoke writes the editable aspects only after a
UC4 reviewer approves the proposal, so their *presence* in DataHub is the
approval signal — UC3 needs no separate join, and UC4 draft states (`pending` /
`edited`) are never written to DataHub, so the LLM never learns from another
LLM's unreviewed guess.

| `attr/conf` field | Purpose |
|---|---|
| `is_enabled` | Master switch for the inference DAG |
| `schedule_tier` | `hourly` / `daily` / `weekly` re-inference cadence |
| `dataset_filter` | Optional scope filter — `tags` (list of DataHub tag URNs), `glossary_terms` (list of glossary term URNs), and `dataset_urns` (list of explicit `urn:li:dataset:(…)` URNs for pinning to a known set). Filters are OR-ed across all three dimensions; an empty array on any dimension contributes nothing; `{}` means all datasets. URN format is validated at PUT/PATCH time; entries that don't resolve in DataHub at run time are skipped and reported in the run-complete event's `unresolved_urns` field. Same shape as UC5's `measurement_query.dataset_filter` |
| `default_run_prompt` | Optional Markdown string used as the one-shot prompt for runs that do not supply their own — i.e., periodic Airflow runs, and manual `POST /method/run` calls with no body. Null disables the default |

**Seeds steer inference.** A seed is a human-authored **Markdown document** (prompt,
domain hint, naming convention) that the inference run consumes alongside the data
sources. The seed body — request and response — is raw Markdown
(`Content-Type: text/markdown`); only `seed_id` and timestamps are managed
out-of-band. Multiple seeds coexist. POST creates (server assigns `seed_id`), PATCH
replaces the document, DELETE retires.

**Run semantics.** Inference runs are serialised: a duplicate `method/run` while one
is in flight returns `409 ONTOGEN_RUNNING`. `?dry_run=true` evaluates the inference and
returns the would-be node / edge / triple set without persisting changes — useful for
previewing the effect of a `seed` or `dataset_filter` change before committing.

**Incremental inference.** Each run starts from the existing reusable ontology —
the LLM does not re-derive from scratch. New proposals are layered on top: when a
candidate matches an existing node by name or embedding similarity (via
`node_embeddings`), the existing node ID is reused. The reuse pool spans all
non-`rejected` statuses (`llm_pending`, `llm_approved`, `approved`) so the same
concept doesn't fork into duplicate rows while awaiting human review. Otherwise a
new `llm_pending` node is proposed. Edges and triples follow the same reuse rule.
`rejected` results are not carried forward as inputs.

**One-shot run prompt.** A `POST /method/run` may carry a Markdown body
(`Content-Type: text/markdown`) that acts as a transient prompt for that single run,
on top of the persistent seeds. It is not stored. Use this for "steer this one run"
experiments without committing to a seed.

**Default one-shot prompt.** Runs that do not supply their own body — periodic
Airflow runs and manual `POST /method/run` calls with an empty body — fall back to
`attr/conf.default_run_prompt` (Markdown). This is the place to encode the
"how every scheduled run should be steered" guidance. An explicit body on a manual run
overrides the default; sending an empty body always uses the default.

**Review dependency.** A triple cannot be human-approved until both its endpoint
nodes and its edge are `status='approved'` (an `llm_approved` dependency does NOT
satisfy the gate — the human must explicitly approve each component first).
Attempting otherwise returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING`. The reviewer
therefore typically processes **nodes → edges → triples**.

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

**Inputs.** Per the conf, DataSpoke reads DataHub aspects for the three OLTP
tables: `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
`editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
`document` entities whose `relatedAssets` reference one of the in-scope datasets
(Markdown body). The seed shapes naming choices.

**Inferred output.** Three nodes, two edges, two triples. Each row's `status` is
either `llm_approved` (if the Adversarial Debate accepted it with confidence
≥ `ONTOLOGY_CONFIDENCE_THRESHOLD`) or `llm_pending` (otherwise):

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

Approval marks the entry as approved in DataSpoke storage. The ontology graph
lives in DataSpoke (PostgreSQL relational + pgvector).

When `is_enabled=false`, non-dry-run calls to `method/run` return `409 ONTOGEN_DISABLED`. Dry-run (`?dry_run=true`) is always permitted regardless of `is_enabled`. Dry-run records `ONTOGEN.RUN_COMPLETE` with `dry_run: true` in the event detail, same as real runs.

---

## UC4: Metadata Generation

**MANIFESTO §2.1 feature**: *Metadata Generation — based on the ontology, inspects the
state of data documentation and proposes metadata via generative AI, including APIs and
a review process.*

This feature proposes values for documentation fields that already exist in DataHub
metadata. It does **not** propose ontology structure (UC3 owns that).

**Inputs (proofread DataHub boundary).** UC4 reads the same DataHub aspect set
as UC3: `datasetProperties`, `schemaMetadata`, `editableDatasetProperties`,
`editableSchemaMetadata`, `glossaryTerms`, and `documentInfo.contents.text` on
`document` entities whose `relatedAssets` reference the in-scope dataset.
UC4 also reads the UC3-approved ontology nodes and triples (filtered to
`status='approved'` via `dataset_node_map`) from DataSpoke storage.

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
| Cross-data | Cross-data documentation | Markdown | `documentInfo.contents.text` on `document` entities (whose `relatedAssets` list the related datasets); the generator may propose create / modify / delete actions — see Design decision below |

Future scope (mentioned, not modelled here): proposals for `domains` and `globalTags`.

> *(Design decision)* `cross_data.md` proposals are not keyed off a UC3 node. The
> doc generator reads existing `document` entities whose `relatedAssets` overlap the
> in-scope dataset (their titles and bodies) as input context and decides itself
> what to propose. A single `cross_data.md` proposal is a **list of actions**, each
> one of:
> - **create** — a new `document` with a generator-chosen descriptive title (a topic
>   phrase), Markdown body in `documentInfo.contents.text`, and `relatedAssets`
>   listing the dataset URNs the topic spans, when an uncovered topic is identified
>   and existing documents are fine as-is;
> - **modify** — replace `documentInfo.contents.text` on an existing `document` (and
>   optionally extend `relatedAssets`) while keeping its title and URN;
> - **delete** — soft-delete an existing `document` via `status.removed = true` when
>   its topic is fully absorbed into a new replacement.
>
> Each action carries a stable `action_id` in the result payload. The reviewer
> approves, edits, or rejects each action individually via the same PATCH mechanism
> used for per-field proposals — the `fields` array references actions as
> `cross_data.md.<action_id>`.

### API Mapping

| Endpoint | Used for |
|---|---|
| `PUT/PATCH/GET/DELETE /spoke/common/data/{urn}/attr/metagen/conf` | Configure target fields, schedule_tier, status |
| `POST /spoke/common/data/{urn}/method/metagen/run` | Trigger a generation run |
| `GET /spoke/common/data/{urn}/attr/metagen/result?latest=true` | Get the latest proposal for a dataset |
| `PATCH /spoke/common/data/{urn}/attr/metagen/result/{result_id}` | Approve / partial-approve / reject — body `{ "verdict": "approve"\|"reject", "fields": [...], "reason": "…" }`. Approval writes the chosen subset to DataHub. |
| `GET /spoke/common/data/{urn}/event/metagen` | Per-dataset generation event history |
| `GET /spoke/common/metagen` | Cross-dataset list with conf + latest result |

### Imazon Example

The catalog team enables doc generation on `catalog.books`:

```http
PUT /api/v1/spoke/common/data/urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)/attr/metagen/conf
```
```json
{
  "targets": ["dataset.description", "column.description", "cross_data.md"],
  "schedule_tier": "weekly",
  "is_enabled": true
}
```

**Run.**

```http
POST .../method/metagen/run
```

**Latest proposal.**

```http
GET .../attr/metagen/result?latest=true
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

cross_data.md actions:
  Existing documents considered: (none)
  Proposed:
    - action_id: a1
      action:    create
      title:     "How orders reference books"
      body:      "`orders.line_items.book_id` joins to `catalog.books.book_id` ..."
      related_assets:
        - urn:li:dataset:(urn:li:dataPlatform:postgres,orders.line_items,PROD)
        - urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.books,PROD)
      confidence: 0.81
```

**Review.** The reviewer approves the table description and 4 of 5 columns, then issues
follow-up calls to edit `author` and reject the cross-data MD:

```http
PATCH .../attr/metagen/result/7e8b…
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

A second PATCH approves an edited `author` description; a third PATCH rejects the
proposed `cross_data.md` create action with `{"verdict": "reject", "fields":
["cross_data.md.a1"], "reason": "..."}`. DataSpoke writes each approved action to
DataHub on the same call.

The team can then watch the proposal lifecycle:

```http
GET .../event/metagen
```

When `is_enabled=false`, non-dry-run calls to `method/metagen/run` return `409 GENERATION_DISABLED`. Dry-run is always permitted regardless of `is_enabled`. Dry-run records `METAGEN.COMPLETE` with `dry_run: true` in the event detail, same as real runs.

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

**Result row shape.** Every measurement run persists one `attr/result` row carrying
both an aggregate `value` and a per-dataset `breakdown` — which datasets contributed
which sub-values. The breakdown lets time-range queries on `attr/result` answer
"which datasets failed last Tuesday" without re-running the metric.

**Run semantics.** Runs are serialized per metric: a duplicate `method/run` while one
is in flight returns `409 METRIC_RUNNING`. `dry_run: true` evaluates the query and
returns the would-be result without persisting to `attr/result` or emitting events —
useful for testing a new `measurement_query` before letting the schedule fire.

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

When `is_enabled=false`, non-dry-run calls to `method/run` on a metric return `409 METRIC_DISABLED`. Dry-run (`dry_run: true`) is always permitted regardless of `is_enabled`.
