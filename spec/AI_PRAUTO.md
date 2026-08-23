# PRauto: Autonomous PR Worker

> **Document Status**: Specification v0.8 (2026-08-23)
> This document specifies "prauto" -- an autonomous PR worker that monitors GitHub issues,
> writes code via a headless coding-agent CLI, and submits pull requests. Prauto extends the AI
> scaffold (`spec/AI_SCAFFOLD.md`) with unattended, scheduled development automation.

> **Architecture note (v0.8)**: Prauto is re-specified as a **loop master + contract** split.
> The *contract* (labels, phase state machine, the evidence-based plan gate, generator ≠ reviewer,
> deploy ordering, GitHub-as-SSOT, the security model) is substrate-independent and durable. The
> *loop* (scheduling, agent selection, worker dispatch, quota probing) was previously a
> hand-rolled bash harness (`.prauto/heartbeat.sh` + `lib/*.sh`), removed in v0.8. It is replaced
> by a **loop master** — a scheduler-triggered meta-agent that wakes on a cadence, picks a coding
> agent (Claude Code, falling back to Codex), and spawns worker/reviewer processes. The contract
> below
> is written agent-agnostically; a reference loop-master binding (Hermes Agent) is in
> [Meta-Agent Loop Bindings](#meta-agent-loop-bindings).

---

## Table of Contents

1. [Overview](#overview)
2. [Worker Identity and Configuration](#worker-identity-and-configuration)
3. [Loop Master Cycle](#loop-master-cycle)
4. [Agent Availability](#agent-availability)
5. [Job State Machine](#job-state-machine)
6. [Issue Discovery Protocol](#issue-discovery-protocol)
7. [Worker Agent Invocation](#worker-agent-invocation)
8. [Dev Cluster and Deploys](#dev-cluster-and-deploys)
9. [PR Lifecycle](#pr-lifecycle)
10. [Write Idempotency](#write-idempotency)
11. [Security Model](#security-model)
12. [Integration with AI Scaffold](#integration-with-ai-scaffold)
13. [Meta-Agent Loop Bindings](#meta-agent-loop-bindings)

---

## Overview

### What prauto is

Prauto is a scheduled, unattended worker that automates the issue-to-PR pipeline. Each wake:

1. Checks whether a coding agent is available (Claude Code, else Codex; else exit)
2. Claims new work if under `PRAUTO_OPEN_ISSUE_LIMIT`
3. Processes **all** claimed issues (oldest first), each via a self-contained state machine

Two layers, with different lifespans and owners:

- **Contract** — GitHub labels, the phase state machine, the evidence-based plan gate, the
  generator ≠ reviewer rule, deploy ordering, and the security model. This is the part that
  survives any re-implementation; it is specified here.
- **Loop** — the scheduler trigger, agent selection, subagent dispatch, and quota probing. This
  is provided by a **loop master**; a reference binding is described in
  [Meta-Agent Loop Bindings](#meta-agent-loop-bindings).

### Key design decisions

- **GitHub as single source of truth**: Every wake derives its next action from **remote
  GitHub state** (labels, assignees, comments, review status). Local state, where any exists, is
  for debugging only. This principle matters *more* under a meta-agent loop than under a
  long-lived bash worker: a scheduler tick is a fresh process with no in-memory carryover, so
  "derive everything from GitHub, keep nothing in memory" is the only thing that makes the loop
  resumable across ticks.
- **No resume, by default**: Each worker session starts fresh. The implementation prompt
  instructs the agent to check the branch for existing committed work and continue from there.
  Uncommitted work from a session that died mid-run is invisible to this mechanism and is
  restarted, not resumed.
- **Ready-label timestamp as lifecycle anchor**: When `prauto:ready` is set (or re-set), the
  timestamp of that label event marks the start of the current lifecycle. All comment-scanning
  ignores comments before it, enabling clean restarts without manual cleanup.

### Execution environment

Runs on a local developer machine. Requires: a coding-agent CLI with a logged-in account
(Claude Code and/or Codex — both use login-based OAuth, not API tokens), the `gh` CLI
(authenticated), `git`, and a scheduler capable of waking the loop master (cron, launchd, or a
meta-agent scheduler). Docker/K8s/cloud deployments are out of scope for v1.

---

## Worker Identity and Configuration

### Two configuration tiers

| File | Committed | Purpose |
|------|-----------|---------|
| `config.env` | Yes | Repo-level conventions: labels, branch prefix, max retries, model, org-member filter, reviewer |
| `config.local.env` | No | Instance identity (`PRAUTO_WORKER_ID`), agent choice and turn/budget limits, dev-cluster binding, `ANTHROPIC_API_KEY`, `GH_TOKEN` |

A single machine may run multiple prauto instances (distinct worker IDs) sharing the same
GitHub credential. If `ANTHROPIC_API_KEY` or `GH_TOKEN` is empty, CLIs fall back to system
authentication.

`PRAUTO_AGENT` (new in v0.8) selects the worker's coding agent: `claude` (default), `codex`, or
`auto` (Claude first, Codex fallback — see [Agent Availability](#agent-availability)). The
turn/budget limits in `config.local.env` are per-agent: `PRAUTO_*_MAX_TURNS_*` and
`PRAUTO_*_MAX_BUDGET_*` apply to whichever agent is selected; an agent that does not support a
given cap (e.g. Codex has no `--max-budget-usd`) ignores it.

---

## Loop Master Cycle

Each wake runs seven steps in order. The loop master owns every step; the worker/reviewer
subagents only run when dispatched in step 6.

1. **Concurrency gate** — exit if a worker subagent is already running or pending (a prior
   tick's worker that is mid-run or waiting out a token reset). Enforced by the loop master's
   live-subagent inventory plus a durable marker; see [Security Model](#security-model).
2. **Load config** — `config.env` + `config.local.env`.
3. **Agent availability** — pick Claude Code, else Codex, else exit and post a
   quota-paused comment on WIP issues. See [Agent Availability](#agent-availability).
4. **Claim a new issue** if under `PRAUTO_OPEN_ISSUE_LIMIT`.
5. **Process all claimed issues** (oldest first, self-contained state machine per issue):
   `prauto:done`/`prauto:failed` skip, `prauto:wip` derives phase and dispatches a worker
   subagent for the actionable phase, `prauto:review` squash-finalizes or addresses feedback.
6. **Dispatch** — one worker subagent per actionable issue (analysis, implementation,
   integration-fix), then a reviewer subagent over the worker's diff where the contract calls
   for adversarial review (implementation).
7. **Finalize** — the loop master (not the worker) pushes, opens/updates PRs, posts test
   results, and swaps labels.

**Claim-first, then process-all**: Step 4 counts open issues assigned to this worker (excluding
ready-only restarted issues). If under limit, claims the oldest `prauto:ready` issue. Step 5
loops over all claimed issues.

**Worktree isolation**: Every worker session runs in a dedicated git worktree. The main repo
directory is never the working directory during worker invocations.

**Cadence**: The user-specified interval (default 4 hours). The schedule is owned by the
scheduler trigger (cron / launchd / meta-agent scheduler), not by `config.local.env`.

---

## Agent Availability

The loop master probes agent availability with a two-step check per candidate, in order:

1. **Claude Code**: `claude auth status` (checks a logged-in OAuth session), then a minimal
   one-turn dry-run (`claude -p "Reply with exactly: OK" --max-turns 1 --allowedTools ""`).
2. **Codex**: presence of a CLI OAuth session (`~/.codex/auth.json`), then a minimal dry-run
   (`codex exec "Reply with exactly: OK"`).

Both agents are authenticated by **login-based subscription accounts, not API tokens**, so the
probe must invoke the CLI itself — there is no token to inspect out-of-band. If `PRAUTO_AGENT`
is `claude` or `codex`, only that agent is probed; `auto` probes Claude then Codex.

Outcomes:

- An agent passes → it is selected for this wake.
- Neither passes → the loop master exits. If a WIP issue exists, post a "Paused" comment
  (with marker); the retry counter is not incremented. On the next wake with an agent
  available, post "Resumed" before continuing.

A dry-run timeout (network slowness) is **not** treated as exhausted — proceed anyway.

---

## Job State Machine

### Phases

Minor issues flow `analysis → implementation → integration-fix → pr → complete`. Non-minor
issues insert a `plan-approval` gate between `analysis` and `implementation`; from
`plan-approval`, an approval advances, a counter-proposal loops back to re-analysis, and no
response waits until the next wake.

Phase is always derived fresh from GitHub -- never read from local state.

| Phase | Description |
|-------|-------------|
| `analysis` | Worker reads issue + codebase, produces a plan |
| `plan-approval` | Wait for human approval (retries not counted) |
| `implementation` | Worker writes code, runs unit tests, commits |
| `integration-fix` | Run integration tests; on failure, worker fixes (up to N attempts) |
| `pr-review` | Worker addresses reviewer feedback on existing PR |
| `pr` | Push branch, create/update PR |

### The plan gate is evidence-based

Only `minor` issues skip `plan-approval`, and **the issue body does not decide that**. The
`### Change Size` field an author fills in is a hint. `AGENTS.md §Implementation Workflow` lets a
change skip planning only when **all** of its skip-plan criteria hold:

- touches < 3 files **and** adds/modifies < 60 lines of logic,
- introduces no new API endpoint, DB table/column, pgvector collection, or Airflow DAG,
- requires no cross-layer coordination, and
- the human explicitly signals "just do it".

Prauto satisfies the last criterion structurally: an author's `### Change Size: minor` plus a human
applying `prauto:ready` is prauto's form of that signal. It is necessary but not sufficient — the
first three are evaluated by the analysis phase against **its own plan**, the first artifact that
actually knows the shape of the work. Any single hit downgrades the issue to `medium` and routes it
through `plan-approval`, regardless of what the issue body claimed. An author's `minor` can be
overridden upward; it can never buy a skip the plan's own evidence does not support.

Reading `### Change Size` straight from the issue body would be the self-classification
`AGENTS.md` forbids — *never self-classify a task as "trivial" to skip planning* — and by an author
who has not yet seen the plan. Deferring the judgment to the analysis phase is better evidence, though
it remains an agent session classifying its own plan, not an escape from self-classification.

### Phase derivation from GitHub

On every wake (comment checks scoped to current lifecycle):

1. PR exists for this branch -> `pr`
2. `prauto:plan-review` label present -> `plan-approval`
3. Plan comment exists + "go ahead" reply -> `implementation`
4. Plan comment exists + no approval -> `plan-approval`
5. No plan comment -> `analysis`

### Retry tracking

Each wake posts a marker comment on the issue. `count_heartbeat_comments()` counts markers
within the current lifecycle only (after ready-label timestamp + most recent `Claimed` comment).
At `PRAUTO_MAX_RETRIES_PER_JOB`, the issue is abandoned. The `plan-approval` phase is exempt.

### Job completion and abandonment

| Scenario | Actions |
|----------|---------|
| New issue -> PR | Push, create PR (with `prauto:review` label), remove `prauto:wip`, add `prauto:review` |
| PR feedback | Address with commits, push, post marker |
| Workflow ESCALATE | Do **not** finalize a PR; remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment naming the escalating stage and its findings |
| Max retries | Remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment |

---

## Issue Discovery Protocol

### Label lifecycle

A human sets `prauto:ready`. On claim, prauto removes `prauto:ready`, adds `prauto:wip`, sets
the assignee. Non-minor-ness is a property of the plan, not knowable at claim time, so
`prauto:plan-review` is added when the plan is posted ([the plan gate is evidence-based](#the-plan-gate-is-evidence-based))
and removed on approval. On success the issue and PR both move `prauto:wip` → `prauto:review`; once approved and
squash-finalized, both move to `prauto:done`. On failure, `prauto:wip` is replaced with
`prauto:failed`. An unclaimed issue simply stays `prauto:ready`.

### Search and claiming

Issues discovered via `gh issue list` filtered by `prauto:ready`, sorted oldest-first.
Org-member filter on by default; disable in `config.local.env` (`PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY`).

**Optimistic claim protocol**: Check for `prauto:wip` -> record timestamp, add label ->
re-fetch, check for competing claims within window -> remove `prauto:ready`, set assignee, post
claim comment.

### Issue restart protocol

To restart an issue: remove all `prauto:` labels except `prauto:ready`, unassign worker, delete
working branch/PR. The ready-label timestamp ensures all comment-scanning functions
automatically ignore stale comments from previous attempts.

---

## Worker Agent Invocation

### Multi-phase execution model

The worker is a headless coding-agent session (Claude Code or Codex), one per phase. The loop
master supplies the phase's goal and bounds; the worker runs the phase's prompt template
(`.prauto/prompts/`) with the agent's native tool scoping.

| Phase | Tools | Turn cap |
|-------|-------|-----------|
| Analysis | Read + Write (plan file only) + limited git | `PRAUTO_MAX_TURNS_ANALYSIS` |
| Implementation | Read + Write + Edit + subagents + workflow + limited Bash (git; `uv sync`/`uv run` pytest, python3, ruff, mypy; `npm run`, `npx prettier`, `npx tsc`, `npx eslint`, `pnpm`) | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Integration fix | Same as implementation | `PRAUTO_MAX_TURNS_INTEGRATION_FIX` |
| PR review | Same as implementation | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Squash commit / Feedback response | No tools (text only) | 1 |

**Denylist (all phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`,
`gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`. It binds
the parent session only — see [Security Model](#security-model) for what it does and does not
enforce.

**Branch-based continuity**: On restart, the prompt instructs the agent to check for existing
commits on the branch and continue from there.

### The implementation phase runs the AGENTS.md workflow

Prauto's implementation phase is the unattended form of `AGENTS.md §Implementation Workflow`
steps 4–9: it drives the generator → adversarial-reviewer → one-fix-pass loop over the plan's
`stages` (with `security` flagging stages that need `security-reviewer` in parallel). In the
Claude binding this is `.claude/workflows/wf-minimal.js`; in the Codex binding it is the
equivalent orchestration expressed in the Codex worker prompt. The analysis phase emits
`stages` (in plan order, inner arrays for concurrent stages) and `security` alongside its plan.

Review is therefore **per-stage and adversarial** — each generator is evaluated by a separate
context before later stages build on its output, upholding the generator ≠ reviewer rule that
exists to prevent the self-praise failure mode.

The workflow's stages leave their changes unstaged — its `NO_COMMIT` contract tells each subagent
so — and the implementation phase commits them after the run returns; the parent session holds the
`git add`/`git commit` grants for exactly this. A run that dies mid-workflow therefore leaves
delegated work uncommitted, and so invisible to the branch-based continuity mechanism
([Overview](#overview)), which only sees committed work; such a run restarts the workflow rather
than resuming it.

An ESCALATE outcome halts the workflow at the escalating stage group, so later stages never run and
the branch holds a partial implementation. Prauto must not carry that forward to tests or a PR: it
abandons the job ([Job completion and abandonment](#job-completion-and-abandonment)) rather than
finalizing.

### Loop-master-owned review gate

In addition to the in-workflow per-stage review, the loop master runs a **final adversarial review
gate** over the worker's committed diff before the PR is opened: a fresh reviewer subagent — a
separate context that has not seen the worker's session — reads the diff against
`scaffold/roles/reviewer.md` and returns a verdict. This is the strengthening that a meta-agent
loop makes cheap: the reviewer is a genuinely separate process, not an in-session subagent whose
tool scope the parent whitelist failed to contain. A REVISE verdict feeds one fix pass; an ESCALATE
abandons the job as above. The gate is mandatory for `implementation`; analysis, integration-fix,
and pr-review do not re-open it.

Deploys stay orchestrator-owned. Prauto's analysis phase never emits `k8s-helm` as a stage: that
stage would deploy under whatever its kubeconfig points at, ignoring the worker-cluster binding and
the api-then-frontend ordering ([Branch image deploys](#branch-image-deploys)), and it carries no
reviewer. All cluster mutation runs from the loop master against `$PRAUTO_DEV_ENV_FILE`.

---

## Dev Cluster and Deploys

### Per-worker dedicated cluster

Each prauto instance binds to **its own dev-profile cluster**, selected by `PRAUTO_DEV_ENV_FILE`
(default `helm-charts/.env.dev`). This binding is what makes provisioning and deploying safe to
automate at all: prauto never contends with a human engineer's cluster for the dev-env lock, and
the blast radius of anything it does — a bad chart, a wedged namespace, a destructive reset —
stops at a cluster only prauto uses.

The env file resolves under `$REPO_DIR`, **never the worktree**. This is a security property, not
a path convention: the worktree holds branch-authored content, so resolving cluster credentials
from it would let a branch redirect prauto's deploys and resets at a cluster of its choosing.

### Provisioning

The cluster is prauto's to create, not a precondition it waits on. When
`./helm-charts/bin/health-check.sh --env-file $PRAUTO_DEV_ENV_FILE --keep-lock` exits 1 — probes ran and the
deployment is unhealthy or absent — prauto runs a full
`install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE` and re-checks; the cluster stages are
skipped only if **provisioning itself** fails. Exit 2 is a local setup fault, not cluster evidence
(`HELM_CHART.md` §Health Check): prauto reports it and provisions nothing, so a missing `kubectl`
or an unresolvable context cannot trigger an unsupervised cluster build. Gated by
`PRAUTO_CLUSTER_PROVISION_ENABLED` (default `true`). The health check is run under a wall-clock
backstop and in a private `TMPDIR`, because this worker is unsupervised: nothing outside it would
notice a check that never returns, and a run the backstop stops must not leave behind the
kubeconfig copy the check writes. A fired backstop counts as exit 1. Every
`install.sh`/`health-check.sh` invocation
carries `--env-file $PRAUTO_DEV_ENV_FILE`; without it both default to `helm-charts/.env.dev`, the
shared cluster the per-worker binding exists to avoid. The health check additionally carries
`--keep-lock` and runs with stdin closed: an unattended gate must never wait on a release prompt,
and prauto meets a held lock through its own acquire, which skips on 409.

Provisioning cost does not count against `PRAUTO_MAX_RETRIES_PER_JOB` — standing up a cluster is
not an attempt at the issue, and charging it would abandon jobs for infrastructure latency that
says nothing about the work.

**Autopilot abort mode**: on GKE Autopilot, a GMS scale-up timeout aborts `install.sh` before the
DataHub ingress+PAT step. The resume is `--from-component datahub`, not `dataspoke-infra`. Note
that fragmented `--from-component` resumes skip the env-sync step and leave stale
`DATASPOKE_DEV_*` credentials in the env file; those are rebuilt from cluster secrets.

### Branch image deploys

Cluster stages test **deployed artifacts**, so the branch's code reaches them only by being
built and deployed. These deploys run the **worktree's** `install.sh` / `build-image.sh`, so
`docker build` runs over the branch's `src/`, its `Dockerfile`, and its chart — all three are under
test. This is deliberate: a branch that changes a Dockerfile or a chart can only be proven by
executing that change, which a trusted base checkout cannot do.

| Diff touches | Deploy (run from the worktree) |
|---|---|
| `src/{api,backend,shared}` | `install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE --components api` |
| `src/frontend/` | `install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE --components frontend` (plus a forced rollout restart — a belt-and-braces guard so the stage never tests a stale pod, independent of the chart's digest stamping) |

**Trusted cluster selection, untrusted build — the one invariant of the deploy.** The two halves
resolve from different trees on purpose: the `--env-file` always resolves from `$REPO_DIR`, never the
worktree, so a branch cannot redirect which cluster is hit; the build/deploy scripts run from the
worktree, so branch infra changes are actually exercised. Provisioning and health-check stay on
`$REPO_DIR` — trusted infra bring-up, not the thing under test.

**Order is api, then frontend — this is a correctness constraint.** A `--components api` upgrade
is a full-release upgrade that reverts `frontend.enabled → false`, deleting the cluster frontend.
Running frontend first therefore destroys the very UI the E2E stage needs, and the failure
surfaces as a confusing E2E error rather than as a deploy fault.

The E2E gate is `src/frontend/`, `tests/e2e/`, or `src/api/`. `src/api/` counts because it is the
contract surface the UI consumes; `src/backend/` and `src/shared/` do not, because api-wired already
proves those over REST against the freshly deployed API image, and a frontend rebuild plus a browser
run would only re-prove it at far higher cost.

---

## PR Lifecycle

### Branch naming

`prauto/I-{issue_number}` (e.g., `prauto/I-42`). Created as isolated git worktrees.

### Push and PR creation

After implementation, the loop master (not the worker) pushes the branch, checks for an existing
PR, and creates one if none exists (with `prauto:review` label, assignee, optional reviewer).

### PR review handling

Issues with `prauto:review` label are checked for unaddressed non-prauto comments. The
feedback-addressed marker breaks the re-pickup loop; new reviewer comments after the marker
make the PR actionable again.

### Test execution

Prauto runs the unattended form of the protocol in [`TESTING.md`](TESTING.md), which is
authoritative for layers, commands, and constraints. Stages run in order; each is skipped when
the diff does not reach its layer. Each stage names its **actor**: the worker runs a stage from
its prompt template inside the session's tool whitelist, while the loop master runs a stage
directly and invokes the worker only for fix sessions.

**Pre-flight gate** *(loop master)*: the health check in its
[Provisioning](#provisioning) form — `--env-file $PRAUTO_DEV_ENV_FILE --keep-lock` — runs before
any integration work. On exit 1, prauto provisions its own cluster and re-checks
([Provisioning](#provisioning)); the integration and E2E stages are **skipped, not failed** only
if provisioning fails. On exit 2 it reports the setup fault, provisions nothing, and skips those
stages rather than failing the issue. An unprovisionable cluster is evidence about the
infrastructure, not the branch, so failing it would burn retries against unrelated code.

**Environment**: the loop master's integration and E2E stages source the worker's env file
(`$PRAUTO_DEV_ENV_FILE`, resolved under `$REPO_DIR`) via `set -a` (the file carries no `export`
prefixes) and hold the dev-env lock at `$DATASPOKE_DEV_LOCK_URL`.

**Stage 1 -- Static gates** *(worker)*: `uv run ruff check src/ tests/` and `uv run mypy src/`,
invoked as **checks, never `--fix`** — prauto verifies the author-run gate rather than mutating
the diff until it passes. Frontend-touching work adds `npx tsc --noEmit` and `npx eslint src/`
from `src/frontend/`; diffs touching `tests/e2e/` add `pnpm -C tests/e2e typecheck`. These are
the four author-run gates of [`TESTING.md §CI Behavior`](TESTING.md#ci-behavior); no
`.github/workflows/` exists, so they are the only thing standing between prauto and a red `dev`.

**Stage 2 -- Unit** *(worker)*: `uv run pytest tests/unit/`; frontend-touching work also runs
`pnpm -C src/frontend test` (offline, mocked). Needs no cluster and no lock.

**Stage 3 -- Integration fix loop (pre-push)** *(loop master; worker for fixes)*: after
implementation, under the dev-env lock. A diff touching `src/{api,backend,shared}` deploys the
branch's API first ([Branch image deploys](#branch-image-deploys)) so the tests reach the
branch's code rather than a stale image. Then spot (`tests/integration/spot/`) and api-wired
(`tests/integration/api_wired/`) run as **two separate groups**, never mixed — a mixed run puts
competing Airflow load on the cluster and flakes on timing. The split binds every integration
invocation, Stage 5 included. Failures feed the worker's fix loop up to
`PRAUTO_INTEGRATION_FIX_MAX_RETRIES`.

**Stage 4 -- E2E (Playwright)** *(loop master; worker for fixes)*: runs when the diff touches
`src/frontend/`, `tests/e2e/`, or `src/api/` ([Branch image deploys](#branch-image-deploys)), and
acquires the dev-env lock for its own run, strictly after the integration groups release theirs. It
deploys the branch's frontend, then runs `pnpm -C tests/e2e test`.

- This stage gives prauto a **cluster + browser dependency** no other stage has.
- The hold is separate rather than inherited because the integration loop has another caller —
  the PR-review path — where E2E is not wanted. The cost is bounded: if another owner takes the
  lock in the gap, E2E skips cleanly, and E2E's own setup reset-seeds rather than depending on
  state inherited from the integration groups.
- Ordering is a constraint, not a preference. Two reasons compound: the frontend deploy rolls the
  API pod, and `--components api` would delete the cluster frontend if it ran second. E2E must
  land strictly after the integration groups and never run concurrently with them.
- `PRAUTO_E2E_FIX_MAX_RETRIES` defaults to `1`, which is **report-only**: the loop invokes a fix
  session only on a non-final attempt, so a single attempt runs the suite and reports the result
  without fixing. Raising it buys fix attempts at a full rebuild + redeploy each.

**Stage 5 -- Final test report (post-push)** *(loop master)*: runs unit + integration tests —
integration under Stage 3's two-group split — and posts results as collapsible PR comments.

### What a green run proves

The cluster-dependent stages reach the API and UI over ingress, so they test **deployed
artifacts** — never the worktree directly. Those artifacts are built from the branch: the Stage 3
and Stage 4 deploys build their images from the worktree source, so api-wired runs against the
branch's own API rather than a stale one — the staleness that would otherwise leave branch API code
unproven is genuinely closed, not relocated. When a deploy is skipped because the diff does not
touch that layer, the stage runs against the image already on the cluster, which is correct: an
untouched layer has nothing new to prove.

The remaining gap is the deploy's own fidelity. A green run proves the branch's built images pass
against a dev-profile cluster running stub clients by default
([`TESTING.md §Stub Toggles`](TESTING.md#stub-toggles-runtimeconfig)) — not that the code holds
against real LLM, Redis, or notification backends.

### Squash-finalize

**Trigger**: PR has `prauto:review` label, assigned to worker, mergeable, clean, latest review
APPROVED.

**Steps**: Rebase on base -> generate squash commit message (1-turn worker, no tools) ->
`git reset --soft` + commit -> force-push with lease -> update PR title -> labels to
`prauto:done` on issue + PR. Does **not** merge or close -- left to the human.

**Commit format**: Conventional commit with max 5-line body, issue/PR reference,
`Co-Authored-By` trailers.

---

## Write Idempotency

### Comment idempotency

| Context | Keyword | Idempotent? |
|---------|---------|-------------|
| Claim | `Claimed` | No -- always fresh (anchors retry counting) |
| Abandonment | `Abandoning` | Yes |
| Plan | `Plan` / `Plan (rev N)` | Yes |
| Quota pause | `Paused` | Yes |
| Heartbeat | `Heartbeat` | No -- each is a new retry marker |
| Implementation start | `Heartbeat -- implementation starting` | No |
| Workflow escalation | `Heartbeat -- workflow escalated` | No |
| Integration fix | `Heartbeat -- integration test fix loop` | No |
| Review/Feedback response | `Review response` / `Feedback response` | No -- multiple valid |

### Optimistic claim locking

Check-then-add with timestamp-based verification window. Not fully atomic but catches most
races; a no-op safeguard for single-worker deployments.

---

## Security Model

### What the phase whitelist is

The per-phase tool whitelist is **defense-in-depth, not an enforced boundary**. It raises the
cost of casual misuse and catches accidental and low-effort failure modes, which is real value.
It does not contain a determined or prompt-injected session, and must not be relied on as though
it does.

The implementation phase grants general-purpose code execution (e.g. `Bash(uv run python3 *)`),
which is general-purpose code execution: a session that shells out from Python reaches every
command the denylist names — `kubectl`, `helm`, `curl`, `wget`, `git push`. Sessions also run with
permissions auto-approved, and no path restriction bounds `Write`/`Edit`. The grant is
load-bearing (prauto writes Python), so the gap is inherent to the phase's purpose rather than
an oversight to be patched by trimming the list.

**Delegation removes even the speed bump.** Tool scoping does not propagate to subagents; a
subagent's own definition governs its tools. The project's generators (`backend`, `test`,
`k8s-helm`) each declare unrestricted `Bash` with no denylist, so granting subagent delegation
means delegated work reaches `kubectl`, `helm`, `git push`, and `curl` directly — no reach-around
needed. The parent denylist binds the parent session and nothing beyond it. The same holds for
`Read(.prauto/config.local.env)`: a subagent reads that file directly, exposing
`ANTHROPIC_API_KEY` and `GH_TOKEN`. That denial was never the real boundary regardless — both are
already exported into every child's environment — so delegation widens an existing exposure
rather than opening a new one.

Turn and budget caps thin out the same way. A parent turn cap bounds the parent loop only;
subagents take their own limits, and no project agent sets one, so `PRAUTO_MAX_TURNS_*` stops
bounding delegated work. Whether a budget cap aggregates across subagents is **unverified** —
treat delegated spend as unbounded until someone establishes otherwise.

### The meta-agent loop narrows the boundary, and relocates it

The loop-master split changes the trust calculus in one direction: the **reviewer is now a
separate process** with its own context, so the "generator reviews its own work" escape no longer
exists — the reviewer subagent was never in the worker's session and inherits no tool grants from
it. This is a real strengthening of the generator ≠ reviewer rule.

It widens the boundary in another: a meta-agent's worker/reviewer subagents run with the loop
master's own tool surface (terminal/file access), which is *broader* than prauto's whitelisted
bash parent. The cluster binding (`$PRAUTO_DEV_ENV_FILE` resolved from `$REPO_DIR`) is therefore
the only real containment left, and it must be treated as the primary boundary — not the tool
whitelist.

| Layer | Restriction | Enforced? |
|-------|-------------|-----------|
| Coding-agent tools | Phase-specific whitelists | No — speed bump; `uv run python3` reaches around it, subagent delegation bypasses it |
| Network access | No web fetch, curl, wget | No — `npx`/`pnpm dlx` fetch and execute arbitrary packages |
| Cluster access | No kubectl, helm for the parent session | No — same reach-around; generators grant `Bash` outright; loop master deploys via `install.sh` |
| Destructive ops | No rm -rf, sudo | No — speed bump only |
| Git push | Only loop master pushes | No — speed bump; still valuable (see below) |
| Issue author | Org-member filter (on by default; disable in `config.local.env`) | Yes — `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` |
| Turn limits | Per-job caps | Parent only — subagents take their own limits; none set |
| Budget limits | Per-job cap | Unverified across subagents |
| Concurrency | Max open issues + a single live worker per wake | Yes — `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) + the loop master's concurrency gate |
| Cluster blast radius | Worker-dedicated dev cluster (default points at the shared one) | Partly — `--env-file` from `$REPO_DIR` pins where deploys/resets land, even for the worktree-run deploy scripts; a delegated `Bash` session still reaches any context in the machine's kubeconfig |
| Secrets | Gitignored + denylist | No against delegation — a subagent `Read`s `config.local.env` directly, and `ANTHROPIC_API_KEY`/`GH_TOKEN` are already in the child's environment |

**Why the push separation still earns its place**: keeping `git push` out of the worker's tool
vocabulary means a confused or drifting session does not push to an unexpected branch or remote
in the ordinary course of its work. It is a speed bump against accident, not a control against
intent.

### Prauto executes unreviewed branch code

This is inherent, not incidental: test code must come from the branch to test the branch, so any
stage that runs integration or E2E executes code the branch authored, before a human has read it.
No arrangement of the deploy removes this; it is the cost of testing a branch at all.

- Branch-authored `conftest.py` and `tests/e2e` package scripts run **as the loop master** on the
  dev machine, not inside a builder.
- The branch's own `install.sh` / `build-image.sh` run **as the loop master** during the deploy
  stages, and their `docker build` runs over branch source, so branch build-time content
  (`package.json`, `next.config`, the Dockerfile, the chart) executes on the dev machine and
  in-cluster. This is the accepted cost of testing a branch's infra changes: proving them requires
  running them ([Branch image deploys](#branch-image-deploys)).
- All of this precedes `finalize_issue_pr` — it happens before the PR **exists**, so there is no
  point at which a human could have reviewed it first.

The one thing the branch cannot rewrite is which cluster it lands on: the `--env-file` resolves from
`$REPO_DIR`, so branch build code runs — but always against the worker's configured cluster, never
one of the branch's choosing.

---

## Integration with AI Scaffold

| Scaffold element | Integration |
|---|---|
| `CLAUDE.md` / `AGENTS.md` | Gives the worker full project context automatically; `AGENTS.md`'s Implementation Workflow is what the implementation phase runs |
| `.claude/settings.json` | Permission prompts do not apply — sessions run with permissions auto-approved; the denylist is prauto's own layer |
| `.claude/agents/` / `.codex/agents/` | The implementation phase delegates to the generator and reviewer subagents; their definitions govern their tools and turns, their bodies point at the canonical role definitions in `scaffold/roles/` |
| `.claude/workflows/` | `wf-minimal.js` drives the Claude binding's per-stage generate → review cycles (Codex expresses the equivalent orchestration in its worker prompt) |
| `scaffold/roles/` | Canonical generator/evaluator roles — the loop master's final review gate reads `reviewer.md` directly |
| `scaffold/contracts/` | `reviewer-verdict.schema.json` is the verdict schema the review gate validates against |
| `spec/` hierarchy | Analysis phase reads specs per `AGENTS.md` |

Prauto is self-contained — it does not modify `.claude/` files. The scaffold serves
interactive sessions; prauto serves unattended automation. The dependency runs one way and is
load-bearing: prauto's containment and turn bounds for delegated work are whatever the agent
definitions say they are.

---

## Meta-Agent Loop Bindings

This section is the reference implementation of the loop layer. It is binding-specific; the
contract above is the durable part and must hold across any binding.

### Reference binding: Hermes Agent

A meta-agent harness provides the scheduler, the concurrency inventory, and subagent isolation
that the v0.7 bash harness hand-rolled. The mapping:

| Loop-master step | Hermes primitive |
|---|---|
| Wake on a cadence | `cronjob` (schedule `every 4h`, from `PRAUTO_LOOP_MASTER_HERMES_SCHEDULE` in `config.env`) |
| Concurrency gate | `process(action='list')` (durable background processes) + a durable marker (a GitHub label or a state file) for "pending token reset" |
| Agent availability | a `terminal` pre-step running the [Agent Availability](#agent-availability) probes |
| Spawn worker | `terminal(background=true, notify_on_complete=true)` running `claude -p` / `codex exec` (via the `claude-code`/`codex` skills) with the phase goal; survives the tick |
| Spawn reviewer | a fresh, separate `claude -p` / `codex exec` invocation over the worker's diff (a new process is what makes generator ≠ reviewer real) |
| Plan-approval push | `attach_to_session` + `clarify`, or (preferred) the pull model: post the plan comment and poll on the next tick |

The loop master itself is a skill (`prauto-loop-master`) loaded by the cron job; it carries the
seven-step cycle, the phase-derivation logic, the agent-selection probes, and the dispatch
contracts. The worker/reviewer prompts are the `.prauto/prompts/` templates, translated into the
agent's invocation.

**Durability note**: a meta-agent scheduler tick is even less durable than the bash worker was —
the tick's own context dies on process exit and nothing persists in memory between ticks. The
worker must therefore be a **background process** (`terminal(background=true)`), not a subagent:
`delegate_task` children are discarded on session exit and would not survive the multi-hour
implementation run. The GitHub-as-SSOT principle ([Overview](#overview)) is the load-bearing
resumability mechanism — the next tick re-derives everything from GitHub. Do not weaken it when
re-binding the loop.

### Installing the loop-master cron job

The loop master is scheduled as a Hermes cron job. This is the concrete install guide for the
reference binding; the job is defined once and managed with `hermes cron`.

**Prerequisites**

- Hermes Agent installed. The loop master runs in whichever Hermes profile created the job — no
  dedicated profile is required. Each cron tick is a fresh session that passes `skip_memory=True`
  and runs with a scoped toolset, so sharing a profile with interactive sessions is safe; the
  loop master neither contends for the conversation nor leaks memory into it.
- The `prauto-loop-master`, `claude-code`, and `codex` skills available to that profile.
- The repo checked out locally (the job's `workdir`), with `config.local.env` and the `prauto:*`
  labels already in place.
- **The gateway running.** The cron scheduler only fires when the Hermes gateway is up; a job
  defined under a stopped gateway is inert. Install it once:

  ```bash
  hermes gateway install     # launchd/systemd user service, auto-starts at login
  hermes cron status         # must print "✓ Gateway is running — cron jobs will fire automatically"
  ```

**Job definition (canonical)** — every field is preserved in the env files so the job is
reproducible from the repo. Repo-level fields (what the job *is*) live in `config.env` (committed);
instance-identity fields (where/who runs it) live in `config.local.env` (gitignored — the repo is
public, so a checkout path or personal delivery target must not be committed).

| Field | Env var | File | Value |
|---|---|---|---|
| `schedule` | `PRAUTO_LOOP_MASTER_HERMES_SCHEDULE` | config.env | `every 4h` (24/7; a tick with no actionable issue is a no-op) |
| `name` | `PRAUTO_LOOP_MASTER_HERMES_NAME` | config.env | `DataSpoke PRauto loop master` |
| `skills` | `PRAUTO_LOOP_MASTER_HERMES_SKILLS` | config.env | `prauto-loop-master claude-code codex` |
| `enabled_toolsets` | `PRAUTO_LOOP_MASTER_HERMES_TOOLSETS` | config.env | `terminal file` (the loop master only probes CLIs, drives `gh`/`git`, and dispatches) |
| Hermes profile | `PRAUTO_LOOP_MASTER_HERMES_PROFILE` | config.local.env | the profile that hosts the job (e.g. `developer`) — a *record*, not a control |
| `workdir` | `PRAUTO_LOOP_MASTER_HERMES_WORKDIR` | config.local.env | the local checkout (loads `AGENTS.md`/`CLAUDE.md` into the tick) |
| `deliver` | `PRAUTO_LOOP_MASTER_HERMES_DELIVER` | config.local.env | `local` by default; a gateway platform (e.g. `telegram`) for per-tick summaries |

The `prompt` is fixed prose (not an env var) — see below.

**Create it** — in a Hermes session, invoke the `cronjob` tool, substituting each value from the
env files (`$VAR` below is a stand-in; the tool takes literal values):

```
cronjob(action="create", name="$PRAUTO_LOOP_MASTER_HERMES_NAME",
        schedule="$PRAUTO_LOOP_MASTER_HERMES_SCHEDULE",
        skills=["prauto-loop-master", "claude-code", "codex"],
        enabled_toolsets=["terminal", "file"],
        workdir="$PRAUTO_LOOP_MASTER_HERMES_WORKDIR",
        prompt="<one-tick instruction — see below>")
```

**The prompt** (self-contained; the cron session knows nothing of this repo):

> Run ONE tick of the PRauto loop master for the DataSpoke repository, following the
> `prauto-loop-master` skill. Repository: /path/to/dataspoke-baseline (base branch `dev`).
> Work the skill's tick procedure in order, stopping at the first terminal condition:
> concurrency gate → load config → agent availability (Claude, else Codex) → claim work →
> derive phase from GitHub → dispatch. Spawn the worker as a background `terminal` process
> (`background=true`, `notify_on_complete=true`); never use `delegate_task` for the worker.
> GitHub labels and comments are the single source of truth; derive everything from them. If
> no issue needs work, exit cleanly and spawn nothing. End with a one-line tick summary.

**Lifecycle** — manage the job from the CLI:

```bash
hermes cron list                     # job id + status
hermes cron run <job_id>             # fire one tick now (verify before waiting for the schedule)
hermes cron pause <job_id>           # stop firing, keep definition
hermes cron resume <job_id>          # resume
hermes cron edit <job_id> --schedule "every 2h"   # change cadence
hermes cron remove <job_id>          # delete
```

**Two invariants that shape the design**

- **The 3-minute interrupt.** Each cron tick is hard-interrupted at ~3 minutes. The loop
  master's tick is deliberately fast — gate, probe, derive phase, dispatch — so it completes well
  inside the budget. The actual coding work is the **background worker process**, which the tick
  starts and then abandons; the 3-minute bound applies to the tick, not to the worker it spawns.
  This is why the worker must be a background process rather than work done inside the tick.
- **Cron sessions pass `skip_memory=True`.** The loop master must not rely on agent memory across
  ticks — everything is re-derived from GitHub. This is exactly the GitHub-as-SSOT rule and is why
  the contract is written memory-free.

### Other bindings

The contract maps cleanly onto any scheduler that can spawn a headless coding-agent process:
cron/launchd, GitHub Actions (`claude-code-action` with a
`schedule:`/`issues: [labeled]` trigger), or a CI runner. Each must reproduce the seven-step
cycle and the four non-negotiables: GitHub-as-SSOT, the evidence-based plan gate, generator ≠
reviewer, and the `$REPO_DIR`-anchored cluster binding.

### Migration from the v0.7 bash harness

The v0.7 bash harness (`.prauto/heartbeat.sh` + `lib/*.sh`) and its two operational skills
(`prauto-run-heartbeat`, `prauto-check-status`) have been removed. The loop master is the sole
executor. What remains in `.prauto/` is the substrate-independent contract surface: the prompt
templates (`prompts/*.md`) and the configuration (`config.env`, `config.local.env`). Both the
former harness and the loop master consume these, so a future re-binding to another scheduler
(GitHub Actions, a CI runner) reuses them unchanged.
