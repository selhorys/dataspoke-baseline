# DataSpoke Frontend — Validation

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

Validation hosts the per-dataset validation slot (description + declared
variables) and the historical timeseries that the data pipeline POSTs to
DataSpoke after each partition write.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/validation` | List | `/spoke/validation` |
| `/validation/data/[urn]` | Per-dataset detail | `/spoke/common/data/{dataset_urn}/attr/validation/{conf,result}`, `/event/validation` |

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/validation` | `GET /spoke/validation` | — |
| `/validation/data/[urn]` | `GET .../attr/validation/conf`, `GET .../attr/validation/result?from&until&limit` (timeseries), `GET .../event/validation?from&to` | `PUT/DELETE .../attr/validation/conf` (fields: `description`, `variables[]`) |

Each dataset has one validation slot. The data pipeline runs the validation
logic and POSTs results to `attr/validation/result`. Teams that need multiple distinct
checks per dataset use DataHub's native assertion APIs directly. See
[`spec/feature/VALIDATION.md`](VALIDATION.md) for the full contract and
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation)
for the service surface.

The list page shows one row per dataset with a validation slot — columns:
dataset, description, declared variable count, latest `data_time`, latest
`score` (UI header "Quality Score"; "—" until the first result row arrives).
It defaults to active slots only (`GET /spoke/validation?removed=false`). A
"Show deleted" toggle re-fetches without the `removed` param (returning both
active and removed slots); rows whose `is_removed` is true render with a muted
style and a `deleted` Badge. The list is read-only for every role and paged by
the shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) control
(page-size selector defaulting to 20, Prev/Next, numbered pages) bound to the
`/spoke/validation` standard `offset`/`limit`/`total_count` envelope.

The detail page is a single editor for `description` plus a variables list.
Each variable row edits both a `name` input and a `description` input in
place, with an `[×]` remove button (disabled at the minimum of 1 variable);
`[+ Add]` appends a new `{name, description}` row. The conf read-only view
renders each variable's description next to its name. Field constraints
(rule-description char cap, variable name regex, per-variable description
≤200 chars empty-allowed, count cap) per
[VALIDATION §Rule Configuration](VALIDATION.md#rule-configuration).
Saving issues `PUT .../attr/validation/conf`.
The shared [RangePicker](FRONTEND_BASIC.md#shared-component-notes) (presets Last
1 day / 7 days / 2 weeks (default) / 4 weeks / 12 weeks, plus a custom calendar
range) drives both time-windowed panels. In `date` granularity it feeds the
timeseries panel: the RangePicker's inclusive `{from, to}` maps to
`?from=&until=&limit=` — `until` is the endpoint's end-bound param
(`until = to`). It renders a `score` line chart, then **small multiples** — one
auto-scaled, full-width line chart per declared variable stacked in a single
column (one chart per row), each captioned with the variable's name and
description so differing value scales do not flatten each other. Both the score
chart and the per-variable charts draw straight lines (linear interpolation, no
smoothing). The
event log consumes `GET .../event/validation` — config lifecycle
(create/update/delete) plus one `RESULT_RECORDED` entry per accepted result
POST, each rendered with its `event_type`, status, and detail. A `datetime`
RangePicker drives this panel, mapping its inclusive `{from, to}` to the
endpoint's `from`/`to` params. The timeseries
and event panels (and the list view) poll on a 15s interval, paused while the
tab is hidden; the selected range is stable per window.
The header "Latest score" reads the most recent result within the selected
range window, rendered to 4 decimals.

The detail page's primary action controls all live in the header's top-right
cluster and are mode-driven: the read-only view shows `Edit` and `Delete`; edit
mode shows `Cancel` and `Save`; the resurrect empty-state (after soft-delete or
404) shows `Create`. The per-row field-array controls `+ Add` and `[×]` are not
header controls — they stay inline inside the variables editor.

Delete (button → ConfirmDialog) issues `DELETE .../attr/validation/conf` and
redirects to `/validation`. After a soft-delete the detail route's 404 branch
shows a create/resurrect empty-state to re-create the conf.

```
┌───────────────────────────────────────────────────────────────┐
│  ← orders.line_items  Latest score 1.0000  [Last 2 weeks ▾] [Edit][Delete]│
├───────────────────────────────────────────────────────────────┤
│  Description (attr/validation/conf.description)               │
│    [editable textarea, ≤ 2,000 chars]                         │
│                                                               │
│  Variables (attr/validation/conf.variables[])                 │
│    [ row_cnt         ] [ Daily row count       ] [×]          │
│    [ qty_negative_cnt] [ Negative-qty rows     ] [×]          │
│    [ qty_total       ] [ Total quantity        ] [×]          │
│    [ user_id_null_cnt] [ Null user_id count    ] [×]          │
│                                          [+ Add]              │
│                                                               │
│  Historical timeseries                                        │
│    (attr/validation/result?from=…&until=…&limit=…)            │
│    [Recharts: score line chart]                               │
│    small multiples — one full-width chart per row:            │
│    [ row_cnt          — line chart, full width            ]   │
│    [ qty_negative_cnt — line chart, full width            ]   │
│    [ qty_total        — line chart, full width            ]   │
│    [ user_id_null_cnt — line chart, full width            ]   │
│                                                               │
│  event/validation         [ Pagination ]                      │
└───────────────────────────────────────────────────────────────┘
        Detail (`/validation/data/[urn]`)
```

Write actions on the detail page are rendered only when
`role ∈ {Editor, Admin}` — the mode-driven header controls
(`Edit`/`Delete`/`Cancel`/`Save`/`Create`) and the inline variables-editor
controls (`+ Add`/`[×]`) alike. The list view is read-only for every role.
