# DataSpoke Frontend — Data Engineering (DE) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Layout and shared components in [FRONTEND_BASIC](FRONTEND_BASIC.md).
> API routes in [API](../API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Navigation](#navigation)
3. [Ingestion Management (UC1)](#ingestion-management-uc1)
4. [Validation & SLA (UC2)](#validation--sla-uc2)
5. [Metadata Generation (UC4)](#metadata-generation-uc4)
6. [Ontology Generation (UC3)](#ontology-generation-uc3)
7. [Dataset Detail Page](#dataset-detail-page)

---

## Overview

The DE workspace focuses on dataset operational management: ingestion pipelines, quality
validation, metadata generation, and ontology review.

The DE tier (`/spoke/de/` routes, `/de` UI pages) is an **extensibility surface** — the
baseline DataSpoke product ships no DE-exclusive routes, and this workspace consumes
baseline features under `/spoke/common/…` with DE-flavoured presentation. Organizations
customising DataSpoke can add DE-exclusive routes and pages here.

---

## Navigation

Sidebar items for the DE workspace:

```
┌───────────┐
│  DE       │
│  ───────  │
│  Home     │
│  Ingest.  │
│  Valid.   │
│  Metagen  │
│  Ontogen  │
│  ───────  │
│  [DA][DG] │
└───────────┘
```

| Item | Route | API Base |
|------|-------|----------|
| Home | `/de` | — |
| Ingestion | `/de/ingestion` | `/spoke/common/ingestion/` |
| Validation | `/de/validation` | `/spoke/common/validation/` |
| Metadata Generation | `/de/metagen` | `/spoke/common/metagen/` |
| Ontology Generation | `/de/ontogen` | `/spoke/common/ontogen/` |

---

## Ingestion Management (UC1)

### Ingestion List (`/de/ingestion`)

Cross-dataset view of all ingestion configurations. Uses `GET /spoke/common/ingestion`.

```
┌────────────────────────────────────────────────────────────┐
│  Ingestion Configurations                                  │
│                                                            │
│  [Search...          ]  Status: [All v]  [+ New Config]    │
├────────────────────────┬────────┬──────────┬───────────────┤
│  Dataset               │ Source │  Status  │  Last Run     │
├────────────────────────┼────────┼──────────┼───────────────┤
│  catalog.title_master  │ Oracle │  ● Active│  2h ago  ✓    │
│  publishers.feed_raw   │ Excel  │  ● Active│  1d ago  ✓    │
│  reviews.user_ratings  │ API    │  ○ Paused│  3d ago  ▲    │
├────────────────────────┴────────┴──────────┴───────────────┤
│  1-20 of 45                          [< 1 2 3 >]          │
└────────────────────────────────────────────────────────────┘
```

Row click → ingestion detail page.

### Ingestion Detail (`/de/ingestion/[dataset_urn]`)

Shows config, run history (events), and trigger controls.

```
┌────────────────────────────────────────────────────────────┐
│  ← Ingestion / catalog.title_master                       │
│                                                            │
│  ┌─ Config ──────────────────────────────────────────┐    │
│  │  Source: Oracle / DWPROD                           │    │
│  │  Schedule: Daily 02:00 UTC                         │    │
│  │  Deep Spec: Enabled                                │    │
│  │  Enrichment Sources: Confluence, Excel, Custom API │    │
│  │  Custom Extractors: plsql_lineage_parser           │    │
│  │                                                    │    │
│  │  [Edit Config]  [Run Now]  [Dry Run]               │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Recent Runs ─────────────────────────────────────┐    │
│  │  2026-03-05 02:00  ✓ Success  │ 500 tables  12m   │    │
│  │  2026-03-04 02:00  ✓ Success  │ 500 tables  11m   │    │
│  │  2026-03-03 02:00  ▲ Partial  │ 498 tables  15m   │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Edit Config** → opens config form modal. Submits via
  `PUT /spoke/common/data/{urn}/attr/ingestion/conf`. The mode toggle (`active` /
  `passive`) is the first field; the schedule + auth panels are hidden when `passive` is
  selected.
- **Run Now** / **Dry Run** → `POST /spoke/common/data/{urn}/method/ingestion/run`
  (with `dry_run: true` for dry-run). Disabled in the UI when `mode: passive`.
- **Recent Runs** → `GET /spoke/common/data/{urn}/event/ingestion`. For active configs the
  events come from DataSpoke runs; for passive configs they are mirrored hourly from
  DataHub by the `ingestion-passive-sync-hourly` DAG, but the API surface is identical.

### Config Editor

Modal form for ingestion configuration. Fields driven by the ingestion config schema. Enrichment
sources and custom extractors are managed as dynamic form arrays.

---

## Validation & SLA (UC2)

### Validation List (`/de/validation`)

Cross-dataset view of validation configurations and latest results.
Uses `GET /spoke/common/validation`.

```
┌────────────────────────────────────────────────────────────┐
│  Validation Dashboard                                      │
│                                                            │
│  [Search...          ]  Status: [All v]  Score: [All v]    │
├────────────────────────┬───────┬──────────┬────────────────┤
│  Dataset               │ Score │  Status  │  Last Check    │
├────────────────────────┼───────┼──────────┼────────────────┤
│  catalog.title_master  │  96   │  ● OK    │  1h ago        │
│  orders.fulfillment    │  72   │  ▲ Warn  │  30m ago       │
│  reviews.legacy        │  34   │  ✕ Bad   │  2h ago        │
├────────────────────────┴───────┴──────────┴────────────────┤
│  Score color: ● >70 green │ ▲ 50-70 amber │ ✕ <50 red     │
└────────────────────────────────────────────────────────────┘
```

### Validation Detail (`/de/validation/[dataset_urn]`)

Shows quality score breakdown, anomaly timeline, SLA status, and alternatives.

```
┌────────────────────────────────────────────────────────────┐
│  ← Validation / orders.daily_fulfillment_summary           │
│                                                            │
│  Quality Score: 72/100                     [Run Now]       │
│                                                            │
│  ┌─ Score Breakdown ─────────────────────────────────┐    │
│  │  Completeness ████████░░  80                       │    │
│  │  Freshness    ██████░░░░  60                       │    │
│  │  Documentation████████░░  85                       │    │
│  │  Ownership    ██████████  100                      │    │
│  │  Assertions   ████░░░░░░  45                       │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Anomaly Timeline (30 days) ──────────────────────┐    │
│  │  Score                                             │    │
│  │  100 ┤                                             │    │
│  │   80 ┤ ─────────╲                                  │    │
│  │   60 ┤           ╲─────                            │    │
│  │   40 ┤                 ╲──── ← anomaly detected    │    │
│  │   20 ┤                                             │    │
│  │      └──┬──┬──┬──┬──┬──┬──┬──                     │    │
│  │        Feb 5  10  15  20  25  Mar                  │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ SLA Status ──────────────────────────────────────┐    │
│  │  Target: 9:00 AM daily                             │    │
│  │  Current: On track (predicted 8:45 AM)             │    │
│  │  Prediction confidence: 91%                        │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Issues & Recommendations ────────────────────────┐    │
│  │  ▲ Freshness: Last updated 3 hours ago (SLA: 1h)  │    │
│  │  ● Recommendation: Review upstream carrier_status  │    │
│  │  ● Alternative: orders.fulfillment_v2 (score: 91) │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Recent Events ───────────────────────────────────┐    │
│  │  (event list from /event/validation)               │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Run Now** → `POST /spoke/common/data/{urn}/method/validation/run`
- **Anomaly Timeline** → `GET /spoke/common/data/{urn}/attr/validation/result?from=...&to=...`
  rendered as a Recharts line chart
- **Real-time progress** → WS `/spoke/common/data/{urn}/stream/validation` shows step-by-step
  progress bar during a run
- **Config** tab → `GET/PUT /spoke/common/data/{urn}/attr/validation/conf` for editing rules,
  schedules, SLA targets

---

## Metadata Generation (UC4)

### Metagen List (`/de/metagen`)

Cross-dataset view of metadata generation configs and latest results.
Uses `GET /spoke/common/metagen`.

```
┌────────────────────────────────────────────────────────────┐
│  Metadata Generation                                       │
│                                                            │
│  [Search...          ]  Status: [All v]                    │
├────────────────────────┬───────────┬──────────┬────────────┤
│  Dataset               │ Coverage  │  Status  │  Last Gen  │
├────────────────────────┼───────────┼──────────┼────────────┤
│  catalog.books         │  89%      │  ● Done  │  1d ago    │
│  orders.line_items     │  72%      │  ◌ Run.  │  now       │
│  customers.profiles    │  95%      │  ● Done  │  2d ago    │
├────────────────────────┴───────────┴──────────┴────────────┤
│  1-20 of 38                          [< 1 2 >]            │
└────────────────────────────────────────────────────────────┘
```

### Metagen Detail (`/de/metagen/[dataset_urn]`)

Shows the latest proposal grouped by `target` (table description, column descriptions,
cross-data MD actions). Each field row exposes individual approve / edit / reject actions
— the reviewer can approve a subset in one PATCH.

```
┌────────────────────────────────────────────────────────────┐
│  ← Metagen / catalog.books                                 │
│                                                            │
│  [Generate Now]                                            │
│                                                            │
│  ┌─ Table Description (editableDatasetProperties) ───┐    │
│  │  # Books                                          │    │
│  │  Master catalog of every title Imazon offers...    │    │
│  │  Confidence: 0.92                                  │    │
│  │  [Approve] [Edit] [Reject]                         │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Column Descriptions (5 fields) ───────────────────┐   │
│  │  book_id    Stable, opaque identifier ...   [✓][✏][✕]│  │
│  │  title      Display title shown to ...      [✓][✏][✕]│  │
│  │  author     Free-text author / creator ...  [✓][✏][✕]│  │
│  │  isbn       ISBN-13 string; '0000…' when …  [✓][✏][✕]│  │
│  │  price      List price in USD, two decimals [✓][✏][✕]│  │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Cross-Data MD (dataProductProperties) ───────────┐    │
│  │  + create  "How orders reference books"   conf 0.81│    │
│  │     `orders.line_items.book_id` joins to ...       │    │
│  │     [Approve] [Edit] [Reject]                       │    │
│  │                                                    │    │
│  │  ~ modify  "Catalog onboarding"  (existing)        │    │
│  │     [Approve] [Edit] [Reject]                       │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Generate Now** → `POST /spoke/common/data/{urn}/method/metagen/run`
- **Per-field Approve / Edit / Reject** →
  `PATCH /spoke/common/data/{urn}/attr/metagen/result/{result_id}` with
  `{"verdict": "approve"|"reject", "fields": ["dataset.description", "column.description.book_id", "cross_data.md[0]", ...], "reason": "…"}`.
  Approval writes the listed subset to **editable** DataHub aspects only
  (`editableDatasetProperties`, `editableSchemaMetadata.editableSchemaFieldInfo`,
  `dataProductProperties`).
- The confirm dialog labels the destination aspect, e.g. "This will write to
  editableDatasetProperties on DataHub."

## Ontology Generation (UC3)

### Ontogen Conf (`/de/ontogen/conf`)

Singleton conf editor (UC3 has no per-dataset config). Edits via
`PUT/PATCH /spoke/common/ontogen/attr/conf` with fields `is_enabled`, `schedule_tier`,
`sources`, `dataset_filter`. A `[Run Now]` button calls
`POST /spoke/common/ontogen/method/run` (optional `?dry_run=true`; optional
`Content-Type: text/markdown` body acts as a one-shot prompt for this single run).

### Ontogen Seeds (`/de/ontogen/seed`)

Markdown-document editor for inference seeds. List shows existing seeds with previews;
clicking a seed opens a Markdown editor backed by
`GET/PATCH/DELETE /spoke/common/ontogen/attr/seed/{seed_id}` (`Content-Type: text/markdown`).
A `[New Seed]` action POSTs the editor body to `/spoke/common/ontogen/attr/seed`.

### Ontogen Triple Browser (`/de/ontogen`)

Three-tab browser over the triple ontology — **Nodes**, **Edges**, **Triples**. Uses
`GET /spoke/common/ontogen/result/{node|edge|triple}`. Triple review is gated: if a
triple's subject node, edge, or object node is still pending, the `[Approve]` button is
disabled with an inline hint, and a forced approve attempt surfaces the
`422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` error.

```
┌────────────────────────────────────────────────────────────┐
│  Ontology   [ Nodes | Edges | Triples ]                    │
│                                                            │
│  Nodes                                                     │
│    BOOK            conf 0.96   ✓ approved                  │
│      member: catalog.books                                 │
│    CUSTOMER        conf 0.94   ✓ approved                  │
│      member: customers.profiles                            │
│    ORDER_LINE      conf 0.71   ⏳ pending                  │
│      member: orders.line_items                             │
│      [Approve] [Reject]                                    │
│                                                            │
│  Edges                                                     │
│    references      conf 0.95   ✓ approved                  │
│    placed_by       conf 0.87   ✓ approved                  │
│                                                            │
│  Triples                                                   │
│    ORDER_LINE  --references--> BOOK       conf 0.95   ⏳   │
│      [Approve] (blocked: ORDER_LINE pending)               │
│    ORDER_LINE  --placed_by --> CUSTOMER   conf 0.87   ⏳   │
│      [Approve] (blocked: ORDER_LINE pending)               │
└────────────────────────────────────────────────────────────┘
```

- **Approve / Reject** → `POST /spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review`
  with `{"verdict": "approve"|"reject", "reason": "…"}`. On node approval, a glossary
  term is attached to each member dataset (`glossaryTerms` aspect); on triple approval,
  a glossary-term relationship is created between subject and object terms — the confirm
  dialog states this.

---

## Dataset Detail Page

Shared entry point for any dataset: `/de/dataset/[dataset_urn]`. Aggregates ingestion, validation,
and generation views as tabs.

```
┌────────────────────────────────────────────────────────────┐
│  ← catalog.title_master                                    │
│  Platform: Oracle / DWPROD  │  Owner: maria.garcia         │
│  Quality: 96/100  │  Tags: PII, Editorial_Reviewed         │
│                                                            │
│  [ Overview | Ingestion | Validation | Metagen | Events ]  │
│  ─────────────────────────────────────────────────────     │
│                                                            │
│  (tab-specific content from sections above)                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

- **Overview** → `GET /spoke/common/data/{urn}` + `/attr`
- **Ingestion** tab → same as ingestion detail
- **Validation** tab → same as validation detail
- **Metagen** tab → same as metagen detail
- **Events** tab → `GET /spoke/common/data/{urn}/event` (all event types, unified timeline)
