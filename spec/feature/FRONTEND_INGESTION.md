# DataSpoke Frontend — Ingestion Control

> Conforms to [MANIFESTO](../MANIFESTO_en.md). Shared shell in
> [FRONTEND_BASIC](FRONTEND_BASIC.md). API in [API.md](../API.md).

Ingestion Control surfaces ingestion configuration and per-run history for
every dataset DataSpoke knows about — whether DataSpoke runs the extractor
(`active-custom`) or an external system does (`passive`).

---

## Navigation

| UI route | Title | API base |
|---|---|---|
| `/ingestion` | List | `/spoke/ingestion`, `/spoke/common/data/{urn}/attr/ingestion/conf` |
| `/ingestion/data/[urn]` | Per-dataset detail | `/spoke/common/data/{urn}/attr/ingestion/conf`, `/method/ingestion/run`, `/event/ingestion` |

---

## Page contracts

| Page | Read | Write |
|---|---|---|
| `/ingestion` | `GET /spoke/ingestion` | — |
| `/ingestion/data/[urn]` | `GET .../attr/ingestion/conf`, `GET .../event/ingestion` | `PUT/PATCH .../attr/ingestion/conf` (fields: `mode: 'active-custom' \| 'passive'`, `platform`, `identifier`, `is_enabled`, plus `locator`/`auth`/`schedule_tier` for `active-custom`); `POST .../method/ingestion/run` (`{dry_run?}`, `active-custom` only); `DELETE .../attr/ingestion/conf` |

The `mode` field gates form behaviour. `active-custom` shows all input fields and
enables both Run and Dry-Run buttons. `passive` hides `locator`, `auth`, and
`schedule_tier`, disables both run buttons (passive runs are external;
`method/ingestion/run` returns `409 INGESTION_NOT_APPLICABLE`), and surfaces a
"Configure ingestion in DataHub" deep link
(`http://datahub.<INGRESS_DOMAIN>/ingestion`) as a convenience. Passive
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
        List (`/ingestion` ← `/spoke/ingestion`)
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
        Detail — active-custom (`/ingestion/data/[urn]`)
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
        Detail — passive (`/ingestion/data/[urn]`)
```

Write actions — `+ New Conf`, `Edit`, `Delete`, `Run Now`, `Dry Run` — are
rendered only when `role ∈ {Editor, Admin}`.
