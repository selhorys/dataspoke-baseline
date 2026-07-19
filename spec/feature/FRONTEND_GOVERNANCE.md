# DataSpoke Frontend — Governance

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

Governance hosts the metric catalogue, a Dashboard view that visualises
the metric timeseries, and a cross-feature Datasets catalog. `/governance/dashboard`
is the post-login home — clients hitting `/` are redirected here. Every UI element
below traces to a route in `API.md`.

The Datasets page lives under the Governance menu for navigation convenience; its
API is the cross-feature collection root `GET /spoke/common/data`, not a governance
route (menu placement and API namespace need not match).

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/governance/dashboard` | Dashboard | `/spoke/governance/metric`, `/spoke/governance/metric/{id}/attr/result` |
| `/governance/datasets` | Dataset catalog | `/spoke/common/data` |
| `/governance/metrics` | Metric list | `/spoke/governance/metric` |
| `/governance/metrics/new` | Metric create | `/spoke/governance/metric` |
| `/governance/metrics/[id]` | Metric detail | `/spoke/governance/metric/{id}` |

---

## Dashboard (`/governance/dashboard`)

The Dashboard is a read-only visualization of every enabled metric as a
responsive grid of combined cards:

| Element | Read | Notes |
|---|---|---|
| Combined metric card | `GET /spoke/governance/metric` (filter `is_enabled=true`) + latest `GET .../{id}/attr/result?limit=1` per metric + trend `GET .../{id}/attr/result?from=…&to=…` per metric | One card per enabled metric. Each card stacks, top to bottom: the metric `title` (emphasized heading), a `metric_type` outline badge, the latest `values` dict rendered as a compact stat row (each key a muted label with its value emphasized alongside) with its measured-at date, and that metric's per-metric trend chart (one line per that metric's `values` key). Per-metric charts avoid collapsing shared keys (e.g. `total`) across metrics onto one ambiguous line. No per-card delta indicator |
| Shared RangePicker | drives every card's trend `from`/`to` (plus a limit) | A single [RangePicker](FRONTEND_BASIC.md#shared-component-notes) (`date` granularity, presets Last 1 day / 7 days / 2 weeks (default) / 4 weeks / 12 weeks) sits above the grid and applies the same window to every card's chart together |
| Responsive grid | — | Equal-width cards with an enforced minimum width, laid out as `repeat(auto-fit, minmax(~22rem, 1fr))`. The grid wraps dynamically 3→2→1 as the viewport narrows, with **no fixed column cap** — on an ultra-wide viewport with more than three enabled metrics a fourth may pack into a row |

Trend charts poll on the 15s interval (paused when the tab is hidden) per the
BASIC convention; the latest-`values` snapshot on each card is fetched once per
load (refreshed on range change or manual refetch), not polled.

```
┌──────────────────────────────────────────────────────────┐
│  Governance · Dashboard               [Last 2 weeks ▾]   │
├──────────────────────────────────────────────────────────┤
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────┐ │
│  │ Ingestion Fresh.│ │ Validation Score│ │ Doc Health  │ │
│  │ (freshness)     │ │ (val-score)     │ │ (doc-health)│ │
│  │ total       142 │ │ total       142 │ │ total    87 │ │
│  │ in-time     131 │ │ sum     118.50  │ │ doc_h    61 │ │
│  │ 2026-05-26      │ │ 2026-05-26      │ │ 2026-05-26  │ │
│  │ ╭─trend chart─╮ │ │ ╭─trend chart─╮ │ │ ╭─trend───╮ │ │
│  │ ╰─────────────╯ │ │ ╰─────────────╯ │ │ ╰─────────╯ │ │
│  └─────────────────┘ └─────────────────┘ └─────────────┘ │
└──────────────────────────────────────────────────────────┘
        Dashboard (`/governance/dashboard`)
```

The Dashboard issues no writes. Reader, Editor, and Admin roles see the
same view.

---

## Datasets (`/governance/datasets`)

A read-only catalog of every registered dataset and its cross-feature coverage,
backed by `GET /spoke/common/data`. It answers "what datasets exist, who ingests
each, and which metagen confs cover it" in one cross-dataset table.

| Element | Read | Notes |
|---|---|---|
| Dataset table | `GET /spoke/common/data` (paginated `offset`/`limit`/`total_count`, sortable by `dataset_urn`) | One row per registered dataset. Columns below |
| Shared Pagination | the standard envelope | [Pagination](FRONTEND_BASIC.md#shared-component-notes) (page-size selector, Prev/Next, numbered pages) |

Columns:

| Column | Source | Renders |
|---|---|---|
| `dataset_urn` | row `dataset_urn` | the URN, linked to `/data/[urn]` |
| `datahub` | row `dataset_urn` | the shared [DataHub dataset deep-link](FRONTEND_BASIC.md#shared-component-notes) (`<datahub_url>/dataset/{urn}`); omitted when the DataHub URL resolves empty |
| `ingestion` | row `ingestion[]` | one label per covering source (a dataset may be covered by several). Each label's text is the source `platform` (its ingestion type), links to `/ingestion/sources/[source_id]`, and carries the source `mode` badge alongside; `—` when the list is empty (unmanaged) |
| `validation` | row `validation.covered` | a `Covered` / `Uncovered` badge from the boolean |
| `metagen` | row `metagen` | each matching conf `name`, linked to `/metagen/conf/[conf_id]`; `—` when the list is empty |

The page issues no writes; Reader, Editor, and Admin see the same view. It sits
under the Governance sidebar group even though its data is the cross-feature
`common/data` collection root.

---

## Metrics (`/governance/metrics`)

The Metrics page is the configuration surface for the metric catalogue —
list, create, edit, run, disable, delete.

| Page | Read | Write |
|---|---|---|
| `/governance/metrics` (list) | `GET /spoke/governance/metric` — rendered filter bar (`metric_type` / `mode` / status Selects, mapped to query params) plus the shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) control (page-size selector defaulting to 20, Prev/Next, numbered pages) bound to the standard `offset`/`limit`/`total_count` envelope. Each row shows the `title` (link to detail) with `metric_id` as a subtitle, a `metric_type` badge, the `mode` and `schedule_tier`, an `Enabled`/`Disabled` status badge, the `updated_at` timestamp, and a **Last Run** column (`last_run_at`, formatted via the shared tz/datetime helper; `—` when `null`) as the last column | "New metric" action → `/governance/metrics/new` |
| `/governance/metrics/new` | — | `POST /spoke/governance/metric` (definition fields **plus** a client-supplied `metric_id`) |
| `/governance/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to` (a `date`-granularity [RangePicker](FRONTEND_BASIC.md#shared-component-notes) above the chart drives `from`/`to`), `GET .../event?offset&limit&from&to&sort=occurred_at_desc` (a `datetime` RangePicker drives the event panel's `from`/`to`, with Pagination on `offset`/`limit`) | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`?dry_run=true`) |

The create form is the edit form (below) with one extra leading field: a
`metric_id` text input — **create-only** (validated per
[API §Metric](../API.md#metric-spokegovernancemetric); collision and
malformed input surfaced inline). On `/governance/metrics/[id]` the id
comes from the path and is shown read-only. On success the page redirects
to `/governance/metrics/[id]` for the new metric.

The form's **Cancel/Save buttons sit top-right**. On
`/governance/metrics/new` they occupy the form's top action bar; on
`/governance/metrics/[id]` they sit top-right of the `Config` panel
header (the same row as the `Config` heading) while editing.

The detail read-only view (`/governance/metrics/[id]`, not editing) renders
`description` alongside `mode`, `metric_type`, `schedule_tier`,
`is_enabled`, `metrics`, `metric_conf`, and `dataset_filter`.

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
│  Config                                              │
│    mode: active  metric_type: doc-health             │
│      schedule_tier: daily   ✓ enabled               │
│    metrics: total, doc_health   metric_conf: (none)  │
│    description: Daily documentation-completeness      │
│    dataset_filter: origin=DEV                        │
│                                                      │
│  Result                      [Last 2 weeks ▾]        │
│    [Recharts line chart — one line per `values` key] │
│                                                      │
│  Event                     [Last 2 weeks ▾]          │
│   occurred_at │ status │ event_type    │ detail      │
│   2026-04-25  │ ✓ ok   │ RUN_COMPLETE  │ {…} ⤢       │
│   2026-04-18  │ ✓ ok   │ RUN_COMPLETE  │ {…} ⤢       │
│                              [‹ Prev  Next ›]        │
└──────────────────────────────────────────────────────┘
        Detail (`/governance/metrics/[id]`)
```

```
┌─────────────────────────────────────────────────────┐
│  Metric definition                  [Cancel] [Save] │
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
└─────────────────────────────────────────────────────┘
        Config form (PUT/PATCH .../attr/conf)
```

The **Event** panel is a table mirroring the Ingestion source-detail event table
(`MetricEventTable`, modeled on `IngestionEventTable`): columns `occurred_at`, `status`,
`event_type`, and `detail`, newest first, bound to `GET .../event`. The `detail` cell shows the
compact JSON truncated and click-to-expand into a pretty-printed dialog (the shared event-detail
cell); status badges use the shared status-variant mapping. A `datetime` RangePicker drives
`from`/`to` (resetting `offset` on range change) and Pagination drives `offset`/`limit` with
`sort=occurred_at_desc`.

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
