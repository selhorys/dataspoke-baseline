---
name: dataspoke-validation
description: Write data-quality validation into a data pipeline, backed by DataSpoke as the result store (UC2). Use whenever the user is authoring or editing pipeline code that builds a partition and writes it to a destination table — PySpark, awswrangler/pandas, dbt, SQL, Airflow tasks — and wants row-count / null-ratio / freshness checks on what it just wrote, with history and trend tracked across runs. Generates the metric computation, the baseline fetch, the scoring logic, and the DataSpoke calls that register the validation slot and put the score. Also manages validation slots directly — register/edit a conf, post or query results, browse the cross-dataset list. Triggers on "add validation to this pipeline", "validate the partition I just wrote", "row count check for this table", "register validation metrics", "validate table X".
argument-hint: "[manage | routine] [question or dataset]"
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(dataspoke-api *), Bash(dataspoke-schema *), Bash(datahub-graphql *), WebFetch, AskUserQuestion
---

## Purpose

Two modes against a deployed DataSpoke. If `dataspoke-api` reports no access, send the user to
`/dataspoke:dataspoke-access` first.

1. **routine** — the flagship. The user is writing pipeline code (PySpark, awswrangler/pandas,
   dbt, SQL, an Airflow task) that builds a partition and writes it to a destination table. You
   write the validation that runs against what it just wrote, and wire it to DataSpoke.
2. **manage** — operate the validation slot directly: read/register/edit a conf, post/query
   results, browse the cross-dataset view.

Default to **routine** whenever there is pipeline code in play — including when the user never
says the word "validation" but asks for a row-count check, a null check, a freshness check, or
"make sure the write looks right."

## What DataSpoke does and does not do (state this honestly)

DataSpoke offers an **API for registration, get, and put of values** — nothing more:

| DataSpoke provides | DataSpoke does **not** provide |
|---|---|
| Register a validation slot (conf: `description` + declared `variables[]`) | Any computing engine |
| `PUT`/`GET` the score and variable values, with history | Timeseries prediction / forecasting |
| Cross-run history, cross-dataset views, event reports | Anomaly detection |
| Emission of results to DataHub as assertions | Threshold or rule evaluation |

There is **no metric computation, no forecast engine, no anomaly detector, and no rule engine
inside DataSpoke**. Every number — each variable *and* the final pass/fail `score` — is computed
by the pipeline, on the user's own engine, with the user's own credentials. DataSpoke receives
finished numbers and stores them.

This is not a limitation to apologize for; it is the division of labor. **You write the
computing code** — the metrics, the baseline comparison, the anomaly logic, the thresholds — and
it runs in the user's pipeline. If the user asks DataSpoke to "detect anomalies" or "set a
threshold," correct the framing: you will write that logic into their pipeline, and DataSpoke
will store what it decides. Never imply DataSpoke evaluates anything.

## Mode: routine — write validation into the user's pipeline (flagship)

Drive three phases in order.

### Phase 1 — Prerequisite chain (strict; stop on first failure)

1. **Access** — `dataspoke-api GET /auth/me` returns an **Editor/Admin** effective role.
   Otherwise → `/dataspoke:dataspoke-access`.
2. **Ingested** — `dataspoke-api GET /spoke/common/data/{urn}/attr/ingestion` confirms the
   dataset is covered. If not → `/dataspoke:dataspoke-ingestion` first (validation assumes the
   dataset is registered in DataHub).
3. **Conf** — `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf`. If absent,
   register one (PUT) with the variables the routine will compute, after confirming with the user.

### Phase 2 — Resolve `dataset_urn` (never guess)

1. **Gather hints** — Grep/Glob the engineer's workspace (pipeline scripts, SQL, configs, dbt/
   Airflow files) for platform / schema / table signals. The destination of the write you are
   validating is the strongest hint — read it out of the code in front of you.
2. **Confirm** — restate the inferred platform + schema + table; get explicit agreement.
3. **Resolve via DataHub search** — query DataHub's GraphQL endpoint directly:
   ```bash
   datahub-graphql '{"query":"query($q:String!){ search(input:{type:DATASET, query:$q, start:0, count:10}){ searchResults{ entity{ urn } } } }","variables":{"q":"<schema.table>"}}'
   ```
   Present the candidate URNs. This URN-search capability is preserved in full — if
   `datahub-graphql` reports no DataHub access, send the user to `/dataspoke:dataspoke-access`
   to add a DataHub GMS URL + token, then retry the search. Only if they decline DataHub access,
   fall back to asking them to supply the exact URN manually (a last resort, never the default).
4. **Double-check** — confirm the exact URN with the user before it is used in any call. A wrong
   URN silently writes to the wrong dataset.

### Phase 3 — Write the validation into their pipeline

Write/Edit into **their** pipeline file — their engine, their credentials, never DataSpoke's.

**Attachment point.** The validation goes *after* the write it validates, gated on that write
having succeeded. Validate what actually landed: prefer re-reading the destination partition over
reusing the in-memory DataFrame, since the two diverge exactly when something went wrong (partial
write, schema coercion, silently dropped rows). Say which one you chose and why.

**The three DataSpoke calls — the entire API surface this routine touches:**

| Step | When | Call |
|------|------|------|
| **register** the slot | once, at setup — *not* per run | `PUT …/attr/validation/conf` |
| **get** the baseline | per run, if scoring compares to history | `GET …/attr/validation/result?from=…` |
| **put** the result | per run, at the end | `POST …/attr/validation/result` |

Registration is a one-time act. Do it from the skill session with `dataspoke-api` (Phase 1 step
3) rather than emitting a `PUT` into the pipeline — a conf re-registered on every run is a
recurring chance to silently change the declared variables out from under the history.

Everything between the get and the put is code you write, running on their engine:

```python
import os, datetime as dt, requests

DATASPOKE = os.environ["DATASPOKE_API_URL"].rstrip("/")           # …/api/v1
TOKEN     = os.environ["DATASPOKE_API_TOKEN"]
URN       = "<confirmed dataset_urn>"
H         = {"Authorization": f"Bearer {TOKEN}"}
RESULT    = f"{DATASPOKE}/spoke/common/data/{URN}/attr/validation/result"
data_time = "<partition timestamp, RFC3339 UTC>"                  # the partition being validated

# 1) COMPUTE the declared metrics over what was just written — your engine (see below).
row_count, content_type_null_ratio = compute_metrics(...)

# 2) GET the recent baseline from DataSpoke. Bound by the time window, NOT by `limit`:
#    one row per distinct data_time, so N days != N rows if the pipeline runs
#    more than once a day. Server default limit is 1000, cap 10000.
since = (dt.date.today() - dt.timedelta(days=14)).isoformat()
page  = requests.get(RESULT, headers=H, params={"from": since}).json()
hist  = page["results"]                                            # newest-first
assert page["total_count"] == len(hist), "baseline truncated — narrow the window"
series = [r["variables"]["row_count"] for r in reversed(hist)]     # oldest→newest

# 3) FORECAST / DETECT ANOMALIES in pipeline code. DataSpoke does none of this.
#    Anything you like: a rolling median band, an IQR fence, Prophet, a hard floor.
expected_low, expected_high = forecast_band(series)

# 4) DECIDE the score — your thresholds, your logic. DataSpoke just stores the number.
ok_nulls = content_type_null_ratio <= 0.10
ok_count = expected_low <= row_count <= expected_high
score    = 1.0 if (ok_nulls and ok_count) else 0.0

# 5) PUT the result. Variable keys must match the registered conf exactly.
requests.post(RESULT, headers=H,
              json={"data_time": data_time, "score": score,
                    "variables": {"row_count": float(row_count),
                                  "content_type_null_ratio": float(content_type_null_ratio)}}
              ).raise_for_status()
```

`score` is a float in `[0.0, 1.0]`, not a boolean — a graded score (e.g. the fraction of checks
that passed) carries more signal in the history than a 0/1 flag. Every `variables` value must be
a float; cast counts explicitly.

**Metric computation, per engine.** Only step 1 changes; steps 2–5 are identical everywhere.

*PySpark* — re-read the destination partition, aggregate in one pass:

```python
from pyspark.sql import functions as F

part = spark.read.format("delta").load(DEST).where(F.col("dt") == PARTITION)
agg  = part.agg(
    F.count(F.lit(1)).alias("row_count"),
    F.avg(F.col("content_type").isNull().cast("double")).alias("null_ratio"),
).first()
row_count, content_type_null_ratio = int(agg["row_count"]), float(agg["null_ratio"] or 0.0)
```

*awswrangler / pandas* — push the aggregation into Athena rather than pulling the partition:

```python
import awswrangler as wr

df = wr.athena.read_sql_query(
    "SELECT COUNT(*) AS row_count, "
    "       AVG(CASE WHEN content_type IS NULL THEN 1.0 ELSE 0.0 END) AS null_ratio "
    f"FROM {TABLE} WHERE dt = :dt",
    database=DB, params={"dt": PARTITION},          # parameterized — never f-string the value
)
row_count, content_type_null_ratio = int(df.row_count[0]), float(df.null_ratio[0])
```

For a `wr.s3.to_parquet(..., dataset=True)` write, run this *after* the catalog update so the new
partition is visible to Athena.

**Airflow.** Keep validation a separate task downstream of the write, not a tail appended to it —
the write stays retryable on its own, and a validation failure is visible as its own task. Whether
a low score should fail the task is the user's call: ask, and default to recording the result and
letting the DAG continue, since DataSpoke is a result store rather than a gate.

**Re-run safety.** A retry that re-POSTs the same `data_time` is safe: reads collapse to the
newest write per `data_time`, so the partition is corrected, not duplicated. No dedup guard is
needed around the POST — do not generate one.

**`data_time` must identify the partition, not the moment of the run.** This is the single
decision that determines whether the baseline series is meaningful. Use the partition's own
timestamp — the `dt`/`ds` value, the Airflow logical date, the window start — truncated to the
grain the table is partitioned at. Never `datetime.now()`: it makes every run a distinct
`data_time`, so retries stop collapsing and accumulate as separate points, a day's worth of
hourly runs looks like a day's worth of daily history, and comparing "today vs. the last 14
values" silently compares against the last 14 *hours*. State the chosen grain explicitly in what
you generate.

**When the pipeline runs more than once per day**, `series` holds one point per run, not per day.
Either scale the window to the run frequency (a 14-run baseline for an hourly job is ~14 hours —
usually not what the user means), or bucket by day in pipeline code before comparing:

```python
by_day = {}                                                        # newest-first input,
for r in hist:                                                     # so the first hit per day
    by_day.setdefault(r["data_time"][:10], r)                      # is that day's latest
series = [by_day[d]["variables"]["row_count"] for d in sorted(by_day)]
```

Ask which the user wants rather than assuming — the right answer depends on whether the metric is
per-partition (row count) or per-day (daily total).

**Credentials.** The routine reads `DATASPOKE_API_URL` / `DATASPOKE_API_TOKEN` from the pipeline's
environment. Never inline a `dsk_` token into generated code, and never point the pipeline at
`~/.dataspoke/config.json` — that file is the plugin's local credential store, not a deployment
artifact. Tell the user to provision the token the way their orchestrator handles secrets.

**Confirm the shapes before generating code against them.** The routine reads `results`,
`total_count`, and `variables` out of the response, and posts a body that must match the conf.
Read the real contract rather than trusting this file:

```bash
dataspoke-schema attr/validation/result       # request + response schemas
dataspoke-schema attr/validation/conf
```

`/redoc` is the same document rendered for **humans** — give the user that URL (it is in
`~/.dataspoke/config.json` as `redoc_url`) when they want to browse it themselves.

## Mode: manage — capabilities → routes

| Intent | Call |
|--------|------|
| Read a dataset's conf | `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf` |
| Register / replace conf | `dataspoke-api PUT /spoke/common/data/{urn}/attr/validation/conf '<json>'` |
| Partially update conf | `dataspoke-api PATCH /spoke/common/data/{urn}/attr/validation/conf '<json>'` |
| **Destroy** the slot (see below) | `dataspoke-api DELETE /spoke/common/data/{urn}/attr/validation/conf` |
| Append a result | `dataspoke-api POST /spoke/common/data/{urn}/attr/validation/result '<json>'` |
| Query result history | `dataspoke-api GET '/spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…'` |
| Cross-dataset list | `dataspoke-api GET '/spoke/validation?coverage=covered'` |
| Validation event reports | `dataspoke-api GET /spoke/common/data/{urn}/event/validation` |
| Full per-dataset timeline | `dataspoke-api GET '/spoke/common/data/{urn}/event?event_major_type=VALIDATION'` |

**Conf** body: `{"description": "...", "variables": [{"name": "row_count", "description": "..."}]}`
— `name` matches `[a-z][a-z0-9_]{0,99}` and is unique; `description` is required, ≤200 chars,
empty string allowed. A `PUT` for a URN that DataHub does not track returns
`422 DATASET_NOT_IN_DATAHUB` — the dataset must be ingested first.

**Result** body: `{"data_time": "<RFC3339 UTC>", "score": <0.0–1.0>, "variables":
{"row_count": 1250.0, …}}` — keys must match declared names (`422 UNKNOWN_VARIABLE` otherwise);
`score` outside `[0,1]` returns `422 INVALID_SCORE`. Returns `201`.

**Reads collapse last-write-wins per `data_time`.** The table itself is append-only — there is no
uniqueness on `(dataset_urn, data_time)`, so POSTing the same `data_time` twice stores two rows —
but `GET …/result` returns only the newest by ingestion time for each distinct `data_time`, and
`total_count` counts distinct `data_time` values. A retried run therefore corrects its partition
rather than duplicating it in the history.

**One row per distinct `data_time`, not per day.** `data_time` is a timestamp, not a date. Two
runs stamping different times inside the same day are two separate partitions and both are
returned. Whether the series is daily is entirely a property of what the pipeline puts in
`data_time`.

The time window is **half-open**: `from <= data_time < until`. Passing the same value for both
matches nothing. Reads are fixed `data_time DESC` (newest-first); `?limit` defaults to `1000`
with a server cap of `10000`, and is applied *after* collapsing.

`coverage` on the cross-dataset list selects the row set: `covered` (default — datasets holding
a validation slot), `uncovered` (registered datasets with no conf; null `description`,
`variable_count`, `latest_data_time`, `latest_score`), or `both`.

Confirm before any write; surface `403 READ_ONLY_ROLE` verbatim.

### `DELETE` conf is a hard delete — warn explicitly before calling it

It is **not** a soft delete or an archive. In one transaction it removes the conf row, **all of
the dataset's validation results**, and its `VALIDATION.*` events, then hard-deletes the
assertion entity from DataHub. It returns `204`; afterwards the dataset reads as never-created
(`GET`/`PATCH` → `404 CONFIG_NOT_FOUND`) and a fresh `PUT` starts an empty slot. The history is
unrecoverable. Spell out that the result history will be destroyed and get explicit agreement —
if the user only wants to stop validating, they should stop POSTing results instead.

### Changing a conf's variables breaks history continuity

`PUT`/`PATCH` replaces the declared `variables[]`, but past results keep whatever keys they were
posted with. Renaming `row_count` to `rows` leaves every historical row keyed `row_count`, so a
baseline query returns a series the new routine cannot read, and the pipeline's next POST
`422 UNKNOWN_VARIABLE`s until it is updated to match. When a rename or removal is requested, say
what it does to the existing series and offer adding a new variable alongside the old instead.
