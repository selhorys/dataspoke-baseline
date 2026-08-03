# DataSpoke End-User AI Scaffold (Claude Code Plugin)

## Table of Contents

1. [Purpose](#purpose)
2. [Audience & Boundary](#audience--boundary)
3. [Architecture](#architecture)
4. [Credential Model](#credential-model)
5. [Skills](#skills)
6. [Validation Routine Authoring (Flagship)](#validation-routine-authoring-flagship)
7. [Open Questions](#open-questions)

---

## Purpose

The **End-User AI Scaffold** specified here is a distributable Claude Code **plugin** that
helps engineers *consume* a running DataSpoke service through its public HTTP API. It is a
sibling deliverable to the **Developer AI Scaffold** (the MANIFESTO §2.2 "AI Scaffold" —
the in-repo `.claude/` configuration that *builds* the product, `spec/AI_SCAFFOLD.md`).

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
    │   └── plugin.json        ← plugin manifest (name "dataspoke", version, skills)
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

Six skills, each tracing to routes in `spec/API.md`. Three are full capabilities; three are
stubs pending demand.

| Skill | UC | Maturity | Primary routes |
|-------|----|----------|----------------|
| `dataspoke-access` | — | full | `GET /ready`, `GET /auth/me`, `POST /auth/token`, `POST /auth/api-tokens` |
| `dataspoke-ingestion` | UC1 | full | `/spoke/ingestion/sources` CRUD, `…/method/run`, `…/event`, `…/datasets`, `/spoke/ingestion/unmanaged` |
| `dataspoke-validation` | UC2 | full (flagship) | routine authoring into the user's pipeline, over `…/attr/validation/{conf,result}`; plus `/spoke/validation`, `…/event/validation` |
| `dataspoke-ontogen` | UC3 | stub | `/spoke/ontogen/…` |
| `dataspoke-metagen` | UC4 | stub | `/spoke/metagen/…`, `…/attr/metagen/…` |
| `dataspoke-governance` | UC5 | stub | `/spoke/governance/…` |

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

### `dataspoke-ontogen` / `dataspoke-metagen` / `dataspoke-governance` (stubs)

Each stub knows its route prefix, reads the current contract with `bin/dataspoke-schema`
(handing `/redoc` to the user when they want to browse it), and answers basic questions about
the feature's surface. They are deliberately thin — marked **TBD** until a concrete end-user
workflow justifies promoting them to full capabilities. No invented behavior beyond what the
API already exposes.

---

## Validation Routine Authoring (Flagship)

The flagship capability writes data-quality validation into the pipeline the engineer is
building — the code that runs after a partition is written and checks what landed.

It must be honest about the division of labor. DataSpoke validation is an **API for
registration, get, and put of values**: the conf declares only `{description, variables[]}`,
and the service stores results and emits them to DataHub. It ships **no computing engine** —
no metric computation, no forecasting, no anomaly detection, and no threshold or rule
evaluation. Every number, each variable and the pass/fail `score` alike, is computed by the
pipeline on the engineer's own engine with their own credentials.

The skill therefore authors the computing code — metrics, baseline comparison, anomaly logic,
thresholds — and touches DataSpoke through exactly three calls: **register** the slot (once, at
setup), **get** the recent baseline, and **put** each run's result. A request to have DataSpoke
"detect anomalies" or "enforce a threshold" is answered by writing that logic into the pipeline,
not by implying the service evaluates it. (See `spec/feature/VALIDATION.md`.)

The skill drives three phases.

### 1. Prerequisite chain (strict order)

Each step must pass before the next; failure stops the flow with a remediation pointer.

1. **Access configured** — `dataspoke-access` has produced a working token (`GET /auth/me`
   returns an Editor/Admin effective role).
2. **Dataset ingested** — the target dataset exists and is covered, confirmed via
   `GET /spoke/common/data/{dataset_urn}/attr/ingestion`. A dataset with no ingestion
   coverage is sent back to `dataspoke-ingestion` first.
3. **Conf registered** — a validation slot with the declared `variables` exists
   (`GET …/attr/validation/conf`); the skill registers one via `PUT` when absent.

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

### 3. Generate the routine

The skill emits code **into the engineer's own pipeline script** (their environment, their
credentials — never DataSpoke's). The generated routine:

1. Computes the declared metrics over the partition just written. Only this step is
   engine-specific; the skill adapts it to the stack in front of it (a Spark aggregation, an
   aggregation pushed into Athena, plain SQL). It validates what actually landed — re-reading
   the destination partition rather than reusing the in-memory frame, since the two diverge
   exactly when the write went wrong.
2. Fetches the recent baseline via `GET …/attr/validation/result?from=<~14d ago>` (the
   historical-result cache; newest-first, so index 0 is the latest sample).
3. Fits a forecast over that baseline (e.g. Prophet with default settings) or applies
   whatever comparison the engineer specifies, deriving expected ranges.
4. **Decides the `score`** in pipeline code — pass/fail (or a fractional value) from the
   computed metrics versus the forecast/baseline.
5. POSTs `{data_time, score, variables}` to `…/attr/validation/result`, keying `variables`
   by the conf's declared names (unknown keys are rejected `422 UNKNOWN_VARIABLE`;
   `score` must satisfy `0.0 ≤ score ≤ 1.0`).

Registration stays out of the generated code. A conf re-registered on every run is a recurring
opportunity to change the declared variables underneath the accumulated history, so the skill
performs the `PUT` itself during the prerequisite chain.

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

## Open Questions

- [ ] MCP promotion criteria — which (if any) skill warrants a structured MCP tool surface
      over the curl-wrapper approach.
- [ ] Promotion of the ontogen / metagen / governance stubs — concrete end-user workflows
      that justify full skills.
- [ ] Forecast library choice in generated routines — Prophet is the default example;
      whether to template alternatives (statsmodels, simple rolling thresholds) per user
      preference.
