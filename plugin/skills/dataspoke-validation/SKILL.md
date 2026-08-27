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
| Register a validation slot (conf: four sections, below) | Any computing engine |
| `PUT`/`PATCH`/`GET` the score and variable values, with history | Timeseries prediction / forecasting |
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

**The conf has four sections**, not two — `description` and `variables` are what the pipeline
declares (what it will report); `attribute` states the dataset's own data-arrival cadence, which
DataSpoke reads back to anchor the governance `validation-score` metric's window; `parameter` is
optional opaque storage for the pipeline's own hyperparameters, which DataSpoke never
interprets. Full shapes: [Mode: manage §Conf](#mode-manage--capabilities--routes) below. Because
`PUT` is a full replace, generating the two-section body alone against an existing conf **clears
`parameter` and resets `attribute` to its defaults** — say so at the point any conf body is
generated, not just in this reference section.

## Mode: routine — write validation into the user's pipeline (flagship)

Drive six phases in order (five authoring phases — `spec/AI_PLUGIN.md` §Validation Routine
Authoring's four, with its §4 split here into utility-authoring and dataset-wiring — plus a
closing test/backfill phase).

### Phase 1 — Prerequisite chain (strict; stop on first failure)

1. **Access** — `dataspoke-api GET /auth/me` reports an Editor/Admin **account** role (that is
   not the same as the token's effective role, which the route never returns — see
   `/dataspoke:dataspoke-access`'s `role_snapshot` note; a `403` on the first write despite this
   passing means the token itself is stale). Otherwise → `/dataspoke:dataspoke-access`.
2. **Ingested** — `dataspoke-api GET /spoke/common/data/{urn}/attr/ingestion` confirms the
   dataset is covered. If not → `/dataspoke:dataspoke-ingestion` first (validation assumes the
   dataset is registered in DataHub).
3. **Conf** — `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf`. If absent,
   register one (`PUT`) with the variables the routine will compute and the real arrival
   cadence (`attribute` — ask the user; see Phase 3's cadence table), after confirming with the
   user. **Do not name the implementing utility in `description` here** — which utility applies
   isn't decided until Phase 3; naming it is that phase's job (via `PATCH`), not this step's.

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

### Phase 3 — Check for reuse before authoring

Before writing new check logic, search for an existing implementation rather than assuming none
exists:

- `dataspoke-api GET '/spoke/validation?coverage=covered'` — other datasets' registered confs.
  The conf `description` conventionally names the implementing module, so a match is
  recognizable (this is *why* Phase 4's authoring step writes it there — the convention only
  works if every conf's `description` actually names its utility).
- Grep the user's own shared/validation package in their workspace for an existing check of the
  same shape.

**Path A — an existing utility already covers the check.** Wire that utility into the pipeline
(Phase 5) rather than re-authoring the logic. Skip Phase 4.
**Path B — the check does not exist yet.** Author it (Phase 4), then wire it in (Phase 5).

**On both paths**, once you know which utility implements the check, name it in the conf's
`description` via `dataspoke-api --confirm PATCH /spoke/common/data/{urn}/attr/validation/conf
@PATH` (a `description`-only body — `PATCH` leaves `variables`/`attribute`/`parameter`
untouched). This
keeps the reuse convention accurate for the next dataset that searches it.

**This ordering is load-bearing, not a nicety.** The conf must be registered (Phase 1) *before*
the pipeline ever calls the utility, on both paths. A missing conf does not raise: Phase 4's
failure-policy invariant means the utility's public entry point catches a `404
CONFIG_NOT_FOUND` into a logged warning, not an exception — so a pipeline calling an
unregistered check completes as if it had validated, while nothing reaches DataSpoke's history.

**Cadence.** Before registering or updating `attribute`, ask the user the dataset's real arrival
cadence — do not leave it at the default (daily, no lag) unless that is actually correct.
`attribute` is `{cadence_unit, cadence_offset}` in seconds; the offset table for common cases
(offset = *n* − 1 for D-*n* data):

| Cadence | `cadence_unit` | `cadence_offset` |
|---|---|---|
| Daily, arrives D-1 | `86400` | `0` |
| Daily, arrives D-3 | `86400` | `2` |
| Hourly, previous hour | `3600` | `0` |

Getting this wrong is silent in both directions (see [Mode: manage](#mode-manage--capabilities--routes)
below): too-fresh a default makes a genuinely-on-time dataset read as failing every day; too-lax
lets a genuinely stale dataset read as passing. The governance `validation-score` metric anchors
its window on this value per dataset — cross-reference `/dataspoke:dataspoke-governance` if the
user is also setting up that metric.

**No specific check named?** Default to suggesting a per-partition **row-count anomaly check
via Prophet forecasting** — this is the flagship default (`spec/AI_PLUGIN.md` §Validation
Routine Authoring names it explicitly) because it is simple, needs only the history DataSpoke
already stores, and catches the single most common data-quality failure (a partition that's
empty, truncated, or duplicated). State it as a suggestion and get explicit acceptance before
writing anything — the user may want a different metric or algorithm instead. Phase 4 below
walks through it end to end as the worked example.

### Phase 4 — Author the reusable validator utility (Path B only)

**What goes here vs. Phase 5.** Everything dataset-*independent* — metric computation, baseline
fetch, scoring, outage handling around the DataSpoke calls — belongs in a reusable utility in the
user's own package, not inline in the pipeline script. Only `dataset_urn` resolution and the call
into this utility are dataset-specific; that's Phase 5. This split is what makes Phase 3's reuse
search meaningful: a utility worth finding again has to actually live somewhere reusable.

**Registration stays out of both.** Neither the utility nor the pipeline script ever `PUT`s or
`PATCH`es the conf — that happens once, from the skill session with `dataspoke-api`, in Phase 1
(register) and Phase 3 (name the utility in `description`). A conf re-registered on every run is
a recurring opportunity to silently change the declared `variables` out from under the
accumulated history.

**Location.** Ask which package and naming convention the team uses — never invent one. A
pattern many teams recognize is `<team-package>/validator/<algorithm>_<NN>` (the `_NN` suffix
disambiguates multiple variants of the same algorithm, e.g. different parameter presets). This
worked example uses `src/team_a/common/validator/prophet_01` — **an illustration of the pattern,
not a house rule**; ask the real team/package name before writing anything.

#### `parameter` vs `variables` — get the direction right

Both are `[{name, description}]`-shaped lists (`parameter` has one extra field, `value`) on the
same conf, and they flow in **opposite directions**:

| | Direction | Type | Meaning |
|---|---|---|---|
| `parameter` | conf → code | string | An **argument to** the validator (a hyperparameter — retunable without a redeploy) |
| `variables` | code → conf | float | A **measurement the code produces**, stored as a timeseries |

Getting it backwards is easy and the API cannot catch it — both are just named lists.

**A `variables` series is the scorer's own intermediate store.** A later run reads it back as its
baseline (the worked example's baseline `GET`, below). This is why a check must **never re-derive history by
rescanning the source**: a check that rescans a trailing 7-day window on every run costs 7× the
query and gets a *worse* answer, because it cannot see what earlier runs actually observed at the
time — only what the source looks like now, which may itself have changed. Serving history back
cheaply from `variables` is the entire reason results are stored at all.

Corollaries: renaming a `variables` key orphans the series (past results keep the keys they were
posted with — see [Mode: manage](#mode-manage--capabilities--routes)); posting an undeclared key
is `422 UNKNOWN_VARIABLE`; a subset of declared variables is fine, including an empty map; the
two namespaces are independent, so a name may appear in both.

#### Reading a `parameter` safely — it is an opaque, unvalidated string

The server never checks a `parameter` value beyond the length/charset rules in [Mode:
manage](#mode-manage--capabilities--routes) — a typo reaches the code as an ordinary string. The
rule: **parse, range-check, warn, and fall back to the module default. Never raise** — a bad
`parameter` must not disable the check that would have caught a bad partition. Three supporting
rules:

- The module default is the shipped behavior — a malformed conf degrades to the previous
  thresholds and keeps judging, it does not stop judging.
- An explicit call argument (if the utility is also callable directly, e.g. from a test) beats
  the conf.
- **Resolve lazily** — read `parameter` after the routine's hard gates (partition exists, has
  rows to even measure), not before. An eager read that runs before those gates turns an empty
  partition into a swallowed exception instead of a reported `score: 0.0`.

#### Failure policy — the routine reports, it never raises

- **Nothing raises out of the utility's public entry point** — not an unreachable result store,
  not a bug in the scoring, and **not a bad partition either**. A failed check is a `score: 0.0`
  in the history, never a raised exception. Structure it as a thin public function wrapping a
  private one: the public entry point is a bare `except Exception` → warn (log) → return, so a
  bug never escapes into the pipeline's own error handling; the private function raises freely,
  which keeps bugs visible in unit tests and any direct call.
- **The backstop is the governance layer, not the pipeline.** For a dataset whose conf *is*
  registered, the `validation-score` metric reports it out of `valid_in_time` when the latest
  result scores `< 1.0` — so raising inside the routine buys nothing that metric doesn't already
  catch, at the cost of a red pipeline task and a blocked downstream. That backstop does **not**
  cover an unregistered conf (Phase 3's ordering invariant is what guards that case) — validation
  runs *after* the write, so it can only report on the run, never improve it.
- **Report the worst finding rather than staying silent.** "No data at all" is a result to
  *post*, not a reason to skip the post: measure what can be measured (a row count of `0`), score
  it `0.0`, post it at the partition's `data_time`. This is counter-intuitive but has a
  quantitative reason: `validation-score` fails a dataset whose latest result is not `score >=
  1.0`, so a posted `0.0` is caught on the metric's very next measurement — while a *missing*
  point leaves the previous `1.0` standing as the latest result until it ages out of the window
  (`time_window_sec`-wide; two days at the factory default). **Silence delays detection; it never
  speeds it up.**

#### Outage vs. fatal — draw the line explicitly for the result-store calls

The tempting shortcut — a blanket `except RequestException` around every DataSpoke call — is
wrong: it produces a check that reports "fine" forever while validating nothing. Use an
allowlist instead, with the reasoning stated so it doesn't erode over time:

| Treat as an outage → skip this partition, keep going | Keep fatal → abandon the call |
|---|---|
| Connection refused / DNS failure | **4xx**: `401`/`403`, `404 CONFIG_NOT_FOUND`, `422` |
| Read/connect timeout | Malformed API URL / misconfigured origin |
| Retries exhausted against `429`/`5xx` | A response that parsed but violated a checked invariant |
| `5xx` | Anything raised by the metric query itself |

The asymmetry: an outage is transient and self-heals, so skipping costs one partition's verdict.
Everything in the right column is a **standing fault that will recur on every run** — it needs a
human, not a retry. "Fatal" here means "stop this call," never "fail the pipeline" — it sits
*inside* the entry point's catch-all above, not instead of it. Put the classifier in a shared
utility next to the DataSpoke HTTP calls (not duplicated inside each scorer) — every check needs
the same verdict, and the rule is about the *store's* reachability, not the metric being checked.

One route-specific split changes the classification, and the two codes that look alike here mean
opposite things: on the result `POST`, **`502` does not mean the measurement was lost** — the row
is committed locally first, and `502` there means only the *subsequent* DataHub assertion emit
failed (`502 DATAHUB_UNAVAILABLE`), so it's an outage to the caller while the row is already
durably stored. **`503` is the opposite** — it means the row was *not* committed at all, either
because the storage tier itself is unreachable (`503 STORAGE_UNAVAILABLE`) or a required
peripheral isn't configured (`503 PERIPHERAL_NOT_CONFIGURED`, a standing misconfiguration, not a
transient outage). Treat `502` as "skip and retry next run"; treat `503` on this route as fatal,
the same as the 4xx column above.

#### Worked example — `prophet_01`: per-partition row-count anomaly via Prophet

Applying everything above to the Phase 3 default suggestion (row-count anomaly detection). The
conf this expects (registered in Phase 1, `description` set in Phase 3 to point back here):

```json
{
  "description": "prophet_01 - team_a",
  "variables": [{"name": "row_count", "description": "rows in the partition just written"}],
  "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
  "parameter": [
    {"name": "lookback_days_max", "value": "112", "description": "max history fed to Prophet"},
    {"name": "lookback_days_min", "value": "7", "description": "min contiguous history required, ending at the target day"},
    {"name": "growth", "value": "linear", "description": "Prophet growth argument"},
    {"name": "weekly_seasonality", "value": "True", "description": "Prophet weekly_seasonality argument"},
    {"name": "interval_width", "value": "0.95", "description": "Prophet prediction-interval width"}
  ]
}
```

`src/team_a/common/validator/prophet_01/__init__.py` (illustrative — team package name and
exact structure are the team's call, per the reuse-search convention above):

```python
import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DATASPOKE = os.environ["DATASPOKE_API_URL"].rstrip("/")  # must include the /api/v1 prefix
TOKEN = os.environ["DATASPOKE_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

# Module defaults — the shipped behavior a malformed `parameter` degrades to.
_DEFAULT_LOOKBACK_MAX = 112
_DEFAULT_LOOKBACK_MIN = 7
_DEFAULT_GROWTH = "linear"
_DEFAULT_WEEKLY_SEASONALITY = True
_DEFAULT_INTERVAL_WIDTH = 0.95


def _parse_params(raw: dict[str, str]) -> dict:
    """Parse conf `parameter` values, falling back to the module default per-field
    on anything malformed. Never raises — a bad parameter must not disable the check.
    Each field is parsed independently, so one malformed value doesn't discard the
    other, perfectly valid ones."""
    out = {
        "lookback_days_max": _DEFAULT_LOOKBACK_MAX,
        "lookback_days_min": _DEFAULT_LOOKBACK_MIN,
        "growth": _DEFAULT_GROWTH,
        "weekly_seasonality": _DEFAULT_WEEKLY_SEASONALITY,
        "interval_width": _DEFAULT_INTERVAL_WIDTH,
    }
    if "lookback_days_max" in raw:
        try:
            v = int(raw["lookback_days_max"])
            out["lookback_days_max"] = v if v > 0 else _DEFAULT_LOOKBACK_MAX
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad lookback_days_max, using default: %s", exc)
    if "lookback_days_min" in raw:
        try:
            v = int(raw["lookback_days_min"])
            out["lookback_days_min"] = (
                v if 0 < v <= out["lookback_days_max"] else _DEFAULT_LOOKBACK_MIN
            )
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad lookback_days_min, using default: %s", exc)
    if "growth" in raw:
        if raw["growth"] in ("linear", "logistic", "flat"):
            out["growth"] = raw["growth"]
        else:
            logger.warning("prophet_01: bad growth %r, using default", raw["growth"])
    if "weekly_seasonality" in raw:
        out["weekly_seasonality"] = raw["weekly_seasonality"].strip().lower() == "true"
    if "interval_width" in raw:
        try:
            v = float(raw["interval_width"])
            out["interval_width"] = v if 0.0 < v < 1.0 else _DEFAULT_INTERVAL_WIDTH
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad interval_width, using default: %s", exc)
    return out


class _Outage(Exception):
    """Result store unreachable — skip this partition, do not fail the pipeline."""


def _dataspoke_get(url: str, **kw) -> dict:
    # Every 5xx here is an outage: a plain GET (conf, baseline) has none of the
    # "might already be stored elsewhere" nuance the result POST has below — the
    # example doesn't retry before giving up; add backoff around this in
    # production if transient 5xx bursts are common on your deployment.
    try:
        resp = requests.get(url, headers=H, timeout=10, **kw)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _Outage(str(exc)) from exc
    if resp.status_code in (401, 403, 404, 422):
        resp.raise_for_status()  # fatal — a standing fault, not an outage
    if resp.status_code >= 500:
        raise _Outage(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def _dataspoke_post_result(url: str, body: dict) -> None:
    try:
        resp = requests.post(url, headers=H, json=body, timeout=10)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _Outage(str(exc)) from exc
    if resp.status_code in (401, 403, 404, 422):
        resp.raise_for_status()  # fatal
    if resp.status_code == 502:
        # The row is committed locally before the DataHub emit; 502 here means
        # only that emit failed (502 DATAHUB_UNAVAILABLE) — an outage to the
        # caller, not a lost measurement.
        raise _Outage("HTTP 502 (result already stored; DataHub emit deferred)")
    if resp.status_code == 503:
        # Unlike 502: nothing was committed. 503 on this route means the storage
        # tier itself is unreachable (503 STORAGE_UNAVAILABLE) or a peripheral
        # isn't configured (503 PERIPHERAL_NOT_CONFIGURED) — a standing fault,
        # fatal the same as the 4xx cases above.
        resp.raise_for_status()
    resp.raise_for_status()


def _measure_row_count(dataset_urn: str, data_time: datetime) -> int:
    """The one engine-specific step. Replace this body with the real aggregation
    for your stack — see Phase 5's PySpark / awswrangler examples, which show
    what goes here for each engine."""
    raise NotImplementedError("wire in the per-engine aggregation from Phase 5")


def _check(dataset_urn: str, data_time: datetime) -> None:
    """Raises freely — bugs stay visible in tests and direct calls."""
    # 1) COMPUTE first — the one hard gate this routine has (can the partition
    #    even be measured). Nothing else is worth fetching if this fails.
    row_count = _measure_row_count(dataset_urn, data_time)

    # Everything below is resolved lazily, after that gate — per § Reading a
    # `parameter` safely: an eager conf read ahead of the measurement would
    # turn an unmeasurable partition into a swallowed exception with nothing
    # useful logged about why.
    from prophet import Prophet  # heavy import, kept inside the function

    conf_url = f"{DATASPOKE}/spoke/common/data/{dataset_urn}/attr/validation/conf"
    result_url = f"{DATASPOKE}/spoke/common/data/{dataset_urn}/attr/validation/result"

    conf = _dataspoke_get(conf_url)
    raw_params = {p["name"]: p["value"] for p in conf.get("parameter", [])}
    params = _parse_params(raw_params)

    # 2) GET the baseline, bounded by an explicit limit — never the server default.
    since = (data_time.date() - timedelta(days=params["lookback_days_max"])).isoformat()
    page = _dataspoke_get(result_url, params={"from": since, "limit": 10000})
    hist = list(reversed(page["results"]))  # oldest -> newest

    # 3) FORECAST / DECIDE the score.
    if len(hist) < params["lookback_days_min"]:
        # Cold start: not enough history to forecast from yet. This says
        # nothing about whether *this* partition is bad, so it is not scored
        # as a failure — score 1.0 and still post the measurement, so the
        # point exists for a future run's baseline (Phase 6 §3: the cold-start
        # stretch is expected, not a bug).
        score = 1.0
        logger.warning(
            "prophet_01: only %d prior points, need %d — recording baseline, not yet scoring",
            len(hist), params["lookback_days_min"],
        )
    else:
        series = [(h["data_time"][:10], h["variables"]["row_count"]) for h in hist]
        df = pd.DataFrame(series, columns=["ds", "y"])
        model = Prophet(
            growth=params["growth"],
            weekly_seasonality=params["weekly_seasonality"],
            interval_width=params["interval_width"],
        )
        model.fit(df)
        forecast = model.predict(pd.DataFrame({"ds": [data_time.date().isoformat()]}))
        low, high = float(forecast["yhat_lower"].iloc[0]), float(forecast["yhat_upper"].iloc[0])
        score = 1.0 if low <= row_count <= high else 0.0
        # A genuinely empty partition (row_count == 0) needs no special case:
        # it flows through this same comparison and naturally scores 0.0
        # whenever the forecast expected anything above zero.

    # 4) POST — per § Failure policy: post even a zero rather than skip.
    #    data_time must be an aware UTC datetime — a naive value serializes
    #    without an offset and is rejected 422 INVALID_PARAMETER, which this
    #    routine then treats as fatal.
    _dataspoke_post_result(result_url, {
        "data_time": data_time.isoformat(),
        "score": score,
        "variables": {"row_count": float(row_count)},
    })


def validate_row_count(dataset_urn: str, data_time: datetime) -> None:
    """Public entry point. Never raises — a store outage, a scoring bug, or a
    bad partition alike become a logged warning; on a store outage nothing is
    posted (§ Failure policy — the governance backstop only covers a landed
    result, so this path relies on Phase 3's registration-ordering, not on
    this function raising)."""
    try:
        _check(dataset_urn, data_time)
    except _Outage as exc:
        logger.warning("prophet_01: DataSpoke unreachable, skipping this partition: %s", exc)
    except Exception:
        logger.exception("prophet_01: unexpected failure validating %s", dataset_urn)
```

This is one worked shape, not a template to reproduce verbatim — adapt `_measure_row_count`'s
body to the real engine (Phase 5 shows PySpark and awswrangler), and the parameter set to what
the user's check actually needs. The pattern that matters is structural: thin never-raising
public function, freely-raising private function, the measurement itself as the one hard gate
before anything else resolves lazily, explicit `limit`, the 502/503 outage split, always-post-
on-reachable.

### Phase 5 — Wire the dataset-specific code

Write/Edit into **their** pipeline file — their engine, their credentials, never DataSpoke's.
This is deliberately thin: resolve the URN (Phase 2), then call the Phase 4 utility (or an
existing one, Phase 3 Path A) with that URN and the partition's `data_time`.

**Attachment point.** The validation goes *after* the write it validates, gated on that write
having succeeded. Validate what actually landed: prefer re-reading the destination partition over
reusing the in-memory DataFrame, since the two diverge exactly when something went wrong (partial
write, schema coercion, silently dropped rows). Say which one you chose and why.

```python
# Airflow task / pipeline script — dataset-specific, thin.
from team_a.common.validator.prophet_01 import validate_row_count

validate_row_count(
    dataset_urn="<confirmed dataset_urn>",       # Phase 2
    data_time=partition_logical_date,             # see "data_time must identify the partition" below
)
```

**Metric computation, per engine.** The one engine-specific piece — replace the worked example's
`_measure_row_count` stub in Phase 4's utility (currently `raise NotImplementedError`) with the
real aggregation for the stack in front of you:

*PySpark* — re-read the destination partition, aggregate in one pass:

```python
def _measure_row_count(dataset_urn: str, data_time: datetime) -> int:
    from pyspark.sql import functions as F

    part = spark.read.format("delta").load(DEST).where(F.col("dt") == PARTITION)
    return part.agg(F.count(F.lit(1)).alias("row_count")).first()["row_count"]
```

*awswrangler / pandas* — push the aggregation into Athena rather than pulling the partition:

```python
def _measure_row_count(dataset_urn: str, data_time: datetime) -> int:
    import awswrangler as wr

    df = wr.athena.read_sql_query(
        "SELECT COUNT(*) AS row_count FROM {table} WHERE dt = :dt".format(table=TABLE),
        database=DB, params={"dt": PARTITION},      # parameterized — never f-string the value
    )
    return int(df.row_count[0])
```

For a `wr.s3.to_parquet(..., dataset=True)` write, run this *after* the catalog update so the new
partition is visible to Athena.

**Airflow.** Keep validation a separate task downstream of the write, not a tail appended to it —
the write stays retryable on its own, and a validation failure is visible as its own task. Whether
a low score should fail the *task* is the user's call at the orchestrator level: ask, and default
to recording the result and letting the DAG continue, since DataSpoke is a result store rather
than a gate. This is a distinct decision from Phase 4's failure policy — the utility never raises
internally regardless of this answer; this question is only about what the *orchestrator* does
with a low score once it's already recorded.

**Re-run safety.** A retry that re-calls the utility with the same `data_time` is safe: reads
collapse to the newest write per `data_time`, so the partition is corrected, not duplicated. No
dedup guard is needed — do not generate one.

**`data_time` must identify the partition, not the moment of the run.** This is the single
decision that determines whether the baseline series is meaningful. Use the partition's own
timestamp — the `dt`/`ds` value, the Airflow logical date, the window start — truncated to the
grain the table is partitioned at. Never `datetime.now()`: it makes every run a distinct
`data_time`, so retries stop collapsing and accumulate as separate points, a day's worth of
hourly runs looks like a day's worth of daily history, and comparing "today vs. the last 14
values" silently compares against the last 14 *hours*. State the chosen grain explicitly in what
you generate. It also **must be timezone-aware UTC** — the API's `data_time` field is an aware
datetime; a naive value serializes with no offset and is rejected `422 INVALID_PARAMETER` (fatal
per the outage table, then swallowed by the entry point — a silent no-op, not a loud failure).
Attach `tzinfo=timezone.utc` explicitly if the partition value you have is naive.

**When the pipeline runs more than once per day**, the fetched history holds one point per run,
not per day. Either scale the window to the run frequency (a 14-run baseline for an hourly job is
~14 hours — usually not what the user means), or bucket by day in the utility before comparing:

```python
by_day = {}                                                        # newest-first input,
for r in hist:                                                     # so the first hit per day
    by_day.setdefault(r["data_time"][:10], r)                      # is that day's latest
series = [by_day[d]["variables"]["row_count"] for d in sorted(by_day)]
```

Ask which the user wants rather than assuming — the right answer depends on whether the metric is
per-partition (row count) or per-day (daily total).

**Credentials.** The routine reads `DATASPOKE_API_URL` / `DATASPOKE_API_TOKEN` from the pipeline's
environment. `DATASPOKE_API_URL` here must include the `/api/v1` prefix — the generated routine
builds URLs by simple string concatenation, unlike `dataspoke-api` (the plugin's own CLI wrapper),
which tolerates either the bare origin or the `/api/v1`-suffixed form and normalizes it; if the
pipeline's env var is provisioned from the same value a person copies out of `dataspoke-api`'s
own config, confirm which shape it actually is before assuming. Never inline a `dsk_` token into
generated code, and never point the pipeline at `~/.dataspoke/config.json` — that file is the
plugin's local credential store, not a deployment artifact. Tell the user to provision the token
the way their orchestrator handles secrets.

**Confirm the shapes before generating code against them.** The routine reads `results`,
`total_count`, and `variables` out of the response, and posts a body that must match the conf.
Read the real contract rather than trusting this file:

```bash
dataspoke-schema attr/validation/result       # request + response schemas
dataspoke-schema attr/validation/conf
```

`/redoc` is the same document rendered for **humans** — give the user that URL (it is in
`~/.dataspoke/config.json` as `redoc_url`) when they want to browse it themselves.

### Phase 6 — Test and backfill

1. **Unit tests**, no network: monkeypatch the metric query and every result-store call
   (`_dataspoke_get`/`_dataspoke_post_result` in the worked example). Cover: each score branch;
   **both outage directions** (an injected connection error is caught and logged, not raised; an
   injected `404`/`401` propagates out of the *private* function — assert this directly against
   `_check`, not the public wrapper); the `data_time` derivation; a malformed `parameter` falling
   back to the module default; and that **nothing escapes the public entry point** — call
   `validate_row_count` with every failure mode injected and assert it never raises. One
   subtlety: the conf read (`_dataspoke_get(conf_url)`) is not opt-in — it runs on every call —
   so its stub must be `autouse`, or any test whose partition reaches the scoring step reaches
   for the real network.
2. **One real partition**: run the routine once against real data, then read the result back
   through `dataspoke-api GET .../attr/validation/result` and report what landed — don't just
   trust that the POST returned `2xx`.
3. **Backfill oldest-to-newest.** Re-running is safe (last-write-wins per `data_time`). Two
   things to tell the user up front: the cold-start stretch (fewer than `lookback_days_min`
   points) cannot be judged by a history-dependent rule — that's expected, not a bug; and **a
   backfill of the underlying data itself should run with validation off**, then be validated in
   a second pass — judging a partition while the baseline is still being written measures the
   backfill's own progress, not the data.

**Sanity-check the chosen variable against real history before shipping.** A metric that is
constant by construction (e.g. row count on a table with a fixed daily volume enforced upstream)
gives a check that never fires; one whose series doesn't fit the comparison rule (Prophet on a
wildly non-seasonal series) fires constantly. Look at the real history and say so if either
looks likely, proposing a different variable rather than shipping either.

## Mode: manage — capabilities → routes

| Intent | Call |
|--------|------|
| Read a dataset's conf | `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf` |
| Register / replace conf | `dataspoke-api --confirm PUT /spoke/common/data/{urn}/attr/validation/conf @PATH` |
| Partially update conf | `dataspoke-api --confirm PATCH /spoke/common/data/{urn}/attr/validation/conf @PATH` |
| **Destroy** the slot (see below) | `dataspoke-api --confirm DELETE /spoke/common/data/{urn}/attr/validation/conf` |
| Append a result | `dataspoke-api --confirm POST /spoke/common/data/{urn}/attr/validation/result @PATH` |
| Query result history | `dataspoke-api GET '/spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…'` |
| Cross-dataset list | `dataspoke-api GET '/spoke/validation?coverage=covered'` |
| Validation event reports | `dataspoke-api GET /spoke/common/data/{urn}/event/validation` |
| Full per-dataset timeline | `dataspoke-api GET '/spoke/common/data/{urn}/event?event_major_type=VALIDATION'` |

Write any JSON body to a scratch file with the `Write` tool and pass it as `@PATH` — never inline
a multi-field conf body as a literal shell argument.

**Conf** body has four sections:

```json
{
  "description": "...",
  "variables": [{"name": "row_count", "description": "..."}],
  "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
  "parameter": [{"name": "lookback_days_max", "value": "112", "description": "..."}]
}
```

- `description` / `variables`: required on `PUT`. The top-level `description` is required,
  ≤2,000 chars, empty string allowed — this is the field Phase 3 writes the implementing
  utility's name into. Each `variables` item's own `name` matches `[a-z][a-z0-9_]{0,99}` and is
  unique within its own list; its per-item `description` is required, ≤200 chars, empty allowed
  — a separate, much shorter field from the top-level one.
- `attribute`: optional on `PUT` — but omitting it stores the **all-defaults object**
  (`{86400, 0}`, i.e. "daily, D-1"), never left absent. Ask the real cadence (Phase 3 above)
  rather than accepting this default silently.
- `parameter`: optional; each entry adds `value` (string, required, ≤200 chars, empty allowed)
  alongside the same per-item `name`/`description` shape as `variables`.

A `PUT` for a URN that DataHub does not track returns `422 DATASET_NOT_IN_DATAHUB` — the dataset
must be ingested first.

**The verb matters per section, not per body** — more than "`PUT` replaces, `PATCH` merges":

| | `PUT` | `PATCH` |
|---|---|---|
| Omitted `description`/`variables` | required (rejected if missing) | unchanged |
| Omitted `attribute` | stored as all-defaults | unchanged |
| Supplied `attribute` | wholesale | **wholesale — not a deep merge** |
| Omitted `parameter` | **section cleared** | unchanged |
| `parameter: null` | cleared | cleared |
| `parameter: []` | rejected `422` | rejected `422` |

So `PATCH {"attribute": {"cadence_offset": 7}}` also resets `cadence_unit` to its default — resend
the whole `attribute` object on any `PATCH` that touches it. `null` is the only spelling of
"clear the parameters."

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

**The result route is a time window, not a page walk — set `limit` explicitly.** The parameter
name differs from every other route (`from`/**`until`**, not `from`/`to`), and the default
differs too: `?limit` defaults to **`1000`** (server cap `10000`) versus the usual default-20
pagination. The window is **half-open**: `from <= data_time < until`; passing the same value for
both matches nothing. Reads are fixed `data_time DESC` (newest-first), applied *after*
collapsing. Set `limit` explicitly on every baseline read (Phase 4's worked example does this) —
relying on the default-1000 without setting it is how a wide backfill silently compares against a
truncated series. An assertion on `total_count == len(results)` is a good guard on top, not a
substitute for setting the bound.

`coverage` on the cross-dataset list (`GET /spoke/validation`) selects the row set: `covered`
(the **default**), `uncovered`, or `both`. `covered` answers "what **is** validated," never "what
**could be**" — and the covered set is typically a small minority of the estate, so don't report
a first, default-filtered list as the full coverage picture. `uncovered` rows come back with
`description`, `variable_count`, and `latest_*` all `null`; that is the shape of "no conf," not
missing data.

Confirm before any write; surface `403 READ_ONLY_ROLE` verbatim.

### `DELETE` conf is a hard delete — warn explicitly before calling it

It is **not** a soft delete or an archive. In one transaction it removes the conf row, **all of
the dataset's validation results**, and its `VALIDATION.*` events, then hard-deletes the
assertion entity from DataHub. It returns `204`; afterwards the dataset reads as never-created
(`GET`/`PATCH` → `404 CONFIG_NOT_FOUND`) and a fresh `PUT` starts an empty slot. The history is
unrecoverable. Spell out that the result history will be destroyed and get explicit agreement —
if the user only wants to stop validating, they should stop calling the utility instead.

### Changing a conf's variables breaks history continuity

`PUT`/`PATCH` replaces the declared `variables[]`, but past results keep whatever keys they were
posted with. Renaming `row_count` to `rows` leaves every historical row keyed `row_count`, so a
baseline query returns a series the new routine cannot read, and the pipeline's next POST
`422 UNKNOWN_VARIABLE`s until it is updated to match. When a rename or removal is requested, say
what it does to the existing series and offer adding a new variable alongside the old instead.
