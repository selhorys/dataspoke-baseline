# DataSpoke Frontend — Governance

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

Governance hosts the metric catalogue and a Dashboard view that visualises
the metric timeseries. `/governance/dashboard` is the post-login home —
clients hitting `/` are redirected here. Every UI element below traces to
a route in `API.md`.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/governance/dashboard` | Dashboard | `/spoke/governance/metric`, `/spoke/governance/metric/{id}/attr/result` |
| `/governance/metrics` | Metric list | `/spoke/governance/metric` |
| `/governance/metrics/new` | Metric create | `/spoke/governance/metric` |
| `/governance/metrics/[id]` | Metric detail | `/spoke/governance/metric/{id}` |

---

## Dashboard (`/governance/dashboard`)

The Dashboard is a read-only visualization of every enabled metric:

| Block | Read | Notes |
|---|---|---|
| Metric cards | `GET /spoke/governance/metric` (filter `is_enabled=true`) + latest `GET .../{id}/attr/result?limit=1` per metric | One card per enabled metric. Each card shows `title`, the latest `values` dict (each key on its own line as `key: number`), and the run time |
| Timeseries chart | `GET /spoke/governance/metric/{id}/attr/result?from=…&to=…` per metric on the same daily window | One line per `values` key per metric. Default window is the last 30 days |

```
┌──────────────────────────────────────────────────────────┐
│  Governance · Dashboard                                  │
├──────────────────────────────────────────────────────────┤
│  Ingestion Freshness    Validation Score    Doc Health   │
│  ┌────────────────┐    ┌────────────────┐  ┌──────────┐  │
│  │ total       142│    │ total       142│  │ total  87│  │
│  │ in-time     131│    │ sum     118.50 │  │ sum   61 │  │
│  │ 2026-05-26 ↑   │    │ 2026-05-26 ↑   │  │ 2026-05-26│ │
│  └────────────────┘    └────────────────┘  └──────────┘  │
│                                                          │
│  Daily trend (last 30 d)                                 │
│  ┌──────────────────────────────────────────────────┐    │
│  │  (Recharts line chart — one line per values key) │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
        Dashboard (`/governance/dashboard`)
```

The Dashboard issues no writes. Reader, Editor, and Admin roles see the
same view.

---

## Metrics (`/governance/metrics`)

The Metrics page is the configuration surface for the metric catalogue —
list, create, edit, run, disable, delete.

| Page | Read | Write |
|---|---|---|
| `/governance/metrics` (list) | `GET /spoke/governance/metric` (paginated; filter by `metric_type`, `mode`, `is_enabled`) | "New metric" action → `/governance/metrics/new` |
| `/governance/metrics/new` | — | `POST /spoke/governance/metric` (definition fields **plus** a client-supplied `metric_id`) |
| `/governance/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to`, `GET .../event` | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`{dry_run?}`) |

The create form is the edit form (below) with one extra leading field: a
`metric_id` text input — **create-only** (validated per
[API §Metric](../API.md#metric-spokegovernancemetric); collision and
malformed input surfaced inline). On `/governance/metrics/[id]` the id
comes from the path and is shown read-only. On success the page redirects
to `/governance/metrics/[id]` for the new metric.

`dataset_filter` follows the standard four-dimension shape — see
[API §Metric `dataset_filter`](../API.md#metric-spokegovernancemetric).

Built-in metric types and their `metric_conf` shapes are in
[USE_CASE §UC5](../USE_CASE_en.md#uc5-governance). `mode: "passive"` is
reserved — the form's mode toggle disables the Save button with the hint
*passive mode not yet supported*.

```
┌──────────────────────────────────────────────────────┐
│  ← doc-health-dev        [Edit] [Run] [Disable]      │
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
        Detail (`/governance/metrics/[id]`)
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

Write actions (`POST /governance/metrics/new`, `PUT/PATCH/DELETE .../attr/conf`,
`POST .../method/run`) are shown only when `role ∈ {Editor, Admin}`.
Reader users see the list, the detail view, and the timeseries chart, but
the Edit, Run, Disable, Delete buttons are not rendered.
