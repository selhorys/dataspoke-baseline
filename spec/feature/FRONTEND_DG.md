# DataSpoke Frontend — Data Governance (DG) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared layer in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

DG is the only workspace that hosts the baseline `/spoke/dg/metric` route, and the
only one that exposes ontogen approval actions
(`POST /spoke/common/ontogen/result/{node|edge|triple}/{id}/method/review`).

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/dg/metrics` | Metrics dashboard | `/spoke/dg/metric` |
| `/dg/metrics/list` | Metric list | `/spoke/dg/metric` |
| `/dg/metrics/new` | Metric create | `/spoke/dg/metric` |
| `/dg/metrics/[id]` | Metric detail | `/spoke/dg/metric/{id}` |
| `/dg/ontogen` | Ontogen review | `/spoke/common/ontogen/...` |
| `/dg/ontogen/conf` | Ontogen conf editor | `/spoke/common/ontogen/attr/conf` |
| `/dg/ontogen/seed` | Ontogen seeds editor | `/spoke/common/ontogen/attr/seed/...` |

---

## Page contracts

### Metrics (UC5)

| Page | Read | Write |
|---|---|---|
| `/dg/metrics` (dashboard) | `GET /spoke/dg/metric`, latest `GET .../{id}/attr/result?limit=1` per metric | — |
| `/dg/metrics/list` | `GET /spoke/dg/metric` (paginated; filter by `metric_type`, `mode`, `is_enabled`) | "New metric" action → `/dg/metrics/new` |
| `/dg/metrics/new` | — | `POST /spoke/dg/metric` (definition fields **plus** a client-supplied `metric_id`) |
| `/dg/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to`, `GET .../event` | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`{dry_run?}`) |

The create form is the edit form (below) with one extra leading field: a `metric_id`
text input — **create-only** (validated per
[API §Metric](../API.md#metric-spokedgmetric); collision and malformed input
surfaced inline). On `/dg/metrics/[id]` the id comes from the path and is shown
read-only. On success the page redirects to `/dg/metrics/[id]` for the new metric.

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokedgmetric).

Built-in metric types and their `metric_conf` shapes are in
[USE_CASE §UC5](../USE_CASE_en.md#uc5-governance). `mode: "passive"` is reserved —
the form's mode toggle disables the Save button with the hint *passive mode not
yet supported*.

```
┌────────────────────────────────────────────────────────┐
│  Metrics                                               │
├────────────────────────────────────────────────────────┤
│  Ingestion Freshness   total 142   in-time 131  ↑      │
│  Validation Score      total 142   sum 118.5    ↑      │
│  Doc Health (DEV)      total  87   sum  61.0    ↓      │
└────────────────────────────────────────────────────────┘
      Dashboard (`/dg/metrics`) ← `/spoke/dg/metric` + latest result
```

```
┌──────────────────────────────────────────────────────┐
│  ← doc-health-dev        [Edit] [Run] [Disable]     │
├──────────────────────────────────────────────────────┤
│  attr/conf                                           │
│    mode: active   metric_type: doc-health            │
│    schedule_tier: daily    ✓ enabled                 │
│    dataset_filter: origin=DEV                        │
│                                                      │
│  attr/result?from&to                                 │
│    [Recharts area chart — one line per `values` key] │
│                                                      │
│  event  (METRIC.RUN_COMPLETE …)                      │
│    2026-04-25 values: total 142, doc_health 119      │
│    2026-04-18 values: total 140, doc_health 112      │
└──────────────────────────────────────────────────────┘
        Detail (`/dg/metrics/[id]`)
```

```
┌─────────────────────────────────────────────────────┐
│  Metric definition                                  │
├─────────────────────────────────────────────────────┤
│  metric_id:    [ doc-health-dev          ] (create) │
│  mode:         ( • active )  ( passive — disabled ) │
│  metric_type:  [ doc-health                     v ] │
│  title:        [ Doc Health (DEV)                 ] │
│  description:  [ Daily documentation-completeness ] │
│  metrics:      [x] total   [x] doc_health           │
│  metric_conf:  (none for doc-health)                │
│  schedule_tier:[ hourly | daily | weekly         v] │
│  is_enabled:   [x]                                  │
│                                                     │
│  dataset_filter                                     │
│    origin:           [ DEV                       v] │
│    tags[]:           [urn:li:tag:env:DEV,     ]     │
│    glossary_terms[]: [urn:li:glossaryTerm:…,  ]     │
│    dataset_urns[]:   [urn:li:dataset:(…),     ]     │
│                                                     │
│  [Cancel]                                  [Save]   │
└─────────────────────────────────────────────────────┘
        Config form (PUT/PATCH .../attr/conf)
```

### Ontogen Review (UC3)

DG holds the singleton conf, the seed library, and the **approval actions**
for the triple ontology — DE and DA browse but cannot approve.

| Page | Read | Write |
|---|---|---|
| `/dg/ontogen/conf` | `GET /spoke/common/ontogen/attr/conf` | `PUT/PATCH/DELETE .../attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) |
| `/dg/ontogen/seed` | `GET .../attr/seed`, `GET .../{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../{seed_id}` |
| `/dg/ontogen` | `GET .../result/{node\|edge\|triple}` (+ `/{id}`, `/attr`, `/event`) | `POST .../method/run`; `POST .../result/{node\|edge\|triple}/{id}/method/review` body `{verdict: "approve"\|"reject", reason}` |

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokedgmetric).

Review proceeds **nodes → edges → triples** per
[API §Ontology Generation](../API.md#ontology-generation). When a triple's
dependencies are not yet approved the UI disables the approve button with an
inline hint naming the missing dependency. The confirm dialog states that
approval flips status in DataSpoke storage only and does not write to DataHub.

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
