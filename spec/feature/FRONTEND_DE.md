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
[BACKEND §Custom Ingestor Authoring Contract](BACKEND.md#custom-ingestor-authoring-contract).

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
| `/de/validation/[urn]` | `GET .../attr/validation/conf`, `GET .../attr/validation/result?latest=true` (per-rule), `GET .../attr/validation/result?from&to` (timeline), `GET .../event/validation` | `PUT/PATCH/DELETE .../attr/validation/conf` (fields: `rules[]`, `is_enabled`, `schedule_tier`); `POST .../method/validation/run` (`{partition?, dry_run?}`) |

The "Quality Score" displayed in the list and detail header is the
server-provided `quality_score` field on `GET /spoke/common/dataset` rows
and `GET /spoke/common/data/{urn}` (computed and cached server-side as
`passed_rules / total_rules`; see
[BACKEND §Dataset Service](BACKEND.md#dataset-service-srcbackenddataset)).
The score is omitted when no validation results exist; the UI renders "—"
in that case. Rule type vocabulary is fixed: `freshness`, `volume`,
`field`, `schema`, `sql`, `custom` (with optional `subtype: "sql_timeseries"`
for the partition-aware ML extension).

For `freshness` and `volume` rules, the rule builder exposes a **Source**
dropdown selecting how the metric is sourced — `datahub_operation` (freshness
default; reads `OperationClass`), `datahub_profile` (reads `DatasetProfileClass`,
freshness uses `timestampMillis`, volume uses `rowCount` — also volume's
default), or `query` (executes against the source platform). Selecting
`query` reveals `last_modified_field` (freshness only, required) and
`filter` (optional WHERE clause). Other rule types do not show this
dropdown. See
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation)
for source semantics and the `no_data` failure mode when DataHub-resident
profile/operation aspects are missing.

```
┌──────────────────────────────────────────────────────┐
│  ← orders.line_items   Score 4/6   [Run Now]         │
├──────────────────────────────────────────────────────┤
│  Per-rule (attr/validation/result?latest=true)       │
│    freshness  fresh_daily        ✓ SUCCESS           │
│    volume     daily_volume       ✓ SUCCESS           │
│    field      qty_positive       ✗ FAILURE  (12 rows)│
│    schema     required_columns   ✓ SUCCESS           │
│    sql        order_total_match  ✗ FAILURE  ( 1 row) │
│    custom     qty_anomaly        ✓ SUCCESS           │
│                                                      │
│  Timeline (attr/validation/result?from&to)           │
│    [Recharts line chart of pass-rate]                │
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
(`editableDatasetProperties`, `editableSchemaMetadata.editableSchemaFieldInfo`,
`dataProductProperties`); the confirm dialog labels the destination aspect.

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
| `/de/ontogen/conf` | `GET /spoke/common/ontogen/attr/conf` | `PUT/PATCH/DELETE .../attr/conf` (fields: `is_enabled`, `schedule_tier`, `dataset_filter`, `max_manual_queries_per_dataset`, `max_system_queries_per_dataset`, `default_run_prompt`) |
| `/de/ontogen/seed` | `GET .../attr/seed`, `GET .../attr/seed/{seed_id}` (Markdown) | `POST .../attr/seed` (Markdown body), `PATCH/DELETE .../attr/seed/{seed_id}` |
| `/de/ontogen` | `GET .../result/{node\|edge\|triple}` (+ `/{id}`, `/attr`, `/event`) | `POST .../method/run` (optional Markdown body — one-shot prompt; `?dry_run=true`) |

`POST .../result/{node\|edge\|triple}/{id}/method/review` is **not** exposed
in the DE workspace — see [FRONTEND_DG §Ontogen Review](FRONTEND_DG.md#ontogen-review-uc3).
The browser shows the same pending / approved / rejected status badges so
engineers can see what is in flight.

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
