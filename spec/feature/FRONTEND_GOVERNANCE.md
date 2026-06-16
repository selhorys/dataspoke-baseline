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
| Metric cards | `GET /spoke/governance/metric` (filter `is_enabled=true`) + latest `GET .../{id}/attr/result?limit=1` per metric | One card per enabled metric. Each card shows `title`, a `metric_type` outline badge under the title, the latest `values` dict (each key on its own line as `key: value`), and the formatted measured-at date. No per-card delta indicator |
| Timeseries chart | `GET /spoke/governance/metric/{id}/attr/result?from=…&to=…` per metric on one shared daily window | Small multiples — one chart per metric, each plotting one line per that metric's `values` key. Per-metric charts avoid collapsing shared keys (e.g. `total`) across metrics onto one ambiguous line. A single shared [RangePicker](FRONTEND_BASIC.md#shared-component-notes) (`date` granularity, presets Last 1 day / 7 days / 2 weeks (default) / 4 weeks / 12 weeks) sits above the grid and drives every metric card's `from`/`to` (plus a limit) together. Cards and charts poll on a 15s interval (paused when the tab is hidden) per the BASIC convention |

```
┌──────────────────────────────────────────────────────────┐
│  Governance · Dashboard                                  │
├──────────────────────────────────────────────────────────┤
│  Ingestion Freshness    Validation Score    Doc Health   │
│  (freshness)            (val-score)         (doc-health) │
│  ┌────────────────┐    ┌────────────────┐  ┌──────────┐  │
│  │ total       142│    │ total       142│  │ total  87│  │
│  │ in-time     131│    │ sum     118.50 │  │ sum   61 │  │
│  │ 2026-05-26     │    │ 2026-05-26     │  │ 2026-05-26│ │
│  └────────────────┘    └────────────────┘  └──────────┘  │
│                                                          │
│  Daily trend                          [Last 2 weeks ▾]   │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐  │
│  │ (one chart per │ │ (one chart per │ │ (one chart   │  │
│  │  metric — lines│ │  metric — lines│ │  per metric) │  │
│  │  per values    │ │  per values    │ │              │  │
│  │  key)          │ │  key)          │ │              │  │
│  └────────────────┘ └────────────────┘ └──────────────┘  │
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
| `/governance/metrics` (list) | `GET /spoke/governance/metric` — rendered filter bar (`metric_type` / `mode` / status Selects, mapped to query params) plus Previous/Next pagination controls with a page count. Each row shows the `title` (link to detail) with `metric_id` as a subtitle, a `metric_type` badge, and an `Enabled`/`Disabled` status badge | "New metric" action → `/governance/metrics/new` |
| `/governance/metrics/new` | — | `POST /spoke/governance/metric` (definition fields **plus** a client-supplied `metric_id`) |
| `/governance/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to` (a `date`-granularity [RangePicker](FRONTEND_BASIC.md#shared-component-notes) above the chart drives `from`/`to`), `GET .../event?from&to` (a `datetime` RangePicker drives the event panel's `from`/`to`) | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`?dry_run=true`) |

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
*passive mode not yet supported*. `schedule_tier` offers hourly / daily /
weekly (default daily) plus an "On-demand only" (null) option; list and
detail render a null tier as *on-demand*.

```
┌──────────────────────────────────────────────────────┐
│  ← doc-health-dev        [Edit] [Run] [Delete]       │
├──────────────────────────────────────────────────────┤
│  attr/conf                                           │
│    mode: active   metric_type: doc-health            │
│    schedule_tier: daily    ✓ enabled                 │
│    dataset_filter: origin=DEV                        │
│                                                      │
│  attr/result?from&to        [Last 2 weeks ▾]        │
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
│  schedule_tier:[ hourly | daily | weekly | on-demand v]│
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

The detail action bar is `[Edit] [Run] [Delete]`; disabling a metric is
done via the `is_enabled` checkbox inside the Edit form. `Run` opens a
confirm dialog with a "Dry run" checkbox — when checked the run issues
`?dry_run=true` (evaluate without persisting a result). The detail
result/event panels poll on a 15s interval (paused when the tab is hidden)
per the BASIC convention.

Write actions (`POST /governance/metrics/new`, `PUT/PATCH/DELETE .../attr/conf`,
`POST .../method/run`) are shown only when `role ∈ {Editor, Admin}`.
Reader users see the list, the detail view, and the timeseries chart, but
the Edit, Run, Delete buttons are not rendered.
