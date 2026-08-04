# PRauto: Autonomous PR Worker

> **Document Status**: Specification v0.7 (2026-07-17)
> This document specifies "prauto" -- an autonomous PR worker that monitors GitHub issues,
> writes code via Claude Code CLI, and submits pull requests. Prauto extends the AI scaffold
> (`spec/AI_SCAFFOLD.md`) with unattended, cron-driven development automation.

---

## Table of Contents

1. [Overview](#overview)
2. [Worker Identity and Configuration](#worker-identity-and-configuration)
3. [Heartbeat Cycle](#heartbeat-cycle)
4. [Token Quota Checking](#token-quota-checking)
5. [Job State Machine](#job-state-machine)
6. [Issue Discovery Protocol](#issue-discovery-protocol)
7. [Claude Code Invocation](#claude-code-invocation)
8. [Dev Cluster and Deploys](#dev-cluster-and-deploys)
9. [PR Lifecycle](#pr-lifecycle)
10. [Write Idempotency](#write-idempotency)
11. [Security Model](#security-model)
12. [Integration with AI Scaffold](#integration-with-ai-scaffold)
13. [Future: GitHub Actions Migration](#future-github-actions-migration)

---

## Overview

### What prauto is

Prauto is a cron-triggered bash-based worker that automates the issue-to-PR pipeline. Each
heartbeat:

1. Checks whether Claude Code API tokens are available
2. Claims new work if under `PRAUTO_OPEN_ISSUE_LIMIT`
3. Processes **all** claimed issues (oldest first), each via a self-contained state machine

Directory structure and files live in `.prauto/`. See `AI_SCAFFOLD.md §Prauto` for a summary;
see the directory itself for current structure.

### Key design decisions

- **GitHub as single source of truth**: Every heartbeat derives its next action from **remote
  GitHub state** (labels, assignees, comments, review status). Local state files exist for
  debugging only.
- **No `--resume`**: Every Claude session starts fresh. The implementation prompt instructs
  Claude to check the branch for existing work and continue from there.
- **Ready-label timestamp as lifecycle anchor**: When `prauto:ready` is set (or re-set), the
  timestamp of that label event marks the start of the current lifecycle. All comment-scanning
  functions ignore comments before it, enabling clean restarts without manual cleanup.

### Execution environment

Runs on a local developer machine. Requires: `claude` CLI (authenticated), `gh` CLI
(authenticated), `git`, `jq`, and `cron`. Docker/K8s/cloud deployments are out of scope for v1.

---

## Worker Identity and Configuration

### Two configuration tiers

| File | Committed | Purpose |
|------|-----------|---------|
| `config.env` | Yes | Repo-level conventions: labels, branch prefix, max retries, model, org-member filter, reviewer |
| `config.local.env` | No | Instance identity (`PRAUTO_WORKER_ID`), Claude turn/budget limits, `ANTHROPIC_API_KEY`, `GH_TOKEN` |

A single machine may run multiple prauto instances (distinct worker IDs) sharing the same
GitHub credential. If `ANTHROPIC_API_KEY` or `GH_TOKEN` is empty, CLIs fall back to system
authentication.

---

## Heartbeat Cycle

Each heartbeat runs seven steps in order:

1. Acquire the PID-based lock (exit if already held).
2. Load `config.env` + `config.local.env`.
3. Secure secrets — back up `config.local.env` (a backup for restore, not a containment boundary;
   see [Security Model](#security-model)).
4. Check token quota — exit if exhausted; post a quota-paused comment on WIP issues.
5. Claim a new issue if under `PRAUTO_OPEN_ISSUE_LIMIT`.
6. Process all claimed issues (oldest first, self-contained state machine per issue):
   `prauto:done`/`prauto:failed` skip, `prauto:wip` derives phase and handles, `prauto:review`
   squash-finalizes or addresses feedback.
7. Restore secrets and release the lock (EXIT trap).

**Claim-first, then process-all**: Step 5 counts open issues assigned to this worker (excluding
ready-only restarted issues). If under limit, claims the oldest `prauto:ready` issue. Step 6
loops over all claimed issues.

**Worktree isolation**: Every Claude session runs in a dedicated git worktree. The main repo
directory is never the working directory during Claude invocations.

**Cron**: Recommended every 30 minutes during working hours.

---

## Token Quota Checking

Two-step probe: (1) `claude auth status`, (2) minimal 1-turn dry-run. If either fails with
quota error, heartbeat exits.

- No WIP issue: exit cleanly, retry next heartbeat
- WIP issue exists: post "Paused" comment (with marker), exit. Retry counter not incremented.
- On next heartbeat (quota restored): post "Resumed" comment before continuing.

---

## Job State Machine

### Phases

Minor issues flow `analysis → implementation → integration-fix → pr → complete`. Non-minor
issues insert a `plan-approval` gate between `analysis` and `implementation`; from
`plan-approval`, an approval advances, a counter-proposal loops back to re-analysis, and no
response waits until the next heartbeat.

Phase is always derived fresh from GitHub -- never read from local state.

| Phase | Description |
|-------|-------------|
| `analysis` | Claude reads issue + codebase, produces a plan |
| `plan-approval` | Wait for human approval (retries not counted) |
| `implementation` | Claude writes code, runs unit tests, commits |
| `integration-fix` | Run integration tests; on failure, Claude fixes (up to N attempts) |
| `pr-review` | Claude addresses reviewer feedback on existing PR |
| `pr` | Push branch, create/update PR |

### The plan gate is evidence-based

Only `minor` issues skip `plan-approval`, and **the issue body does not decide that**. The
`### Change Size` field an author fills in is a hint. `CLAUDE.md §Implementation Workflow` lets a
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
`CLAUDE.md` forbids — *never self-classify a task as "trivial" to skip planning* — and by an author
who has not yet seen the plan. Deferring the judgment to the analysis phase is better evidence, though
it remains a Claude session classifying its own plan, not an escape from self-classification.

### Phase derivation from GitHub

On every heartbeat (comment checks scoped to current lifecycle):

1. PR exists for this branch -> `pr`
2. `prauto:plan-review` label present -> `plan-approval`
3. Plan comment exists + "go ahead" reply -> `implementation`
4. Plan comment exists + no approval -> `plan-approval`
5. No plan comment -> `analysis`

### Retry tracking

Each heartbeat posts a marker comment on the issue. `count_heartbeat_comments()` counts markers
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

## Claude Code Invocation

### Multi-phase execution model

| Phase | Tools | Max turns |
|-------|-------|-----------|
| Analysis | Read + Write (plan file only) + limited git | `PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS` |
| Implementation | Read + Write + Edit + `Agent` + `Workflow` + limited Bash (git; `uv sync`/`uv run` pytest, python3, ruff, mypy; `npm run`, `npx prettier`, `npx tsc`, `npx eslint`, `pnpm`) | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` (parent loop only) |
| Integration fix | Same as implementation | `PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX` |
| PR review | Same as implementation | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` |
| Squash commit / Feedback response | No tools (text only) | 1 |

**Denylist (all phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`,
`gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`. It binds
the parent session only — see [Security Model](#security-model) for what it does and does not
enforce.

**Branch-based continuity**: On restart, the prompt instructs Claude to check for existing
commits on the branch and continue from there.

### The implementation phase runs the CLAUDE.md workflow

Prauto's implementation phase is the unattended form of `CLAUDE.md §Implementation Workflow`
steps 4–9: it drives `.claude/workflows/wf-minimal.js` (`args = {plan, stages, security}`), which
runs each reviewed stage as generator → adversarial reviewer → one fix pass on REVISE, escalating
when a REVISE persists (`k8s-helm` is unreviewed, per step 9). `security-reviewer` runs in parallel
with `reviewer` on stages named in `security`. The analysis phase emits `stages` (in plan order,
inner arrays for concurrent stages) and `security` alongside its plan.

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

Deploys stay orchestrator-owned. Prauto's analysis phase never emits `k8s-helm` as a stage: that
stage would deploy under whatever its kubeconfig points at, ignoring the worker-cluster binding and
the api-then-frontend ordering ([Branch image deploys](#branch-image-deploys)), and it carries no
reviewer. All cluster mutation runs from `.prauto/` against `$PRAUTO_DEV_ENV_FILE`.

**Divergence from the official recommendation**: the Agent SDK, not the CLI, is what Anthropic
recommends for unattended multi-agent pipelines. Prauto stays on the CLI because prauto is bash,
and rewriting it around the SDK is a larger change than the automation currently justifies. The
tradeoff is real and is accepted knowingly: prauto gets no SDK-level session control, and the
containment gaps in [Security Model](#security-model) are a direct consequence of driving
multi-agent work through a CLI whose flags bind only the parent.

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
`./helm-charts/bin/health-check.sh --env-file $PRAUTO_DEV_ENV_FILE` comes back red or the cluster is
absent, prauto runs a full `install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE` and re-checks;
the cluster stages are skipped only if **provisioning itself** fails. Gated by
`PRAUTO_CLUSTER_PROVISION_ENABLED` (default `true`). Every `install.sh`/`health-check.sh` invocation
carries `--env-file $PRAUTO_DEV_ENV_FILE`; without it both default to `helm-charts/.env.dev`, the
shared cluster the per-worker binding exists to avoid.

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

After implementation, push branch, check for existing PR, create one if none exists (with
`prauto:review` label, assignee, optional reviewer).

### PR review handling

Issues with `prauto:review` label are checked for unaddressed non-prauto comments. The
feedback-addressed marker breaks the re-pickup loop; new reviewer comments after the marker
make the PR actionable again.

### Test execution

Prauto runs the unattended form of the protocol in [`TESTING.md`](TESTING.md), which is
authoritative for layers, commands, and constraints. Stages run in order; each is skipped when
the diff does not reach its layer. Each stage names its **actor**: Claude runs a stage from its
prompt template inside the session's tool whitelist, while the orchestrator runs a stage from
`.prauto/` and invokes Claude only for fix sessions.

**Pre-flight gate** *(orchestrator)*: `./helm-charts/bin/health-check.sh` runs before any
integration work. On red, prauto provisions its own cluster and re-checks
([Provisioning](#provisioning)); the integration and E2E stages are **skipped, not failed** only
if provisioning fails. An unprovisionable cluster is evidence about the infrastructure, not the
branch, so failing it would burn retries against unrelated code.

**Environment**: the orchestrator's integration and E2E stages source the worker's env file
(`$PRAUTO_DEV_ENV_FILE`, resolved under `$REPO_DIR`) via `set -a` (the file carries no `export`
prefixes) and hold the dev-env lock at `$DATASPOKE_DEV_LOCK_URL`.

**Stage 1 -- Static gates** *(Claude)*: `uv run ruff check src/ tests/` and `uv run mypy src/`,
invoked as **checks, never `--fix`** — prauto verifies the author-run gate rather than mutating
the diff until it passes. Frontend-touching work adds `npx tsc --noEmit` and `npx eslint src/`
from `src/frontend/`; diffs touching `tests/e2e/` add `pnpm -C tests/e2e typecheck`. These are
the four author-run gates of [`TESTING.md §CI Behavior`](TESTING.md#ci-behavior); no
`.github/workflows/` exists, so they are the only thing standing between prauto and a red `dev`.

**Stage 2 -- Unit** *(Claude)*: `uv run pytest tests/unit/`; frontend-touching work also runs
`pnpm -C src/frontend test` (offline, mocked). Needs no cluster and no lock.

**Stage 3 -- Integration fix loop (pre-push)** *(orchestrator; Claude for fixes)*: after
implementation, under the dev-env lock. A diff touching `src/{api,backend,shared}` deploys the
branch's API first ([Branch image deploys](#branch-image-deploys)) so the tests reach the
branch's code rather than a stale image. Then spot (`tests/integration/spot/`) and api-wired
(`tests/integration/api_wired/`) run as **two separate groups**, never mixed — a mixed run puts
competing Airflow load on the cluster and flakes on timing. The split binds every integration
invocation, Stage 5 included. Failures feed Claude's fix loop up to
`PRAUTO_INTEGRATION_FIX_MAX_RETRIES`.

**Stage 4 -- E2E (Playwright)** *(orchestrator; Claude for fixes)*: runs when the diff touches
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

**Stage 5 -- Final test report (post-push)** *(orchestrator)*: runs unit + integration tests —
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

**Steps**: Rebase on base -> generate squash commit message (1-turn Claude, no tools) ->
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

The implementation phase grants `Bash(uv run python3 *)`, which is general-purpose code
execution: a session that shells out from Python reaches every command the denylist names —
`kubectl`, `helm`, `curl`, `wget`, `git push`. Sessions also run with
`--dangerously-skip-permissions`, and no path restriction bounds `Write`/`Edit`. The grant is
load-bearing (prauto writes Python), so the gap is inherent to the phase's purpose rather than
an oversight to be patched by trimming the list.

**Delegation removes even the speed bump.** `--allowedTools`/`--disallowedTools` do not propagate
to subagents; a subagent's own `.claude/agents/<name>.md` frontmatter governs. The project's
generators (`backend`, `test`, `k8s-helm`) each declare unrestricted `Bash` with no denylist, so
granting `Agent` means delegated work reaches `kubectl`, `helm`, `git push`, and `curl` directly —
no reach-around needed. `DENY_TOOLS` binds the parent session and nothing beyond it. The same holds
for `Read(.prauto/config.local.env)`: a subagent reads that file directly, exposing
`ANTHROPIC_API_KEY` and `GH_TOKEN`. That denial was never the real boundary regardless — both are
already exported into every Claude child's environment — so delegation widens an existing exposure
rather than opening a new one.

Turn and budget caps thin out the same way. `--max-turns` bounds the parent loop only; subagents
take `maxTurns` from frontmatter and no project agent sets one, so `PRAUTO_CLAUDE_MAX_TURNS_*`
stops bounding delegated work. Whether `--max-budget-usd` aggregates across subagents is
**unverified** — treat delegated spend as unbounded until someone establishes otherwise.

**The real boundary is the machine the heartbeat runs on and the credentials available to it.**
Scope those — not the whitelist — when deciding what a prauto instance may reach. The per-worker
dedicated cluster ([Dev Cluster and Deploys](#dev-cluster-and-deploys)) bounds *where prauto's
deploys land*: every `install.sh`/`health-check.sh` call carries `--env-file $PRAUTO_DEV_ENV_FILE`
resolved from `$REPO_DIR`, so even though the branch deploys execute the worktree's own build/deploy
scripts, a bad chart or a destructive reset lands only on the worker's cluster, and the credentials
resolved from `$REPO_DIR` stay out of branch-authored reach. It does not bound what a delegated
session *may* do:
a subagent with `Bash` inherits the dev machine's kubeconfig and can `kubectl config use-context`
any cluster the developer can reach. And the binding is only as strong as its configuration — the
default `PRAUTO_DEV_ENV_FILE` is `helm-charts/.env.dev`, the shared cluster, so containment holds
only once a worker points it at a dedicated one in `config.local.env`.

| Layer | Restriction | Enforced? |
|-------|-------------|-----------|
| Claude CLI tools | Phase-specific whitelists | No — speed bump; `uv run python3` reaches around it, `Agent` bypasses it |
| Network access | No web fetch, curl, wget | No — `npx`/`pnpm dlx` fetch and execute arbitrary packages |
| Cluster access | No kubectl, helm for the parent session | No — same reach-around; generators grant `Bash` outright; orchestrator deploys via `install.sh` |
| Destructive ops | No rm -rf, sudo | No — speed bump only |
| Git push | Only orchestrator pushes (`git-ops.sh`) | No — speed bump; still valuable (see below) |
| Issue author | Org-member filter (on by default; disable in `config.local.env`) | Yes — `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` |
| Turn limits | Per-job caps | Parent only — subagents take `maxTurns` from frontmatter; none set |
| Budget limits | Per-job cap | Unverified across subagents — `--max-budget-usd` |
| Concurrency | Max open issues + PID lock | Yes — `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) |
| Cluster blast radius | Worker-dedicated dev cluster (default points at the shared one) | Partly — `--env-file` from `$REPO_DIR` pins where deploys/resets land, even for the worktree-run deploy scripts; a delegated `Bash` session still reaches any context in the machine's kubeconfig |
| Secrets | Gitignored + denylist + temp backup | No against delegation — a subagent `Read`s `config.local.env` directly, and `ANTHROPIC_API_KEY`/`GH_TOKEN` are already in the child's environment |

**Why the push separation still earns its place**: keeping `git push` out of the prompt's tool
vocabulary means a confused or drifting session does not push to an unexpected branch or remote
in the ordinary course of its work. It is a speed bump against accident, not a control against
intent.

### Prauto executes unreviewed branch code

This is inherent, not incidental: test code must come from the branch to test the branch, so any
stage that runs integration or E2E executes code the branch authored, before a human has read it.
No arrangement of the deploy removes this; it is the cost of testing a branch at all.

- Branch-authored `conftest.py` and `tests/e2e` package scripts run **as the orchestrator** on the
  dev machine, not inside a builder.
- The branch's own `install.sh` / `build-image.sh` run **as the orchestrator** during the deploy
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
| `CLAUDE.md` | Gives prauto full project context automatically; its Implementation Workflow is what the implementation phase runs |
| `.claude/settings.json` | Permission prompts do not apply — sessions run `--dangerously-skip-permissions`; `DENY_TOOLS` is prauto's own layer |
| `.claude/agents/` | The implementation phase delegates to the generator and reviewer subagents; their frontmatter governs their tools and turns |
| `.claude/workflows/` | `wf-minimal.js` drives the implementation phase's per-stage generate → review cycles |
| `.claude/skills/` | Available if Claude detects matching context |
| `spec/` hierarchy | Analysis phase reads specs per CLAUDE.md |

Prauto is self-contained in `.prauto/` -- does not modify `.claude/` files. The scaffold serves
interactive sessions; prauto serves unattended automation. The dependency runs one way and is
load-bearing: prauto's containment and turn bounds for delegated work are whatever
`.claude/agents/` frontmatter says they are.

---

## Future: GitHub Actions Migration

Prauto's design maps directly to `claude-code-action`:

| Prauto (local) | `claude-code-action` (GH Actions) |
|---|---|
| `heartbeat.sh` (cron) | `schedule:` trigger |
| `lib/issues.sh` | `issues: [labeled]` event trigger |
| `prompts/*.md` | `prompt:` / `claude_args:` inputs |
| `--allowedTools` / `--disallowedTools` | `claude_args:` |
| `config.env` | Workflow environment variables |
| `config.local.env` | GitHub Actions secrets |
