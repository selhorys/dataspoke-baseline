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
**Ontology Generation** browser — a subject / predicate / object triple ontology built
by UC3 (nodes, edges, triples) that lets analysts navigate from a business concept node
(e.g. `BOOK`) down to the authoritative dataset, and along approved triples to peer
nodes.
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

The DA home page features node navigation prominently — discovery-first UX.

---

## Ontology Generation Navigation (UC3)

The ontology follows a **subject / predicate / object triple model**. The DA browser
exposes three tabs — **Nodes** (subjects / objects), **Edges** (predicates), and
**Triples** (facts) — each independently reviewable. DA users see all three but cannot
approve; approval is gated to governance permissions.

### Triple-model Browser (`/da/ontogen`)

```
┌────────────────────────────────────────────────────────────┐
│  DataSpoke — Ontology     [ Nodes | Edges | Triples ]      │
│                                                            │
│  Nodes                                                     │
│    BOOK              conf 0.96   member: catalog.books     │
│    CUSTOMER          conf 0.94   member: customers.profiles│
│    ORDER_LINE        conf 0.71   member: orders.line_items │
│                                                            │
│  Edges                                                     │
│    references        conf 0.95   foreign-key reference     │
│    placed_by         conf 0.87   agent / actor             │
│                                                            │
│  Triples (subject — predicate — object)                    │
│    ORDER_LINE  --references--> BOOK       conf 0.95        │
│    ORDER_LINE  --placed_by --> CUSTOMER   conf 0.87        │
└────────────────────────────────────────────────────────────┘
```

### Node / Edge / Triple Detail

| Element | Source | Behaviour |
|---------|--------|-----------|
| Node card header | `GET /spoke/common/ontogen/result/node/{node_id}` | Name, confidence, description, status |
| Node attributes panel | `GET /spoke/common/ontogen/result/node/{node_id}/attr` | Confidence, source evidence |
| Member datasets | Ontology Generation service | Link each dataset to `/da/dataset/{urn}` |
| Node change history | `GET /spoke/common/ontogen/result/node/{node_id}/event` | Timestamped feed |
| Edge card header | `GET /spoke/common/ontogen/result/edge/{edge_id}` | Label, confidence, semantics, status |
| Edge attributes panel | `GET /spoke/common/ontogen/result/edge/{edge_id}/attr` | Confidence, source evidence |
| Edge change history | `GET /spoke/common/ontogen/result/edge/{edge_id}/event` | Timestamped feed |
| Triple card | `GET /spoke/common/ontogen/result/triple/{triple_id}` | Resolved subject node, edge, object node |
| Triple attributes panel | `GET /spoke/common/ontogen/result/triple/{triple_id}/attr` | Confidence, source evidence |
| Triple change history | `GET /spoke/common/ontogen/result/triple/{triple_id}/event` | Timestamped feed |
| Outgoing triples on a node | `GET /spoke/common/ontogen/result/triple?subject_node_id={id}` | Rendered as arrows to peer nodes |

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

Tabs: **Overview | Fitness Check | Ontology**

The **Ontology** tab surfaces the dataset's ontology membership (which node, which member
variant, the approved triples leading to peer nodes) so the analyst sees the authoritative
dataset for the node if a better option exists.

```
┌────────────────────────────────────────────────────────────┐
│  ← orders.purchase_history                                 │
│  Platform: Oracle  │  Certified for Reporting ✓            │
│  Fitness: 94/100   │  Schema stable 90 days ✓              │
│                                                            │
│  [ Overview | Fitness Check | Ontology ]                   │
│  ─────────────────────────────────────────                 │
│                                                            │
│  (tab content)                                             │
│                                                            │
└────────────────────────────────────────────────────────────┘
```
