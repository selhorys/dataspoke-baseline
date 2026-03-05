# DataSpoke Frontend — Data Engineering (DE) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Layout and shared components in [FRONTEND_BASIC](FRONTEND_BASIC.md).
> API routes in [API](API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Navigation](#navigation)
3. [Ingestion Management (UC1)](#ingestion-management-uc1)
4. [Validation & SLA (UC2, UC3)](#validation--sla-uc2-uc3)
5. [Documentation & Ontology (UC4)](#documentation--ontology-uc4)
6. [Dataset Detail Page](#dataset-detail-page)

---

## Overview

The DE workspace focuses on dataset operational management: ingestion pipelines, quality validation, SLA monitoring, and documentation generation. All features consume `/api/v1/spoke/common/` routes — no DE-exclusive routes exist currently.

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
│  Docs     │
│  Search   │
│  ───────  │
│  [DA][DG] │
└───────────┘
```

| Item | Route | API Base |
|------|-------|----------|
| Home | `/de` | — |
| Ingestion | `/de/ingestion` | `/spoke/common/ingestion/` |
| Validation | `/de/validation` | `/spoke/common/validation/` |
| Docs | `/de/generation` | `/spoke/common/gen/` |
| Search | `/de/search` | `/spoke/common/search` |

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

- **Edit Config** → opens config form modal. Submits via `PUT /spoke/common/data/{urn}/attr/ingestion/conf`.
- **Run Now** → `POST /spoke/common/data/{urn}/attr/ingestion/method/run`
- **Dry Run** → same endpoint with `?dry_run=true`
- **Recent Runs** → `GET /spoke/common/data/{urn}/attr/ingestion/event`

### Config Editor

Modal form for ingestion configuration. Fields driven by the ingestion config schema. Enrichment sources and custom extractors are managed as dynamic form arrays.

---

## Validation & SLA (UC2, UC3)

### Validation List (`/de/validation`)

Cross-dataset view of validation configurations and latest results. Uses `GET /spoke/common/validation`.

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
│  │  (event list from /attr/validation/event)          │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Run Now** → `POST /spoke/common/data/{urn}/attr/validation/method/run`
- **Anomaly Timeline** → `GET /spoke/common/data/{urn}/attr/validation/result?from=...&to=...` rendered as a Recharts line chart
- **Real-time progress** → WS `/spoke/common/data/{urn}/stream/validation` shows step-by-step progress bar during a run
- **Config** tab → `GET/PUT /spoke/common/data/{urn}/attr/validation/conf` for editing rules, schedules, SLA targets

---

## Documentation & Ontology (UC4)

### Generation List (`/de/generation`)

Cross-dataset view of doc generation configs and latest results. Uses `GET /spoke/common/gen`.

```
┌────────────────────────────────────────────────────────────┐
│  Documentation Generation                                  │
│                                                            │
│  [Search...          ]  Status: [All v]                    │
├────────────────────────┬───────────┬──────────┬────────────┤
│  Dataset               │ Coverage  │  Status  │  Last Gen  │
├────────────────────────┼───────────┼──────────┼────────────┤
│  catalog.title_master  │  89%      │  ● Done  │  1d ago    │
│  products.digital_cat  │  72%      │  ◌ Run.  │  now       │
│  orders.purchase_hist  │  95%      │  ● Done  │  2d ago    │
├────────────────────────┴───────────┴──────────┴────────────┤
│  1-20 of 38                          [< 1 2 >]            │
└────────────────────────────────────────────────────────────┘
```

### Generation Detail (`/de/generation/[dataset_urn]`)

Shows generated documentation, ontology proposals, and diff view for review/apply workflow.

```
┌────────────────────────────────────────────────────────────┐
│  ← Generation / catalog.title_master                       │
│                                                            │
│  [Generate Now]  [Apply to DataHub]                        │
│                                                            │
│  ┌─ Generated Description ───────────────────────────┐    │
│  │  Master catalog of all book titles. One row per    │    │
│  │  ISBN+edition. Source of truth for pricing...      │    │
│  │                                                    │    │
│  │  Source: Confluence + Source Code Analysis          │    │
│  │  Confidence: 0.95                                  │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Column Descriptions (12 of 62 generated) ────────┐   │
│  │  isbn          │ International Standard Book...    │    │
│  │  list_price    │ Publisher-set retail price in...  │    │
│  │  genre_code    │ Genre classification code — ...   │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Ontology Proposal ───────────────────────────────┐    │
│  │  Cluster: BOOK / PRODUCT (6 tables)                │    │
│  │  Canonical: catalog.product_master (proposed)      │    │
│  │                                                    │    │
│  │  ┌────────────────┐  MERGE  ┌──────────────────┐  │    │
│  │  │ title_master   │ ──────► │ product_master   │  │    │
│  │  └────────────────┘         │ (canonical)      │  │    │
│  │  ┌────────────────┐  MERGE  │                  │  │    │
│  │  │ digital_catalog│ ──────► │                  │  │    │
│  │  └────────────────┘         └──────────────────┘  │    │
│  │                                                    │    │
│  │  [Approve] [Reject] [View Full Proposal]           │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

- **Generate Now** → `POST /spoke/common/data/{urn}/attr/gen/method/generate`
- **Apply to DataHub** → `POST /spoke/common/data/{urn}/attr/gen/method/apply` (confirm dialog: "This will write to DataHub")
- **Ontology approve/reject** → `POST /spoke/common/ontology/{concept_id}/method/approve` or `reject`

### Ontology Browser (`/de/generation/ontology`)

Browse the full concept taxonomy. Uses `GET /spoke/common/ontology`.

```
┌────────────────────────────────────────────────────────────┐
│  Ontology Browser                                          │
│                                                            │
│  ├─ Product/Catalog                                        │
│  │  ├─ Book (6 datasets)                                   │
│  │  │  ├─ catalog.title_master  [0.98]                     │
│  │  │  ├─ catalog.editions      [0.91]                     │
│  │  │  └─ products.digital_cat  [0.95]                     │
│  │  └─ Inventory (3 datasets)                              │
│  ├─ Customer                                               │
│  │  ├─ Profile (4 datasets)                                │
│  │  └─ Transaction (5 datasets)                            │
│  └─ ...                                                    │
│                                                            │
│  [numbers] = confidence score                              │
│  Click node → detail panel with relationships              │
└────────────────────────────────────────────────────────────┘
```

---

## Dataset Detail Page

Shared entry point for any dataset: `/de/dataset/[dataset_urn]`. Aggregates ingestion, validation, and generation views as tabs.

```
┌────────────────────────────────────────────────────────────┐
│  ← catalog.title_master                                    │
│  Platform: Oracle / DWPROD  │  Owner: maria.garcia         │
│  Quality: 96/100  │  Tags: PII, Editorial_Reviewed         │
│                                                            │
│  [ Overview | Ingestion | Validation | Docs | Events ]     │
│  ─────────────────────────────────────────────────────     │
│                                                            │
│  (tab-specific content from sections above)                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

- **Overview** → `GET /spoke/common/data/{urn}` + `/attr`
- **Ingestion** tab → same as ingestion detail
- **Validation** tab → same as validation detail
- **Docs** tab → same as generation detail
- **Events** tab → `GET /spoke/common/data/{urn}/event` (all event types, unified timeline)
