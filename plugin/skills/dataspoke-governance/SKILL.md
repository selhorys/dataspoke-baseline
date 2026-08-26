---
name: dataspoke-governance
description: Guide DataSpoke Governance metric configuration and operation (UC5) on a deployed instance. Use for governance metric questions; authoring or editing human-facing YAML; creating, updating, deleting, enabling, or running metrics; and interpreting results, dataset scope/verdicts, freshness, unresolved URNs, or events. Supports ingestion-freshness, validation-score, and doc-health through the public REST API.
argument-hint: "[question or metric action]"
allowed-tools: Read, Bash(dataspoke-api *), Bash(dataspoke-schema *), AskUserQuestion
---

## Purpose and boundary

Guide the complete lifecycle of active Governance metrics through DataSpoke's public API. Every
call uses `dataspoke-api`. Never use admin/internal routes, the operational database, cluster
access, or direct DataHub access, and never bypass a missing public capability. If API access is
missing, send the user to `/dataspoke:dataspoke-access`.

YAML is only a user-guide and human-authoring representation. The REST wire format is always
JSON. The deployment's live OpenAPI document, read through `dataspoke-schema`, is authoritative
for routes, request/response schemas, enums, and availability; these examples are starting points,
not a substitute for it.

## Guided lifecycle

Follow UC5 as an end-user workflow: scope, schedule, and configure one named active metric; create
it; evaluate it immediately; then return to its trend and affected datasets. Passive metrics are
reserved in this release: do not propose them (`mode: "passive"` is not implemented).

1. **Check access and inspect.** Run `dataspoke-schema governance/metric --list`, call
   `GET /auth/me`, report the effective role, then list `GET /spoke/governance/metric` and, for a named metric, read
   `GET /spoke/governance/metric/{metric_id}/attr/conf`. Writes require Editor or Admin. Do not
   infer that an id is new from an incomplete paginated page; inspect it directly.
2. **Load the live contract.** Query the narrow relevant operation without `--list` immediately
   before every definition `POST`, `PUT`, or `PATCH`, every definition `DELETE`, and every dry or
   non-dry `method/run`. Validate each definition body against that live schema and its cross-field
   constraints.
3. **Scaffold YAML.** Start with the matching editable example below and adapt the title,
   description, schedule, series styling, and dataset scope with the user.
4. **Convert and preview.** Parse the constrained YAML subset below and serialize it to JSON. Show
   the exact HTTP method, public route, and derived JSON body. Never send YAML or imply the API
   accepts it.
5. **Choose identity semantics explicitly.** A missing id is created only with
   `POST /spoke/governance/metric`. An existing definition is replaced with `PUT` or partially
   changed with `PATCH` at `/spoke/governance/metric/{metric_id}/attr/conf`. Updates are not
   upserts. `metric_id` is create-only: remove it from every PUT/PATCH JSON body because the path
   identifies the metric.
6. **Confirm and apply.** Immediately before any definition write, deletion, schedule enablement,
   or non-dry run, obtain explicit confirmation. State the metric id, operation, filter scope,
   schedule effect, and exact JSON payload when there is one. Then call `dataspoke-api` once with
   the confirmed request; if state changed before it lands, stop and report the response.
7. **Exercise safely.** The safe default sequence is **create disabled → dry run → confirm PATCH
   enable**. After creation or a material definition change, use exactly `dataspoke-api POST
   '/spoke/governance/metric/{metric_id}/method/run?dry_run=true'` (no body) before enabling its
   schedule. A dry run evaluates but persists neither a result, a verdict replacement, nor an event;
   it is not merely request validation. For UC5's enabled Imazon doc-health example, first confirm
   the enabled create body and then the immediate non-dry run; call its bodyless endpoint as
   `dataspoke-api POST '/spoke/governance/metric/{metric_id}/method/run'`. Never issue a non-dry
   run without confirmation.
8. **Trend and act.** Later query the chosen time range at `/attr/result`, inspect its persisted
   failing-dataset `breakdown`, and use `/dataset` and `/event` to understand the current verdicts
   and recorded lifecycle changes.

## Editable YAML guides

The three safe scaffold examples below default to `is_enabled: false`, so creating one does not
immediately enable its schedule. This excludes the separate enabled UC5 Imazon scenario below.
`schedule_tier: null` means on-demand only. Descriptor `idx` values are unique positive display
positions; colors are `#RRGGBB` strings.

### Ingestion freshness

Valid series names are `total` and `ingested_in_time`; values are dataset counts.

```yaml
metric_id: prod-ingestion-freshness
mode: active
is_enabled: false
metric_type: ingestion-freshness
title: Production ingestion freshness
description: Counts primary production datasets with ingestion evidence inside a two-day window.
metrics:
  - name: total
    color: "#64748B"
    idx: 1
  - name: ingested_in_time
    color: "#22C55E"
    idx: 2
metric_conf:
  time_window_sec: 172800
schedule_tier: daily
dataset_filter: "origin = 'PROD' AND is_primary = true"
```

### Validation score

Valid series names are `total`, `valid_confd`, and `valid_in_time`; all are dataset counts.
The window is anchored per dataset on its validation configuration cadence, as defined by the live
contract, rather than being a client-calculated score window.

```yaml
metric_id: prod-validation-score
mode: active
is_enabled: false
metric_type: validation-score
title: Production validation coverage
description: Counts primary production datasets configured and validated within their two-day window.
metrics:
  - name: total
    color: "#64748B"
    idx: 1
  - name: valid_confd
    color: "#3B82F6"
    idx: 2
  - name: valid_in_time
    color: "#22C55E"
    idx: 3
metric_conf:
  time_window_sec: 172800
schedule_tier: daily
dataset_filter: "origin = 'PROD' AND is_primary = true"
```

### Documentation health

Valid series names are `total` and `doc_health`; values are dataset counts. `metric_conf` must be
an empty object.

```yaml
metric_id: governed-doc-health
mode: active
is_enabled: false
metric_type: doc-health
title: Governed asset documentation health
description: Counts primary datasets in the governed catalog scope that meet documentation health.
metrics:
  - name: total
    color: "#64748B"
    idx: 1
  - name: doc_health
    color: "#A855F7"
    idx: 2
metric_conf: {}
schedule_tier: weekly
dataset_filter: "'urn:li:tag:area:catalog' IN tag_urns AND is_primary = true"
```

### UC5 Imazon sequence: enabled DEV doc health

The UC5 Imazon story is deliberately different from the safe guides: it creates an **enabled**
daily DEV doc-health metric, then immediately makes a non-dry run. Preserve these wire fields if
the user asks for that scenario, but display the derived JSON and obtain confirmation first for
the enabled create and again for the run.

```yaml
metric_id: doc-health-dev
mode: active
is_enabled: true
metric_type: doc-health
title: Doc Health (DEV)
description: Daily documentation-completeness check across DEV datasets
metrics:
  - name: total
    color: "#2563EB"
    idx: 1
  - name: doc_health
    color: "#16A34A"
    idx: 2
metric_conf: {}
schedule_tier: daily
dataset_filter: "origin = 'DEV'"
```

For the two windowed types, `time_window_sec: 172800` means two days; do not silently substitute
a different unit. Filters are SQL-WHERE-like strings over the dataset registry, not arbitrary SQL
or DataHub search. Use only syntax accepted by live OpenAPI. In particular, booleans are bare
(`is_primary = true`), while string values use single quotes. An empty string covers every
registered dataset.

### What UC5 values mean

All emitted values are floats. The server does not store ratios: derive a ratio client-side from
the named values and handle a zero denominator.

- **Ingestion freshness:** `total` is every dataset in the resolved filter scope;
  `ingested_in_time` is those whose latest ingestion evidence is inside `time_window_sec` before
  the measurement. Evidence is the owning source's per-dataset observation when DataHub reports
  one, otherwise its newest non-dry-run `INGESTION.COMPLETE`.
- **Validation score:** `total` is the resolved scope; `valid_confd` is datasets with a validation
  configuration; `valid_in_time` is configured datasets whose latest result overall (not merely a
  result selected from the window) has score `1.0` and is on time. Timeliness is anchored to each
  dataset's validation-configuration arrival cadence and declared lag, with
  `time_window_sec` as its width. Datasets without a configuration are not failures: they are
  unevaluated and read `met: "unknown"` from `/dataset`.
- **Doc health:** `total` is the resolved scope; `doc_health` counts datasets with both a non-empty
  table description and a non-empty description on every column. Other datasets fail and appear in
  the result breakdown.

### Constrained YAML subset

Treat authoring YAML as a JSON-compatible notation, not as general YAML. Accept only mappings with
string keys, arrays, strings, finite JSON numbers, `true`, `false`, and `null`. Reject the entire
document before preview or send if it contains duplicate mapping keys, merge keys (`<<`), tags,
anchors or aliases, timestamps, binary values, non-finite numbers (`.nan`/`.inf`), or any other
YAML-only or ambiguous scalar semantics. Quote a scalar when its type could be inferred differently.

After parsing, compare the parsed YAML value tree with the value tree obtained by parsing the exact
serialized JSON preview. Keys, array order, scalar types, and values must be equal. If they differ,
stop and show the mismatch; never send. JSON object key order and whitespace are not semantic.

## Preview and REST calls

Show the losslessly derived JSON before a write. For example, the first YAML guide derives this
create request (formatting changes are harmless; key/value meaning must not change):

```text
POST /spoke/governance/metric
```

```json
{
  "metric_id": "prod-ingestion-freshness",
  "mode": "active",
  "is_enabled": false,
  "metric_type": "ingestion-freshness",
  "title": "Production ingestion freshness",
  "description": "Counts primary production datasets with ingestion evidence inside a two-day window.",
  "metrics": [
    {"name": "total", "color": "#64748B", "idx": 1},
    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2}
  ],
  "metric_conf": {"time_window_sec": 172800},
  "schedule_tier": "daily",
  "dataset_filter": "origin = 'PROD' AND is_primary = true"
}
```

After confirmation, serialize the verified value tree as compact JSON and transport it safely to
the wrapper. Wrap the complete serialized JSON in POSIX shell single quotes. For every apostrophe
inside the JSON string, close the surrounding single quote, add the apostrophe as a double-quoted
single quote, and reopen the surrounding single quote. The literal five-character replacement is
`'"'"'`. Never interpolate individual fields into an unquoted or double-quoted shell argument.

For example, this is a complete safely quoted call whose JSON contains
`origin = 'PROD' AND is_primary = true`:

```bash
dataspoke-api POST /spoke/governance/metric '{"metric_id":"prod-ingestion-freshness","mode":"active","is_enabled":false,"metric_type":"ingestion-freshness","title":"Production ingestion freshness","description":"Counts primary production datasets with ingestion evidence inside a two-day window.","metrics":[{"name":"total","color":"#64748B","idx":1},{"name":"ingested_in_time","color":"#22C55E","idx":2}],"metric_conf":{"time_window_sec":172800},"schedule_tier":"daily","dataset_filter":"origin = '"'"'PROD'"'"' AND is_primary = true"}'
```

For PUT/PATCH, construct the verified operation-specific JSON first, omitting `metric_id`, and apply
the same whole-argument quoting algorithm. Do not use a shell placeholder that could be mistaken
for raw substitution.

Use PUT only for intentional full replacement and PATCH only for an intentional partial change.
Before enabling a schedule with `{"is_enabled":true}`, explain that scheduled execution begins
according to `schedule_tier`, recommend a successful dry run, and confirm immediately before the
PATCH. For deletion, explain that
`DELETE /spoke/governance/metric/{metric_id}/attr/conf` removes the metric definition, then confirm.

## Reads and interpretation

| Question | Public call |
|---|---|
| Metric list and last completed run | `GET /spoke/governance/metric` |
| Summary / latest attributes | `GET /spoke/governance/metric/{metric_id}` and `GET .../{metric_id}/attr` |
| Full definition | `GET .../{metric_id}/attr/conf` |
| Result history | `GET .../{metric_id}/attr/result?from=...&to=...` |
| Scoped datasets and verdicts | `GET .../{metric_id}/dataset` (repeat `met=true|false|unknown` as needed) |
| Run and definition events | `GET .../{metric_id}/event?from=...&to=...` |

Respect pagination and `total_count`; do not describe the first page as the complete estate.

- `/attr/result` rows are the persisted history: each has `values` and a per-dataset `breakdown`
  containing only affected/failing datasets. The immediate `method/run` detail has `values`,
  `unresolved_urns`, and `breakdown_summary`; its `dataset_count` is the resolved scope and its
  `affected_count` is the size of that breakdown. Series values are floats. If a ratio such as
  `ingested_in_time / total` is useful, label it client-derived and handle a zero denominator;
  never present it as a stored server value.
- Inspect immediate run detail and persisted run-completion events for `unresolved_urns`. These are well-formed dataset URN
  literals that matched no registered dataset at run time; report them rather than silently
  dropping or rewriting the filter.
- Dataset `met: "true"` means the latest persisted evaluation met the criterion; `"false"` means
  it did not; `"unknown"` means the dataset is in scope but has no verdict (for example never
  evaluated, newly in scope, or not evaluable for that metric type). A non-dry run replaces the
  metric's persisted verdict set wholesale. A dry run leaves the prior set unchanged.
- `last_check_at` is per-dataset evidence/run timing. `attrs_synced_at` is the newest registry
  attribute-sync timestamp among datasets in this metric's scope. It is scope-relative, unaffected
  by verdict filtering or paging, and is neither measurement time nor registry-wide freshness.
- `/event` is the persisted feed of run-completion and definition-change events. Do not claim
  failure or dry-run history unless the live schema explicitly exposes it; baseline dry runs
  persist no event, result, or verdict replacement.

## Errors: stop and surface, never work around

- `403 READ_ONLY_ROLE`: the effective token role cannot write; direct the user to
  `/dataspoke:dataspoke-access` rather than retrying through another surface.
- `409 METRIC_EXISTS`: create id already exists; inspect it and ask whether the user intends an
  explicit update or a different id.
- `404 METRIC_NOT_FOUND`: update/run target is absent; do not turn the failed update into an
  implicit create.
- `409 METRIC_DISABLED`: a non-dry run requires an enabled metric; offer a dry run or an explicitly
  confirmed enable operation.
- `409 METRIC_RUNNING`: another run is active; report it and inspect events rather than launching a
  parallel workaround.
- `501 NOT_IMPLEMENTED` for passive mode: passive governance metrics are reserved; do not emulate
  them elsewhere.
- `422 INVALID_DATASET_FILTER`: show the API detail and reported character position, then help edit
  the YAML guide; do not broaden the scope silently.
- `422 INVALID_DATASET_URN`: surface the malformed literal and require correction.
- `unresolved_urns`: report them from run output/events as runtime scope findings; do not treat them
  as syntax errors or bypass registry resolution.

For any other schema or route mismatch, return to the narrow live OpenAPI fragment and explain the
deployment-specific contract instead of inventing a request.
