# Validation

## Table of Contents

1. [Philosophy](#philosophy)
2. [Scope](#scope)
3. [API Surface](#api-surface)
4. [Rule Configuration](#rule-configuration)
5. [Validation Result](#validation-result)
6. [DataHub Aspect Mapping](#datahub-aspect-mapping)
7. [Single-rule Scope](#single-rule-scope)
8. [Open Items](#open-items)
9. [References](#references)

## Philosophy

Validation logic is fundamentally hard to embed in a data catalog. The rule space is
unbounded (row counts, distributional drift, schema drift, anomaly detection based on
ML), and the execution-environment requirements vary by platform, data scale, and
credential boundary. Giving a catalog production-engine credentials to evaluate
hundred-terabyte rules is not a path that scales — and exposing an ever-richer DSL to
express rules is reaching for an expressiveness target that does not exist.

The pragmatic locus of validation is the **data pipeline itself** — the validation task
sits immediately after the partition-writing task it audits, runs in the pipeline's
own environment with the right credentials and resources, and emits its result
afterwards. This is the data-engineering analogue of shipping unit tests with backend
code; the only twist is that the quality task ships to *production* alongside the
pipeline, not to a dev environment.

DataSpoke's role is therefore narrow and passive. It solves two pain points that the
in-pipeline pattern does not solve on its own:

- **Centralized result storage.** Without a shared, schema-disciplined result store,
  every team rolls their own logging table; results scatter and cannot be aggregated.
  DataSpoke offers one HTTP endpoint that all quality tasks across the company write to.
- **Historical aggregate cache.** Validation that compares today's partition to a
  baseline (rolling row-count median, last week's null-rate, etc.) re-aggregates the
  same history every run on big tables. By making prior validation results queryable
  via a time range, DataSpoke turns the result store into a free baseline cache —
  today's task fetches yesterday's `row_cnt` and uses it directly.

DataSpoke runs **no validation logic**. It accepts a configuration and results from
external pipelines and stores both as native DataHub assertion aspects so they appear
in the DataHub Quality tab and benefit from DataHub's existing UI, search, and access
control.

## Scope

| In scope | Out of scope |
|---|---|
| Configure one DataSpoke validation rule per dataset | Execute any rule logic |
| Accept timeseries result POSTs from external pipelines | Schedule validation runs |
| Serve historical results for use as baselines | Maintain a rule grammar (freshness/volume/field/schema/sql/...) |
| Emit `assertionInfo` and `assertionRunEvent` to DataHub | Run anomaly detection or ML over results |
| Hard-delete the rule (cascading its results and events) | Manage the source-platform connection or credentials |

The single rule is a free-form bag of named scalar variables plus a single pass/fail
score. Interpretation of the variables is the pipeline's responsibility; DataSpoke just
stores them. Teams that need multiple distinct checks per dataset use DataHub's native
assertion APIs directly — see [§7 Single-rule Scope](#single-rule-scope).

## API Surface

All routes are under `/api/v1`. Authorization follows the standard JWT model in
[API §Authentication](../API.md#authentication--authorization). All write routes require
the dataset to already exist in DataHub (`422 DATASET_NOT_IN_DATAHUB` otherwise).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Get the validation configuration |
| `PUT` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Create or replace the validation configuration |
| `PATCH` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Partially update the validation configuration |
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Hard-delete the rule — removes the conf, cascades its results and validation events, and hard-deletes the DataHub assertion entity (`204`) |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Append a validation result |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | List historic results (`?from=…&until=…`) |
| `GET` | `/spoke/common/data/{dataset_urn}/event/validation` | Validation event timeline — config lifecycle (`CONFIG_CREATE`/`CONFIG_UPDATE`) plus `RESULT_RECORDED`, one per accepted result POST |

The cross-dataset list view at `/spoke/validation` follows the semantics in
[API §Validation](../API.md#validation-spokevalidation), including the `coverage`
filter (default `covered`) that selects covered / uncovered / both row sets.

## Rule Configuration

The configuration is a small, fixed-shape document with four sections: `description` and
`variables` declare what the pipeline reports, `attribute` states when the dataset's data
is expected to arrive, and the optional `parameter` section is opaque storage for the
pipeline's own hyperparameters.

```json
{
  "description": "Daily row count plus key column means and null counts",
  "variables": [
    {"name": "row_cnt", "description": "Daily row count"},
    {"name": "col1_mean", "description": "Mean of col1"},
    {"name": "col2_null_cnt", "description": "Null count of col2"}
  ],
  "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
  "parameter": [
    {"name": "z_threshold", "description": "Std-dev cutoff for outliers"}
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `description` | `string` | yes | Free-form rule description. Surfaced in the DataHub assertion detail UI. Required key, but the empty string is allowed. ≤ 2,000 chars. No ASCII control characters except `\t` (0x09) and `\n` (0x0a). |
| `variables` | `list[object]` | yes | The variables this rule will report, each a `{name, description}` object. There MUST be ≥ 1 entry; hard cap **200** entries. |
| `attribute` | `object` | no | Data-arrival cadence, `{cadence_unit, cadence_offset}` — see below. Omitting it on `PUT` stores the all-defaults object; it is never absent from a stored conf or from a response. |
| `parameter` | `list[object]` \| absent | no | Pipeline hyperparameters, each a `{name, description}` object with the same shape and per-item rules as `variables`. Absent by default. When the key is present it MUST carry 1–200 entries — an explicit `[]` is rejected exactly as an empty `variables` is. |

Each `variables` element — and each `parameter` element, under the same rules in its own
separate namespace (a name may appear in both lists; uniqueness is per list):

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Variable name. MUST match `\A[a-z][a-z0-9_]{0,99}\Z` and MUST be unique across the list. |
| `description` | `string` | yes | Per-variable description (the meaning of the measurement). Required key, but the **empty string is allowed**. ≤ 200 chars. No ASCII control characters except `\t` (0x09) and `\n` (0x0a). |

Each `attribute` field:

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `cadence_unit` | `int` | no | `86400` | The period, in seconds, at which the dataset's data is expected to arrive. MUST be `> 0` and `<= 315,360,000` (ten years — the same ceiling as `metric_conf.time_window_sec`, [API §Metric](../API.md#metric-spokegovernancemetric)). |
| `cadence_offset` | `int` | no | `0` | How many `cadence_unit` periods the arriving data lags the arrival instant. MUST be `>= 0`, and `cadence_offset * cadence_unit` MUST be `<= 315,360,000` — the same product bound the `validation-score` window arithmetic applies to `time_window_sec`, so an accepted `attribute` can never make a governed window's arithmetic overflow. Daily D-1 data is `unit = 86400, offset = 0`; daily D-8 data is `unit = 86400, offset = 7`. |

`attribute` is a **closed, typed object** — unknown keys are not stored. Its shape may grow
named fields in future releases; it is not an open bag. Supplying `attribute` on `PUT` or
`PATCH` writes the **complete** per-field-defaulted object, replacing the previous value
outright rather than deep-merging into it — the same wholesale-replacement rule `variables`
follows. A `PATCH` carrying `{"attribute": {"cadence_offset": 7}}` therefore also resets
`cadence_unit` to `86400`.

`parameter` is the only **optional-by-absence** section, so its lifecycle is stated in full:

- **`PUT`** is a full replace, like every other field on it: omitting `parameter` stores it as
  absent, clearing any previously stored value.
- **`PATCH`** is a partial update. Omitting `parameter` leaves the stored value unchanged.
  `"parameter": null` clears it to absent — that is the one spelling for "clear". A non-empty
  list (1–200 entries, validated exactly as `variables` is) replaces the stored value
  wholesale. `"parameter": []` is **rejected** (`422`), the same as an empty `variables`, so
  there is no second spelling of "clear".
- **`GET`** omits the `parameter` key entirely from the response body when the section is
  absent; it is never serialized as `null`.

`attribute` is the one section DataSpoke reads for itself: the governance `validation-score`
metric anchors its per-dataset criterion window on this cadence (see
[BACKEND §Metrics Service](BACKEND.md#metrics-service-srcbackendmetrics)). `parameter` is
the opposite — DataSpoke never interprets it and no feature reads it. It exists so a
pipeline's tunables travel with the rule that uses them instead of in a side channel; the
algorithm that consumes them is entirely outside DataSpoke, exactly as the rule logic behind
`variables` already is.

Result POSTs (§5) and stored `validation_results` are keyed by variable **name** only;
descriptions live solely on the conf and are not echoed into results. The
`customAssertion.logic` emitted to DataHub (§6) is the comma-joined list of variable
**names** — descriptions are not emitted into the logic string. Neither `attribute` nor
`parameter` reaches DataHub: the assertion aspects carry the reported schema, and cadence
and hyperparameters are DataSpoke-side facts with no DataHub-native slot.

Notes:

- The configuration carries **no** rule logic. `description`, `variables`, and `parameter`
  are declarations — `variables` is the schema that enables server-side validation of
  subsequent result POSTs (§5) — and `attribute` states an expectation about the data, not
  a computation over it.
- `PATCH` accepts a partial body. Replacing `variables` is allowed but is a breaking
  edit: prior result rows whose keys are no longer in the declared variable **names**
  remain queryable but silently fall outside the current schema. Pipelines should treat
  a `variables` edit as a versioning event.
- `DELETE` performs a **hard delete with cascade**, in a single transaction: the
  `validation_configs` row is removed, the dataset's `validation_results` history is
  deleted, the dataset's validation events (`VALIDATION.*`) are deleted, and the DataHub
  assertion entity is hard-deleted (no `status.removed` tombstone is left behind). It
  records **no** event — the cascade wipes the dataset's validation events, so its
  validation events panel becomes empty. `DELETE` returns `204`. Afterwards the dataset
  reads as **never-created**: `GET conf` and `PATCH conf` return `404 CONFIG_NOT_FOUND`,
  the dataset is absent from `/spoke/validation`, and a fresh `PUT` simply creates a new
  conf (`201`) — there is no resurrection concept and no `409`.

## Validation Result

The pipeline POSTs results as it produces partitions. The body is small.

```json
{
  "data_time": "2026-05-08T00:00:00Z",
  "score": 1.0,
  "variables": {
    "row_cnt": 50.0,
    "col1_mean": 31.1,
    "col2_null_cnt": 15.0
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `data_time` | RFC 3339 timestamp (UTC) | yes | The time the underlying data is for — typically the partition timestamp, **not** the time the validation ran. Becomes the timeseries axis (§6). |
| `score` | `double` | yes | `0.0 ≤ score ≤ 1.0`. `1.0` = pass; `0.0` = fail; intermediate values reserved for partial-success semantics — currently treated as fail at the DataHub enum boundary, but the raw value is preserved. |
| `variables` | `map[string, double]` | yes | Measured values keyed by variable name. Subset of the declared `variables` is allowed (missing keys are recorded as absent); keys not declared in conf are **rejected** (`422 UNKNOWN_VARIABLE`). |

### Validation rules on POST

- `variables` keys MUST be a subset of the conf's `variables`. Unknown keys → `422
  UNKNOWN_VARIABLE` listing the offending names. Missing declared keys are accepted
  silently (a result with partial coverage is a legitimate signal, e.g. when one
  measurement failed to compute).
- `score` MUST satisfy `0.0 ≤ score ≤ 1.0`; otherwise `422 INVALID_SCORE`.
- `data_time` MUST parse as RFC 3339; otherwise `422 INVALID_PARAMETER`.
- The dataset MUST have a validation conf; posting to a dataset with no conf returns
  `404` — the same absent-resource view as `GET conf`.

### Duplicate `data_time` policy

Multiple POSTs with the same `data_time` are **append-only**: each becomes a distinct
`assertionRunEvent` row in DataHub's timeseries store. On read, the GET endpoint
returns the most recent one (last-write-wins) for each distinct `data_time`. Append is
chosen because DataHub's timeseries aspect is fundamentally append-only and forcing
replace requires `messageId` workarounds; last-write-wins on read keeps the surface
clean for the common case (idempotent retry from the pipeline).

### GET result

```
GET .../attr/validation/result?from=2026-05-01T00:00:00Z&until=2026-05-08T00:00:00Z
→ {
    "offset": 0, "limit": 1000, "total_count": 7,
    "results": [
      { "data_time": "2026-05-07T00:00:00Z", "score": 1.0,
        "variables": {"row_cnt": 51.0, "col1_mean": 31.1, "col2_null_cnt": 9.0} },
      { "data_time": "2026-05-06T00:00:00Z", ... },
      ...
      { "data_time": "2026-05-01T00:00:00Z", ... }
    ]
  }
```

The body is the standard pagination envelope (`offset`/`limit`/`total_count` per
[API_DESIGN_PRINCIPLE §5](../API_DESIGN_PRINCIPLE_en.md#5-url-query-segments-are-for-filtering-sorting-and-pagination)),
with the collapsed rows under `results`.

| Query param | Default | Notes |
|---|---|---|
| `from` | none | Inclusive lower bound (RFC 3339). Filters on `data_time`. |
| `until` | none | Exclusive upper bound (RFC 3339). Filters on `data_time`. |
| `limit` | 1,000 | Max rows returned. Server cap **10,000**. |

Rows are ordered by `data_time` **descending** (newest first) so the most recent
partition appears at the head of the response — the common case for baseline
queries that always want the latest sample at index 0.

### Cache use case

This GET is the historical-aggregate cache. A pipeline computing today's anomaly check
against a 30-day rolling baseline issues one `GET ?from=<30d ago>&until=<today>` to
recover the prior `row_cnt` series instead of re-aggregating the source table.
DataSpoke does not need to model this pattern explicitly — it falls out of the
result-store contract.

## DataHub Aspect Mapping

DataSpoke writes three native DataHub aspects on the assertion entity:
`assertionInfo` (versioned, on PUT/PATCH), `status` (versioned, alongside
`assertionInfo`) and `assertionRunEvent` (timeseries, per result POST). `DELETE`
hard-deletes the whole assertion entity. No metadata-model extensions.

### Assertion URN

```
urn:li:assertion:<datahub_guid({
    "platform": "dataspoke-validation",
    "entity":   <dataset_urn>
})>
```

Deterministic — recomputable from `dataset_urn` alone. PUT and PATCH are idempotent;
`DELETE` hard-deletes this assertion entity. A subsequent `PUT` re-creates the entity
under the same URN.

### `assertionInfo` (versioned aspect)

Emitted on PUT and PATCH.

```
assertionInfo:
  type: CUSTOM
  description: <conf.description>
  source:
    type: EXTERNAL
  customAssertion:
    type: "DATASPOKE_VALIDATION"        # Quality-tab categorization label
    entity: <dataset_urn>
    logic: "row_cnt, col1_mean, col2_null_cnt"   # comma-separated variable names
  lastUpdated:                          # audit stamp (see DATAHUB_INTEGRATION §Assertion Aspects)
    time: <server now() epoch ms>
    actor: "urn:li:corpuser:dataspoke"
```

- `customAssertion.logic` is free-form text per
  [`CustomAssertionInfo.pdl`](../../ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/CustomAssertionInfo.pdl);
  DataSpoke fills it with the comma-separated list of declared variable names from the
  conf, joined with `", "`. The DataHub UI surfaces `logic` verbatim in the assertion
  detail view, so users read the declared variable schema as a plain list without
  DataSpoke needing custom UI. The variable-name regex (`[a-z][a-z0-9_]{0,99}`) excludes
  commas, so parsing on read is unambiguous: split on `,` and strip whitespace around
  each token.
- A `structuredProperties` aspect is **not** used in v1 — sticking with the plain
  `logic` string avoids extending the entity registry. Migrating to a structured aspect
  later is possible if discovery use cases (search by variable name across datasets, or
  per-variable type / description metadata) become load-bearing.

### `status` (versioned aspect)

Emitted as `status.removed = false` alongside `assertionInfo` on every register.

DataHub does not require the aspect for visibility — search excludes soft-deleted
entities with `mustNot(removed = true)`, which does not match an entity carrying no
`status` at all, and `Status.pdl` defaults `removed` to `false`. The emission is
instead an idempotent un-remove: if an operator soft-deletes the assertion from the
DataHub UI, the next `PUT` restores it to the Quality tab rather than writing
`assertionInfo` onto an entity that stays hidden. This mirrors how DataSpoke's
ingestion extractors and corp-group sync emit the same aspect.

### `assertionRunEvent` (timeseries aspect)

Emitted on every result POST.

```
assertionRunEvent:
  timestampMillis: <data_time epoch ms>          # ← data_time on the time axis, not ingest time
  runId: <uuid4>
  asserteeUrn:  <dataset_urn>
  assertionUrn: <deterministic URN above>
  status: COMPLETE
  result:
    type: SUCCESS if score == 1.0 else FAILURE
    actualAggValue: <score>                       # raw float — DataHub auto-charts
    nativeResults:
      row_cnt:       "50.0"
      col1_mean:     "31.1"
      col2_null_cnt: "15.0"
      score:         "1.0"                        # raw score also kept here for fidelity
  runtimeContext:
    ingestion_time: <server-side now() epoch ms>  # for audit
```

Key choices, with rationale:

| Field | Choice | Why |
|---|---|---|
| `timestampMillis` | `data_time` (not ingest time) | Aligns DataHub's UI chart axis and `from`/`until` filters with the user mental model — "when the data is for", not "when we wrote it". |
| `result.type` | `SUCCESS` if `score == 1.0` else `FAILURE` | Maps the 0..1 `score` onto the `AssertionResultType` enum (`INIT / SUCCESS / FAILURE / ERROR`), using only `SUCCESS` and `FAILURE`. The raw `score` is preserved in `actualAggValue` and `nativeResults["score"]` so partial-success semantics are not lost when introduced. |
| `result.actualAggValue` | `score` | Single float per run; DataHub UI plots it as a timeseries chart for free. |
| `result.nativeResults` | `Map<string,string>` of variables | The DataHub-native slot for "other results of evaluation". Variables serialized via `repr(float)` (round-trip safe under IEEE 754) and parsed back on read. |
| `partitionSpec` | omitted (DataHub default) | DataHub's `PartitionType` enum is `@deprecated` ("Unused!" — see `PartitionSpec.pdl`) and the assertion / Quality tab UI does not consume `partitionSpec`. Partition identity already lives in `timestampMillis` (= `data_time`); setting `partitionSpec` would be decoration with no consumer. |
| `runtimeContext.ingestion_time` | server now() | Preserves the audit trail of when the result was actually accepted by DataSpoke. |

### Deletion (hard-delete)

`DELETE /attr/validation/conf` hard-deletes the assertion entity from DataHub — it does
**not** leave a `status.removed = true` tombstone. The assertion URN is derived
deterministically from the dataset URN, so a subsequent `PUT` re-creates a fresh
assertion under the same URN. DataSpoke is authoritative for the assertion lifecycle,
and the entity simply ceases to exist on delete.

### What does NOT need to be emitted

- No `dataPlatformInstance` — the dataset URN already carries platform context.
- No `assertionActions` — DataSpoke has no notification hooks of its own.

## Single-rule Scope

DataSpoke deliberately exposes **one** validation slot per dataset. The shape is the
common-case envelope: a description plus a flat bag of named scalar measurements with
a single pass/fail score. Most pipelines have one quality-check task per partition
write, and one DataSpoke rule absorbs its full output.

Teams that need multiple distinct checks per dataset (separate freshness, volume, and
schema assertions; per-column validators; multi-team ownership of independent rules)
should use **DataHub's native assertion APIs** — the GraphQL `upsertCustomAssertion` /
REST assertion endpoints — directly. DataHub already implements multi-assertion CRUD,
the Quality tab already lists multiple assertions per dataset, and reproducing that
surface inside DataSpoke would be undifferentiated.

DataSpoke's value-add is the centralized result-store contract and the historical
cache pattern. Both are useful for any custom assertion regardless of how it was
created — pipelines that emit DataHub-native assertions can still POST their result
timeseries through `assertionRunEvent` directly to DataHub. DataSpoke is the
opinionated single-rule shortcut for the 80% case, not the only path.

## Open Items

These are deferred and should be handled when a real use case appears, rather than
spec'd preemptively:

- **Result retention.** `assertionRunEvent` is timeseries and grows without bound.
  DataHub-side TTL or sampling policy is not addressed here; revisit when storage
  pressure becomes real.
- **Cadence in the cross-dataset list view.** `GET /spoke/validation` projects each row from
  the conf and the latest result, and does not surface `attribute.cadence_unit` /
  `cadence_offset`. Whether it should is deferred — revisit when a use case needs the arrival
  cadence visible across datasets without opening each dataset's conf.
- **Notification hooks.** Config lifecycle and result records already land on the
  `event/validation` timeline (see §API Surface), but there is no push/subscription
  delivery — pipelines that want to be notified on "rule failed" must poll. Active
  delivery is out of scope for v1.

## References

- [DataHub: Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec)
- [DataHub: Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion)
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/CustomAssertionInfo.pdl`
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/AssertionRunEvent.pdl`
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/AssertionResult.pdl`
- [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects)
- [API_DESIGN_PRINCIPLE](../API_DESIGN_PRINCIPLE_en.md)
