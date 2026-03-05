# DataSpoke Frontend — Data Analysis (DA) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Layout and shared components in [FRONTEND_BASIC](FRONTEND_BASIC.md).
> API routes in [API](API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Navigation](#navigation)
3. [Natural Language Search (UC5)](#natural-language-search-uc5)
4. [Text-to-SQL Context (UC7)](#text-to-sql-context-uc7)
5. [Validation (UC2)](#validation-uc2)

---

## Overview

The DA workspace prioritizes **discovery and understanding** of data. Its primary surface is natural language search, enriched with text-to-SQL metadata for AI-assisted query generation. Validation is shared with DE but with a fitness-for-use focus. All features consume `/api/v1/spoke/common/` routes.

---

## Navigation

```
┌───────────┐
│  DA       │
│  ───────  │
│  Home     │
│  Search   │
│  Valid.   │
│  ───────  │
│  [DE][DG] │
└───────────┘
```

| Item | Route | API Base |
|------|-------|----------|
| Home | `/da` | — |
| Search | `/da/search` | `/spoke/common/search` |
| Validation | `/da/validation` | `/spoke/common/validation/` |

The DA home page features the search bar prominently — search-first UX.

---

## Natural Language Search (UC5)

### Search Page (`/da/search`)

Full-page search experience. The search bar is the hero element.

```
┌────────────────────────────────────────────────────────────┐
│                                                            │
│                     DataSpoke Search                       │
│                                                            │
│  ┌──────────────────────────────────────────────────┐     │
│  │  Find tables with European customer PII used     │     │
│  │  by marketing analytics                       [→]│     │
│  └──────────────────────────────────────────────────┘     │
│                                                            │
│  Recent: "order tables" "PII audit" "shipping metrics"     │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

Submits to `GET /spoke/common/search?q=...`.

### Search Results

```
┌────────────────────────────────────────────────────────────┐
│  ┌────────────────────────────────────────────────────┐   │
│  │  European customer PII in marketing [Clear]     [→]│   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  7 results (2.3s)                    [SQL Context: OFF v]  │
│                                                            │
│  ┌─ HIGH PRIORITY ───────────────────────────────────┐    │
│  │                                                    │    │
│  │  1. customers.eu_profiles              Score: 98%  │    │
│  │     PII: email, full_name, shipping_address        │    │
│  │     Tags: EU/GDPR, PII                             │    │
│  │     Quality: 94/100  │  Owner: data-governance     │    │
│  │     ┌ Lineage ─────────────────────────────────┐   │    │
│  │     │ → marketing.eu_email_campaigns (active)  │   │    │
│  │     │   → dashboards.eu_campaign_performance   │   │    │
│  │     └──────────────────────────────────────────┘   │    │
│  │     Compliance: ✓ Retention ✓ Encryption ▲ Delete  │    │
│  │                                                    │    │
│  │  2. orders.eu_purchase_history         Score: 94%  │    │
│  │     PII: customer_id (linkable), shipping_address  │    │
│  │     ...                                            │    │
│  │                                                    │    │
│  ├─ MEDIUM PRIORITY ─────────────────────────────────┤    │
│  │                                                    │    │
│  │  3. marketing.eu_reader_segments       Score: 87%  │    │
│  │     PII: hashed_email (derived)                    │    │
│  │     ...                                            │    │
│  │                                                    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                            │
│  ┌─ Follow-up ───────────────────────────────────────┐    │
│  │  Which tables lack automated right-to-deletion? [→]│    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### Search UX Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Result grouping | Priority tiers (high/medium/low) | Matches compliance audit workflow |
| Inline lineage | Expandable lineage tree per result | Analysts need to trace data flow without leaving search |
| Follow-up bar | Persistent at bottom | Supports conversational refinement (UC5 scenario) |
| SQL Context toggle | Opt-in via toggle | Keeps default results clean; SQL metadata is verbose |
| Click target | Result row → dataset detail | Navigate to `/da/dataset/[urn]` for full view |

### Result Card Anatomy

Each search result card shows:
- **Dataset name** + relevance score
- **PII fields** (highlighted if PII-related query)
- **Tags** — filterable badges
- **Quality score** + owner
- **Lineage preview** — expandable 2-level downstream tree
- **Compliance summary** — icons for retention, encryption, deletion status

---

## Text-to-SQL Context (UC7)

Activated by toggling **SQL Context: ON** on the search results page. Adds `?sql_context=true` to the search query.

When enabled, each search result card expands to include SQL-optimized metadata:

```
┌────────────────────────────────────────────────────────────┐
│  1. catalog.title_master                      Score: 95%   │
│     ...standard result fields...                           │
│                                                            │
│  ┌─ SQL Context ─────────────────────────────────────┐    │
│  │                                                    │    │
│  │  Key Columns:                                      │    │
│  │  ┌────────────┬───────────────────────────────┐   │    │
│  │  │ genre_code │ FIC-001, NF-002, SCI-003...   │   │    │
│  │  │            │ Cardinality: 48                │   │    │
│  │  │            │ → maps to genre_hierarchy.code │   │    │
│  │  ├────────────┼───────────────────────────────┤   │    │
│  │  │ isbn       │ 978-0-13-468599-1, ...        │   │    │
│  │  │            │ Cardinality: 12,345            │   │    │
│  │  └────────────┴───────────────────────────────┘   │    │
│  │                                                    │    │
│  │  Recommended Join Path:                            │    │
│  │  purchase_history → order_items → editions         │    │
│  │    → title_master → genre_hierarchy                │    │
│  │  Confidence: 0.95                                  │    │
│  │                                                    │    │
│  │  Sample Query:                                     │    │
│  │  ┌────────────────────────────────────────────┐   │    │
│  │  │ SELECT gh.display_name, COUNT(*)           │   │    │
│  │  │ FROM orders.order_items oi                 │   │    │
│  │  │ JOIN catalog.editions e ON ...             │   │    │
│  │  │ ...                                [Copy]  │   │    │
│  │  └────────────────────────────────────────────┘   │    │
│  │                                                    │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### SQL Context Components

| Component | Source | Interaction |
|-----------|--------|-------------|
| Column profiles | `sql_context=true` response `column_profiles` | Read-only display |
| Join paths | `sql_context=true` response `join_paths` | Visual path diagram, expandable |
| Sample queries | `sql_context=true` response `sample_queries` | Copy-to-clipboard, expandable SQL block |
| Date conventions | `sql_context=true` response `date_conventions` | Inline tooltip on date columns |

---

## Validation (UC2)

DA validation reuses the same validation infrastructure as DE (see [FRONTEND_DE §Validation](FRONTEND_DE.md#validation--sla-uc2-uc3)) but with a **fitness-for-use** framing.

### DA Validation List (`/da/validation`)

Same layout as DE validation list. Differences:

| Aspect | DE Framing | DA Framing |
|--------|-----------|-----------|
| Score label | "Quality Score" | "Fitness Score" |
| Key checks | Completeness, freshness, assertions | Certification, schema stability, freshness |
| Primary action | "Run Validation" | "Check Fitness" |
| Recommendations | Pipeline-oriented | Dashboard/reporting-oriented |

### DA Dataset Detail (`/da/dataset/[dataset_urn]`)

Tabs: **Overview | Fitness Check | SQL Context**

The SQL Context tab shows the text-to-SQL metadata for a single dataset (same data as the expanded search result card).

```
┌────────────────────────────────────────────────────────────┐
│  ← orders.purchase_history                                 │
│  Platform: Oracle  │  Certified for Reporting ✓            │
│  Fitness: 94/100   │  Schema stable 90 days ✓              │
│                                                            │
│  [ Overview | Fitness Check | SQL Context ]                │
│  ─────────────────────────────────────────                 │
│                                                            │
│  (tab content)                                             │
│                                                            │
└────────────────────────────────────────────────────────────┘
```
