---
name: dataspoke-validation
description: Write data-quality validation into a data pipeline, backed by DataSpoke as the result store (UC2). Use whenever the user is authoring or editing pipeline code that builds a partition and writes it to a destination table — PySpark, awswrangler/pandas, dbt, SQL, Airflow tasks — and wants checks on what it just wrote, with history and trend tracked across runs. Designs the judging method from the dataset's own measured history, generates the metric computation, baseline fetch, and scoring, and wires the DataSpoke calls that register the validation slot and post the score. Also manages validation slots directly — register/edit a conf, post or query results, browse the cross-dataset list. Triggers on "how do I write validation code for this dataset", "add validation to this pipeline", "register validation for this table", "what validation results exist", "validate the partition I just wrote", "row count check for this table", "is this dataset validated", and the equivalent phrasing in the user's own working language.
argument-hint: "[manage | routine] [question or dataset]"
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(dataspoke-api *), Bash(dataspoke-schema *), Bash(datahub-graphql *), WebFetch, AskUserQuestion
---

## Purpose

Two modes against a deployed DataSpoke. If `dataspoke-api` reports no access, send the user to
`/dataspoke:dataspoke-access` first.

1. **routine** — the flagship. The user is writing pipeline code (PySpark, awswrangler/pandas,
   dbt, SQL, an Airflow task) that builds a partition (or maintains a whole dataset) and writes it
   to a destination table. Design and write the validation that runs against what it just wrote,
   and wire it to DataSpoke.
2. **manage** — operate the validation slot directly: read/register/edit a conf, post/query
   results, browse the cross-dataset view.

## Example invocation

> /dataspoke-api Based on dataspoke validation config, validation codes should be written in this pipeline for table 'example_v1'. guide me the process. I want the EDA is done based on the data after 2026.06.01 (around 3 months ago).

Default to **routine** whenever there is pipeline code in play — including when the user never
says the word "validation" but asks for a row-count check, a null check, a freshness check, or
"make sure the write looks right."

## What DataSpoke does and does not do (state this honestly)

DataSpoke offers an **API for registration, get, and put of values** — nothing more:

| DataSpoke provides | DataSpoke does **not** provide |
|---|---|
| Register a validation slot (conf: four sections) | Any computing engine |
| `PUT`/`PATCH`/`GET` the score and variable values, with history | Timeseries prediction / forecasting |
| Cross-run history, cross-dataset views, event reports | Anomaly detection |
| Emission of results to DataHub as assertions | Threshold or rule evaluation |

There is **no metric computation, no forecast engine, no anomaly detector, and no rule engine
inside DataSpoke**. Every number — each variable *and* the final `score` — is computed by the
pipeline, on the user's own engine, with the user's own credentials. DataSpoke receives finished
numbers and stores them; it never stores or executes the check logic itself.

**You write the computing code** — the metrics, the baseline comparison, the judging logic, the
thresholds — and it runs in the user's pipeline. If the user asks DataSpoke to "detect anomalies"
or "set a threshold," correct the framing: that logic gets authored into their pipeline, and
DataSpoke stores what it decides. Never imply DataSpoke evaluates anything.

## Validation is selective, and designed from real history — not a default check

Teams add validation to datasets whose failure has meaningful impact (many downstream consumers,
a direct relationship to an important business metric), not to every dataset as a matter of
course. Ask about downstream impact when the pipeline doesn't already make it obvious — it ranks
candidate measurements.

**There is no flagship default check to suggest before looking at the data.** The design comes
from dataset-specific exploratory analysis over representative history, choosing whichever
judging method — invariant, forecast, relation, or a mix — the measured series actually supports.
Bias the design toward **high recall**: missed alarms are costlier than false alarms, and a
separate human or judging agent may triage the flagged candidates later, but no such judge ships
with this plugin or with DataSpoke. `references/validation-authoring.md` owns the open method
set, the owner-question set to ask before measuring, and the scoring arithmetic (a criterion that
cannot yet be judged counts against the score — there is no cold-start sentinel pass).

## Mode: routine — write validation into the user's pipeline (flagship)

**Before doing anything, print the whole 9-step route below and say which step is current.** The
route depends on one question, answered first, before step 1: does an existing conf or module
(`references/validation-authoring.md` §Reuse search) already cover this dataset? Reuse is the
normal answer, so ask it up front rather than defaulting to design-from-scratch. Tell the user
which of the two resulting paths they're on before proceeding:

- **New check** — no existing coverage. Walks the full route, starting at step 1.
- **Reuse** — an existing utility already covers this check. Skip straight to step 4: this
  dataset still needs its own conf (a conf is per-dataset, not per-implementation) and its own
  two review gates, but no new design — step 6 is then "wire the call site to the existing
  utility," not "author a new one."

1. **Analysis.** Resolve access, ingestion coverage, and `dataset_urn` (below), then ask the
   owner-question set and measure candidate variables over representative history —
   `references/validation-authoring.md` owns this in full. The point is to settle a judging
   method per candidate, not to produce a report.
2. **Plan.** Write the design down: judging method and rationale per criterion, conf shape
   (`variables`/`attribute`/`parameter`), which partition is scored, cadence, scoring
   denominator, cold-start floors, backfill range, the pipeline insertion point, and — when a
   shared engine or existing module is nearby but not being extended — which of its files stay
   deliberately untouched (`references/validation-authoring.md` §Open method set).
3. **Plan review — stop for review.** The user reviews the plan. **No code and no conf write
   until they answer.**
4. **Register the conf.** `PUT`/`PATCH` through `dataspoke-api`, after plan approval — never
   before, and never generated into the recurring pipeline code itself.
   `references/validation-conf.md` owns the exact body and the description convention.
5. **Conf review — stop for review.** Read the stored conf back and diff it against the approved
   plan: description, variable names/order, parameter names/values, cadence. **This is a distinct
   human gate from step 3 — never merge the two into one turn; each ends with the user
   answering.** Names are free to change here and expensive to change once results exist.
6. **Implement.** The scorer (in the team's package, on the new-check path) and the pipeline call
   site (always). `references/validation-authoring.md` owns the worked example, the wiring
   pattern per engine, and the failure/outage policy.
7. **Unit test.** The query and every DataSpoke API call monkeypatched — coverage checklist in
   `references/validation-authoring.md`.
8. **Run one real partition,** then read the result back through the API and diff it against the
   pipeline's own log.
9. **Backfill, oldest to newest.** Only after step 8 passes — **8 must land before 9**, so a
   mistake costs one partition instead of a whole series.

Registering a conf can make a watched `validation-score` metric look worse before it looks
better — say so before the write, not after. `references/validation-conf.md` and
`references/validation-authoring.md` explain why (the metric's latest-result rule, and how a
high-recall design compounds it during warm-up).

### Resolve `dataset_urn` — never guess

1. **Gather hints** from the engineer's workspace (pipeline scripts, SQL, configs, dbt/Airflow
   files) — the destination of the write being validated is the strongest hint.
2. **Confirm** the inferred platform + schema + table with the user before any lookup.
3. **Resolve via DataHub search**:
   ```bash
   datahub-graphql '{"query":"query($q:String!){ search(input:{type:DATASET, query:$q, start:0, count:10}){ searchResults{ entity{ urn } } } }","variables":{"q":"<schema.table>"}}'
   ```
   If `datahub-graphql` reports no DataHub access, send the user to `/dataspoke:dataspoke-access`
   to add a DataHub GMS URL + token, then retry. Manual URN entry is a last resort, only when
   search yields no candidate.
4. **Double-check** the exact URN with the user before it is used in any call — a wrong URN
   silently writes to the wrong dataset.

## Mode: manage — capabilities → routes

| Intent | Call |
|--------|------|
| Read a dataset's conf | `dataspoke-api GET /spoke/common/data/{urn}/attr/validation/conf` |
| Register / replace conf | `dataspoke-api --confirm PUT /spoke/common/data/{urn}/attr/validation/conf @PATH` |
| Partially update conf | `dataspoke-api --confirm PATCH /spoke/common/data/{urn}/attr/validation/conf @PATH` |
| **Destroy** the slot (see reference) | `dataspoke-api --confirm DELETE /spoke/common/data/{urn}/attr/validation/conf` |
| Append a result | `dataspoke-api --confirm POST /spoke/common/data/{urn}/attr/validation/result @PATH` |
| Query result history | `dataspoke-api GET '/spoke/common/data/{urn}/attr/validation/result?from=…&until=…&limit=…'` |
| Cross-dataset list | `dataspoke-api GET '/spoke/validation?coverage=covered'` |
| Validation event reports | `dataspoke-api GET /spoke/common/data/{urn}/event/validation` |
| Full per-dataset timeline | `dataspoke-api GET '/spoke/common/data/{urn}/event?event_major_type=VALIDATION'` |

**Read `references/validation-conf.md` before generating any conf or result body, or reconstructing
one from memory** — it owns the exact field shapes, the `PUT`/`PATCH` verb matrix, the `score_note`
construction rule, the pagination window, the destructive-delete warning, and the error table.
Write any JSON body to a scratch file with the `Write` tool and pass it as `@PATH` — never inline a
multi-field conf body as a literal shell argument. Confirm before any write; surface
`403 READ_ONLY_ROLE` verbatim rather than retrying through another surface.

`coverage=covered` (the default) on the cross-dataset list answers "what **is** validated," not
"what **could be**" — follow `plugin/references/pagination.md` and do not report a first page as
the full picture.
