---
name: ingestion-event-producer-split
description: events.detail.source is the INGESTION producer discriminator — 4 producers, 2 grains, 2 unbounded ms→datetime converters; PASSIVE has no run-level producer at all
metadata:
  type: project
---

`events` rows with `entity_type='ingestion_source'` now come from **four** producers, discriminated
by `detail.source`, at **two grains**:

| `detail.source` | Grain | Modes | Writes `detail.dataset_urn`? |
|---|---|---|---|
| *(absent)* — inline ACM run record | per run | `ACTIVE_CUSTOM_MANAGED` | no (URN *lists* under other keys) |
| `datahub_sync` — execution-request mirror | per run | `DATAHUB_MANAGED` | no |
| `passive_observation` — `Operation` aspect | per dataset | `PASSIVE` | yes |
| `last_ingested_observation` — `Dataset.lastIngested` | per dataset | **all** | yes |

Three consequences that keep biting:

1. **Every producer filter needs an `IS NULL` disjunct.** `detail->>'source'` on a missing key is
   SQL `NULL`, and `NULL NOT IN (...)` is `NULL` — a bare `NOT IN` silently drops the inline ACM
   run record, i.e. exactly the rows a run-outcome read exists to report. Same for
   `detail->>'dataset_urn'` on the per-dataset timeline predicate.
2. **`PASSIVE` books no run-level event at all.** Any blacklist on a run-outcome read therefore
   makes that read permanently empty for the whole mode (`attr/ingestion.latest_run` → `null`,
   list-view badge → blank). Any blacklist on a *freshness* read would leave every PASSIVE dataset
   permanently stale. Check both directions before adding a producer filter.
3. **One ms→datetime converter, and everything must route through it.** `_observed_at()`
   (module level, `src/backend/ingestion/service.py`) is total — rejects `bool`/non-numeric/NaN/
   `<=0`/out-of-range/future-skew, never clamps, never raises, logs each rejection at WARNING. Both
   observation sub-passes **and** `_mirror_execution_requests` (`startTimeMs`, then `requestedAt`)
   now go through it; an execution neither field can date is skipped rather than booked at the
   epoch. When reviewing an ingestion diff, check that any new remote-timestamp path uses it
   instead of a raw `datetime.fromtimestamp(ms/1000)`, which both aborts the sweep on a malformed
   value and future-poisons source-level freshness.
4. **`PASSIVE` reporting no `latest_run` is now the recorded intent**, not an oversight —
   `spec/feature/BACKEND.md` §Sync step 4 and `spec/feature/FRONTEND_INGESTION.md` both say so, and
   the list view renders a muted `—`. Don't re-file it.

**Why:** the req3 run (#154/#159/#160/#161/#162) introduced the observation layer and the
producer vocabulary; every one of the five issues was a consumer treating one grain as the other.

**How to apply:** on any diff touching `events` reads/writes under `src/backend/ingestion/`,
`src/backend/metrics/measurers/ingestion_freshness.py` or the per-dataset event timeline, walk the
table above and confirm the null-handling and the mode coverage explicitly rather than reading the
predicate as English.
