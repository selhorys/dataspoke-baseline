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

The Dashboard is a read-only visualization of the enabled metrics — narrowed
and ordered by the view controls below — as a responsive grid of combined
cards:

| Element | Read | Notes |
|---|---|---|
| Combined metric card | `GET /spoke/governance/metric` (filter `is_enabled=true`) + latest `GET .../{id}/attr/result?limit=1` per metric + trend `GET .../{id}/attr/result?from=…&to=…` per metric | One card per metric visible under the view controls. The card header carries the metric `title` as an emphasized heading on the left and, top-right at a smaller size, a `metric_type` outline badge beside a `Details` button linking to `/governance/metrics/{id}`. Below the heading sits `description` in small muted text. The body then stacks the latest `values` dict as a compact stat row (each key a muted label with its value emphasized alongside) with its measured-at date, and that metric's trend chart — one line per entry of the metric's `metrics[]` series descriptors, drawn in `idx` order and stroked with each descriptor's `color`, one visible point per grain window. `description` and `metrics` both come from the list read, so the card needs no extra fetch. Per-metric charts avoid collapsing shared keys (e.g. `total`) across metrics onto one ambiguous line. No per-card delta indicator |
| Shared RangePicker + ChartGrainPicker | range drives every card's trend `from`/`to` (plus a limit); grain drives no request parameter | A single [RangePicker](FRONTEND_BASIC.md#shared-component-notes) (`date` granularity, presets Last 1 day / 7 days / 2 weeks (default) / 4 weeks / 12 weeks) sits above the grid and applies the same window to every card's chart together, with a [ChartGrainPicker](FRONTEND_BASIC.md#shared-component-notes) immediately beside it applying the same grain to every card's chart. The grain is a client-side display concern — it collapses the fetched rows to one point per window — that window's last measurement — and leaves the read untouched |
| Metric view controls | the same `GET /spoke/governance/metric` (filter `is_enabled=true`) read that backs the cards — **no request parameter** | A row of three controls beneath the header narrows and orders the already-fetched enabled set entirely client-side, the same way the ChartGrainPicker beside them is display-only: a **metric-type filter** (checkbox-group multi-select over the built-in `metric_type` values listed in [USE_CASE §UC5](../USE_CASE_en.md#uc5-governance), each box labelled by its raw `metric_type` value, all selected by default; deselecting every type yields an empty set rather than falling back to all), a **title search** (case-insensitive substring over each metric's `title`, inactive while blank), and a **title sort** (`Title A→Z` / `Title Z→A`, ascending by default) — the title is the metric's human-facing identifier and what the reader scans the grid by, so both controls key off it. Each selection persists across visits in browser `localStorage` under a stable key, by the same rule as the shared [RangePicker](FRONTEND_BASIC.md#shared-component-notes) and [ChartGrainPicker](FRONTEND_BASIC.md#shared-component-notes) selections. Distinct from the single-select, server-side `metric_type` filter on the [metric list page](#metrics-governancemetrics), which does map to a query parameter |
| Cap disclosure | `total_count` of the same read | The dashboard read is capped at `limit=100`. When `total_count` exceeds the returned row count, a muted note above the grid states that only the first 100 enabled metrics are shown and that the filter and sort apply to those 100 only |
| Responsive grid | — | Equal-width cards with an enforced minimum width, laid out as `repeat(auto-fit, minmax(~22rem, 1fr))`. The grid wraps dynamically 3→2→1 as the viewport narrows, with **no fixed column cap** — on an ultra-wide viewport with more than three enabled metrics a fourth may pack into a row |

Trend charts poll on the 15s interval (paused when the tab is hidden) per the
BASIC convention; the latest-`values` snapshot on each card is fetched once per
load (refreshed on range change or manual refetch), not polled.

The grid carries two distinct empty states. With no enabled metrics at all it
points at the Metrics page as the place to enable one. With enabled metrics
present but none surviving the type filter and title search it points at the
view controls instead — the correction is the reader's own selection, not the
catalogue.

```
┌──────────────────────────────────────────────────────────┐
│  Governance · Dashboard     [Last 2 weeks ▾] [Daily ▾]   │
├──────────────────────────────────────────────────────────┤
│  [x] ingestion-freshness [x] validation-score            │
│  [x] doc-health                                          │
│  [ Search titles…          ]  [ Title A→Z            ▾]  │
│  Showing the first 100 of 142 enabled metrics            │
├──────────────────────────────────────────────────────────┤
│  ┌──────────────────────┐ ┌──────────────────────┐       │
│  │ Ingestion Fresh.     │ │ Doc Health           │       │
│  │      freshness [Deta]│ │    doc-health [Deta] │       │
│  │ Hourly freshness SLO │ │ Daily doc completen. │       │
│  │ total            142 │ │ total             87 │       │
│  │ in-time          131 │ │ doc_health        61 │       │
│  │ 2026-05-26           │ │ 2026-05-26           │       │
│  │ ╭─trend chart──────╮ │ │ ╭─trend chart──────╮ │       │
│  │ ╰──────────────────╯ │ │ ╰──────────────────╯ │       │
│  └──────────────────────┘ └──────────────────────┘       │
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
| `/governance/metrics/[id]` | `GET .../attr/conf`, `GET .../attr/result?from&to` (a `date`-granularity [RangePicker](FRONTEND_BASIC.md#shared-component-notes) in the `Result` panel header drives `from`/`to`, with a [ChartGrainPicker](FRONTEND_BASIC.md#shared-component-notes) beside it collapsing the fetched results to one visible point per grain window — display-only, no request parameter), `GET .../dataset?met&offset&limit&sort=dataset_urn` (the Datasets panel), `GET .../event?offset&limit&from&to&sort=occurred_at_desc` (a `datetime` RangePicker drives the event panel's `from`/`to`, with Pagination on `offset`/`limit`) | `PUT/PATCH/DELETE .../attr/conf` (fields: `mode`, `is_enabled`, `metric_type`, `title`, `description`, `metrics`, `metric_conf`, `schedule_tier`, `dataset_filter`); `POST .../method/run` (`?dry_run=true`) |

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
`is_enabled`, `metrics`, `metric_conf`, and `dataset_filter`. `metrics` renders one
line per series descriptor — a color swatch, the `name`, and its `idx` — in `idx` order.

`dataset_filter` is a SQL `WHERE`-clause string
([API §`dataset_filter` grammar](../API.md#dataset_filter-grammar)), rendered through
[DatasetFilterView](FRONTEND_BASIC.md#shared-component-notes) and edited through
[DatasetFilterEditor](FRONTEND_BASIC.md#shared-component-notes) — a resizable monospace
box with an **Auto-indent** button and a folded grammar guide. A `422
INVALID_DATASET_FILTER` from Save renders inline against the field.

The form's `metrics` control is **one row per emitted key** of the selected
`metric_type` (keys listed in [USE_CASE §UC5](../USE_CASE_en.md#uc5-governance)): a
checkbox selecting the key, a color control (native color swatch paired with a `#RRGGBB`
text input, kept in sync) and an order number. Only checked rows are submitted, as
`{name, color, idx}`. Duplicate `idx` values and a malformed hex color are surfaced
inline before submit; the server enforces the same rules
([API §Metric](../API.md#metric-spokegovernancemetric)). Changing `metric_type` reseeds
the rows to the new type's keys.

Built-in metric types and their `metric_conf` shapes are in
[USE_CASE §UC5](../USE_CASE_en.md#uc5-governance). The `time_window_sec` input the two
windowed types render constrains its value to the API's admissible range
([API §Metric](../API.md#metric-spokegovernancemetric)) at both the field and the submit
check, so an out-of-range window is reported inline rather than as a server `422`; the bound
is declared once, beside the other backend-mirroring constants. `mode: "passive"` is
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
│    metrics: ■ total (1)  ■ doc_health (2)            │
│    metric_conf: (none)                               │
│    description: Daily documentation-completeness      │
│    dataset_filter: origin = 'DEV'                    │
│                                                      │
│  Result          [Last 2 weeks ▾] [Daily ▾]          │
│    [Recharts line chart — one line per series, in    │
│     `idx` order, stroked with each `color`]          │
│                                                      │
│  Datasets                                            │
│    criterion met: [x] true [x] false [x] unknown     │
│   dataset_urn │ datahub │ met criterion │ last check │
│   …orders     │  ↗      │ ✓ true        │ 2026-04-25 │
│   …carriers   │  ↗      │ ✗ false       │ 2026-04-25 │
│   …archive    │  ↗      │ ? unknown     │ —          │
│   scope synced 2026-04-25 02:00   [‹ Prev  Next ›]   │
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
│  metrics:      [x] total       [■][#2563EB] idx [1] │
│                [x] doc_health  [■][#16A34A] idx [2] │
│  metric_conf:  (none for doc-health)                │
│  schedule_tier:[ hourly | daily | weekly | on-demand v]│
│  is_enabled:   [x]                                  │
│                                                     │
│  dataset_filter              [ Auto-indent ]        │
│    ┌─ SQL WHERE clause ────────────────────────┐    │
│    │ origin = 'DEV'                            │    │
│    │   AND 'urn:li:tag:env:DEV' IN tag_urns    │    │
│    └──────────────────────────────────────────◢┘    │
│    ▸ Filter grammar                                 │
└─────────────────────────────────────────────────────┘
        Config form (PUT/PATCH .../attr/conf)
```

The **Datasets** panel (`MetricDatasetTable`, modelled on the Ingestion
`SourceDatasetTable`) sits between the `Result` and `Event` panels and answers "which
datasets does this metric cover, and did each meet the criterion". It binds to
`GET .../dataset` with columns `dataset_urn` (linked to `/data/[urn]`), `datahub` (the
shared [DataHub dataset deep-link](FRONTEND_BASIC.md#shared-component-notes)), a `met`
badge (`true` / `false` / `unknown`), and `last check time` (shared tz/datetime helper;
em dash when the row is `unknown`). A three-way toggle group — true / false / unknown,
all on by default — drives the repeatable `met` query param, resetting `offset` on
change. The group carries a visible `criterion met:` label immediately before the three
checkboxes, so the three bare words are readable without relying on the group's accessible
name; the table's own column header is `met criterion`. With **zero** toggles selected the client renders the empty state and issues **no
request**: an omitted repeatable param and an empty one are the same HTTP request, which
the API reads as "all three", so the no-selection case cannot be expressed on the wire and
is resolved client-side instead. The
shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) drives `offset`/`limit`
with `sort=dataset_urn`. Beneath the table a muted line states the envelope's
`attrs_synced_at` as the scope's freshness, so an empty or unexpectedly small table is
readable as a pending sync rather than as a filter that matches nothing.

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
