# DataSpoke Frontend — Data Analysis (DA) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md) (highest authority).
> Layout and shared components in [FRONTEND_BASIC](FRONTEND_BASIC.md).
> API routes in [API](../API.md). Backend services in [BACKEND](BACKEND.md).

---

## Table of Contents

1. [Overview](#overview)
2. [Navigation](#navigation)
3. [Ontology Generation Navigation (UC3)](#ontology-generation-navigation-uc3)
4. [Validation — Fitness for Use (UC2)](#validation--fitness-for-use-uc2)

---

## Overview

The DA workspace is a **discovery-first** surface for analysts. The primary entry is the
**Ontology Generation** browser — a single-level peer-concept set built by UC3 that lets
analysts navigate from a business concept (e.g. `BOOK`) down to the authoritative dataset.
Validation appears with a **fitness-for-use** framing so analysts can judge whether a
candidate dataset is reliable enough for a report or model.

The DA tier (`/spoke/da/` routes, `/da` UI pages) is an extensibility surface — the baseline
DataSpoke product ships no DA-exclusive features, and this workspace consumes baseline features
under `/spoke/common/…` with DA-flavoured presentation. Organizations customising DataSpoke can
add DA-exclusive routes and pages here.

---

## Navigation

```
┌───────────┐
│  DA       │
│  ───────  │
│  Home     │
│  Ontogen  │
│  Valid.   │
│  ───────  │
│  [DE][DG] │
└───────────┘
```

| Item | Route | API Base |
|------|-------|----------|
| Home | `/da` | — |
| Ontology Generation | `/da/ontogen` | `/spoke/common/ontogen/` |
| Validation | `/da/validation` | `/spoke/common/validation/` |

The DA home page features concept navigation prominently — discovery-first UX.

---

## Ontology Generation Navigation (UC3)

### Concept Browser (`/da/ontogen`)

Browse the **single-level** peer-concept set. Each concept card shows member datasets,
confidence, and a short LLM-generated rationale. Clicking a concept drills into its
member datasets and outgoing relationships to other peer concepts.

```
┌────────────────────────────────────────────────────────────┐
│  DataSpoke — Concepts                                      │
│                                                            │
│  BOOK                                          conf 0.96   │
│    members:                                                │
│      catalog.books              (primary)                  │
│                                                            │
│  CUSTOMER                                      conf 0.94   │
│    members:                                                │
│      customers.profiles         (primary)                  │
│                                                            │
│  ORDER_LINE                                    conf 0.71   │
│    members:                                                │
│      orders.line_items          (primary)                  │
│    relationships:                                          │
│      → BOOK     (references, conf 0.95)                    │
│      → CUSTOMER (placed_by,  conf 0.87)                    │
└────────────────────────────────────────────────────────────┘
```

### Concept Detail (`/da/ontogen/[concept_id]`)

Shows member datasets, attributes, cross-concept relationships, and change history. For
proposals awaiting review, displays the LLM rationale; approve/reject actions are gated to
users with governance permissions — DA users see but cannot approve.

| Element | Source | Behaviour |
|---------|--------|-----------|
| Concept card header | `GET /spoke/common/ontogen/{concept_id}` | Name, confidence, description, status |
| Attributes panel | `GET /spoke/common/ontogen/{concept_id}/attr` | Confidence, source evidence |
| Member datasets | Ontology Generation service | Link each dataset to `/da/dataset/{urn}` |
| Cross-concept edges | `GET /spoke/common/ontogen/{concept_id}` (relationships array) | Rendered as arrows to peer concepts |
| Change history | `GET /spoke/common/ontogen/{concept_id}/event` | Timestamped feed |

---

## Validation — Fitness for Use (UC2)

DA validation reuses the same validation infrastructure as DE
(see [FRONTEND_DE §Validation](FRONTEND_DE.md#validation--sla-uc2)) but with a
**fitness-for-use** framing: an analyst looking at `orders.purchase_history` wants to know
"is this trustworthy enough to power my report?" — not "what's broken in the upstream pipeline?".

### DA Validation List (`/da/validation`)

Same layout as DE validation list. Differences:

| Aspect | DE Framing | DA Framing |
|--------|------------|------------|
| Score label | "Quality Score" | "Fitness Score" |
| Key checks | Completeness, freshness, assertions | Certification, schema stability, freshness |
| Primary action | "Run Validation" | "Check Fitness" |
| Recommendations | Pipeline-oriented | Dashboard/reporting-oriented |

### DA Dataset Detail (`/da/dataset/[dataset_urn]`)

Tabs: **Overview | Fitness Check | Concept**

The **Concept** tab surfaces the dataset's ontology membership (which concept, which variant,
cross-concept relationships) so the analyst sees the authoritative dataset for the concept if a
better option exists.

```
┌────────────────────────────────────────────────────────────┐
│  ← orders.purchase_history                                 │
│  Platform: Oracle  │  Certified for Reporting ✓            │
│  Fitness: 94/100   │  Schema stable 90 days ✓              │
│                                                            │
│  [ Overview | Fitness Check | Concept ]                    │
│  ─────────────────────────────────────────                 │
│                                                            │
│  (tab content)                                             │
│                                                            │
└────────────────────────────────────────────────────────────┘
```
