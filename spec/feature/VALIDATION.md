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
| Soft-delete + resurrect the rule on edit | Manage the source-platform connection or credentials |

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
| `DELETE` | `/spoke/common/data/{dataset_urn}/attr/validation/conf` | Soft-delete the rule (DataHub `status.removed = true`) |
| `POST` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | Append a validation result |
| `GET` | `/spoke/common/data/{dataset_urn}/attr/validation/result` | List historic results (`?from=…&until=…`) |
| `GET` | `/spoke/common/data/{dataset_urn}/event/validation` | Validation event timeline — config lifecycle (`CONFIG_CREATE`/`CONFIG_UPDATE`/`CONFIG_DELETE`) plus `RESULT_RECORDED`, one per accepted result POST |

The cross-dataset list view at `/spoke/validation` continues to operate under
the existing semantics in
[API §Validation](../API.md#validation-spokevalidation).

## Rule Configuration

The configuration is a small, fixed-shape document.

```json
{
  "description": "Daily row count plus key column means and null counts",
  "variables": [
    {"name": "row_cnt", "description": "Daily row count"},
    {"name": "col1_mean", "description": "Mean of col1"},
    {"name": "col2_null_cnt", "description": "Null count of col2"}
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `description` | `string` | yes | Free-form rule description. Surfaced in the DataHub assertion detail UI. ≤ 2,000 chars. No ASCII control characters except `\t` (0x09) and `\n` (0x0a). |
| `variables` | `list[object]` | yes | The variables this rule will report, each a `{name, description}` object. There MUST be ≥ 1 entry; hard cap **200** entries. |

Each `variables` element:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Variable name. MUST match `\A[a-z][a-z0-9_]{0,99}\Z` and MUST be unique across the list. |
| `description` | `string` | yes | Per-variable description (the meaning of the measurement). Required key, but the **empty string is allowed**. ≤ 200 chars. No ASCII control characters except `\t` (0x09) and `\n` (0x0a). |

Result POSTs (§5) and stored `validation_results` are keyed by variable **name** only;
descriptions live solely on the conf and are not echoed into results. The
`customAssertion.logic` emitted to DataHub (§6) is the comma-joined list of variable
**names** — descriptions are not emitted into the logic string.

Notes:

- The configuration carries **no** rule logic. It is purely a schema declaration that
  enables server-side validation of subsequent result POSTs (§5).
- `PATCH` accepts a partial body. Replacing `variables` is allowed but is a breaking
  edit: prior result rows whose keys are no longer in the declared variable **names**
  remain queryable but silently fall outside the current schema. Pipelines should treat
  a `variables` edit as a versioning event.
- `DELETE` performs a soft delete by emitting `status.removed = true` on the assertion
  URN. After `DELETE`, `GET conf` returns `404` and `PATCH conf` against the tombstoned
  slot also returns `404` — the resource view treats a soft-deleted rule as absent.
  The cross-dataset list at `/spoke/validation` continues to surface deleted
  rows under `?removed=true`. A subsequent `PUT` resurrects the assertion (clears
  `removed`) and overwrites `assertionInfo`. This follows the existing soft-delete
  resurrection pattern.

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
→ [
    { "data_time": "2026-05-07T00:00:00Z", "score": 1.0,
      "variables": {"row_cnt": 51.0, "col1_mean": 31.1, "col2_null_cnt": 9.0} },
    { "data_time": "2026-05-06T00:00:00Z", ... },
    ...
    { "data_time": "2026-05-01T00:00:00Z", ... }
  ]
```

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
`assertionInfo` (versioned, on PUT/PATCH), `assertionRunEvent` (timeseries, per result
POST), and `status` (versioned, on DELETE / resurrection). No metadata-model extensions.

### Assertion URN

```
urn:li:assertion:<datahub_guid({
    "platform": "dataspoke-validation",
    "entity":   <dataset_urn>
})>
```

Deterministic — recomputable from `dataset_urn` alone. PUT and PATCH are idempotent;
the soft-delete / resurrection cycle reuses the same URN.

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

### `status` (versioned aspect)

Emitted alongside `assertionInfo` on every PUT/PATCH: `status.removed = false`.
This both clears any prior soft-delete (PUT-after-DELETE resurrection) and
reverts out-of-band tombstones — DataSpoke is authoritative for the assertion
lifecycle, so a DataHub-UI admin manually setting `status.removed = true` is
overwritten on the next config save. To durably hide a DataSpoke assertion,
use `DELETE /attr/validation/conf`. Emitted on DELETE: `status.removed = true`.

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
