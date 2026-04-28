# DataSpoke Frontend — Data Governance (DG) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared layer in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

DG is the only workspace that hosts baseline routes (`/spoke/dg/metric` and
`/spoke/dg/overview`) and the only one that exposes ontogen approval actions
(`POST /spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review`).

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/dg/metrics` | Metrics dashboard | `/spoke/dg/metric`, `/spoke/dg/overview` |
| `/dg/metrics/list` | Metric list | `/spoke/dg/metric` |
| `/dg/metrics/[id]` | Metric detail | `/spoke/dg/metric/{id}` |
| `/dg/overview` | Multi-perspective overview | `/spoke/dg/overview` |
| `/dg/ontogen` | Ontogen review | `/spoke/common/ontogen/...` |
| `/dg/ontogen/conf` | Ontogen conf editor | `/spoke/common/ontogen/attr/conf` |
| `/dg/ontogen/seed` | Ontogen seeds editor | `/spoke/common/ontogen/attr/seed/...` |

---

## Page contracts

### Metrics (UC5)

| Page | Read | Write |
|---|---|---|
| `/dg/metrics` (dashboard) | `GET /spoke/dg/metric`, `GET /spoke/dg/overview` (per-dataset breakdown + blind spots from the same call) | — |
| `/dg/metrics/list` | `GET /spoke/dg/metric` (paginated, filter by `theme`, `status`) | — |
| `/dg/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to`, `GET .../event` | `PUT/PATCH/DELETE .../attr/conf` (fields: `title`, `theme`, `measurement_query`, `schedule_tier`, `is_enabled`); `POST .../method/run` (`{dry_run?}`) |

`measurement_query.dataset_filter` carries three OR-ed dimensions:
`tags[]` (DataHub tag URNs), `glossary_terms[]` (glossary term URNs), and
`dataset_urns[]` (explicit dataset URNs for pinning). The metric config form
exposes all three. URN format is validated at `PUT/PATCH` time
(`422 INVALID_DATASET_URN`); URNs that fail to resolve at run time are listed
in the `METRIC.RUN_COMPLETE` event's `unresolved_urns` field.

Baseline metrics: `ingestion-freshness`, `validation-score`. Aggregations
ship with `pct_fresh` and `pct_rules_passing`; unsupported aggregations
return `422 INVALID_PARAMETER`.

```
┌──────────────────────────────────────────────────────┐
│  Metrics                                             │
├──────────────────────────────────────────────────────┤
│  ingestion-freshness:  92%  ↑     validation: 87% ↑  │
│                                                      │
│  Per-dataset breakdown (overview.metrics[*].breakdown)│
│    catalog.books         fresh ✓   validation 96%    │
│    orders.line_items     fresh ✓   validation 72%    │
│    customers.profiles    fresh ✓   validation 91%    │
│    orders.shipments      fresh ✗   validation —      │
│                                                      │
│  Blind spots (overview.blind_spots[])                │
│    publishers.feed_raw                               │
│    shipping.carrier_raw_v1                           │
└──────────────────────────────────────────────────────┘
      Dashboard (`/dg/metrics`) ← `/spoke/dg/{metric,overview}`
```

```
┌──────────────────────────────────────────────────────┐
│  ← ingestion-freshness    [Edit] [Run] [Disable]     │
├──────────────────────────────────────────────────────┤
│  attr/conf                                           │
│    theme: freshness  schedule_tier: hourly  ✓ enabled│
│                                                      │
│  attr/result?from&to                                 │
│    [Recharts area chart of measurement value]        │
│                                                      │
│  event  (METRIC.RUN_COMPLETE …)                      │
│    2026-04-25 measured: 92                           │
│    2026-04-24 measured: 88                           │
└──────────────────────────────────────────────────────┘
        Detail (`/dg/metrics/[id]`)
```

```
┌────────────────────────────────────────────────┐
│  Metric definition                             │
├────────────────────────────────────────────────┤
│  title:          [ingestion-freshness       ]  │
│  theme:          [freshness               v]   │
│  measurement_query.aggregation:                │
│                  [pct_fresh               v]   │
│  schedule_tier:  [ hourly | daily | weekly v]  │
│  is_enabled:     [x]                           │
│                                                │
│  measurement_query.dataset_filter (OR-ed)      │
│    tags[]:           [urn:li:tag:env:PROD,]    │
│    glossary_terms[]: [urn:li:glossaryTerm:…,]  │
│    dataset_urns[]:   [urn:li:dataset:(…),]     │
│                                                │
│  [Cancel]                            [Save]    │
└────────────────────────────────────────────────┘
        Config form (PUT/PATCH .../attr/conf)
```

### Multi-Perspective Overview (UC5)

Single page consuming `GET /spoke/dg/overview`. Five views render the
response sub-fields verbatim — no derived analysis on top:

| View | Source field | Display |
|---|---|---|
| Metric Values | `overview.metrics[]` | Per-metric latest value + 90-day trend |
| Blind Spots | `overview.blind_spots[]` | Datasets present in DataHub but not mapped to any approved UC3 ontology node |
| Ontology Graph | `overview.ontology` | UC3 nodes + approved triples — labelled directed graph (nodes are subjects/objects, edges are predicates) |
| Medallion | `overview.medallion` | Bronze / Silver / Gold / Unknown layer counts |
| Ownership | `overview.ownership` | Owner / team coverage from DataHub `ownership` aspect |

Visualization config persists via `GET/PATCH /spoke/dg/overview/attr`.
Settings are limited to layout / status / size / confidence-threshold filters
that the overview response can answer locally — no fields beyond what
`overview.attr` returns.

```
┌──────────────────────────────────────────────────────┐
│  Data Estate Overview                                │
│  [Metrics] [Blind Spots] [Ontology] [Medallion] […]  │
├──────────────────────────────────────────────────────┤
│  Ontology view (overview.ontology)                   │
│                                                      │
│      ┌──────┐  references   ┌───────────┐            │
│      │ BOOK │ ◀──────────── │ ORDER_LINE│            │
│      └──────┘               └─────┬─────┘            │
│                                   │ placed_by        │
│                                   ▼                  │
│                             ┌──────────┐             │
│                             │ CUSTOMER │             │
│                             └──────────┘             │
│                                                      │
│  ● = ontology node   →  = approved triple (predicate)│
└──────────────────────────────────────────────────────┘
        Overview (`/dg/overview`) — five views, switched by tab
```

### Ontogen Review (UC3)

DG holds the singleton conf, the seed library, and the **approval actions**
for the triple ontology — DE and DA browse but cannot approve.

| Page | Read | Write |
|---|---|---|
| `/dg/ontogen/conf` | `GET /spoke/common/ontogen/attr/conf` | `PUT/PATCH/DELETE .../attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `max_manual_queries_per_dataset`, `max_system_queries_per_dataset`, `default_run_prompt`) |
| `/dg/ontogen/seed` | `GET .../attr/seed`, `GET .../{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../{seed_id}` |
| `/dg/ontogen` | `GET .../result/{node\|edge\|triple}` (+ `/{id}`, `/attr`, `/event`) | `POST .../method/run`; `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

Review proceeds **nodes → edges → triples**. A triple cannot be approved
while any of its subject node, edge, or object node is still pending; the
API returns `422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` and the UI disables the
approve button with an inline hint naming the missing dependency.
Approved nodes/edges/triples are written to DataHub as glossary terms and
glossary-term relationships — the confirm dialog states this.

```
┌──────────────────────────────────────────────────────┐
│  Ontogen Review   [ Nodes | Edges | Triples ] [Run]  │
├──────────────────────────────────────────────────────┤
│  Nodes  (result/node)                                │
│    BOOK         conf 0.96   ✓ approved               │
│    ORDER_LINE   conf 0.71   ⏳ pending               │
│       reason: [_______________]  [Approve] [Reject]  │
│                                                      │
│  Triples  (result/triple)                            │
│    ORDER_LINE --references--> BOOK   ⏳              │
│       [Approve] (blocked: ORDER_LINE node pending)   │
│    ORDER_LINE --placed_by --> CUSTOMER ⏳            │
│       [Approve] (blocked: ORDER_LINE node pending)   │
└──────────────────────────────────────────────────────┘
        Review (`/dg/ontogen`) — DG-only POST .../method/review
```
