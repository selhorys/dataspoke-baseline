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
    ├── skills/<skill>/SKILL.md
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

### Distribution

A user installs from the hosting repository:

```
/plugin marketplace add <org>/<repo>
/plugin install dataspoke@dataspoke      # <plugin>@<marketplace>
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
  "api_base_url": "https://dataspoke.example.com/api/v1",
  "token": "dsk_…",
  "redoc_url": "https://dataspoke.example.com/redoc",
  "ui_url": "https://dataspoke.example.com",
  "datahub_gms_url": "https://datahub.example.com/api/gms",
  "datahub_token": "<DataHub PAT>"
}
```

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
  user asks for a row-count or null check without naming validation at all.
- **Manage the validation slot** (UC2) — read / register / edit the per-dataset conf
  (`GET`/`PUT`/`PATCH`/`DELETE …/attr/validation/conf`), POST and query results
  (`POST`/`GET …/attr/validation/result`), browse the cross-dataset list
  (`GET /spoke/validation`), and read the lifecycle timeline (`GET …/event/validation`).

Two conf operations are destructive and the skill warns before either. `DELETE …/conf` is a
hard delete: it cascades the dataset's results and `VALIDATION.*` events and removes the DataHub
assertion, leaving the slot as never-created. Replacing a conf's `variables[]` does not migrate
past results, which retain the keys they were posted with, so a rename orphans the existing
series and breaks the pipeline's next POST with `422 UNKNOWN_VARIABLE`.

### `dataspoke-governance`

Guide the complete active-metric lifecycle described below: inspect the deployed contract and
existing metrics, scaffold a definition for any built-in metric type, validate and preview the
request, create or update only after confirmation, prefer a dry run before scheduled execution,
and interpret results, per-dataset verdicts, events, unresolved URNs, and scope freshness.

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

The flagship capability writes data-quality validation into the pipeline the engineer is
building — the code that runs after a partition is written and checks what landed.

It must be honest about the division of labor. DataSpoke validation is an **API for
registration, get, and put of values**: the conf carries four sections — `description` and
`variables` are declared by the pipeline (what it will report); `attribute` states the
dataset's own data-arrival cadence (`cadence_unit`/`cadence_offset`), which DataSpoke reads
back to anchor the governance `validation-score` metric's window; and the optional
`parameter` section is opaque storage for the pipeline's own hyperparameters, which
DataSpoke never interprets. The service stores results and emits them to DataHub. It ships
**no computing engine** — no metric computation, no forecasting, no anomaly detection, and
no threshold or rule evaluation. Every number, each variable and the pass/fail `score`
alike, is computed by the pipeline on the engineer's own engine with their own credentials.

The skill therefore authors the computing code — metrics, baseline comparison, anomaly logic,
thresholds — and touches DataSpoke through a small, fixed set of calls: **register** and,
once the reuse decision resolves, **annotate** the conf at setup (§1/§3), **get** the recent
baseline, and **post** each run's result. A request to have DataSpoke
"detect anomalies" or "enforce a threshold" is answered by writing that logic into the pipeline,
not by implying the service evaluates it. (See `spec/feature/VALIDATION.md`.)

The skill drives four phases.

### 1. Prerequisite chain (strict order)

Each step must pass before the next; failure stops the flow with a remediation pointer.

1. **Access configured** — `dataspoke-access` has produced a working Editor/Admin token. A
   token's effective role is fixed at mint time and never upgrades on its own, so this is a
   token-freshness check, not just an account check: `GET /auth/me` reports only the account's
   current role, not the token's.
2. **Dataset ingested** — the target dataset exists and is covered, confirmed via
   `GET /spoke/common/data/{dataset_urn}/attr/ingestion`. A dataset with no ingestion
   coverage is sent back to `dataspoke-ingestion` first.
3. **Conf registered** — a validation slot with the declared `variables` exists
   (`GET …/attr/validation/conf`); the skill registers one via `PUT` when absent. Which
   utility implements the check is not yet decided at this point, so naming it in
   `description` is §3's job, not this step's.

### 2. `dataset_urn` resolution

The engineer rarely knows the exact URN. The skill resolves it carefully, never guessing:

1. **Gather hints** — scan the engineer's workspace (pipeline scripts, configs) for
   platform / schema / table signals.
2. **Confirm with the user** — restate the inferred platform + schema + table and get
   explicit agreement before any lookup.
3. **Resolve via DataHub search** — query DataHub's GraphQL endpoint **directly** through
   the `datahub-graphql` plugin helper, which posts a search query to `<datahub_gms_url>/graphql`
   authenticated with the user-supplied DataHub token (§Optional DataHub access), to find
   candidate dataset URNs matching the confirmed identifiers. When DataHub access is not yet
   configured, the helper prompts for the GMS URL and token first.
4. **Manual entry as a last resort** — when DataHub search returns no candidate (e.g. a
   dataset not yet search-indexed), the skill accepts a user-supplied URN, never defaulting
   to manual entry while search is viable.
5. **Double-check** — present the resolved URN back to the user for final confirmation
   before it is used in any conf or result call. A wrong URN silently writes to the wrong
   dataset, so this confirmation is mandatory.

### 3. Check for reuse before authoring

Before writing new check logic, the skill searches for an existing implementation rather
than assuming none exists: `GET /spoke/validation?coverage=covered` surfaces other datasets'
registered confs, whose `description` conventionally names the implementing module so a
match is recognizable, and the skill also searches the user's own shared/validation package
in their workspace. Once this step resolves which utility implements the check — an existing
one being reused, or a new one about to be authored in §4 — the skill names it in the conf's
`description`, via `PATCH …/attr/validation/conf`, so the convention this very search relies
on stays accurate for the next dataset. When an existing utility already covers the check,
the skill wires that utility into the pipeline rather than re-authoring the logic.

This makes one ordering invariant load-bearing on both paths, a freshly authored check and a
reused one alike: **the conf is registered before the pipeline ever calls the utility.**
Skipping it does not fail loudly — §4's failure-policy invariant means the utility's entry
point never raises, so a call against an unregistered dataset is caught into a logged
warning locally rather than an exception, and the pipeline task completes as if it had
validated while nothing reaches DataSpoke's history.

### 4. Generate the routine

The skill emits code **into the engineer's own codebase** (their environment, their
credentials — never DataSpoke's). What it generates has two parts, and only one of them is
dataset-specific:

- **A reusable utility**, placed in the user's own shared package rather than inline in the
  pipeline script, so it is callable across datasets and pipelines. It owns metric
  computation, baseline fetch, scoring, and outage handling around the DataSpoke calls — the
  logic §3's reuse-check searches for.
- **Dataset-specific wiring**, kept in the pipeline script itself: `dataset_urn` resolution
  (§2) and the call into the utility with that URN and the partition being validated.

The utility:

1. Computes the declared metrics over the partition just written. Only this step is
   engine-specific; the skill adapts it to the stack in front of it (a Spark aggregation, an
   aggregation pushed into Athena, plain SQL). It validates what actually landed — re-reading
   the destination partition rather than reusing the in-memory frame, since the two diverge
   exactly when the write went wrong.
2. Fetches the recent baseline via `GET …/attr/validation/result?from=<~14d ago>` (the
   historical-result cache; newest-first, so index 0 is the latest sample).
3. Fits a forecast over that baseline or applies whatever comparison the engineer specifies,
   deriving expected ranges. When the user has not named a specific check, the skill's named
   default suggestion is a per-partition row-count anomaly check via Prophet forecasting
   (default settings).
4. **Decides the `score`** in pipeline code — pass/fail (or a fractional value) from the
   computed metrics versus the forecast/baseline.
5. POSTs `{data_time, score, variables}` to `…/attr/validation/result`, keying `variables`
   by the conf's declared names (unknown keys are rejected `422 UNKNOWN_VARIABLE`;
   `score` must satisfy `0.0 ≤ score ≤ 1.0`).

Registration stays out of the generated code, on both the freshly authored and the reused
path (§3). A conf re-registered on every run is a recurring opportunity to change the
declared variables underneath the accumulated history, so the skill performs the `PUT`
itself during the prerequisite chain.

**The failure-policy invariant**: the utility's entry point never raises out of itself. That
single guarantee plays out as two different outcomes, depending on whether the POST can
actually land. When the dataset's conf is registered and DataSpoke is reachable, a scoring
bug or a bad partition becomes a posted `0.0` in the history rather than an exception that
fails the pipeline task. When the POST cannot land — DataSpoke unreachable, or the conf
missing (the case §3's registration ordering guards against) — the failure is logged locally
instead, and nothing reaches DataSpoke's history at all. The recorded case is the deliberate
trade-off: for a dataset whose conf is registered, the governance `validation-score` metric
(`spec/feature/BACKEND.md` §Metrics Service) is the backstop — a dataset whose latest result
scored `< 1.0` drops out of `valid_in_time` and surfaces as a failing verdict, so raising
inside the routine buys nothing that metric doesn't already catch, at the cost of a failed
pipeline task. That backstop does not cover a dataset whose conf was never registered — it is
not evaluated at all rather than counted as failing — which is exactly why §3's registration
ordering is load-bearing rather than a nicety.

Two properties of the result store shape what the routine may assume. Reads collapse
last-write-wins per `data_time`, so a retried run corrects its partition rather than duplicating
it and no deduplication guard belongs in the generated code. But collapsing is keyed on
`data_time` alone, not on the day: the series carries one point per distinct `data_time`, so the
baseline must be bounded by the time window rather than by a row count, and `data_time` must
identify the partition — never the moment of the run, which would make every retry a new point
and silently reduce a multi-day window to a handful of hours.

The skill makes the boundary explicit in what it generates and explains: forecasting and
thresholding are **the pipeline's** logic, authored locally; DataSpoke receives only the
final numbers.

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
route availability. The skill consults it through `bin/dataspoke-schema` before preparing a
write. YAML is solely the human-facing guide and authoring representation: samples and working
definitions may be shown or edited as YAML, but each definition-bearing `POST`, `PUT`, or `PATCH`
request uses `Content-Type: application/json` and a JSON body produced by a lossless conversion.
`GET` and bodyless `DELETE` requests remain governed by live OpenAPI. Before a write, the skill
shows derived JSON when the operation carries a body
rather than implying that YAML is accepted by the API.

### Guided flow

The lifecycle proceeds in this order:

1. **Inspect** — verify access and effective role, load the governance fragment from live
   OpenAPI, and list or read existing metrics before deciding whether the requested identity is
   new or existing.
2. **Scaffold** — offer an editable YAML definition for the selected built-in type. The guide
   explains its valid `metrics[].name` series, type-specific `metric_conf`, scheduling behavior,
   and `dataset_filter`; it includes a usable example for each built-in type.
3. **Validate and preview** — convert the YAML losslessly to JSON, validate the JSON against the
   live contract and relevant cross-field constraints, and display the exact method, public
   route, and JSON body. Validation includes create-only `metric_id`, type-appropriate series and
   configuration, filter syntax accepted by the API, and the distinction between a disabled
   definition and an enabled schedule.
4. **Choose create or update** — create a missing metric with `POST /spoke/governance/metric`;
   update an existing metric at `.../{metric_id}/attr/conf`. The skill does not use update as an
   implicit upsert: full replacement and partial update remain explicit choices matching the
   live contract.
5. **Confirm and apply** — require explicit user confirmation immediately before any definition
   write, delete, enabling of scheduled execution, or non-dry run. The confirmation identifies
   the metric, operation, scope, schedule effect, and exact JSON payload where applicable.
6. **Exercise safely** — recommend an on-demand `?dry_run=true` after creation or a material
   definition change and before enabling its schedule. A dry run is presented as evaluation
   without persisted results or verdict replacement, not as a write-validation endpoint.
7. **Interpret** — read result timeseries, per-dataset verdicts, and lifecycle events together.
   Explain `true`, `false`, and `unknown` verdicts; distinguish aggregate values from client-
   derived ratios; surface `unresolved_urns`; and report `attrs_synced_at` as scope-relative
   registry freshness rather than measurement time or registry-wide freshness.

The skill reports API errors without bypassing them. In particular, it preserves create/update
identity semantics, read-only-role rejection, disabled and concurrent-run conflicts, unsupported
passive mode, invalid filters, and unresolved dataset literals as user-visible outcomes.

---

## Open Questions

- [ ] MCP promotion criteria — which (if any) skill warrants a structured MCP tool surface
      over the curl-wrapper approach.
- [ ] Forecast library choice in generated routines — Prophet is the default example;
      whether to template alternatives (statsmodels, simple rolling thresholds) per user
      preference.
