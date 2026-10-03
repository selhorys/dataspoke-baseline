# DataSpoke End-User AI Scaffold (Claude Code Plugin)

## Table of Contents

1. [Purpose](#purpose)
2. [Audience & Boundary](#audience--boundary)
3. [Architecture](#architecture)
4. [Credential Model](#credential-model)
5. [Skills](#skills)
6. [Validation Routine Authoring (Flagship)](#validation-routine-authoring-flagship)
7. [Ontology Generation Workflow](#ontology-generation-workflow)
8. [Metadata Generation Workflow](#metadata-generation-workflow)
9. [Governance Metric Lifecycle](#governance-metric-lifecycle)
10. [Open Questions](#open-questions)

---

## Purpose

The **End-User AI Scaffold** specified here is a distributable Claude Code **plugin** that
helps engineers *consume* a running DataSpoke service through its public HTTP API. It is a
sibling deliverable to the **Developer AI Scaffold** (the MANIFESTO §2.2 "AI Scaffold" —
the in-repo agent-agnostic core, shared skills, and native Claude Code and Codex bindings that
*build* the product, `spec/AI_SCAFFOLD.md`).

The two never overlap: the Developer scaffold has full repo access (specs, `src/`, helm,
DB); the End-User plugin sees only the public API surface of a deployed instance and the
end user's own workspace. A data engineer installs the plugin into their own Claude Code,
points it at their organization's DataSpoke deployment, and asks it to perform the same
tasks the reference UI exposes — plus author quality-check routines into their own
pipelines.

---

## Audience & Boundary

**Audience**: data engineers (and analysts / stewards) operating against a deployed
DataSpoke. They hold a DataSpoke account and an API token; they do not have — and do not
need — access to the deployment internals.

**In scope** — the public API surface only:

| Surface | Use |
|---------|-----|
| `/api/v1/auth/…` | Login, profile, mint/list/revoke API tokens |
| `/api/v1/spoke/…` | The five baseline features (ingestion, validation, ontogen, metagen, governance) |
| `/ready`, `/openapi.json`, `/redoc` | Readiness probe, the machine-readable contract, and its human-facing rendering |

**Out of scope** — explicitly never touched by any skill:

- Cluster operations (helm, `kubectl`), `src/`, the operational database.
- `/api/v1/admin/*` and any `/internal/*` route — these require Admin/operator privilege
  the plugin's audience does not assume.
- Inventing endpoints. Every capability traces to a route in `spec/API.md`; when unsure,
  the plugin reads the deployment's own contract via `bin/dataspoke-schema`.

---

## Architecture

### Skills-first plugin

The plugin is **skills-first**: each capability ships as a Claude Code skill under the
`dataspoke` namespace, invoked as `/dataspoke:<skill>`. An MCP server is **deferred** —
skills calling the HTTP API via a thin helper cover the baseline need without the
operational weight of a long-running server; MCP is revisited only if a capability needs
structured tool schemas or streaming that skills cannot express.

### Packaging

The plugin lives in a `plugin/` directory, and the repository that hosts it doubles as a
**single-plugin marketplace**:

```
<repo root>/
├── .claude-plugin/
│   └── marketplace.json      ← marketplace manifest → lists the one plugin
└── plugin/
    ├── .claude-plugin/
    │   └── plugin.json        ← plugin manifest (name "dataspoke", skills)
    ├── skills/<skill>/
    │   ├── SKILL.md           ← concise router, workflow, and decision doctrine
    │   └── references/*.md    ← exact feature contracts and authoring guidance
    ├── references/
    │   └── pagination.md      ← collection traversal shared by every skill
    ├── bin/dataspoke-api      ← auth + base-URL curl wrapper
    ├── bin/dataspoke-schema   ← OpenAPI contract lookup (filtered by path fragment)
    └── bin/datahub-graphql    ← direct-DataHub GraphQL helper (URN search)
```

`bin/dataspoke-api` is the single I/O primitive for the DataSpoke API: it resolves the base
URL and token (see §Credential Model), attaches the `Authorization` header, and shells out to
`curl`. Every skill calls the API through this wrapper rather than constructing auth inline, so
credential handling lives in one audited place. `bin/datahub-graphql` is the parallel primitive
for direct DataHub access — it resolves `datahub_gms_url` + `datahub_token` and posts a GraphQL
query to `<datahub_gms_url>/graphql`, used by the validation skill for dataset-URN search.

`bin/dataspoke-schema` makes the deployment authoritative about its own contract. It fetches
`/openapi.json` and emits only the operations whose path contains a given fragment, together
with the transitive closure of the schemas they reference — a narrowed lookup rather than the
whole document, which is large enough that dumping it would crowd out the task. Skills consult
it before authoring a request body instead of relying on shapes transcribed into SKILL.md, which
drift. `/redoc` serves the same document as a browser-rendered reference for humans; because it
is a client-side renderer, skills read the contract through this helper and hand `redoc_url` to
the user.

Skills use **progressive disclosure**. `SKILL.md` retains only routing, human gates,
decision rules, and intent-to-route mappings; detailed request shapes, error tables, and
authoring patterns live in focused `references/` documents and are loaded only for the path
that needs them. A router gives a hard read instruction before any operation whose contract is
owned by a reference; it does not invite the agent to reconstruct a conf from memory. Curated
references make recurring feature work concise, while the deployment's live OpenAPI document
remains authoritative whenever the two differ. A bare `references/…` path is skill-local
(`skills/<skill>/references/`); the one reference shared across every skill is always written
with its full path, `plugin/references/pagination.md`.

Every skill that reads a collection follows `plugin/references/pagination.md` and consumes all
pages needed for the user's request, never reporting a first page as the complete collection.
Every list route carries the deployment's standard `offset`/`limit`/`total_count` envelope,
including validation history — which is the one documented deviation from the convention: its
end-bound parameter is named `until` rather than `to`, and its limit default and cap are raised
above the other list routes'. [`API.md`](API.md) and the deployment's live OpenAPI document own
the exact values and ordering. A skill traverses past a page on this route by narrowing
`from`/`until` to the unread remainder rather than by `offset`, since the window bounds the
result set. A validation baseline read always supplies `until` equal to the `data_time` under
judgment, so it contains neither that point nor any future point.

Skill descriptions carry literal, field-observed task phrasing in addition to capability names,
such as “how do I write validation code for this dataset”, “add validation to this pipeline”,
“register validation for this table”, “what validation results exist”, and “is this dataset
ingested”. Maintained localized equivalents receive the same treatment. This phrasing is part
of skill routing, not explanatory prose inside the skill body.

### Distribution

A user installs from the hosting repository:

```
/plugin marketplace add <org>/<repo>
/plugin install dataspoke@dataspoke      # <plugin>@<marketplace>
/reload-plugins                          # load the skills in the current session
```

Skills then appear as `/dataspoke:dataspoke-access`, `/dataspoke:dataspoke-validation`,
and so on.

Neither manifest declares a `version`, so the update-cache key resolves to the commit SHA of
`./plugin` and every merged commit reaches installed users without a bump.

---

## Credential Model

The plugin authenticates with a long-lived **`dsk_` API token**, the self-service
credential defined in `spec/API.md` (§Authentication). It is presented on every call as
`Authorization: Bearer dsk_…` — the same header shape as a user JWT, distinguished by the
`dsk_` prefix.

### Minting

The `dataspoke-access` skill walks the two-step mint flow against the deployment:

1. `POST /api/v1/auth/token` with `{email, password}` → short-lived access token (login).
2. `POST /api/v1/auth/api-tokens` with `{name, expires_at?}` → response carries the raw
   `dsk_…` token **once** (`{token, id, name, role_snapshot, …}`). The plugin captures it
   immediately; it is never retrievable again.

### Storage & overrides

Resolved configuration lives in `~/.dataspoke/config.json`, written `chmod 600`:

```json
{
  "api_base_url": "https://api.dataspoke.example.com/api/v1",
  "token": "dsk_…",
  "redoc_url": "https://api.dataspoke.example.com/redoc",
  "ui_url": "https://app.dataspoke.example.com",
  "datahub_gms_url": "https://datahub.example.com/api/gms",
  "datahub_token": "<DataHub PAT>"
}
```

`api_base_url` and `redoc_url` address the **API ingress**; `ui_url` addresses the **frontend
ingress**. They share an origin only when one host serves both, and differ in most prod
deployments (the `app.`/`api.` split in `helm-charts/values-prod.example.yaml`). Every DataSpoke
call the plugin makes (`/ready`, `/api/v1/...`) goes to the API origin.

Environment variables override the file when present, for CI and ephemeral shells:
`DATASPOKE_API_URL`, `DATASPOKE_API_TOKEN`, `DATAHUB_GMS_URL`, `DATAHUB_TOKEN`.

### Optional DataHub access

Alongside the `dsk_` token, the config may carry **optional** direct-DataHub
credentials — a DataHub GMS URL (`datahub_gms_url`) and a DataHub personal access
token (`datahub_token`). They are the user's own DataHub credentials, distinct from
the DataSpoke token, and the same `chmod 600` file holds both. These power the
validation skill's dataset-URN search, which queries DataHub's GraphQL endpoint
directly (see §Validation Routine Authoring). DataHub access is optional: when it is
absent, the URN-search capability is preserved — the plugin requests the user's
DataHub GMS URL and token at the point of use and offers to persist them, rather than
dropping the capability.

### Effective role

The token's effective privilege is `min(role_snapshot, owner.users.role)` per `spec/API.md`.
Write operations (source CRUD, conf PUT/PATCH/DELETE, result POST) require an effective
**Editor** or **Admin** role; a token resolving to **Reader** receives
`403 READ_ONLY_ROLE` on any write and the skill surfaces that verbatim rather than retrying.

---

## Skills

Six skills, each tracing to routes in `spec/API.md`, provide end-user workflows for the
five baseline features.

| Skill | UC | Maturity | Primary routes |
|-------|----|----------|----------------|
| `dataspoke-access` | — | full | `GET /ready`, `GET /auth/me`, `POST /auth/token`, `POST /auth/api-tokens` |
| `dataspoke-ingestion` | UC1 | full | `/spoke/ingestion/sources` CRUD, `…/method/run`, `…/event`, `…/datasets`, `/spoke/ingestion/unmanaged` |
| `dataspoke-validation` | UC2 | full (flagship) | routine authoring into the user's pipeline, over `…/attr/validation/{conf,result}`; plus `/spoke/validation`, `…/event/validation` |
| `dataspoke-ontogen` | UC3 | full | `/spoke/ontogen/…` |
| `dataspoke-metagen` | UC4 | full | `/spoke/metagen/…`, `…/attr/metagen/…` |
| `dataspoke-governance` | UC5 | full | `/spoke/governance/…` |

### `dataspoke-access`

Configure and verify connectivity to a deployment. Probes `GET /ready`, confirms identity
and role via `GET /auth/me`, and runs the mint flow above when no token is configured.
Writes / reads `~/.dataspoke/config.json`. This skill is the prerequisite for all others.

### `dataspoke-ingestion`

Manage ingestion sources (UC1). Lists and inspects sources (`GET /spoke/ingestion/sources`,
`…/{id}`), creates / edits `ACTIVE_CUSTOM_MANAGED` and `PASSIVE` sources (`DATAHUB_MANAGED`
is synced, not authored), and triggers extractor runs (`POST …/{id}/method/run`) in
**dry-run** (`?dry_run=true`, connection check, no writes) before a real run. Surfaces run
history (`…/event`), the source→dataset mapping (`…/datasets`), and the unmanaged bucket
(`GET /spoke/ingestion/unmanaged`). Honors the read-only and concurrency error codes
(`409 INGESTION_SOURCE_READONLY`, `409 INGESTION_RUNNING`,
`409 INGESTION_RUN_NOT_APPLICABLE`) by reporting them, not working around them.

### `dataspoke-validation` (flagship)

Two modes, in priority order:

- **Author a validation routine** into the engineer's own pipeline — the differentiating
  capability, detailed below. The skill activates on pipeline-authoring context (PySpark,
  awswrangler/pandas, dbt, SQL, an Airflow task that writes a partition), including when the
  user asks how to write validation code for a dataset, add validation to a pipeline, register
  validation for a table, or asks for a row-count, null, or freshness check without ever using
  the word "validation".
- **Manage the validation slot** (UC2) — read / register / edit the per-dataset conf
  (`GET`/`PUT`/`PATCH`/`DELETE …/attr/validation/conf`), POST and query results
  (`POST`/`GET …/attr/validation/result`), browse the cross-dataset list
  (`GET /spoke/validation`), and read the lifecycle timeline (`GET …/event/validation`).

Two conf operations are destructive and the skill warns before either. `DELETE …/conf` is a
hard delete: it cascades the dataset's results and `VALIDATION.*` events and removes the DataHub
assertion, leaving the slot as never-created. Replacing a conf's `variables[]` does not migrate
past results, which retain the keys they were posted with, so a rename orphans the existing
series and makes any pipeline still posting the old key fail with `422 UNKNOWN_VARIABLE`.

The router gives hard pointers to `references/validation-conf.md` before conf/result management
and to `references/validation-authoring.md` before pipeline authoring, tests, or backfill. These
references own the detailed payload and authoring patterns; the route and decision doctrine stay
in `SKILL.md`.

### `dataspoke-governance`

Guide the complete active-metric lifecycle described below: inspect the deployed contract and
existing metrics, scaffold a definition for any built-in metric type, validate and preview the
request, create or update only after confirmation, prefer a dry run before scheduled execution,
and interpret results, per-dataset verdicts, events, unresolved URNs, and scope freshness. Its
router reads the curated metric reference for these operations and falls back to live OpenAPI
as the authority when the deployment differs.

### `dataspoke-ontogen`

Guide UC3's global ontology lifecycle: inspect or change its singleton conf, manage the
Markdown seeds that steer inference, exercise manual inference, and review nodes, edges, and
triples. It exposes the global run history and per-result histories so a reviewer can assess a
proposal before deciding. The skill processes the review queue in the required **nodes → edges
→ triples** order and surfaces `ONTOGEN_TRIPLE_DEPENDENCY_PENDING` rather than attempting to
review a triple whose dependencies are not human-approved.

### `dataspoke-metagen`

Guide UC4's documentation lifecycle: manage named generation confs, inspect their matched and
uncovered datasets, set each dataset's opt-in boundary, run a scoped generation, and review
candidates through the global or per-dataset queue. It makes the boundary explicit: a conf's
filter alone does not permit generation; a dataset also needs an enabled boundary whose
`allowed` kinds cover the target field. It explains the global one-approved-candidate-per-item
invariant, including that approving a sibling from another conf supersedes the prior approval
and that rejecting an approved candidate removes the editable DataHub description it wrote.

---

## Validation Routine Authoring (Flagship)

The flagship capability writes data-quality validation into the engineer's own pipeline, not into
DataSpoke. DataSpoke has no compute engine, forecaster, anomaly detector, or rule engine of its
own — it stores the conf, serves prior results as a queryable baseline, persists the posted
result, and emits the accepted record to DataHub. Every scoring computation runs in the
pipeline's own compute engine and credentials; a request for DataSpoke to detect an anomaly or
enforce a threshold is fulfilled by authoring pipeline code, never by DataSpoke evaluating it. The
full service contract is in [`feature/VALIDATION.md`](feature/VALIDATION.md).

Validation is selective by design — teams add it where failure has real downstream impact rather
than a token check on every dataset — and method selection is data-first: the design starts from
measured history over a representative window and there is no universal default check. The open
invariant/forecast/relation method set, the scoring arithmetic, and the outage-vs-fatal
classification for result-store calls are owned by
`plugin/skills/dataspoke-validation/references/validation-authoring.md`, which the skill router
(`plugin/skills/dataspoke-validation/SKILL.md`) points to at each step of its guided route; the
reference also carries the owner-question set the skill asks before the first analysis query, a
worked example, per-engine wiring, and the test/backfill checklist.

Design work is gated by two distinct, non-mergeable human reviews. A plan review — naming the
target, the judging method and rationale per criterion, conf shape, cadence, cold-start floors,
backfill range, and pipeline insertion point — precedes any conf write or code generation; a conf
review, comparing the stored conf back against the approved plan, follows the write and precedes
implementation. Registration must happen before the pipeline ever calls the routine: an
unregistered check does not raise, it silently no-ops, so getting this ordering right is
load-bearing, not a stylistic preference.

Registering a conf can make the `validation-score` metric look worse before it looks better: the
metric selects the dataset's single latest validation result overall, without regard to any
window, and only then counts it toward `valid_in_time` if that one result both lands inside the
cadence-anchored window and scored `>= 1.0` — it never searches backward for an older qualifying
row ([`feature/BACKEND.md` §Metrics Service](feature/BACKEND.md#metrics-service-srcbackendmetrics)),
so a newly configured, high-recall, or still-cold-starting check can hold a dataset below `1.0`
for a while. The skill discloses this before the conf write rather than leaving it as a
governance surprise.

The routine posts the logical target time as `data_time` — the partition's own timestamp, or the
as-of boundary of an inspected whole-dataset snapshot — never the execution instant; posting the
run time instead would make every run a distinct point and defeat baseline collapse. An
unjudgeable criterion (insufficient history for its method) counts as non-passing under the
scoring semantics, never as a cold-start pass.

`plugin/skills/dataspoke-validation/references/validation-conf.md` owns the exact conf and result
body contract, the `score_note` construction and truncation rule, the destructive-operation
warnings (`DELETE` conf, `variables[]` rename), and the error table.

---

## Ontology Generation Workflow

The `dataspoke-ontogen` skill turns UC3 into a guided workflow for the global ontology. The
live OpenAPI fragment is authoritative for conf and review payloads, content types, and route
availability; the skill retrieves it through `bin/dataspoke-schema` before preparing a write.

1. **Inspect and scope** — load the singleton conf, seed inventory, current results, and recent
   inference events. Explain that the ontology and its conf are global, while a one-shot Markdown
   prompt applies only to its individual manual run.
2. **Configure and seed** — present the exact conf or raw Markdown seed body before creating,
   replacing, patching, enabling, disabling, or deleting it. A newly created seed is disabled,
   so the skill makes a separate, explicit choice before it can steer inference.
3. **Exercise safely** — recommend `dry_run=true` before a non-dry manual inference and show
   its scope and one-shot prompt, if any. Surface `ONTOGEN_RUNNING` and `ONTOGEN_DISABLED` as
   outcomes; do not retry around concurrent execution or a disabled non-dry run.
4. **Review in dependency order** — filter and inspect proposed nodes, then edges, then triples,
   including their detail and event histories. Before each review verdict, show the target,
   verdict, and reason, then require explicit confirmation. A triple review remains blocked until
   both nodes and its edge are human-approved.

The skill requires explicit confirmation immediately before every conf or seed write, seed
enablement change, deletion, non-dry run, and review verdict. It reports role, validation, and
conflict errors verbatim rather than inferring a different global state.

---

## Metadata Generation Workflow

The `dataspoke-metagen` skill turns UC4 into a guided workflow for generated editable DataHub
descriptions. It reads the live OpenAPI fragment before writing and keeps three distinct scopes
visible: a named conf's dataset filter, a dataset's opt-in boundary, and a manual run's optional
dataset-URN selection.

1. **Inspect coverage** — list and read confs, inspect a conf's matched datasets, check the
   per-dataset rollup and review queues, and use the uncovered view to distinguish
   `no_conf_match` from `boundary_blocked`.
2. **Set policy and boundary** — preview a conf's exact JSON body before its CRUD operation and
   a boundary before its CRUD operation. Explain that each target dataset needs both a matching
   enabled conf and an enabled boundary whose `allowed` kinds include the requested description
   slot.
3. **Exercise safely** — prefer a dry run before a non-dry generation. For every run, show the
   conf, narrowed dataset-URN scope when supplied, and whether durable candidates will be
   created. Surface `METAGEN_RUNNING`, `METAGEN_DISABLED`, duplicate-name, and filter errors
   without bypassing them.
4. **Review deliberately** — open an item from the global or per-dataset queue and display every
   candidate, its producing conf, status, evidence, and proposed Markdown before seeking a
   verdict. Approval writes the editable DataHub description and is globally mutable across
   confs; rejecting an approved candidate removes that description. The skill therefore requires
   a fresh confirmation immediately before every candidate verdict, and surfaces
   `METAGEN_DATASET_NOT_IN_BOUNDARY` when the dataset is not opted in.

The same confirmation gate applies to every conf or boundary write, delete, enablement change,
and non-dry run. Before deleting a conf, the skill explains that its results become orphaned and
approved descriptions remain in DataHub; before deleting or disabling a boundary, it explains
that future generation for the dataset is excluded or blocked.

---

## Governance Metric Lifecycle

The `dataspoke-governance` skill turns UC5's metric API into a guided workflow for the three
built-in active types: `ingestion-freshness`, `validation-score`, and `doc-health`. It operates
only through the public `/spoke/governance/…` routes and never substitutes database, DataHub,
cluster, or admin access for a missing public capability.

The deployment's live OpenAPI document is authoritative for request schemas, enum values, and
route availability. The skill first reads its curated `references/governance-metric.md` for the
route map, definition fields, built-in series, filter rules, and error interpretations, then
consults live OpenAPI through `bin/dataspoke-schema` before preparing a write and whenever the
deployment differs. JSON is the primary authoring representation because it is the format the API
accepts. If the user supplies YAML, the skill converts it losslessly, shows both representations,
and verifies that their parsed trees match before sending the JSON body. `GET` and bodyless
`DELETE` requests remain governed by live OpenAPI.

### Guided flow

The lifecycle proceeds in this order:

1. **Inspect** — verify access and effective role, load the governance fragment from live
   OpenAPI, and list or read existing metrics before deciding whether the requested identity is
   new or existing.
2. **Scaffold** — offer an editable JSON definition for the selected built-in type. The guide
   explains its valid `metrics[].name` series, type-specific `metric_conf`, scheduling behavior,
   and `dataset_filter`; it includes a usable example for each built-in type. A new metric is
   scaffolded with `is_enabled: false` so scope can be inspected before scheduled execution.
3. **Preflight, validate, and preview** — validate the JSON against the live contract and relevant
   cross-field constraints, confirm the user's metric-type and scope intent, and display the exact
   method, public route, and JSON body. For a create, establish client-supplied kebab-case
   `metric_id` availability with a direct GET. Validation covers type-appropriate series and
   configuration, filter grammar accepted by the API, and the distinction between a disabled
   definition and an enabled schedule. No public pre-create route resolves an arbitrary new
   filter or enumerates whether its tag and URN literals exist, so this step does not claim a
   semantic scope preview.
4. **Confirm and create or update** — require explicit user confirmation before the definition
   write. Create a missing metric, disabled, with `POST /spoke/governance/metric`. An update that
   changes `dataset_filter` or can otherwise change the resolved scope atomically writes the
   changed definition with `is_enabled: false` at `.../{metric_id}/attr/conf`; it never leaves the
   prior schedule enabled against an unreviewed scope. The skill does not use update as an implicit
   upsert: full replacement and partial update remain explicit choices matching the live contract.
5. **Inspect the resolved scope** — after a create or scope-changing update, page through
   `GET /spoke/governance/metric/{metric_id}/dataset` and show the resolved dataset set and its
   scope-relative `attrs_synced_at`. An empty or unintended scope returns to an explicitly
   confirmed definition edit; the skill does not proceed to a dry run or enablement on an
   unreviewed scope.
6. **Exercise safely** — run an on-demand `?dry_run=true` after the scope review and before
   enabling or re-enabling the schedule. A dry run is presented as evaluation without persisted
   results or verdict replacement, not as a write-validation endpoint.
7. **Enable deliberately** — require a fresh confirmation before enabling scheduled execution,
   deleting a definition, or running non-dry. The confirmation identifies the metric, operation,
   reviewed scope, schedule effect, and exact JSON payload where applicable.
8. **Interpret** — read result timeseries, per-dataset verdicts, and lifecycle events together.
   Explain `true`, `false`, and `unknown` verdicts; distinguish aggregate values from client-
   derived ratios; surface `unresolved_urns`; and report `attrs_synced_at` as scope-relative
   registry freshness rather than measurement time or registry-wide freshness.

An update that cannot change scope may take the shorter path: contract validation, exact payload
preview, confirmation, and apply, without disabling and re-reviewing the unchanged dataset set.

The skill reports API errors without bypassing them. In particular, it preserves create/update
identity semantics, read-only-role rejection, disabled and concurrent-run conflicts, unsupported
passive mode, invalid filters, and unresolved dataset literals as user-visible outcomes.

The three seeded factory metrics are editable API resources, but the plugin normally recommends
leaving them unchanged: their empty filters cover the whole registry and they serve as
deployment-wide reference definitions. A team-specific policy is normally a new metric with a
new client-supplied kebab-case identifier, for example `validation-score-orders`, rather than an
edit to a factory row. This is an operating recommendation, not a prohibition; an explicit request may still edit or
disable a factory metric as the API permits. Deleting one is not durable — the startup bootstrap
re-inserts any missing built-in `metric_type` row, disabled with an empty filter, on the next API
start — so disabling (`is_enabled: false`) rather than deleting is the durable way to retire a
factory metric.

---

## Open Questions

- [ ] MCP promotion criteria — which (if any) skill warrants a structured MCP tool surface
      over the curl-wrapper approach.
