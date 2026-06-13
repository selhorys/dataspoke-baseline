---
name: test-manual-ui
description: |
  Walk a single api-wired UC scenario through the browser UI manually: for each
  step in the source test file, print the UI gesture (page + action), ask you to
  perform it and report what you observed, then independently probe the backend
  (REST read-back, DB rows, DataHub aspects, k8s secrets) to confirm the side
  effect fired. The human-driven sibling of the automated `tests/e2e/` use-case
  group — same scenarios, with a human at the browser. Optional argument `scope`
  selects the case (free-form: "UC1", "UC1 Case 2", "passive kafka", filename
  fragment, etc.).
disable-model-invocation: true
argument-hint: '[scope, e.g. "UC1 Case 2"]'
allowed-tools: Bash(*), Read, Edit, Glob, Grep, AskUserQuestion, Skill(k8s-deploy)
---

## Purpose

A guided manual harness that walks one `tests/integration/api_wired/test_*.py`
scenario through the **browser UI** instead of curl. It is the browser-facing
sibling of `/test-manual-api-wired` and the human-driven counterpart of the
automated E2E use-case group (`tests/e2e/use-case/`, `spec/TESTING.md
§End-to-End (E2E) Testing`) — same UC scenarios, run with a human at the browser
when you want eyes on the rendered UI rather than an unattended Playwright run.

Use when:
- verifying the reference UI renders and drives each UC end-to-end
- confirming a frontend change still produces the right backend side effects
- exploring how a UC scenario looks to a user, page by page
- catching UI/backend divergence — UI looked right but nothing persisted, or it
  persisted but the UI never reflected it

Do not use as a substitute for `pytest tests/integration/api_wired/...` (the
regression gate) or `/test-manual-api-wired` (the curl-level harness).

## Division of labour

No browser-automation tool is available, so the work is split:

- **You** drive the browser, perform each gesture, and report what you observed.
  You may paste a screenshot — this skill reads it (vision) and checks it against
  the expected UI state.
- **This skill** scripts each gesture from the source test, and after you act,
  **independently probes the backend** to confirm the mutation landed.
- A step **passes only when both agree**: your UI observation ✓ *and* the backend
  probe ✓. Either alone is insufficient.

## Source of truth (no hardcoded scripts)

Every step is derived dynamically, never hardcoded here:

- **The api-wired test file** is the canonical sequence — step order, request
  payloads, expected statuses, and the side effects to verify. If the test
  changes, the walkthrough changes with it. Payloads shown to you are copied
  **verbatim** from the test, never paraphrased.
- **`spec/feature/FRONTEND_*.md`** route tables map each API route to its **page
  path + UI gesture**. These specs anchor every UI element to an API route, so
  the route→gesture mapping is reliable (see §UI-gesture mapping).

## Reused helpers (shared with test-manual-api-wired)

This skill does not duplicate the env/token/probe machinery — it calls the
sibling skill's helpers by path:

- `.claude/skills/test-manual-api-wired/helpers/setup_env.sh` — bootstrap env +
  admin JWT into `/tmp/_manual_test_env`.
- `.claude/skills/test-manual-api-wired/helpers/refresh_token.sh` — re-issue the
  admin JWT on 401.
- `.claude/skills/test-manual-api-wired/helpers/probes.py` — DB / GMS / k8s
  side-effect probes. Run `python3 .../probes.py --list` to enumerate.

Own helper:

- `.claude/skills/test-manual-ui/helpers/preflight.sh` — health-check, confirm
  the frontend is reachable, print the app URL + login credentials, optional
  reset-seed. Bootstraps env by calling `setup_env.sh`.

## Workflow

### 1. Resolve scope (dynamic discovery — no hardcoded list)

Glob `tests/integration/api_wired/test_*.py`. For each file, `Read` the first
~80 lines and extract `(filename, test_function_name, docstring_first_line)`.

If `$ARGUMENTS` (scope) is given: lowercase + tokenize, score each candidate by
token-overlap against `{filename, function name, docstring first line}`. Single
hit → confirm with one `AskUserQuestion`. Multiple hits → menu of matches. Zero
hits → menu of all discovered scenarios.

If no scope arg → menu of all discovered scenarios. Cap at 4 options per
`AskUserQuestion`; if more, group by UC prefix.

### 2. Pre-flight

```bash
bash .claude/skills/test-manual-ui/helpers/preflight.sh
```

This runs `./helm-charts/bin/health-check.sh`, bootstraps `/tmp/_manual_test_env`
via `setup_env.sh`, resolves and **curl-probes the frontend URL** (host
`pnpm dev` at `http://localhost:3000` when `--frontend local`, else the cluster
`http://app.<INGRESS_IP>.nip.io/`), and prints the app URL plus the admin login
(`dataspoke@dataspoke.local` / `dataspoke`).

On any health-check FAIL: name the failed subsystem, offer to reinstall via
`Skill(k8s-deploy)` action `reinstall`, do not proceed until green. If the
frontend URL is unreachable: tell the user to start it (`pnpm -C src/frontend dev`
for `--frontend local`, or `install.sh --profile dev --components frontend` for
the cluster build) and stop.

Ask once: reset-seed baseline? Default Yes (per `feedback_reset_before_api_wired`).
Yes → `set -a && source helm-charts/.env && set +a && uv run python -m tests.integration.util --reset-seed`.
For UC4 scenarios also run `--uc4-seed` (and `--uc4-restore` on completion), per
`/test-manual-api-wired`.

Then have the user log in at the printed URL before Step 1.

### 3. Granularity prompt (once)

```
Approval granularity? [per-step (default) | per-phase (setup/verify/cleanup)]
```

Per-step: one gesture at a time. Per-phase: bundle the setup gestures, the
read-only verifications, and the cleanup into 3 prompts. Observe-only steps
(read-back the UI already polls) never block on a gesture but still confirm.

### 4. Step extraction + UI-gesture mapping

`Read` the resolved test file. Walk the test function body in source order.
Extract every `await api_client.{put,post,patch,get,delete}(...)`, every
`httpx.{get,post}(...)` against `${GMS}`, and every internal-route call
(`/internal/activities/...`). For each, record method + URL, body (verbatim from
the test), and the expected status/assertions that immediately follow.

Then classify and map each call to a UI step:

- **`[UI gesture]`** — the call is something a user triggers in the browser
  (create / edit / delete a config, trigger a run, approve / reject). Map the
  route → page path + the concrete gesture via §UI-gesture mapping.
- **`[observe]`** — a read the UI performs on its own (list/detail/timeseries
  the page renders or polls). Map to "what the page should now show."
- **`[API-fired, no UI surface]`** — internal-activity triggers, GMS GraphQL
  seeding, direct DB setup the test does to stage state. These have no user
  gesture; fire them via the curl path exactly as `/test-manual-api-wired` would
  (same approval gate) so the scenario stays coherent. State plainly that this
  step has no UI surface and is being fired on the user's behalf.

Preserve test source order. Group consecutive `[observe]` reads under one
heading.

### 5. Per-step loop

For each step:

1. **Print preview** (concise — under 25 lines):
   ```
   STEP N: <operation-framed action>                       [UI gesture]
     page:   <UI path>
     do:     <gesture — clicks / field values / submit>
     expect (UI):      <what you should see — toast, row, badge, redirect>
     expect (backend): <method URL → status; side effects to probe>
   ```
   For `[API-fired, no UI surface]` steps, replace `page/do` with a `REQUEST`
   block (full pretty-printed body, verbatim from the test) as in
   `/test-manual-api-wired`.
2. **Gesture gate** (per-step mode): `AskUserQuestion` Done / Skip / Abort.
3. **You act, then report**: describe the observed UI state, or paste a
   screenshot. The skill checks it against `expect (UI)`.
4. **Backend probe**: confirm the side effect independently —
   - the REST read-back the test asserts (the same `GET` the page makes), via
     curl with the admin token from `/tmp/_manual_test_env`; and
   - deeper probes the UI cannot show (k8s secret, DataHub aspect, event row)
     via `helpers/probes.py` (see §Probe selection).
   On HTTP 401, run `refresh_token.sh` and retry once.
5. **Result line**: ✓ pass (UI ✓ + backend ✓) / ✗ fail / ⚠ warn — one-line
   reason. If UI and backend disagree, the step is ✗ and the disagreement is the
   headline (e.g. "backend emitted 2 datasets but the Datasets table is empty").

### 6. Cleanup phase

Walk the test's `finally:` block as its own phase (usually a delete). Same prompt
rules. Honour "skip cleanup" — leave state for inspection; the next run
reset-seeds.

### 7. Summary

Dual-confirmation table:

```
| step | action                       | UI observed        | backend probe     | result |
|------|------------------------------|--------------------|-------------------|--------|
| 1    | create active-custom source  | detail, secret ●●● | 201 + k8s secret  | ✓      |
| 2    | dry-run                      | run: success, 0 ds | emitted_count=0   | ✓      |
| 3    | real run                     | run: success       | emitted_count≥2   | ✓      |
| 4    | datasets table               | 2 rows, emitted    | GET datasets ≥2   | ✓      |
| 5    | events table                 | INGESTION.COMPLETE | event status=success | ✓   |
| 6    | reverse-lookup page          | owning source, run | source_id matches | ✓      |
| 7    | delete source                | gone from list     | GET source → 404  | ✓      |
```
Then a one-paragraph narrative of what the run proved about the UI↔backend wiring.

## UI-gesture mapping

Map each extracted route to a page + gesture using the `FRONTEND_*.md` route
table for that feature (read the relevant spec at runtime — do not hardcode).
The stable anchors:

| Feature | API route prefix | Page(s) | Gesture source |
|---|---|---|---|
| Ingestion | `/spoke/ingestion/...`, `/spoke/common/data/{urn}/attr/ingestion` | `/ingestion`, `/ingestion/sources/new`, `/ingestion/sources/[id]`, `/ingestion/data/[urn]` | FRONTEND_INGESTION.md |
| Validation | `/spoke/validation`, `/spoke/common/data/{urn}/attr/validation/...` | `/validation`, `/validation/data/[urn]` | FRONTEND_VALIDATION.md |
| OntoGen | `/spoke/ontogen/...` | `/ontogen`, `/ontogen/conf`, `/ontogen/seed` | FRONTEND_ONTOGEN.md |
| MetaGen | `/spoke/metagen/...`, `/spoke/common/data/{urn}/attr/metagen/...` | `/metagen`, `/metagen/data/[urn]` | FRONTEND_METAGEN.md |
| Governance | `/spoke/governance/metric/...` | `/governance/dashboard`, `/governance/metrics`, `/governance/metrics/new`, `/governance/metrics/[id]` | FRONTEND_GOVERNANCE.md |

Method → gesture heuristics (refine from the spec's component notes):
`POST .../sources` → create form + Submit; `POST .../method/run` → run panel
(toggle `dry_run`); `POST .../method/review` → Approve/Reject on the candidate
card (ConfirmDialog); `PUT/PATCH .../attr/conf` → conf editor Save;
`DELETE ...` → delete behind ConfirmDialog; bare `GET` list/detail → the page
renders or polls it (`[observe]`, no gesture).

## Probe selection

Reuse the `/test-manual-api-wired` SKILL's "Probe selection" table verbatim —
same route→probe rules, same `helpers/probes.py`. The UI skill adds one layer:
the **REST read-back** the page itself performs (the `GET` whose JSON the page
renders) is run first as the primary backend confirmation, then `probes.py` for
what the UI can't surface (k8s secret contents, DataHub aspects, raw event
rows). If no probe rule matches, report "no automatic probe — confirm from the
UI only" and continue.

## Worked example — UC1 Case 2 (`test_uc1_active_custom_postgres.py`)

Payloads and expectations below are copied **verbatim** from the test; the skill
generates the same for any scenario.

**STEP 1 — create ACTIVE_CUSTOM_MANAGED source** `[UI gesture]`
```
page:   /ingestion/sources/new
do:     mode selector → ACTIVE_CUSTOM_MANAGED; in the YAML recipe editor enter
        the lossless YAML form of this body, then Submit:
          mode: ACTIVE_CUSTOM_MANAGED
          name: dummy postgres example_db in catalog schema
          schedule: '0 0 * * *'
          recipe.source.type: postgres
          recipe.source.config:
            host_port: example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432
            database: example_db
            username: postgres
            password: ${dummy-data-pg__password}
            env: DEV
            schema_pattern.allow: ['^catalog$']
expect (UI):      success → redirect to source detail; Recipe panel renders the
                  YAML with password masked as ${dummy-data-pg__password}
                  (never the plaintext); mode badge ACTIVE_CUSTOM_MANAGED.
expect (backend): POST /spoke/ingestion/sources → 201; body.mode=ACTIVE_CUSTOM_MANAGED,
                  body.schedule='0 0 * * *', NO schedule_tier on the wire,
                  recipe…password == '${dummy-data-pg__password}' verbatim,
                  plaintext password absent from the response.
probe:            GET /spoke/ingestion/sources/{id} (read-back, assert masked ref);
                  probes.py k8s_secret dataspoke-source-cred-dummy-data-pg
```
Precondition (test skip-guard): `GET /spoke/ingestion/secrets` must list ref
`dummy-data-pg__password`; if absent, stop and tell the user to pre-create the
K8s Secret `dataspoke-source-cred-dummy-data-pg` (key `password`).

**STEP 2 — dry-run** `[UI gesture]`
```
page:   /ingestion/sources/[id]  → Run panel
do:     toggle dry_run ON, trigger Run
expect (UI):      run result shows success; "no datasets emitted".
expect (backend): POST .../method/run?dry_run=true → 200; detail.dry_run=true,
                  detail.emitted_urns_count == 0; run_id non-empty.
```

**STEP 3 — real run** `[UI gesture]`
```
page:   /ingestion/sources/[id]  → Run panel
do:     toggle dry_run OFF, trigger Run
expect (UI):      run result success; emitted-dataset count ≥ 2.
expect (backend): POST .../method/run → 200; detail.dry_run=false,
                  detail.emitted_urns_count >= 2.
probe:            probes.py gms_aspect <catalog.title_master urn> datasetProperties;
                  probes.py gms_aspect <…> schemaMetadata; probes.py gms_lastingested <…>
```
where `<catalog.title_master urn>` =
`urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)`.

**STEP 4 — datasets table** `[observe]`
```
page:   /ingestion/sources/[id]  → Datasets panel
expect (UI):      ≥ 2 catalog rows (title_master, editions), origin = emitted.
expect (backend): GET .../sources/{id}/datasets → ≥ 2 urns, origins include 'emitted'.
```

**STEP 5 — events table** `[observe]`
```
page:   /ingestion/sources/[id]  → Events panel
expect (UI):      an INGESTION.COMPLETE row for this run, newest first.
expect (backend): GET .../sources/{id}/event → event with detail.run_id==run_id,
                  event_type=INGESTION.COMPLETE, status='success' (poll ≤ 30s).
```

**STEP 6 — per-dataset reverse-lookup** `[UI gesture]`
```
page:   /ingestion/data/<encoded catalog.title_master urn>
expect (UI):      Ingestion panel names the owning source (links to its detail),
                  mode ACTIVE_CUSTOM_MANAGED, latest run = success.
expect (backend): GET /spoke/common/data/{urn}/attr/ingestion → source_id==this source,
                  mode=ACTIVE_CUSTOM_MANAGED, latest_run.status='success' (poll ≤ 30s
                  for ES settle, per project_es_indexing_lag_after_reset_seed).
```

**STEP 7 — cleanup: delete source** `[UI gesture]` (the test's `finally:`)
```
page:   /ingestion/sources/[id]
do:     Delete (ConfirmDialog)
expect (UI):      source gone from /ingestion list.
expect (backend): GET .../sources/{id} → 404.
```

## Operating principles

- **The test file is the source of truth.** Discover steps, payloads, and
  expectations from the test source — never hardcode them here. Payloads shown to
  the user are verbatim from the test, never paraphrased.
- **Two independent confirmations per step.** A green UI with no backend write is
  a fail; a backend write the UI never shows is a fail. Report the disagreement.
- **Operation-framed previews** (`feedback_naming_operation_framed`): "create
  active-custom source" beats "POST /spoke/ingestion/sources".
- **Don't patch src/ from here.** This is a manual-test session: on a real UI or
  backend bug, abort and surface the gap for the Plan → generator → reviewer
  loop (`feedback_no_onthefly_fix_during_manual_test`). Editing the test file or
  this skill to correct a step is fine.
- **Reset before run** unless the user opts out (`feedback_reset_before_api_wired`).
- **JWT TTL is short.** Refresh proactively on 401 via `refresh_token.sh`.
- **Never truncate** response bodies in the backend-probe output.
