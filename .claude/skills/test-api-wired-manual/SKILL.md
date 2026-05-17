---
name: test-api-wired-manual
description: |
  Walk a single api-wired UC scenario manually: read the
  source test file, print each REST request, ask for approval, fire it, print
  the response, and probe side effects (DB rows, DataHub aspects, k8s secrets).
  Optional argument `scope` selects the case (free-form: "UC1", "UC1 Case 2",
  "passive kafka", filename fragment, etc.).
disable-model-invocation: true
user-invocable: true
argument-hint: [scope, e.g. "UC1 Case 2"]
allowed-tools: Bash(*), Read, Edit, Glob, Grep, AskUserQuestion, Skill(dev-env)
---

## Purpose

A guided manual harness for `tests/integration/api_wired/test_*.py`. Drives one
scenario end-to-end via curl, pausing for human approval before every external
mutation. Faithful to `spec/TESTING.md §Manual REST API Testing` — but the source
of truth for steps, payloads, and assertions is the test file itself, not this
skill.

Use when:
- exploring a UC scenario interactively to understand the contract
- debugging a flaky api-wired test by stepping through it
- verifying a DataHub-side change without re-running the full suite
- checking what side effects a route actually produces

Do not use as a substitute for `pytest tests/integration/api_wired/...` — that
remains the regression gate.

## Workflow

### 1. Resolve scope (dynamic discovery — no hardcoded list)

Glob `tests/integration/api_wired/test_*.py`. For each file, `Read` the first
~80 lines and extract `(filename, test_function_name, docstring_first_line)`.

If `$ARGUMENTS` (scope) is given:
- Lowercase + tokenize. Score each candidate by token-overlap against
  `{filename, function name, docstring first line}`.
- Single hit → confirm with one `AskUserQuestion`.
- Multiple hits → menu of matches.
- Zero hits → menu of all discovered scenarios.

If no scope arg → menu of all discovered scenarios. Cap at 4 options per
`AskUserQuestion`; if more, group by UC prefix.

### 2. Pre-flight

Run `./dev_env/health-check.sh`. On any FAIL:
- Tell the user exactly which subsystem failed.
- Offer to reinstall via `Skill(dev-env)` action `reinstall`.
- Do not proceed until green.

Ask once: reset-seed baseline? Default Yes (per `feedback_reset_before_api_wired`).
Yes → `set -a && source dev_env/.env && set +a && uv run python -m tests.integration.util --reset-seed`.

For UC4 scenarios: after reset-seed, run `uv run python -m tests.integration.util --uc4-seed` to pre-stage the LLM context (fulfillment doc + ontogen nodes + DataHub masking); run `uv run python -m tests.integration.util --uc4-restore` to clean up when done.

Bootstrap env:
```bash
bash .claude/skills/test-api-wired-manual/helpers/setup_env.sh
```
Writes `/tmp/_manual_test_env` with `BASE`, `ADMIN_TOKEN`, `GMS`, `GMS_TOKEN`,
`INTERNAL_TOKEN`, `PG_HOST/PORT/DB/USER/PASSWORD`. Source via
`set -a && source /tmp/_manual_test_env && set +a` in every Bash call.

### 3. Granularity prompt (once)

```
Approval granularity? [per-call (default) | per-phase (setup/verify/cleanup)]
```

Per-call: pause before every mutation. Per-phase: bundle setup mutations,
verifications, and cleanup as 3 prompts. Read-only side-effect probes never
pause.

### 4. Step extraction

`Read` the resolved test file. Walk the test function body. Extract every
`await api_client.{put,post,patch,get,delete}(...)`, every direct
`httpx.{get,post}(...)` against `${GMS}`, and every internal-route call
(`/internal/activities/...`). For each:

- HTTP method + URL template (resolve URN encoding)
- Headers (admin / internal / GMS auth)
- Body (substitute env vars from `/tmp/_manual_test_env`)
- Expected status from the assertion immediately following
- Side-effect probes inferred from following assertions (see §Probe selection)

Order matters — preserve test source order. Group consecutive read-only
checks under one heading.

### 5. Per-step loop

For each extracted step:

1. **Print preview** (concise — under 25 lines):
   ```
   STEP N: <one-line operation framing>
     METHOD URL
     auth: admin | internal | gms
     expected: HTTP <status> [+ <key field assertions>]
     side-effects to verify: <bullet list>
   ```
   Then a separate fenced **REQUEST** block with the full pretty-printed body
   (`(none)` if no body). Keep request preview and response preview symmetrical
   — the user reviews both side-by-side.
2. **Approval gate** (per-call mode only): `AskUserQuestion` Approve / Skip / Abort.
3. **Fire**: write body to `/tmp/_step_body.json`, run curl with `-d @file`.
4. **Print response**: emit one fenced **RESPONSE** block with `HTTP <code>`
   on the first line and the full pretty-printed JSON body (or raw text if not
   JSON). Never truncate. The request block from step 1 and this response block
   are the canonical artifacts the user reviews to approve the step.
5. **401 retry**: if HTTP 401 with `error_code=UNAUTHORIZED`, run
   `bash .claude/skills/test-api-wired-manual/helpers/refresh_token.sh`,
   then re-fire once. If still 401, abort with diagnostics.
6. **Side-effect probes**: invoke `helpers/probes.py` (see §Probe selection).
7. **Result line**: ✓ pass / ✗ fail / ⚠ warn (with one-line reason).

### 6. Cleanup phase

Walk the test's `finally:` block as its own phase. Same prompt rules apply.
Honor user choice if they say "skip cleanup" — leave state for inspection
(future runs will reset-seed).

### 7. Summary

Concise table:
```
| step | op                          | http | side-effect | result |
|------|-----------------------------|------|-------------|--------|
| 1    | PUT active-custom conf      | 201  | DB+secret ✓ | ✓      |
| 2    | dry-run while disabled      | 200  | no DH write | ✓      |
| 3    | real-run while disabled     | 409  | no events   | ✓      |
...
```
Then a one-paragraph narrative of what the run proved.

## Probe selection

Pick probes based on the route + method, not from a hardcoded map:

| Test code pattern | Probes (via helpers/probes.py) |
|---|---|
| `PUT/PATCH .../attr/ingestion/conf` | `db_row ingestion_configs <urn>`, `k8s_secret <name>` if conf carries `secret_ref` |
| `DELETE .../attr/.../conf` | `db_row` (expect 0), `gms_aspect <urn> status` (expect retained), `k8s_secret` (expect retained — vault is independent) |
| `POST .../method/ingestion/run` w/ `dry_run:true` | assert no `lastIngested` change |
| `POST .../method/ingestion/run` w/ `dry_run:false`, expected 200 | `gms_aspect <urn> datasetProperties`, `gms_aspect <urn> schemaMetadata`, `gms_lastingested <urn>`, `gms_systemmetadata <urn> schemaMetadata` |
| `POST .../method/ingestion/run` expected 4xx | `events_by_run_id <urn> <ts_window>` (expect empty) |
| `POST /internal/activities/ingestion/passive-sync` | `events_passive_count <urn>` (expect growth from snapshot) |
| `POST /internal/activities/ingestion/datahub-sync` | `db_row dataset_registry <urn>` |
| `PUT/PATCH .../attr/validation/conf` | `db_row validation_configs <urn>`, `gms_aspect <assertion_urn> assertionInfo`, `gms_aspect <assertion_urn> status` (cleared on PUT-after-DELETE resurrect) |
| `DELETE .../attr/validation/conf` | `db_row validation_configs <urn>` `is_removed=true`, `gms_aspect <assertion_urn> status.removed=true` |
| `POST .../attr/validation/result` | `db_row validation_results <urn> <data_time>`, `gms_assertion_run_event <assertion_urn> timestampMillis=data_time` |
| `POST .../attr/metagen/conf` | `db_row metagen_configs <urn>` |
| any GraphQL mutation against `${GMS}` | parse mutation name → describe expected entity URN side-effect |

If no rule matches, report "no automatic probe — verify manually" and continue.

## Bug-fix mode

When a probe finds a real bug (not a transient infra failure):

1. Print `BUG FOUND:` then a 2-3 line diagnosis with file:line references.
2. Propose a patch as a unified diff in the message.
3. `AskUserQuestion`: Apply patch / Skip / Abort run.
4. On Apply: edit via `Edit` tool, re-run the failing step (with one approval).
5. Always include a regression assertion in the patch — silent contract drift
   is what created the bug in the first place.

Distinguishing real bugs from transient infra failures:
- Transient: `ConnectTimeout`, `ReadTimeout`, single 5xx that disappears on
  immediate retry. Log a warning, continue.
- Real: 4xx with structured `error_code`, missing aspect, wrong field type,
  systemMetadata sentinel `no-run-id-provided`, etc. Trigger fix mode.

Do NOT mask transient failures by bumping timeouts (`feedback_no_increase_timeout`).

## Helpers

- `helpers/setup_env.sh` — bootstrap env + admin JWT into `/tmp/_manual_test_env`.
- `helpers/refresh_token.sh` — re-issue admin JWT, replace in `/tmp/_manual_test_env`.
- `helpers/probes.py` — DB / GMS / k8s side-effect probes. Run as
  `python3 .claude/skills/test-api-wired-manual/helpers/probes.py <probe> <args>`.

Run `python3 helpers/probes.py --list` to see current probes.

## Operating principles

- **The test file is the source of truth.** This skill discovers steps and
  payloads from the test source — never hardcode them here. If a test changes,
  the skill changes with it.
- **Pause before every mutation.** Default to per-call approval. Read-only
  probes don't pause.
- **Operation-framed previews.** "PUT active-custom conf for title_master"
  beats "PUT /spoke/common/data/.../attr/ingestion/conf" (per
  `feedback_naming_operation_framed`).
- **Use `-d @file` curl, not inline `-d '{...}'`.** Shell quoting JSON inline
  is a trap.
- **JWT TTL is short.** Refresh proactively on 401, don't propagate the error.
- **Reset before run** unless user opts out (`feedback_reset_before_api_wired`).
- **Never truncate response bodies.** Pretty-print whole JSON.
