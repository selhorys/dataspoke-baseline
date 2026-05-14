# DataSpoke Frontend — Data Engineering (DE) Workspace

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared layer in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

The `/spoke/de/` API tier is reserved for organization-specific extensions and
ships no baseline routes; DE pages live under `/de/...` and consume baseline
features at `/spoke/common/...` with engineering-flavoured framing.

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/de/ingestion` | Ingestion list | `/spoke/common/ingestion`, `/spoke/common/data/{urn}/attr/ingestion/...` |
| `/de/ingestion/[urn]` | Ingestion detail | per-dataset ingestion |
| `/de/validation` | Validation list | `/spoke/common/validation`, `/spoke/common/data/{urn}/attr/validation/...` |
| `/de/validation/[urn]` | Validation detail | per-dataset validation |
| `/de/metagen` | Metagen list | `/spoke/common/metagen`, `/spoke/common/data/{urn}/attr/metagen/...` |
| `/de/metagen/[urn]` | Metagen detail | per-dataset metagen |
| `/de/ontogen` | Ontogen browser | `/spoke/common/ontogen/...` |
| `/de/ontogen/conf` | Ontogen conf | singleton `/spoke/common/ontogen/attr/conf` |
| `/de/ontogen/seed` | Ontogen seeds | `/spoke/common/ontogen/attr/seed/...` |
| `/de/dataset/[urn]` | Dataset detail (aggregator) | `/spoke/common/data/{urn}` |

---

## Page contracts

### Ingestion (UC1)

| Page | Read | Write |
|---|---|---|
| `/de/ingestion` | `GET /spoke/common/ingestion` | — |
| `/de/ingestion/[urn]` | `GET .../attr/ingestion/conf`, `GET .../event/ingestion` | `PUT/PATCH .../attr/ingestion/conf` (fields: `mode: 'active-custom' \| 'passive'`, `platform`, `identifier`, plus `locator`/`auth`/`schedule_tier` for `active-custom`); `POST .../method/ingestion/run` (`{dry_run?}`, `active-custom` only); `DELETE .../attr/ingestion/conf` |

The `mode` field gates form behaviour. `active-custom` shows all input fields and
enables both Run and Dry-Run buttons. `passive` hides `locator`, `auth`, and
`schedule_tier`, disables both run buttons (passive runs are external; `method/ingestion/run`
returns `409 INGESTION_NOT_APPLICABLE`), and surfaces a "Configure ingestion in DataHub"
deep link (`http://datahub.<INGRESS_DOMAIN>/ingestion`) as a convenience. Passive
`event/ingestion` history is populated by the hourly `ingestion-passive-hourly` DAG
observing whatever ingestor emits `DataProcessInstance` records — see
[DATAHUB_INTEGRATION §Custom Ingestor Guide](../DATAHUB_INTEGRATION.md#custom-ingestor-guide).

```
┌────────────────────────────────────────────────┐
│  Ingestion                       [+ New Conf]  │
├──────────────────────┬───────┬────────┬────────┤
│  dataset_urn         │ mode  │ tier   │ events │
├──────────────────────┼───────┼────────┼────────┤
│  catalog.books       │ a-cust│ daily  │ 2h ✓   │
│  orders.shipments    │ passiv│ —      │ 1h ✓   │
│  reviews.legacy      │ a-cust│ weekly │ 3d ▲   │
└──────────────────────┴───────┴────────┴────────┘
        List (`/de/ingestion` ← `/spoke/common/ingestion`)
```

```
┌──────────────────────────────────────────────────────┐
│  ← catalog.books               [Run Now] [Dry Run]   │
├──────────────────────────────────────────────────────┤
│  attr/ingestion/conf                                 │
│    mode:           active-custom                     │
│    platform:       postgres                          │
│    locator:        {host: "pg.imazon", port: 5432}   │
│    identifier:     {database, schema_name, table}    │
│    auth:           {username, secret_ref}            │
│    schedule_tier:  daily      is_enabled:  ✓         │
│    [Edit]  [Delete]                                  │
│                                                      │
│  event/ingestion (latest 5)                          │
│    2026-04-25 ✓ INGESTION.COMPLETE                   │
│    2026-04-24 ✓ INGESTION.COMPLETE                   │
└──────────────────────────────────────────────────────┘
        Detail — active-custom (`/de/ingestion/[urn]`)
```

```
┌──────────────────────────────────────────────────────┐
│  ← orders.shipments         [Run Now]✗ [Dry Run]✗    │
├──────────────────────────────────────────────────────┤
│  attr/ingestion/conf                                 │
│    mode:           passive                           │
│    platform:       kafka                             │
│    identifier:     {topic, cluster}                  │
│    is_enabled:     ✓                                 │
│    [Edit]  [Delete]                                  │
│                                                      │
│  ⓘ Passive — runs are configured externally.         │
│    [↗ Configure ingestion in DataHub]                │
│                                                      │
│  event/ingestion (latest 5)                          │
│    2026-04-25 ✓ INGESTION.COMPLETE  (DPI: ext)       │
│    (empty until external ingestor emits a DPI)       │
└──────────────────────────────────────────────────────┘
        Detail — passive (`/de/ingestion/[urn]`)
```

### Validation (UC2)

| Page | Read | Write |
|---|---|---|
| `/de/validation` | `GET /spoke/common/validation` | — |
| `/de/validation/[urn]` | `GET .../attr/validation/conf`, `GET .../attr/validation/result?from&until` (timeseries), `GET .../event/validation` | `PUT/PATCH/DELETE .../attr/validation/conf` (fields: `description`, `variables[]`) |

Each dataset has one validation slot. The data pipeline runs the validation logic
and POSTs results to `attr/validation/result`. Teams that need multiple distinct
checks per dataset use DataHub's native assertion APIs directly. See
[`spec/feature/VALIDATION.md`](VALIDATION.md) for the full contract and
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation)
for the service surface.

The list page shows one row per dataset with a validation slot — columns:
dataset, description, declared variable count, latest `data_time`, latest
`score`. The "Quality Score" header on the list/detail uses the server-provided
`quality_score` field (see
[BACKEND §Dataset Service](BACKEND.md#dataset-service-srcbackenddataset)); the UI
renders "—" until the first result row arrives.

The detail page is a single editor for `description` (free-form, ≤ 2,000 chars)
plus a variables list (add / remove / rename, each name matching
`[a-z][a-z0-9_]{0,99}`, 1..200 entries). Saving issues `PUT/PATCH .../attr/validation/conf`.
The historical timeseries panel plots `score` and a per-variable chart over
`data_time` from `GET .../attr/validation/result?from=&until=`. The event log
consumes `GET .../event/validation` (one entry per accepted result POST).

```
┌──────────────────────────────────────────────────────┐
│  ← orders.line_items     Latest score 1.0            │
├──────────────────────────────────────────────────────┤
│  Description (attr/validation/conf.description)      │
│    [editable textarea, ≤ 2,000 chars]                │
│                                                      │
│  Variables (attr/validation/conf.variables[])        │
│    row_cnt              [rename] [×]                 │
│    qty_negative_cnt     [rename] [×]                 │
│    qty_total            [rename] [×]                 │
│    user_id_null_cnt     [rename] [×]      [+ Add]    │
│                                                      │
│  Historical timeseries                               │
│    (attr/validation/result?from=…&until=…)           │
│    [Recharts: score line chart]                      │
│    [Recharts: per-variable line charts (toggle)]     │
│                                                      │
│  event/validation (latest 5)                         │
└──────────────────────────────────────────────────────┘
        Detail (`/de/validation/[urn]`)
```

### Metagen (UC4)

| Page | Read | Write |
|---|---|---|
| `/de/metagen` | `GET /spoke/common/metagen` | — |
| `/de/metagen/[urn]` | `GET .../attr/metagen/conf`, `GET .../attr/metagen/result?latest=true`, `GET .../event/metagen` | `PUT/PATCH/DELETE .../attr/metagen/conf` (fields: `targets[]`, `is_enabled`, `schedule_tier`); `POST .../method/metagen/run`; `PATCH .../attr/metagen/result/{result_id}` body `{verdict: "approve"\|"reject", fields: ["dataset.description", "column.description.<fieldPath>", "cross_data.md.<action_id>", ...], reason}` |

`targets[]` is drawn from `dataset.description`, `column.description`,
`cross_data.md`. Approval writes only to the editable DataHub aspects
(`editableDatasetProperties`, `editableSchemaMetadata.editableSchemaFieldInfo`)
and to `documentInfo` on `document` entities (with `Status.removed=true` for
`cross_data.md` delete actions); the confirm dialog labels the destination
aspect.

```
┌──────────────────────────────────────────────────────┐
│  ← catalog.books               [Generate Now]        │
├──────────────────────────────────────────────────────┤
│  attr/metagen/result?latest=true (id: 7e8b…)         │
│                                                      │
│  dataset.description           [✓] [✏] [✕]           │
│    "# Books\n\nMaster catalog of every title…"       │
│                                                      │
│  column.description.book_id    [✓] [✏] [✕]           │
│  column.description.title      [✓] [✏] [✕]           │
│  column.description.author     [✓] [✏] [✕]           │
│  column.description.isbn       [✓] [✏] [✕]           │
│  column.description.price      [✓] [✏] [✕]           │
│                                                      │
│  cross_data.md.a1  (create)    [✓] [✏] [✕]           │
│    title: "How orders reference books"               │
└──────────────────────────────────────────────────────┘
        Detail (`/de/metagen/[urn]`) — PATCH … /result/{result_id}
```

### Ontogen (UC3) — read-only

| Page | Read | Write |
|---|---|---|
| `/de/ontogen/conf` | `GET /spoke/common/ontogen/attr/conf` | `PUT/PATCH/DELETE .../attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `default_run_prompt`) |
| `/de/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}` |
| `/de/ontogen` | `GET .../result/{node\|edge\|triple}` (+ `/{id}`, `/attr`, `/event`) | `POST .../method/run` (optional Markdown body — one-shot prompt; `?dry_run=true`) |

`POST .../result/{node\|edge\|triple}/{id}/method/review` is **not** exposed
in the DE workspace — see [FRONTEND_DG §Ontogen Review](FRONTEND_DG.md#ontogen-review-uc3).
The browser shows the same `llm_pending` / `llm_approved` / `approved` / `rejected`
status badges so engineers can see what is in flight and which gate each row has
cleared.

```
┌──────────────────────────────────────────────────────┐
│  Ontology   [ Nodes | Edges | Triples ]   [Run]      │
├──────────────────────────────────────────────────────┤
│  Nodes  (result/node)                                │
│    BOOK         conf 0.96   ✓ approved               │
│    CUSTOMER     conf 0.94   ✓ approved               │
│    ORDER_LINE   conf 0.71   ⏳ pending               │
│                                                      │
│  Edges  (result/edge)                                │
│    references   conf 0.95   ✓ approved               │
│    placed_by    conf 0.87   ✓ approved               │
│                                                      │
│  Triples  (result/triple)                            │
│    ORDER_LINE --references--> BOOK       ⏳          │
│    ORDER_LINE --placed_by --> CUSTOMER   ⏳          │
└──────────────────────────────────────────────────────┘
       Browser (`/de/ontogen`) — read-only; approve in DG
```

### Dataset detail (`/de/dataset/[urn]`)

Aggregator with `Overview / Ingestion / Validation / Metagen / Events` tabs.

| Tab | API |
|---|---|
| Overview | `GET /spoke/common/data/{urn}`, `GET .../attr` |
| Ingestion | reuses `/de/ingestion/[urn]` page contract |
| Validation | reuses `/de/validation/[urn]` page contract |
| Metagen | reuses `/de/metagen/[urn]` page contract |
| Events | `GET /spoke/common/data/{urn}/event` (unified per-dataset timeline) |

```
┌──────────────────────────────────────────────────────┐
│  ← catalog.books                                     │
│  [ Overview | Ingestion | Validation | Metagen | … ] │
├──────────────────────────────────────────────────────┤
│  (active tab content — see per-feature page above)   │
└──────────────────────────────────────────────────────┘
        Aggregator (`/de/dataset/[urn]`)
```
