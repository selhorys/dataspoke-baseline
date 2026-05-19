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
| `/dg/metrics/list` | `GET /spoke/dg/metric` (paginated; filter by `metric_type`, `mode`, `is_enabled`) | — |
| `/dg/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to`, `GET .../event` | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`{dry_run?}`) |

`dataset_filter` carries four optional dimensions: `origin` (the DataHub
`FabricType` value carried as the third URN segment — `PROD`/`DEV`/`CORP`/`EI`/
`STG`/`NON_PROD`/… — passed through verbatim to DataHub), `tags[]` (DataHub tag
URNs), `glossary_terms[]` (glossary term URNs), and `dataset_urns[]` (explicit
dataset URNs). Tag/term/URN dimensions are OR-ed among themselves and AND-ed with
`origin`. URN format is validated at `PUT/PATCH` time (`422 INVALID_DATASET_URN`);
URNs that fail to resolve at run time are listed in the `METRIC.RUN_COMPLETE`
event's `unresolved_urns` field.

Built-in metric types: `ingestion-freshness`, `validation-score`, `doc-health`
(see [USE_CASE §UC5](../USE_CASE_en.md#uc5-governance) for the `values` keys each
emits and the `metric_conf` they consume). `mode: "passive"` is reserved — the
form's mode toggle disables the Save button with the hint *passive mode not
yet supported*; PUT against the API returns `501 NOT_IMPLEMENTED`. Unsupported
`metric_type` or unknown `metrics[]` keys return `422 INVALID_PARAMETER`.

```
┌────────────────────────────────────────────────────────┐
│  Metrics                                               │
├────────────────────────────────────────────────────────┤
│  Ingestion Freshness   total 142   in-time 131  ↑      │
│  Validation Score      total 142   sum 118.5    ↑      │
│  Doc Health (PROD)     total  87   sum  61.0    ↓      │
└────────────────────────────────────────────────────────┘
      Dashboard (`/dg/metrics`) ← `/spoke/dg/metric` + latest result
```

```
┌──────────────────────────────────────────────────────┐
│  ← doc-health-prod        [Edit] [Run] [Disable]     │
├──────────────────────────────────────────────────────┤
│  attr/conf                                           │
│    mode: active   metric_type: doc-health            │
│    schedule_tier: weekly   ✓ enabled                 │
│    dataset_filter: origin=PROD                       │
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
│  mode:         ( • active )  ( passive — disabled ) │
│  metric_type:  [ doc-health                     v ] │
│  title:        [ Doc Health (PROD)                ] │
│  description:  [ Weekly documentation-completeness] │
│  metrics:      [x] total   [x] doc_health           │
│  metric_conf:  (none for doc-health)                │
│  schedule_tier:[ hourly | daily | weekly         v] │
│  is_enabled:   [x]                                  │
│                                                     │
│  dataset_filter                                     │
│    origin:           [ PROD                      v] │
│    tags[]:           [urn:li:tag:env:PROD,    ]     │
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

Review proceeds **nodes → edges → triples**. A triple cannot be human-approved
unless its subject node, edge, and object node all carry `status='approved'`
(an `llm_approved` dependency does NOT satisfy the gate — the human must
explicitly approve each component first); the API returns
`422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING` and the UI disables the approve
button with an inline hint naming the missing dependency.
Approval flips the entry's status in DataSpoke storage; DataHub is not
written to — the confirm dialog states this.

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
