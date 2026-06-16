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
| `/validation/data/[urn]` | `GET .../attr/validation/conf`, `GET .../attr/validation/result?from&limit` (timeseries), `GET .../event/validation` | `PUT/DELETE .../attr/validation/conf` (fields: `description`, `variables[]`) |

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
style and a `deleted` Badge. The list is read-only for every role.

The detail page is a single editor for `description` plus a variables list.
Each variable row edits both a `name` input and a `description` input in
place, with an `[×]` remove button (disabled at the minimum of 1 variable);
`[+ Add]` appends a new `{name, description}` row. The conf read-only view
renders each variable's description next to its name. Field constraints
(rule-description char cap, variable name regex, per-variable description
≤200 chars empty-allowed, count cap) per
[VALIDATION §Rule Configuration](VALIDATION.md#rule-configuration).
Saving issues `PUT .../attr/validation/conf`.
A range Select (7d / 30d / 90d, default 30d) drives the `from` query param.
The historical timeseries panel reads `GET .../attr/validation/result?from=&limit=`;
the UI sends only `from` (+ `limit`), and the backend defaults the upper bound
to now. It renders a `score` line chart, then **small multiples** — one
auto-scaled line chart per declared variable in a responsive grid, each
captioned with the variable's name and description so differing value scales do
not flatten each other. The
event log consumes `GET .../event/validation` — config lifecycle
(create/update/delete) plus one `RESULT_RECORDED` entry per accepted result
POST, each rendered with its `event_type`, status, and detail. The timeseries
and event panels (and the list view) poll on a 15s interval, paused while the
tab is hidden; `from` is stable per selected window.
The header "Latest score" reads the most recent result within the selected
range window, rendered to 4 decimals.

Delete (button → ConfirmDialog) issues `DELETE .../attr/validation/conf` and
redirects to `/validation`. After a soft-delete the detail route's 404 branch
shows a create/resurrect empty-state to re-create the conf.

```
┌──────────────────────────────────────────────────────┐
│  ← orders.line_items     Latest score 1.0000  [30d ▾] │
├──────────────────────────────────────────────────────┤
│  Description (attr/validation/conf.description)      │
│    [editable textarea, ≤ 2,000 chars]                │
│                                                      │
│  Variables (attr/validation/conf.variables[])        │
│    [ row_cnt         ] [ Daily row count       ] [×] │
│    [ qty_negative_cnt] [ Negative-qty rows     ] [×] │
│    [ qty_total       ] [ Total quantity        ] [×] │
│    [ user_id_null_cnt] [ Null user_id count    ] [×] │
│                                          [+ Add]    │
│                                                      │
│  Historical timeseries                               │
│    (attr/validation/result?from=…&limit=…)           │
│    [Recharts: score line chart]                      │
│    [small multiples: one auto-scaled line chart      │
│     per variable, name + description caption]        │
│                                                      │
│  event (latest 5)                            [Delete]│
└──────────────────────────────────────────────────────┘
        Detail (`/validation/data/[urn]`)
```

Write actions on the detail page (`Edit`, `+ Add`, `[×]`, save) are rendered
only when `role ∈ {Editor, Admin}`. The list view is read-only for every
role.
