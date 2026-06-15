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

The detail page is a single editor for `description` plus a variables list.
Each variable name is edited in place via an input with an `[×]` remove
button (disabled at the minimum of 1 variable); `[+ Add]` appends a new one.
Field constraints (description char cap, variable name regex, count cap) per
[VALIDATION §Rule Configuration](VALIDATION.md#rule-configuration).
Saving issues `PUT .../attr/validation/conf`.
A range Select (7d / 30d / 90d, default 30d) drives the `from` query param.
The historical timeseries panel plots `score` and a per-variable chart over
`data_time` from `GET .../attr/validation/result?from=&limit=`; the UI sends
only `from` (+ `limit`), and the backend defaults the upper bound to now. The
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
│    [ row_cnt           ] [×]                         │
│    [ qty_negative_cnt  ] [×]                         │
│    [ qty_total         ] [×]                         │
│    [ user_id_null_cnt  ] [×]              [+ Add]    │
│                                                      │
│  Historical timeseries                               │
│    (attr/validation/result?from=…&limit=…)           │
│    [Recharts: score line chart]                      │
│    [Recharts: per-variable line charts (toggle)]     │
│                                                      │
│  event (latest 5)                            [Delete]│
└──────────────────────────────────────────────────────┘
        Detail (`/validation/data/[urn]`)
```

Write actions on the detail page (`Edit`, `+ Add`, `[×]`, save) are rendered
only when `role ∈ {Editor, Admin}`. The list view is read-only for every
role.
