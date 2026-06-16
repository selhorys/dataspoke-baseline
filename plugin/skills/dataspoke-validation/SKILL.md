---
name: dataspoke-validation
description: Manage DataSpoke validation (UC2) and author validation routines into your own data pipeline. Use to register/edit a dataset's validation slot, post or query results, browse the cross-dataset list — and, the flagship capability, to generate a validation routine (compute metrics, fetch baseline, forecast, decide score, POST result) into the engineer's pipeline script. Triggers on "add a validation routine", "validate table X", "register validation metrics".
argument-hint: "[manage | routine] [question or dataset]"
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(dataspoke-api *), WebFetch, AskUserQuestion
---

## Purpose

Two modes against a deployed DataSpoke. If `dataspoke-api` reports no access, send the user to
`/dataspoke:dataspoke-access` first.

1. **manage** — read/register/edit the per-dataset validation slot, post/query results, browse
   the cross-dataset view.
2. **routine** — generate a validation routine into the engineer's own pipeline (the flagship).

## The passivity boundary (state this honestly)

DataSpoke validation is a **passive result store**. A conf declares only `{description,
variables[]}`. There is **no threshold engine, no forecast engine, no rule evaluation** inside
DataSpoke. The engineer's pipeline computes every metric **and** the pass/fail `score`, then
POSTs `{data_time, score, variables}`. This skill generates the computing code; DataSpoke only
stores and emits the result. Never imply DataSpoke evaluates rules or forecasts.

## Mode: manage — capabilities → routes

| Intent | Call |
|--------|------|
| Read a dataset's conf | `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf` |
| Register / replace conf | `dataspoke-api PUT /spoke/common/data/{urn}/attr/validation/conf '<json>'` |
| Patch / soft-delete conf | `dataspoke-api PATCH|DELETE /spoke/common/data/{urn}/attr/validation/conf …` |
| Append a result | `dataspoke-api POST /spoke/common/data/{urn}/attr/validation/result '<json>'` |
| Query result history | `dataspoke-api GET '/spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…'` |
| Cross-dataset list | `dataspoke-api GET '/spoke/validation?removed=false'` |
| Lifecycle event timeline | `dataspoke-api GET /spoke/common/data/{urn}/event/validation` |

**Conf** body: `{"description": "...", "variables": [{"name": "row_count", "description": "..."}]}`
— names match `[a-z][a-z0-9_]{0,99}`, unique. **Result** body: `{"data_time": "<RFC3339 UTC>",
"score": <0.0–1.0>, "variables": {"row_count": 1250.0, …}}` — keys must match declared names
(`422 UNKNOWN_VARIABLE` otherwise). Results collapse last-write-wins per `data_time`, returned
newest-first.

Confirm before any write; surface `403 READ_ONLY_ROLE` verbatim.

## Mode: routine — author a validation routine (flagship)

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
   Airflow files) for platform / schema / table signals.
2. **Confirm** — restate the inferred platform + schema + table; get explicit agreement.
3. **Resolve via DataHub search** — query the pass-through:
   ```bash
   dataspoke-api POST /hub/graphql '{"query":"query($q:String!){ search(input:{type:DATASET, query:$q, start:0, count:10}){ searchResults{ entity{ urn } } } }","variables":{"q":"<schema.table>"}}'
   ```
   Present the candidate URNs.
4. **Double-check** — confirm the exact URN with the user before it is used in any call. A wrong
   URN silently writes to the wrong dataset.

### Phase 3 — Generate the routine into the engineer's script

Write/Edit the routine into **their** pipeline file (their environment, their credentials —
never DataSpoke's). The routine: computes the declared metrics over the fresh partition, fetches
the recent baseline (`GET …/result?from=<~14d ago>`), fits a forecast / comparison, decides the
`score` in pipeline code, and POSTs the result. Adapt the template to the user's stack:

```python
import os, datetime as dt, requests

DATASPOKE = os.environ["DATASPOKE_API_URL"].rstrip("/")          # …/api/v1
TOKEN     = os.environ["DATASPOKE_API_TOKEN"]
URN       = "<confirmed dataset_urn>"
H         = {"Authorization": f"Bearer {TOKEN}"}
data_time = "<partition timestamp, RFC3339 UTC>"                  # e.g. today's partition

# 1) Compute metrics over the freshly written partition (your engine/SQL).
row_count               = count_rows(...)                         # e.g. SELECT COUNT(*)
content_type_null_ratio = null_ratio(..., "content_type")        # NULLs / total, 0.0–1.0

# 2) Fetch the recent baseline (newest-first; index 0 = latest).
since = (dt.date.today() - dt.timedelta(days=14)).isoformat()
hist  = requests.get(f"{DATASPOKE}/spoke/common/data/{URN}/attr/validation/result",
                     headers=H, params={"from": since, "limit": 14}).json()["results"]
series = [r["variables"]["row_count"] for r in reversed(hist)]    # oldest→newest

# 3) Forecast / compare IN PIPELINE CODE (DataSpoke does not do this).
#    e.g. Prophet with default params, or a simpler rolling baseline:
expected_low, expected_high = forecast_band(series)              # your choice of model

# 4) Decide the score (your thresholds — DataSpoke just stores it).
ok_nulls = content_type_null_ratio <= 0.10
ok_count = expected_low <= row_count <= expected_high
score    = 1.0 if (ok_nulls and ok_count) else 0.0

# 5) POST the result (variable keys must match the registered conf).
requests.post(f"{DATASPOKE}/spoke/common/data/{URN}/attr/validation/result", headers=H,
              json={"data_time": data_time, "score": score,
                    "variables": {"row_count": float(row_count),
                                  "content_type_null_ratio": float(content_type_null_ratio)}}
              ).raise_for_status()
```

Make the boundary explicit in what you generate and explain: forecasting and thresholding are
the **pipeline's** logic; DataSpoke receives only the final numbers. Point the user at the
deployment's `/redoc` (WebFetch the `redoc_url` from `~/.dataspoke/config.json`) for the exact
request/response schemas if they need field-level detail.
