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
| `/validation/data/[urn]` | Redirect to the unified `/data/[urn]` page (deep-link preserved) | — |

The per-dataset validation detail lives as the **Validation** panel on the unified
[`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) page; the dataset's validation events
fold into that page's unified **Events** panel.

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/validation` | `GET /spoke/validation` | — |
| `/data/[urn]` Validation panel | `GET .../attr/validation/conf`, `GET .../attr/validation/result?from&until&limit` (timeseries) | `PUT/DELETE .../attr/validation/conf` (fields: `description`, `variables[]`) |

Each dataset has one validation slot. The data pipeline runs the validation
logic and POSTs results to `attr/validation/result`. Teams that need multiple distinct
checks per dataset use DataHub's native assertion APIs directly. See
[`spec/feature/VALIDATION.md`](VALIDATION.md) for the full contract and
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation)
for the service surface.

The list page shows one row per dataset — columns: dataset, description, declared
variable count, latest `data_time`, latest `score` (UI header "Quality Score"; "—"
until the first result row arrives). It reads `GET /spoke/validation`. A pair of
checkboxes — **covered** (default checked) and **uncovered** (default unchecked) —
filters the row set by mapping to the `coverage` query param: covered-only →
`coverage=covered` (the default current view), both checked → `coverage=both`,
uncovered-only → `coverage=uncovered`, and neither checked → an empty result.
Toggling a checkbox resets pagination. Uncovered rows (registered datasets with no
validation slot) carry null conf/result fields, so their description, variable count,
`data_time`, and `score` cells render "—". The list is read-only for every role and
paged by the shared [Pagination](FRONTEND_BASIC.md#shared-component-notes) control
(page-size selector defaulting to 20, Prev/Next, numbered pages) bound to the
`/spoke/validation` standard `offset`/`limit`/`total_count` envelope.

The Validation panel on [`/data/[urn]`](FRONTEND_BASIC.md#per-dataset-page-dataurn) is a single
editor for `description` plus a variables list.
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
smoothing). Validation events — config lifecycle (create/update) plus one
`RESULT_RECORDED` entry per accepted result POST — are not a separate panel here; they appear
in the page's unified **Events** panel (narrow with `event_major_type=VALIDATION`). The
timeseries panel (and the list view) polls on a 15s interval, paused while the tab is hidden;
the selected range is stable per window.
The header "Latest score" reads the most recent result within the selected
range window, rendered to 4 decimals.

The conf editor sits under a `Config` section heading (same heading register as the
`Quality Score` and `Variables` headings). The panel's primary action controls live on
that `Config` heading's row (`justify-between` — heading left, controls right) and are
mode-driven by the GET-conf outcome: an existing rule's read-only view shows `Edit` and
`Delete`; edit mode shows `Cancel` and `Save`; a slot with no conf
(`404 CONFIG_NOT_FOUND`) shows `Create`. The per-row field-array controls `+ Add` and
`[×]` are not header controls — they stay inline inside the variables editor (rendered
only in `Create`/edit modes).

Delete (button → ConfirmDialog) issues `DELETE .../attr/validation/conf` and
redirects to `/validation`. The delete is a hard delete: afterwards the dataset reads as
never-created, so a re-fetch returns `404 CONFIG_NOT_FOUND` and the panel renders the
Create empty-state with the `Create` form. Submitting that form issues
`PUT .../attr/validation/conf`, which creates a fresh conf — there is no resurrection
branch and no deleted/frozen state to surface.

```
┌───────────────────────────────────────────────────────────────┐
│  ← orders.line_items  Latest score 1.0000  [Last 2 weeks ▾]   │
├───────────────────────────────────────────────────────────────┤
│  Config                                       [Edit] [Delete] │
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
│  (validation events fold into the unified Events panel)       │
└───────────────────────────────────────────────────────────────┘
   Validation panel on `/data/[urn]`
```

Write actions on the Validation panel are rendered only when
`role ∈ {Editor, Admin}` — the mode-driven header controls
(`Edit`/`Delete`/`Cancel`/`Save`/`Create`) and the inline variables-editor
controls (`+ Add`/`[×]`) alike. The list view is read-only for every role.
