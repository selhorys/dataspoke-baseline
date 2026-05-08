# Validation Rule Reference

## Table of Contents
1. [Overview](#overview)
2. [Authoring Surface](#authoring-surface)
3. [Envelope](#envelope)
4. [DataSpoke Rule Extensions](#dataspoke-rule-extensions)
5. [Per-Type Reference](#per-type-reference)
   1. [`type: freshness`](#type-freshness)
   2. [`type: volume`](#type-volume)
   3. [`type: field`](#type-field)
   4. [`type: schema`](#type-schema)
   5. [`type: sql`](#type-sql)
   6. [`type: custom` (DataSpoke `sql_timeseries`)](#type-custom-dataspoke-sql_timeseries)
6. [Operators, Metrics, and Compatibility — Naming Convention](#operators-metrics-and-compatibility--naming-convention)
7. [Authoring Checklist](#authoring-checklist)
8. [References](#references)

## Overview

This document is the authoring reference for DataSpoke validation rule JSON — the body of
`PUT/PATCH /spoke/common/data/{dataset_urn}/attr/validation/conf`. The audience is anyone
writing a rule: backend implementers today, and (in a future iteration) the frontend
user-guide page that walks end users through composing a valid validation conf.

Validation rule JSON is a **DataSpoke envelope wrapping rules whose field names mostly
mirror DataHub's
[Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec)**.
The binding contract — six `type` values mapping 1:1 to DataHub `AssertionInfo.type`,
required typed sub-aspects per type, deterministic URN, `EXTERNAL` source — is documented
in [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects)
and is not repeated here. DataSpoke adds a thin envelope (config-level fields) and a small
set of per-rule extensions (`rule_id`, `source`, `partition`, `order`, `ml_validation`)
plus the DataSpoke-original `custom.subtype = "sql_timeseries"` for partition-aware,
ML-validated SQL checks.

A consistent naming rule applies to enumerated values: `condition.type`, `field.metric`,
and `schema.compatibility` accept the **lowercase snake_case form of the DataHub PDL
constant name** (e.g. DataHub `EQUAL_TO` → DataSpoke `equal_to`). This convention is
stated once here so per-type subsections do not re-enumerate values; for the full set of
allowed values, follow the DataHub Assertion Entity links in §5 and §6.

**Two authoring patterns.** Both produce assertions that DataHub registers and tracks
under the dataset; the difference is which DataHub sub-aspect carries the check
semantics:

- **Pattern A — DataHub-native validation.** Use one of the five native types
  (`freshness`, `volume`, `field`, `schema`, `sql`) with the DataHub field names
  documented on the [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion).
  DataHub's typed sub-aspect (`freshnessAssertion`, `volumeAssertion`, …) carries the
  check definition; DataSpoke runs the check and emits the `assertionRunEvent`. The
  DataHub UI surfaces the assertion as a typed assertion of the matching dimension.
  Use this when an off-the-shelf DataHub assertion type captures what you need.
  See §5.1–5.5.

- **Pattern B — DataSpoke-custom validation, registered to DataHub.** Use
  `type: custom` with a `subtype` that names the DataSpoke-side semantics (baseline
  ships `sql_timeseries` for partition-aware SQL with optional ML-based anomaly
  detection). The check definition is carried in DataHub's free-form `customAssertion`
  sub-aspect, with the SQL stored in `customAssertion.logic`; DataSpoke owns the
  execution semantics and ML logic. The assertion still appears under the dataset in
  DataHub's UI and accumulates `assertionRunEvent` history. Use this when no native
  type fits — typically for trend/anomaly checks or multi-row aggregates that go
  beyond a single condition. See §5.6.

Both patterns share the same envelope (§3), the same DataSpoke extensions (§4), and
the same emission conventions (deterministic URN, `EXTERNAL` source, shared `runId`
per run — see [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects)).

## Authoring Surface

| Question | Where to look |
|---|---|
| "What's the JSON request body shape, and is there an example?" | redoc → request schema of `PUT/PATCH /spoke/common/data/{dataset_urn}/attr/validation/conf` (auto-generated from `src/api/schemas/validation.py`) |
| "What does each DataHub field mean? What operator/metric/compatibility values exist?" | [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion); per-aspect PDL under `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/` |
| "Which DataSpoke fields exist on top of DataHub's? When is each required?" | This doc — §3, §4, §5 |
| "How are rules executed and results emitted?" | [BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation) + [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects) |

## Envelope

The four top-level fields of the validation conf are all DataSpoke-defined.

| Field | Type | Notes |
|---|---|---|
| `rules` | `list[object]` | List of rule entries. Hard cap **200** entries per config. |
| `schedule_tier` | `string` | One of `hourly`, `daily`, `weekly`. **Required** when `is_enabled = true`; selects the periodic Airflow DAG that picks up this conf. |
| `is_enabled` | `bool` | When `true`, the conf is scheduled and `method/run` rejects with `409 VALIDATION_DISABLED` for non-dry-run requests when set to `false`. |
| `owner` | `string` | Email or DataHub user URN responsible for the conf. |

Source: `src/api/schemas/validation.py:66-94`.

## DataSpoke Rule Extensions

Fields below may appear on rule entries; all are DataSpoke-defined and have no DataHub
counterpart. Per-type required fields (the DataHub-side ones — `condition`, `field`,
`statement`, etc.) are described in §5.

| Field | Applies to | Purpose |
|---|---|---|
| `rule_id` | all (**required**) | Stable identifier within a config. Combined with the dataset URN to derive the deterministic assertion URN: `urn:li:assertion:<datahub_guid({"entity": dataset_urn, "rule": rule_id})>`. Re-emits at the same URN on edit, so changing a rule's `type` does not fragment its DataHub timeline. |
| `partition` | `custom` (`subtype = "sql_timeseries"`) only | `list[str]` of column names by which the SQL result is partitioned (e.g. `["day"]`). Used to resolve the target partition row from the SQL result set. See §5.6. |
| `order` | `custom` (`subtype = "sql_timeseries"`) only | `list[str]` of column names defining the time order within a partition. Used to pick the latest row when no partition filter is supplied. See §5.6. |
| `ml_validation` | `custom` (`subtype = "sql_timeseries"`) only | ML-based anomaly detection settings — `targets`, `model` (e.g. `range`), `lookback_partitions`. |

DataHub-side common fields such as `description`, `condition`, `failure_threshold`,
`exclude_nulls`, and `filter` follow the DataHub Assertion model and are documented on
the [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion).

**Note on the runtime partition parameter.** The `POST .../method/validation/run` request
body accepts a separate top-level `partition: dict` field (e.g.
`{"updated_at": "2026-04-04"}`) that scopes a single ad-hoc run. This is **not** a rule
config field — it is passed at run time and applies uniformly to all rules in the
config. The `partition` rule-level field above is sql_timeseries-only metadata declaring
which result columns form the partition key; the run-time dict supplies the values used
to filter against those columns.

## Per-Type Reference

Each subsection follows the same shape: a brief DataHub-side concept summary, the DataHub
references to read for full field detail, and the DataSpoke-only fields that the author
needs to know in addition.

### `type: freshness`

**What it checks** *(DataHub side, brief)*: dataset recency — whether the data has been
updated within an expected interval. DataHub models this via the `freshnessAssertion`
sub-aspect on `assertionInfo`, with a schedule (lookback or cron) and an entity URN.

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `freshnessAssertion` sub-aspect
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/FreshnessAssertionInfo.pdl`
- SDK class: `FreshnessAssertionInfoClass`

**DataSpoke-only fields**:
- `source` — `datahub_operation` *(default; reads `OperationClass.lastUpdatedTimestamp` from the DataHub timeseries)* | `datahub_profile` *(reads `DatasetProfileClass.timestampMillis`)* | `query` *(runs `SELECT MAX(<last_modified_field>)` on the source platform)*
- When `source = query`: `last_modified_field` *(required, must match `\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z`)* and optional `filter` *(SQL WHERE fragment)*
- `lookback_interval` (e.g. `"24 hours"`) is consumed by DataSpoke to populate the
  DataHub `FixedIntervalSchedule` on emit

The full source-discriminator semantics, including the `datahub_*` no-data fallback
(`FAILURE` with `issues=[{type: "no_data"}]`), live in
[BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation).

### `type: volume`

**What it checks** *(DataHub side, brief)*: dataset row-count health — that a measured
row count satisfies an operator (e.g. between, equal_to). DataHub models this via the
`volumeAssertion` sub-aspect with a `RowCountTotal` (or `RowCountChange` /
`IncrementingSegment*` variants).

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `volumeAssertion` sub-aspect
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/VolumeAssertionInfo.pdl`
- SDK class: `VolumeAssertionInfoClass`

**DataSpoke-only fields**:
- `source` — `datahub_profile` *(default; reads `DatasetProfileClass.rowCount`)* | `query` *(runs `SELECT COUNT(*) [WHERE filter]` on the source platform)*
- When `source = query`: optional `filter` *(SQL WHERE fragment)*

DataSpoke baseline emits `ROW_COUNT_TOTAL` for `volume` rules; other DataHub volume
variants are not surfaced in the rule grammar today.

### `type: field`

**What it checks** *(DataHub side, brief)*: a property of a single column — either a
metric (`null_count`, `distinct_count`, `mean`, …) compared against a condition
(`FIELD_METRIC` mode), or per-row predicate matches with a failure threshold
(`FIELD_VALUES` mode). DataHub models both via the `fieldAssertion` sub-aspect.

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `fieldAssertion` sub-aspect
- PDL: `FieldAssertionInfo.pdl`, `FieldMetricAssertion.pdl`, `FieldValuesAssertion.pdl` (same directory)
- SDK class: `FieldAssertionInfoClass` (with `FieldMetricAssertionClass` or `FieldValuesAssertionClass` inside)

**DataSpoke-only fields**: none beyond [§4](#dataspoke-rule-extensions). The mode is
chosen implicitly: presence of `metric` selects `FIELD_METRIC`, otherwise `FIELD_VALUES`.
All other fields (`field`, `condition`, `failure_threshold`, `exclude_nulls`) are the
DataHub names; allowed `metric` values are documented on the DataHub Assertion Entity
page (DataSpoke accepts the lowercase snake_case form per §6).

### `type: schema`

**What it checks** *(DataHub side, brief)*: that the dataset's schema matches a declared
field set. DataHub models this via the `schemaAssertion` sub-aspect with a compatibility
mode (`EXACT_MATCH` / `SUPERSET` / `SUBSET`) and a list of expected fields.

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `schemaAssertion` sub-aspect
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/SchemaAssertionInfo.pdl`
- SDK class: `SchemaAssertionInfoClass`

**Naming note**: DataHub's `AssertionType` PDL constant is `DATA_SCHEMA` (a reserved-word
workaround). DataSpoke surfaces this as `type: "schema"` in the rule JSON; the mapping is
applied in `src/backend/validation/assertions.py` (`_RULE_TYPE_MAP`).

**DataSpoke-only fields**: none beyond [§4](#dataspoke-rule-extensions). DataHub-side
fields used:
- `compatibility` — `superset` *(default)*, `subset`, or `exact_match`. Maps to
  `SchemaAssertionCompatibility` (defined inline in `SchemaAssertionInfo.pdl`); use the
  snake_case form per §6 — the bare token `exact` is not a DataHub PDL value.
- `fields[]` — list of `{field, type}` entries. Authors specify `type` as a native type
  string; DataSpoke fills DataHub's `nativeDataType` and uses `String` as the structural
  `type`.

### `type: sql`

**What it checks** *(DataHub side, brief)*: a custom scalar SQL query against the source
platform whose result is compared with a condition. DataHub models this via the
`sqlAssertion` sub-aspect.

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `sqlAssertion` sub-aspect
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/SqlAssertionInfo.pdl`
- SDK class: `SqlAssertionInfoClass`

**DataSpoke-only fields**: none beyond [§4](#dataspoke-rule-extensions). DataSpoke emits
`type = METRIC`; `METRIC_CHANGE` is not surfaced in the rule grammar.

### `type: custom` (DataSpoke `sql_timeseries`)

**What it checks** *(DataHub side, brief)*: anything not covered by the other five types.
DataHub models this as a free-form `customAssertion` sub-aspect with a `type` (subtype
string) and optional `logic`. DataSpoke's primary use of this is the original
`sql_timeseries` subtype, which executes a partition-aware SQL query repeatedly across
historical partitions and (optionally) runs ML-based anomaly detection over the
collected values.

**DataHub references**:
- [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion) — `customAssertion` sub-aspect
- PDL: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/CustomAssertionInfo.pdl`
- SDK class: `CustomAssertionInfoClass`

**DataSpoke-only fields** (all DataSpoke-defined; no DataHub counterpart):
- `subtype` — names the DataSpoke-side semantics. Baseline supports `sql_timeseries`.
- `sql` — the SQL statement to run on the source platform (mapped onto DataHub
  `customAssertion.logic` on emit)
- `partition[]` — list of column names by which the SQL result is partitioned (e.g. `["day"]`)
- `order[]` — list of column names defining the time order within a partition
- `values[]` — list of measurement column names extracted from the SQL result
- `ml_validation` *(optional)* — `{targets: […], model: "range" | …, lookback_partitions: N}`. The execution path that runs `sql_timeseries` plus optional ML validation is documented in [BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation) ("SQL-Based Timeseries Engine").

## Operators, Metrics, and Compatibility — Naming Convention

DataSpoke maps these values onto DataHub's PDL enum constants without inventing its own
enum vocabulary. The string values accepted in `condition.type`, `field.metric`, and
`schema.compatibility` are the lowercase snake_case form of DataHub's PDL constant names.
To pick a value:

1. Open the matching DataHub PDL file (or the corresponding section on the DataHub
   Assertion Entity page).
2. Lowercase and snake_case the constant name (DataHub `EQUAL_TO` → DataSpoke
   `equal_to`; DataHub `NULL_PERCENTAGE` → DataSpoke `null_percentage`; DataHub
   `EXACT_MATCH` → DataSpoke `exact_match`).

| Surface | DataHub PDL | Reference |
|---|---|---|
| `condition.type` | `AssertionStdOperator` | `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/AssertionStdOperator.pdl` |
| `field.metric` | `FieldMetricType` | `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/FieldMetricType.pdl` |
| `schema.compatibility` | `SchemaAssertionCompatibility` (inline enum in `SchemaAssertionInfo.pdl`) | `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/SchemaAssertionInfo.pdl` |

DataSpoke threads these values through two layers, both of which need an entry when a
new DataHub value is adopted:

- **Bridge layer** (`src/backend/validation/assertions.py`) — translates DataSpoke
  strings to DataHub SDK constants for emission: `_CONDITION_OPERATOR_MAP`,
  `_FIELD_METRIC_MAP`, and the inline map in `_build_schema_sub_aspect`. This is the
  authoritative list of values DataSpoke claims to support on the wire.
- **Runtime layer** (`src/backend/validation/rules/`) — evaluates the rule against the
  data: `helpers.evaluate_condition` for operators, `field.py:_METRIC_ATTR_MAP` for
  field metrics, the compatibility branches in `rules/schema.py`. A value present in
  the bridge but missing from the runtime evaluator registers correctly to DataHub but
  cannot be evaluated, which violates the assertion-result contract (see
  [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects)).
  When adding a new value, update both layers in the same change.

## Authoring Checklist

- Pick the pattern first (§1): a native `type` (Pattern A) when an off-the-shelf
  DataHub assertion fits; `type: custom` with a `subtype` (Pattern B) when it doesn't.
- Every rule needs `rule_id` and `type`.
- `is_enabled = true` requires `schedule_tier` (validated at the API layer; missing
  `schedule_tier` returns 422).
- `freshness` and `volume` are the only types that accept `source`. `source = query`
  requires `last_modified_field` (freshness) and may include `filter` (both). The
  `last_modified_field` value must match `\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z`.
- For `custom.subtype = "sql_timeseries"`, all of `sql`, `partition[]`, `order[]`, and
  `values[]` should be specified. They are not enforced by the API schema today; missing
  fields silently degrade execution (`partition`/`order`/`values` default to empty lists,
  yielding empty result rows). `ml_validation` is optional.
- For DataHub field semantics and the allowed operator / metric / compatibility values,
  consult the [DataHub Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion)
  (linked per-type in §5).

## References

- DataHub: [Assertion Entity page](https://datahubproject.io/docs/generated/metamodel/entities/assertion)
- DataHub: [Open Assertions Spec](https://datahubproject.io/docs/assertions/open-assertions-spec) — binding YAML grammar (CLI is deprecated; aspect emission is via SDK)
- PDL directory: `ref/github/datahub/metadata-models/src/main/pegasus/com/linkedin/assertion/`
- Open Assertions YAML grammar (vendored): `ref/github/datahub/docs/assertions/open-assertions-spec.md`
- [BACKEND §Validation Service](BACKEND.md#validation-service-srcbackendvalidation) — runtime semantics, source discriminator, SQL timeseries engine
- [DATAHUB_INTEGRATION §Assertion Aspects](../DATAHUB_INTEGRATION.md#assertion-aspects) — assertion-emission conventions (URN, `EXTERNAL` source, shared `runId`)
- `src/api/schemas/validation.py` — Pydantic request/response schemas; redoc example payload
- `src/backend/validation/assertions.py` — DataHub assertion bridge; canonical operator/metric maps
