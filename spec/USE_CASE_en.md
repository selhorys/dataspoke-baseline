# DataSpoke: Use Case Scenarios

> **Note on Document Purpose**
> This document presents conceptual scenarios for ideation and vision alignment. Use cases
> illustrate the intended capabilities of DataSpoke — they are not implementation specifications
> or technical requirements. Technical architecture and feature prioritization are defined in
> separate specs (`ARCHITECTURE.md`, `feature/*.md`).

This document demonstrates how DataSpoke realises the five features defined in
`MANIFESTO_en.md` §2.1: **Ingestion Control**, **Validation**, **Ontology**, **Doc Generation**,
and **Governance**. Each use case is written against a single imaginary company — **Imazon**,
an online bookstore — so scenarios coexist and reinforce each other.

User-group framing (Data Engineering / Data Analysis / Data Governance) remains as a UI and
API extensibility surface, but features are no longer partitioned by user group.

---

## Imaginary Company Profile: Imazon

Imazon is a 15-year-old online bookstore. Its data landscape reflects years of organic growth:

- **Legacy Oracle data warehouse** — 500+ tables covering book catalog, customers, orders,
  reviews, publishers, inventory, and shipping
- **Departments** — Engineering, Data Science, Marketing, Finance, Legal, Operations,
  Publisher Relations, Customer Support
- **Key data domains** — `catalog.*` (books, authors, genres), `customers.*`, `orders.*`,
  `reviews.*`, `recommendations.*`, `publishers.*`, `inventory.*`, `shipping.*`
- **DataHub adoption** — recently deployed; standard Oracle connector imported schema metadata
  but missed business context, stored-procedure lineage, and tribal knowledge locked in
  Confluence and spreadsheets

---

## Feature Mapping

| # | MANIFESTO Feature | Use Case |
|---|-------------------|----------|
| UC1 | Ingestion Control | [Legacy Oracle Book Catalog Enrichment](#uc1-ingestion-control--legacy-oracle-book-catalog-enrichment) |
| UC2 | Validation | [Recommendation Pipeline Quality & Predictive SLA](#uc2-validation--recommendation-pipeline-quality--predictive-sla) |
| UC3 | Ontology | [Post-Acquisition Ontology Construction & Discovery](#uc3-ontology--post-acquisition-ontology-construction--discovery) |
| UC4 | Doc Generation | [Doc Proposal & Human-in-the-Loop Review](#uc4-doc-generation--doc-proposal--human-in-the-loop-review) |
| UC5 | Governance | [Enterprise Metadata Health & Multi-Perspective Overview](#uc5-governance--enterprise-metadata-health--multi-perspective-overview) |

---

## UC1: Ingestion Control — Legacy Oracle Book Catalog Enrichment

**MANIFESTO §2.1 feature**: *Ingestion Control — convenience functions for configuring,
controlling, and managing data ingestion in one place.*

### Scenario

Imazon's Oracle data warehouse holds 500+ tables built over 15 years. Standard DataHub
connectors captured schema metadata — table names, column types, primary keys — but missed
the rich business context stored outside the database: Confluence pages describing editorial
taxonomy, Excel feeds from publishers mapping ISBNs to imprints, an internal API for genre
classification, and lineage hidden inside PL/SQL stored procedures that compute bestseller
rankings and royalty calculations.

### Without DataSpoke

Standard Oracle connector output: 500 tables with column types and keys — nothing else. No
business descriptions (stored in Confluence), no publisher metadata (Excel), no genre taxonomy
(API), no stored-proc lineage. Data consumers browse DataHub and see bare technical schemas
with no way to determine what `catalog.title_master` actually tracks or how
`reports.monthly_royalties` is computed.

### With DataSpoke

**Register a multi-source enrichment config (one place, one UI/API):**

```python
# PUT /api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf
{
  "name": "oracle_book_catalog_enriched",
  "platform": "oracle",
  "schedule": "0 2 * * *",

  "enrichment_sources": [
    {"type": "confluence", "space": "BOOK_DATA_DICTIONARY",
     "fields_mapping": {"description": "confluence.content.body",
                         "business_owner": "confluence.labels.owner",
                         "pii_classification": "confluence.labels.pii"}},
    {"type": "excel", "path": "s3://imazon-docs/publisher-feeds/isbn-imprint-mapping.xlsx",
     "fields_mapping": {"publisher_domain": "Imprint", "genre_taxonomy": "Genre_Path"}},
    {"type": "custom_api", "endpoint": "https://taxonomy-api.imazon.internal/genres",
     "fields_mapping": {"genre_hierarchy": "$.genre.path"}}
  ],

  "custom_extractors": [
    {"name": "plsql_lineage_parser", "module": "dataspoke.custom.oracle_lineage",
     "function": "extract_stored_proc_lineage"},
    {"name": "quality_rule_extractor", "module": "dataspoke.custom.oracle_quality",
     "function": "extract_check_constraints_as_rules"}
  ]
}
```

**Operational control surface.** Cross-dataset views (`GET /spoke/common/ingestion`) list every
ingestion config, its last run, failure counts, and enrichment coverage. A single screen drives
the full lifecycle: register → schedule → run (`POST …/method/run`, `dry_run` option) →
observe events (`…/event`) → disable.

**Enriched result — `catalog.title_master`:**

```yaml
Dataset: catalog.title_master
Platform: Oracle / DWPROD

# Base (standard connector)
Columns: 62 | Primary Key: isbn, edition_id

# Enriched — business context (Confluence)
Description: Master catalog of all book titles...
Owner: maria.garcia@imazon.com | Team: Catalog Engineering

# Enriched — publisher metadata (Excel)
Publisher Domain: All imprints | Genre Taxonomy: 4-level hierarchy

# Enriched — lineage (PL/SQL parser)
Upstream: publishers.feed_raw, editorial.review_queue, pricing.base_rates
Generated By: PROC_NIGHTLY_CATALOG_REFRESH
Downstream: recommendations.book_features, reports.catalog_summary

# Enriched — quality rules (CHECK constraints auto-extracted → Validation)
1. list_price > 0
2. publication_date <= SYSDATE
3. isbn IS NOT NULL AND LENGTH(isbn) IN (10, 13)
```

### DataHub Integration Points

Every enrichment phase maps to a DataHub aspect emitted via `DatahubRestEmitter`:

| Phase | Aspect | Purpose |
|-------|--------|---------|
| Base schema | `schemaMetadata` | Columns, types, keys |
| Business descriptions | `datasetProperties` | `description` from Confluence |
| PII / editorial tags | `globalTags` | `urn:li:tag:PII`, `urn:li:tag:Editorial_Reviewed` |
| Publisher classification | `datasetProperties.customProperties` | `publisher_domain`, `genre_taxonomy` |
| Ownership | `ownership` | Owner URN + `BUSINESS_OWNER` type |
| PL/SQL lineage | `upstreamLineage` | Source → target edges |
| Quality rules | `assertionInfo` | CHECK constraints as assertions (feeds UC2) |

DataHub stores what DataSpoke sends; DataSpoke owns the extraction, field mapping, and
orchestration.

### Outcome

| Aspect | Standard Connector | DataSpoke |
|--------|--------------------|-----------|
| Business descriptions | 0% | 89% (445/500) |
| Ownership | 0% | 74% (370/500) |
| Stored-proc lineage | Not supported | 210 edges |
| Quality rules | Manual only | 380 auto-extracted |
| Update frequency | Manual re-run | Automated daily |

---

## UC2: Validation — Recommendation Pipeline Quality & Predictive SLA

**MANIFESTO §2.1 feature**: *Validation — registration, execution, and management of
validation rules, including time-series rules. Supports dry-run, point-in-time historical
validation, and real-time APIs.*

### Scenario

Two quality needs coexist:
- The **recommendation pipeline** consumes `reviews.user_ratings` and
  `orders.fulfillment_status`. Bad data causes poor recommendations (~30% of production
  incidents).
- The **fulfillment SLA** promises "order placed → shipping label within 4 h". Breaches are
  costly; reactive alerting fires only *after* breach.

Both are validation problems, differing only in whether rules are point-in-time or time-series.

### Without DataSpoke

Data quality lives in ad-hoc SQL checks, DBT tests, and tribal knowledge. No unified rule
registry, no consolidated result history, no way to dry-run a pipeline against historical
partitions before promoting it. SLA monitoring is reactive — Ops reads dashboards *after*
breaches.

### With DataSpoke

**Register validation rules per dataset** (point-in-time + time-series in the same config):

```json
// PUT /api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf
{
  "schedule": { "cron": "0 */6 * * *", "manual": true },
  "rules": [
    { "rule_id": "auto", "type": "field",
      "column": "rating", "condition": "between",
      "min": 1, "max": 5,
      "partition": "event_date", "order": "desc" },

    { "rule_id": "auto", "type": "volume",
      "comparison": "ratio", "threshold": 0.8,
      "window": "7d", "partition": "event_date" },

    { "rule_id": "auto", "type": "custom",
      "name": "fulfillment_sla_timeseries",
      "sql": "SELECT AVG(ship_label_minutes) FROM orders.fulfillment_status
              WHERE event_date = :partition",
      "timeseries": {
        "lookback_days": 30,
        "forecast_model": "prophet",
        "alert_threshold": "2h_before_sla_breach"
      }}
  ]
}
```

**Three modes, one API:**

1. **Scheduled runs** — the cron schedule drives rule execution; results are persisted as
   `assertionRunEvent` timeseries aspects on DataHub.
2. **Manual / dry-run** — `POST …/method/run` with `{"dry_run": true}` runs rules without
   writing results. Used by coding agents as an **Online Verifier** before shipping a pipeline.
3. **Point-in-time historical** — `GET …/result?from=…&to=…&partition=…` returns
   per-partition results. Used to re-verify a fix against last week's data.

**Predictive SLA.** The `timeseries` extension on `custom` rules fits a forecast model over
historical assertion results and emits an early-warning event when the forecast crosses the
SLA threshold — hours before the deterministic breach.

```
┌────────────────────────────────────────────┐
│  Fulfillment SLA — predictive window       │
│                                            │
│  ship_label_minutes                        │
│   ┌───── SLA breach (240 min) ──────┐     │
│   │                                 │     │
│   │                     ╱───────────┘     │
│   │                ╱╱╱╱╱  ← forecast      │
│   │           ╱╱╱                         │
│   │      ╱╱╱╱                             │
│   └──────────────────── time ────────────▶│
│     ▲                                      │
│     DataSpoke: early-warning event fired   │
│              ~2 h before projected breach  │
└────────────────────────────────────────────┘
```

**Real-time Online Verifier.** Coding agents building new pipelines call the same validation
API with `dry_run: true` on their output dataset. DataSpoke closes the coding loop: *register
rule → generate pipeline → validate against rule → iterate*.

### DataHub Integration Points

All rules are written as DataHub assertions; all results are `assertionRunEvent` timeseries
aspects. DataSpoke adds:
- **DataSpoke extensions** on top of DataHub's Open Assertions Spec — `rule_id`, `partition`,
  `order`, `timeseries`.
- **Cross-dataset list view** — `GET /spoke/common/validation` aggregates configs across
  datasets for ops dashboards.
- **WebSocket stream** — `WS /spoke/common/data/{urn}/stream/validation` surfaces live run
  progress.

### Outcome

| Dimension | Before | After |
|-----------|--------|-------|
| Recommendation pipeline incident rate | ~30% | < 5% |
| Fulfillment SLA breaches | Reactive | Predicted 2+ h early — zero breaches in month 1 |
| Pipeline iteration | Ship → fix in prod | Dry-run validation before ship |

---

## UC3: Ontology — Post-Acquisition Ontology Construction & Discovery

**MANIFESTO §2.1 feature**: *Ontology — beyond baseline data documentation, analyses source
code (GitHub), SQL logs, external documents, and more to autonomously construct an ontology,
maintained in a graph DB and a vector DB.*

### Scenario

Imazon acquires **eBookNow**, a digital-only book platform. Post-merger, the combined DataHub
catalog has 700+ datasets — 200 from eBookNow — with overlapping concepts. Six tables describe
"a book/product" differently:

```
Imazon (legacy):                eBookNow (acquired):
 catalog.title_master            products.digital_catalog
 catalog.editions                content.ebook_assets
 inventory.book_stock            storefront.listing_items
```

Analysts searching "book" get six answers and cannot tell which to use. The governance team
cannot manually audit 700 datasets.

### Without DataSpoke

Ontology lives in engineers' heads and stale Confluence pages. Each new analyst re-learns which
of the six "book" tables to query. Cross-company lineage is unmapped. Recommendation engines
double-count titles available in both print and digital.

### With DataSpoke

#### Construction — autonomous ontology building

DataSpoke reads DataHub metadata, linked source code (`ebooknow/catalog-service`,
`imazon/pricing-engine`), SQL query logs, and Confluence exports. An LLM (via LangChain)
reasons across inputs to build an ontology graph, persisted in **PostgreSQL with `age` (graph)
and `pgvector` (vector) extensions**.

```python
# Conceptual build pipeline (runs on schedule + incrementally on new ingests)

inputs = load_inputs(
    datahub_aspects=["schemaMetadata", "datasetProperties", "globalTags",
                     "ownership", "upstreamLineage"],
    source_code_refs=github_repos,
    sql_logs=query_history,
    external_docs=confluence_exports,
)

concepts      = llm_classify(inputs)                # dataset → concept(s)
hierarchy     = llm_build_hierarchy(concepts)       # concept tree
relationships = llm_infer_relationships(concepts)   # concept-to-concept edges
embeddings    = embed(concepts + datasets)          # for vector recall

persist_graph(concepts, hierarchy, relationships)   # PostgreSQL age
persist_vectors(embeddings)                         # PostgreSQL pgvector
queue_low_confidence(concepts, threshold=0.7)       # → human review
```

**Output (excerpt):**

```
Concept: BOOK / PRODUCT (confidence 0.94)
  ├─ variant: PRINT
  │     catalog.title_master       (primary, ISBN-keyed)
  │     catalog.editions           (edition-format view)
  │     inventory.book_stock       (warehouse instance)
  └─ variant: DIGITAL
        products.digital_catalog   (primary, product_id-keyed)
        content.ebook_assets       (file/DRM view)
        storefront.listing_items   (marketplace instance)

Cross-concept relationship: PRINT.ISBN ↔ DIGITAL.product_id
  Evidence: 72% record overlap by ISBN match
            Shared downstream consumer: recommendations.book_features
  Status: proposal (awaiting governance approval)
```

#### Consumption — discovery and navigation

The ontology becomes the navigation substrate for everyone else:

| Consumer | How it uses the ontology |
|----------|--------------------------|
| **Doc Generation (UC4)** | Proposes descriptions and merge/deprecate decisions grounded in concept membership |
| **Governance (UC5)** | Rolls up health metrics along the concept tree — "BOOK coverage 84%" |
| **Coding agents** | Retrieve the authoritative dataset for a concept before generating SQL |
| **Analysts browsing the UI** | Start from a concept, drill to member datasets, see confidence and rationale |

**Ontology APIs** (`/spoke/common/ontology/…`) — list concepts, drill to a concept, inspect
attributes and change history, approve/reject proposals. Low-confidence concepts are queued
for governance review; the LLM attaches rationale to each proposal to speed human decisions.

### DataHub Integration Points

- **Inputs**: `schemaMetadata`, `datasetProperties`, `globalTags`, `ownership`,
  `upstreamLineage`, plus `datasetUsageStatistics` for popularity weighting.
- **Outputs**: concept membership is written back as `globalTags`
  (e.g. `urn:li:tag:concept:book`) and as `glossaryTerm` attachments so the DataHub UI reflects
  the ontology. The full graph structure (hierarchy, cross-concept edges, confidence) lives in
  PostgreSQL — DataHub is not a graph store.

### Outcome

| Dimension | Before | After |
|-----------|--------|-------|
| Conceptual overlap detection | Manual, months | Autonomous, hours |
| Ontology substrate | None / Confluence | PostgreSQL graph + vector |
| Analyst time to "which table do I query?" | 30–60 min | < 1 min via concept navigation |
| Re-builds on new ingest | — | Incremental, affected-datasets only |

---

## UC4: Doc Generation — Doc Proposal & Human-in-the-Loop Review

**MANIFESTO §2.1 feature**: *Doc Generation — based on the ontology, inspects the state of
data documentation and proposes documents via generative AI, including APIs and a review
process.*

### Scenario

The merged Imazon + eBookNow catalog has 700 datasets. Documentation coverage is uneven: 64%
of datasets have descriptions, 38% of columns do. For overlapping concepts (from UC3), existing
descriptions contradict each other. The governance team needs descriptions written,
inconsistencies resolved, and merge/deprecate decisions proposed — at a pace humans cannot
sustain.

### Without DataSpoke

Documentation is written manually when anyone cares. Contradictions persist because no one runs
a consistency check. Post-acquisition reconciliation takes 3+ months of governance team time.

### With DataSpoke

**Register a generation config per dataset** (or bulk-apply to a concept):

```json
// PUT /api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf
{
  "targets": ["dataset.description", "column.description",
              "tag.suggested", "ontology.alignment"],
  "period": "weekly",
  "ontology_context": "concept:book",
  "active": true
}
```

**Grounded generation.** The generator reads the UC3 ontology, the dataset's
schema/usage/lineage/source-code references, and any existing documentation. It emits
**proposals** (never direct writes):

```
Proposal: products.digital_catalog

dataset.description (proposed, confidence 0.91):
  "Digital-book catalog acquired from eBookNow. One row per product_id
   (not ISBN — see ontology note). Authoritative source for digital
   pricing and DRM metadata; not authoritative for print editions —
   see catalog.title_master."

column.description proposals: 34 of 41 columns
  product_id    — "Primary key; opaque identifier minted by eBookNow's
                   catalog service (not ISBN). Maps to catalog.title_master.isbn
                   via the PRINT↔DIGITAL bridge; join is lossy (72% match)."
  creator       — "Free-text author/creator name. Unlike catalog.title_master,
                   this is not a normalised FK — downstream joins should first
                   resolve via fuzzy matching."

Ontology alignment (proposed):
  Concept: BOOK / PRODUCT (variant: DIGITAL)
  Deprecation recommendation: None — keep as DIGITAL variant.
  Merge recommendation: None — structural differences preclude table merge.
  Relationship proposal: FK product_id ↔ catalog.title_master.isbn
                         (type: conceptual, confidence 0.78, lossy)
```

**Review process.** Proposals enter a review queue. The governance lead (or dataset owner)
approves, edits, or rejects:

```
UI: Pending Doc Proposals                          47 pending | 12 blocked
───────────────────────────────────────────────────────────────────────
▸ products.digital_catalog          34 columns   confidence 0.91  [Review]
▸ content.ebook_assets              22 columns   confidence 0.88  [Review]
▸ catalog.title_master              12 columns   confidence 0.94  [Review]
▸ ...
```

- **Approve** → `POST …/attr/gen/method/apply` — DataSpoke writes the proposal to DataHub
  (`datasetProperties.description`, `schemaMetadata.fields[].description`, `globalTags`,
  glossary term links).
- **Edit** → reviewer adjusts, then approves.
- **Reject** → proposal is archived; the model notes the rejection reason to improve future
  proposals.

**Consistency inspection.** On every run, the generator also reports *existing* documentation
that contradicts the ontology — e.g. a column described as "ISBN" that is actually a product_id
per ontology evidence. These are flagged as purification candidates (self-purification from
MANIFESTO §1).

### DataHub Integration Points

| Direction | Aspect | Purpose |
|-----------|--------|---------|
| Read | `datasetProperties`, `schemaMetadata`, `datasetUsageStatistics`, `upstreamLineage` | Context for generation |
| Write (on approval) | `datasetProperties.description`, `schemaMetadata.fields[].description`, `globalTags`, `glossaryTerms` | Applied proposal |
| Timeseries | `datasetProperties` history | Audit of what was generated vs. what was approved |

### Outcome

| Dimension | Manual | DataSpoke + review |
|-----------|--------|--------------------|
| Post-acquisition reconciliation | 3 months | Days |
| Description coverage (dataset) | 64% | 96% |
| Description coverage (column) | 38% | 87% |
| Contradictions surfaced | Unknown | 142 flagged, 128 resolved |

---

## UC5: Governance — Enterprise Metadata Health & Multi-Perspective Overview

**MANIFESTO §2.1 feature**: *Governance — APIs for configuring and monitoring governance
metrics such as documentation coverage and data freshness.*

### Scenario

Imazon's Chief Data Officer launches a company-wide initiative to improve data documentation,
ownership accountability, and ingestion freshness. Six departments manage 700+ datasets.
Coverage varies wildly. Quarterly manual audits take 2 weeks and go stale immediately. The CDO
also wants a visual overview of the estate — by concept (from UC3), by ownership, by medallion
layer — to spot blind spots.

### Without DataSpoke

Governance team runs manual audits: review tables, build spreadsheet, email department leads,
follow up in 2 weeks. **Problems:** labour-intensive, point-in-time, no trend data, hard to
measure improvement. Estate visualisation is whiteboard diagrams.

### With DataSpoke

#### Metrics — named, scheduled, time-series

A metric is a named measurement — e.g. "undocumented high-usage datasets" — with a definition
(`attr/conf`) controlling how it's computed, and a time-series of results (`attr/result`).
Metrics *aggregate* existing DataHub metadata and DataSpoke validation results; they never
read source databases directly.

```json
// PUT /api/v1/spoke/dg/metric/{metric_id}/attr/conf
{
  "title": "Documentation coverage — Marketing",
  "theme": "documentation",
  "measurement_query": {
    "dataset_filter": {
      "tags": ["urn:li:tag:department:marketing"]
    },
    "aggregation": "pct_with_description"
  },
  "schedule_tier": "daily",
  "active": true
}
```

**Enterprise dashboard (week 1):**

```
DataSpoke Governance — Enterprise Metadata Health   Score: 59/100

Department Breakdown
┌─────────────────────┬────────┬──────────┬────────┬─────────┐
│ Department          │ Score  │ Datasets │ Issues │ Trend   │
├─────────────────────┼────────┼──────────┼────────┼─────────┤
│ Engineering         │ 76/100 │ 95       │ 23     │ ↑ +3%   │
│ Data Science        │ 69/100 │ 72       │ 22     │ → 0%    │
│ Marketing           │ 54/100 │ 80       │ 37     │ ↓ -2%   │
│ Finance             │ 81/100 │ 38       │  7     │ ↑ +5%   │
│ Operations          │ 45/100 │ 65       │ 36     │ → 0%    │
│ Publisher Relations │ 40/100 │ 55       │ 33     │ ↓ -1%   │
└─────────────────────┴────────┴──────────┴────────┴─────────┘

Critical: 42 | High: 78 | Medium: 118
```

DataSpoke emails owners with specific action items, estimated fix time, and projected score
impact. Progress tracks over time — **month 3**: enterprise score 77/100, all departments above
threshold, documentation decay rate -2.1%/month.

#### Multi-perspective overview

Some governance views cannot be expressed as per-metric time-series. `/spoke/dg/overview` provides:

- **Ontology graph view** — datasets coloured/sized by documentation coverage, overlaid on the
  UC3 concept graph. Clusters of red = concept-wide gaps.
- **Medallion layer coverage** — bronze/silver/gold classification (from DataHub `globalTags`)
  with per-layer freshness and ownership heatmaps.
- **Ownership topology** — datasets grouped by owner; orphans (no owner assigned) are called
  out.

These are all **read-only aggregations** of DataHub aspects + DataSpoke validation and ontology
state. They share the same governance surface (`/spoke/dg/…`) and are scoped to the same
role-based access control as metrics.

### DataHub Integration Points

| Metric input | DataHub aspect | Purpose |
|--------------|----------------|---------|
| Description coverage | `datasetProperties` | Presence of `description` |
| Owner assignment | `ownership` | Owner URN list — empty = unassigned |
| Column documentation | `schemaMetadata` | Per-column `description` presence |
| Tag coverage | `globalTags` | PII / concept tags present |
| Usage popularity | `datasetUsageStatistics` (timeseries) | Prioritise high-usage gaps |
| Entity enumeration | GraphQL: `scrollAcrossEntities` | Iterate all datasets per filter |

Freshness metrics additionally read ingestion-event history (UC1) and assertion-result history
(UC2) — all via the DataSpoke API, not direct DB access.

### Outcome

| Metric | Manual quarterly audit | DataSpoke Governance |
|--------|------------------------|----------------------|
| Audit cycle | 2 weeks, quarterly | Real-time, continuous |
| Issue response time | 12 days avg | 3 days avg |
| Health score improvement | Unmeasured | 59 → 77 in 3 months |
| Governance team effort | 100% manual | -80% |
| Estate visualisation | Whiteboard | Ontology + medallion + ownership views |

---

## Summary: Value Delivered

| UC | Feature | Traditional Approach | With DataSpoke | Improvement |
|----|---------|----------------------|----------------|-------------|
| UC1 | Ingestion Control | Manual metadata, no lineage | Multi-source enrichment, one control surface | 89% description coverage, 210 lineage edges, 380 auto-extracted rules |
| UC2 | Validation | Ad-hoc checks, reactive alerts | Unified rule registry, dry-run, predictive time-series | Incidents 30% → <5%; SLA breaches predicted 2+h early |
| UC3 | Ontology | Tribal knowledge, stale Confluence | Autonomous LLM-built graph + vector ontology | 700-dataset ontology in hours; authoritative concept lookup |
| UC4 | Doc Generation | 3-month manual reconciliation | Grounded proposals + review workflow | 64% → 96% dataset coverage; 142 contradictions surfaced |
| UC5 | Governance | Quarterly spreadsheet audits | Real-time metrics + multi-perspective overview | 59 → 77 health score in 3 months; 80% effort reduction |

### Cross-cutting themes

- **Self-Organization (MANIFESTO §1)** — UC3 builds the ontology; UC4 and UC5 consume it.
- **Self-Purification (MANIFESTO §1)** — UC4 surfaces inconsistencies between documentation
  and ontology; UC5 rolls them up as governance metrics.
- **Online Verifier (MANIFESTO §1)** — UC2 dry-run validation closes the loop for coding agents
  building new pipelines.
- **Shared substrate** — the ontology graph + vector DB from UC3 is the backbone of UC4 and
  UC5; the assertion framework from UC2 feeds UC5 freshness metrics.
