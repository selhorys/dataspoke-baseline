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
| `/validation/data/[urn]` | `GET .../attr/validation/conf`, `GET .../attr/validation/result?from&until` (timeseries), `GET .../event/validation` | `PUT/PATCH/DELETE .../attr/validation/conf` (fields: `description`, `variables[]`) |

Each dataset has one validation slot. The data pipeline runs the validation
logic and POSTs results to `attr/validation/result`. Teams that need multiple distinct
checks per dataset use DataHub's native assertion APIs directly. See
[`spec/feature/VALIDATION.md`](VALIDATION.md) for the full contract and
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation)
for the service surface.

The list page shows one row per dataset with a validation slot — columns:
dataset, description, declared variable count, latest `data_time`, latest
`score` (UI header "Quality Score"; "—" until the first result row arrives).

The detail page is a single editor for `description` plus a variables list
(add / remove / rename); field constraints (description char cap, variable
name regex, count cap) per
[VALIDATION §Rule Configuration](VALIDATION.md#rule-configuration).
Saving issues `PUT/PATCH .../attr/conf`.
The historical timeseries panel plots `score` and a per-variable chart over
`data_time` from `GET .../attr/result?from=&until=`. The event log
consumes `GET .../event` (one entry per accepted result POST).

```
┌──────────────────────────────────────────────────────┐
│  ← orders.line_items     Latest score 1.0            │
├──────────────────────────────────────────────────────┤
│  Description (attr/conf.description)                 │
│    [editable textarea, ≤ 2,000 chars]                │
│                                                      │
│  Variables (attr/conf.variables[])                   │
│    row_cnt              [rename] [×]                 │
│    qty_negative_cnt     [rename] [×]                 │
│    qty_total            [rename] [×]                 │
│    user_id_null_cnt     [rename] [×]      [+ Add]    │
│                                                      │
│  Historical timeseries                               │
│    (attr/result?from=…&until=…)                      │
│    [Recharts: score line chart]                      │
│    [Recharts: per-variable line charts (toggle)]     │
│                                                      │
│  event (latest 5)                                    │
└──────────────────────────────────────────────────────┘
        Detail (`/validation/data/[urn]`)
```

Write actions on the detail page (`Edit`, `+ Add`, `[×]`, save) are rendered
only when `role ∈ {Editor, Admin}`. The list view is read-only for every
role.
