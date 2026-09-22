# PRauto: Autonomous PR Worker

> This document specifies "prauto" -- an autonomous PR worker that monitors GitHub issues,
> writes code via a headless coding-agent CLI, and submits pull requests. Prauto extends the AI
> scaffold (`spec/AI_SCAFFOLD.md`) with unattended, scheduled development automation.

> **Architecture**: Prauto separates a durable, agent-agnostic *contract* from a deterministic
> *executor* and a thin *scheduler*. The contract defines labels, phase state, the evidence-based
> plan gate, generator ≠ reviewer, deploy ordering, GitHub-as-SSOT, and the security model. The
> executor (`.prauto/heartbeat.sh` with `.prauto/lib/*.sh`) owns each tick's lock, configuration,
> agent probes and selection, dispatch, and finalization. A scheduler supplies cadence and
> supervision; the Hermes binding is one supported scheduler integration (an agent supervisor
> that reports to Slack and detaches a background monitor), while the executor remains the sole
> owner of every tick step.

---

## Table of Contents

1. [Overview](#overview)
2. [Worker Identity and Configuration](#worker-identity-and-configuration)
3. [Executor Cycle](#executor-cycle)
4. [Agent Availability](#agent-availability)
5. [Job State Machine](#job-state-machine)
6. [Issue Discovery Protocol](#issue-discovery-protocol)
7. [Worker Agent Invocation](#worker-agent-invocation)
8. [Dev Cluster and Deploys](#dev-cluster-and-deploys)
9. [PR Lifecycle](#pr-lifecycle)
10. [Write Idempotency](#write-idempotency)
11. [Security Model](#security-model)
12. [Integration with AI Scaffold](#integration-with-ai-scaffold)
13. [Executor and Scheduler](#executor-and-scheduler)

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
- **Executor** — performs a tick: concurrency control, configuration, agent availability and
  selection, issue processing, worker/reviewer dispatch, and finalization.
- **Scheduler** — supplies cadence and launches the executor; the reference Hermes binding adds
  an agent supervisor that reports to Slack and monitors the run. It does not select agents,
  inspect quota, or perform issue work.

### Key design decisions

- **GitHub as the SSOT for phase state**: Every wake derives its *next action* from **remote
  GitHub state** (labels, assignees, comments, review status). Phase, retry count, and plan
  approval are re-derived from GitHub on each tick; the executor carries no in-memory phase state
  between ticks. This is what makes the executor tick resumable across a fresh-process scheduler
  launch.
- **Continuity is not GitHub-only**: What a resume *continues* lives outside GitHub phase state.
  Work-product continuity flows through (1) committed checkpoints on the issue branch (pushed,
  linked to the issue, and posted as commit-link comments), (2) agent-native sessions (Claude
  `--session-id`, Codex `thread_id`), and (3) the local `native-sessions/` anchor that gates a
  Codex resume. GitHub is the source of truth for *phase state*; it is not the only carrier of
  *work product*.
- **No uncommitted resume**: Each worker session starts fresh unless the agent-native quota
  resume path is active. The implementation prompt instructs the agent to check the branch for
  existing committed work and continue from there. When the executor regains control, it
  best-effort pushes committed checkpoints, links the branch to the issue, and posts idempotent
  issue comments containing commit links. Uncommitted work from a session that died mid-run is
  discarded with the worktree and is not resumed.
- **Ready-label timestamp as lifecycle anchor**: When `prauto:ready` is set (or re-set), the
  timestamp of that label event marks the start of the current lifecycle. All comment-scanning
  ignores comments before it, enabling clean restarts without manual cleanup.

### Execution environment

Runs on a local developer machine. Requires: a coding-agent CLI with a logged-in account
(Claude Code and/or Codex — both use login-based OAuth, not API tokens), the `gh` CLI
(authenticated), `git`, and a scheduler capable of launching the executor. Docker/K8s/cloud
deployments are out of scope for v1.

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

### GitHub identity has two independent axes

`GH_TOKEN` authenticates `gh`-driven GitHub API calls only — issue labels, comments, PR creation.
It does not authenticate `git push`, which goes over SSH and resolves whatever account the local
`git`/`gh` SSH identity belongs to. `PRAUTO_GIT_AUTHOR_NAME` / `PRAUTO_GIT_AUTHOR_EMAIL` set
commit *authorship* independently of both (applied via `git commit --author=...` in the worker's
system prompt). Running prauto under a dedicated bot GitHub account requires all three — API
token, SSH key, and author identity — to resolve to that same account, or commits, pushes, and
API actions end up attributed to different identities. See `.prauto/README.md` §Optional:
Dedicated GitHub Bot Account for the setup.

`PRAUTO_AGENT` selects the worker's coding agent: `claude`, `codex`, or `auto` (the default;
Claude first, Codex fallback — see [Agent Availability](#agent-availability)). Claude turn and
budget configuration applies only to Claude invocations. Codex has neither the Claude
`--max-turns` nor `--max-budget-usd` interface; the executor does not translate or pass those
flags to Codex.

### Codex model contract

Codex model availability is account- and client-specific. PRauto and the Codex role bindings
therefore omit an explicit model by default and inherit the authenticated account's Codex default.
`PRAUTO_CODEX_MODEL` is an optional fresh-session override for an operator whose client and account
explicitly support it; it is not a portable default and role bindings must not hard-code one. A
model override and `PRAUTO_CODEX_EFFORT` form one pair: both must be configured to override the
account default, and an effort without a model is invalid.

The executor preflights an explicit model override and `PRAUTO_CODEX_EFFORT` before dispatch. A
rejected, malformed, or unsupported override is a configuration/account-compatibility failure: it
posts diagnostic evidence and leaves the work item non-ready; it must not start a worker, consume
a retry, silently substitute a model, or classify the outcome as quota exhaustion. An absent model
override is valid and means to use the account default.

---

## Executor Cycle

Each executor tick runs seven steps in order. Worker and reviewer agents run only when the
executor dispatches them in step 6.

1. **Concurrency gate** — exit if a worker subagent is already running or pending (a prior
   tick's worker that is mid-run or waiting out a token reset). Enforced by the executor's
   durable PID lock; see [Security Model](#security-model).
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
7. **Finalize** — the executor (not the worker) pushes, opens/updates PRs, posts test
   results, and swaps labels.

**Claim-first, then process-all**: Step 4 counts open issues assigned to this worker as
**claimed** — carrying any active `prauto:` label other than a ready-only restart, and excluding
the terminal `prauto:failed` and `prauto:done` labels. If under limit, claims the oldest
`prauto:ready` issue. Step 5 loops over that same claimed set. One definition of "claimed" serves
both: a terminal issue is work this worker has already finished with, so counting it toward the
slot would hold a `PRAUTO_OPEN_ISSUE_LIMIT` slot that nothing can ever release, wedging every
later wake into a no-op until a human edits the labels.

**Worktree isolation**: Every worker session runs in a dedicated git worktree. The main repo
directory is never the working directory during worker invocations.

**Cadence**: The user-specified interval (hourly in the reference binding). The schedule is owned by the
scheduler binding, not by `config.local.env`.

---

## Agent Availability

The executor probes agent availability with a two-step check per candidate, in order:

1. **Claude Code**: `claude auth status` (checks a logged-in OAuth session), then a minimal
   one-turn dry-run (`claude -p "Reply with exactly: OK" --max-turns 1 --allowedTools ""`).
2. **Codex**: presence of a CLI OAuth session (`~/.codex/auth.json`), then a minimal JSONL
   dry-run (`codex exec --json "Reply with exactly: OK"`).

Both agents are authenticated by **login-based subscription accounts, not API tokens**, so the
probe must invoke the CLI itself — there is no token to inspect out-of-band. If `PRAUTO_AGENT`
is `claude` or `codex`, only that agent is probed; `auto` probes Claude then Codex.

Outcomes:

- An agent passes → it is selected for this wake.
- Neither passes → the executor exits. If a WIP issue exists, post a "Paused" comment (with
  marker); the retry counter is not incremented. On the next wake with an agent available,
  post "Resumed" before continuing.

A dry-run timeout (network slowness) is **not** treated as exhausted — proceed anyway.

### Quota-pause and resume

A worker that dies mid-run on a rate/session-limit exit pauses rather than fails: the executor
posts a pause marker carrying the session id, and the next wake resumes the SAME session on the
SAME agent once quota resets. The marker:

```
prauto(<worker>): Paused — <agent> quota exhausted. Will resume automatically on the next quota window.

To abandon this session and restart from scratch instead of resuming, comment "abandon previous session".

prauto:quota-paused
prauto:agent=<agent>
prauto:session=<session-id>
```

The `prauto:quota-paused` / `prauto:agent=` / `prauto:session=` lines are the machine-readable
state the next wake parses; they are plain text, not HTML comments — they are not secret
(comments require write access, and the session id is already in the local state dir), so hiding
them buys nothing while costing parse robustness.

On the next wake, per WIP issue, derived fresh from GitHub:

1. **Paused?** — the latest prauto pause/resume/restart marker is a pause → handle it.
2. **Abandon override?** — a non-prauto comment reading `abandon previous session` posted after
   the pause marker forces a **restart**, not a resume: a fresh session, with the agent re-selected
   under `PRAUTO_AGENT` (so `auto` falls through to Codex if the paused agent's quota has not
   reset). Posts "Restarting".
3. **Quota reset?** — the paused agent's probe passes → post "Resumed" and **resume** the same
   session, using that agent's native resume command and the captured session id.
4. **Still down** — do nothing, exit; the pause never increments the retry counter.

Session identity is agent-native:

| Agent | Fresh-session identity | Resume identity |
|-------|------------------------|-----------------|
| Claude Code | The executor generates a UUID and supplies it with Claude's `--session-id` option. | That UUID is supplied to Claude's `--resume` option. |
| Codex | Codex creates the thread id. The executor captures `thread_id` from the `thread.started` JSONL event emitted by the fresh invocation. | That captured thread id is supplied to `codex exec resume`. |

The executor persists the captured id before it can post a resumable quota marker. A Codex run
that exits before emitting `thread.started` has no resumable identity and must be restarted rather
than represented as a same-session resume. Resuming is same-agent only: a session cannot migrate
agents, and agent-switch is reachable only through the abandon+restart path. The `plan-approval`
phase is exempt from resume — it waits on a human, not on agent quota.

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

A local state file (`.prauto/state/retry-count-<issue>.json`) tracks the retry counter for an
issue's current ready-label lifecycle, identified by `READY_LABEL_TIMESTAMP`. A missing,
malformed, unreadable, or timestamp-mismatched record resets safely to zero and can be overwritten,
so a newly re-queued issue cannot inherit attempts from an earlier lifecycle. The counter is read
and incremented only in the **normal dispatch path** — the code path reached when
no quota-pause marker is present. Quota-pause cycles (marker → resume → re-pause) bypass this path
entirely, so a quota death never burns a retry slot. Only genuine attempt starts (fresh dispatches
and retries after non-quota failures) advance the counter. The plan-approval path also bypasses
the counter — the first implementation dispatch after plan approval is always free.

If PRauto cannot persist a current-lifecycle counter, it posts no heartbeat and dispatches no
agent; it leaves the issue pending for a later wake.

At `PRAUTO_MAX_RETRIES_PER_JOB` (default 4), the issue is abandoned. The counter is checked
**before** incrementing: a dispatch at count 3 with max 4 checks `3 >= 4` → false → proceeds
→ increments to 4. The next dispatch sees `4 >= 4` → abandon.

### Refunding a truncated or infrastructure-blocked attempt

A dispatch is counted at the moment it starts, before its outcome is known. Two distinct outcomes
must be handed back rather than kept, through the same mechanism and the same cap.

The first is a **harness-truncated session**: the implementation phase's agent CLI can terminate
its own session on its background-task wait ceiling — the CLI exits 0 and prints
`Background tasks still running after <N>s; terminating.` — before the workflow ever emits its
`PRAUTO_WORKFLOW_OUTCOME` sentinel, even though the stages that already ran have committed their
work to the branch. That is a harness limitation (see `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` in
[Worker Agent Invocation](#worker-agent-invocation)), not a worker failure, so the attempt it
consumed is refunded back onto the counter rather than charged against the job.

**Detection reads the executor's own wall-clock measurement of the session** — never the agent's
answer, the session stderr, or the merged output. The worker authors its report and runs unreviewed
branch code (see [Prauto executes unreviewed branch code](#prauto-executes-unreviewed-branch-code));
a check keyed on anything a worker can write would let it refund its own retry on every wake, netting
each dispatch to zero, never reaching `PRAUTO_MAX_RETRIES_PER_JOB`, and looping on the issue forever
while holding an open-issue slot. A session whose elapsed time reaches the configured ceiling must
have been terminated by the CLI; the elapsed span is the one account of the session the worker cannot
author. A ceiling of `0` means wait indefinitely, so no truncation is possible.

The second is an **infrastructure-blocked regression** that consumed a dispatch — a wedged dev-env
lock, a provisioning or health-check failure, not a defect in the worker's code — refunded through
the same `refund_retry_count` path under the same `PRAUTO_MAX_REFUNDS_PER_JOB` cap. The
classification is read from `CLUSTER_REGRESSION_EXIT=2`, an exit code the **executor** itself
assigns when it judges a regression infrastructure-blocked
([Deterministic environmental-flake exception](#deterministic-environmental-flake-exception));
the worker session never sets or observes it.
That executor-only origin is what makes the signal legitimate here, for the same reason the
wall-clock measurement above is: nothing the worker authors decides whether its own attempt comes
back.

**Two bounds keep the abandonment guarantee intact even with refunds in play**:

- **Only the path that consumed an attempt may return it.** The normal dispatch path is the sole
  path that increments the counter; only it may refund. The plan-approval and quota-resume paths
  already bypass the counter entirely, so a refund reachable from either would hand back an
  attempt some earlier dispatch took, not one of its own.
- **`PRAUTO_MAX_REFUNDS_PER_JOB` (default 2) caps refunds per ready-label lifecycle.** The refund
  floors at zero and is recorded in the same lifecycle-scoped state file as the counter. Past the
  cap, attempts are consumed normally regardless of cause, so no repeating fault — CLI or
  otherwise — can make a job unabandonable.

The abandonment guarantee therefore holds in terms of **net** attempts, not raw dispatch count: an
issue is abandoned once `(dispatches that consumed an attempt) - (refunds granted)` reaches
`PRAUTO_MAX_RETRIES_PER_JOB`, and the refund side of that subtraction is itself bounded by
`PRAUTO_MAX_REFUNDS_PER_JOB`.

The worker runs unreviewed branch code as the executor's own OS user, and its tool grant includes an
interpreter, so nothing it can write is trustworthy input to this decision — not its report, and not
the session stderr sidecar under the state tree either. `DENY_TOOLS` blocks those paths as defence in
depth, but a tool denylist is not an OS boundary. What bounds the refund is therefore not a fence
around the inputs but the two controls above, both enforced by the executor itself: the
`RETRY_COUNT_CONSUMED` gate and `PRAUTO_MAX_REFUNDS_PER_JOB`. Stalling to the ceiling to earn a refund
costs a worker a full ceiling of wall-clock per attempt and is capped either way.

### Job completion and abandonment

| Scenario | Actions |
|----------|---------|
| New issue -> PR | Push and create or update the PR in `prauto:wip`; run the required post-PR regression and, when needed, its bounded targeted retry; move the issue and PR to `prauto:review` only after readiness succeeds |
| PR feedback | Return to `prauto:wip`, address with commits, push, run the required post-PR regression and, when needed, its bounded targeted retry; restore `prauto:review` only on readiness success |
| Workflow ESCALATE | Do **not** finalize a PR; remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment naming the escalating stage and its findings |
| Max retries | Remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment (naming any accumulated infrastructure-block reasons, see [Deterministic environmental-flake exception](#deterministic-environmental-flake-exception)) |

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

To restart an issue: remove all `prauto:` labels, then apply a fresh `prauto:ready` label,
unassign the worker, and delete the working branch/PR. The fresh ready-label timestamp establishes
a new comment and retry lifecycle, so comment-scanning functions automatically ignore stale
comments and the retry counter cannot inherit attempts from the previous lifecycle.

---

## Worker Agent Invocation

### Multi-phase execution model

The worker is a headless coding-agent session (Claude Code or Codex), one per phase. The executor
supplies the phase's goal and bounds; the worker runs the phase's prompt template
(`.prauto/prompts/`) with the agent's native tool scoping.

### Agent execution adapters

The executor keeps a separate CLI adapter for each agent. A shared phase contract does not imply
shared command-line flags or output formats.

- **Claude Code** receives its native session id, prompt/system-prompt, tool-scoping, turn-cap,
  and optional budget-cap arguments. Its structured result is parsed using Claude's JSON output
  contract.
- **Codex fresh sessions** run with `codex exec --json` and the workspace-write sandbox. When the
  operator configured a compatible model-and-effort override pair, the executor passes it;
  otherwise Codex inherits the authenticated account's defaults. Codex stdout is JSONL: the
  executor reads the `thread.started` event to persist its `thread_id`, then reads terminal events
  for the worker result and quota classification. It must not fabricate a Codex id or pass an
  executor-generated id as though Codex had accepted it.
- **Codex resumed sessions** run with `codex exec resume --json <thread-id> <prompt>`. Resume
  accepts the prior Codex identity and relies on that native thread's established model and
  reasoning settings. It does not reinject model/effort or accept Claude session, tool, turn,
  budget, or fresh-session sandbox flags. The executor uses the JSONL stream on a resumed run for
  result and quota classification as well.

JSONL is a protocol boundary, not display text: parsers select records by their event type and
fields, preserve the raw stream as diagnostic evidence, and do not infer a thread id or quota
state from human-readable prose when the required structured event is absent.

### Implementation-phase Claude CLI environment

The executor applies two Claude CLI environment variables to the **implementation invocation
only** — passed per-invocation, never exported process-wide. Analysis, pr-review,
squash-commit, feedback-response, and the two fix sessions (integration-fix, E2E-fix) are
unaffected and keep the CLI's own defaults.

- **`CLAUDE_CODE_WORKFLOWS`** (default `1`). Claude Code registers its `Workflow` tool only on
  this opt-in; `--allowedTools` cannot re-enable a tool the session never registered. Without it,
  the implementation phase's `wf-minimal` binding — the generator → reviewer per-stage loop
  described in [The implementation phase runs the AGENTS.md workflow](#the-implementation-phase-runs-the-agentsmd-workflow) —
  is unsatisfiable. Scoping the grant does not narrow the delegation consequence
  [Security Model](#security-model) already accepts: only `Workflow` is gated by this variable,
  while the subagent tool is registered by CLI default, and PR review carries the same tool grant
  as implementation — so delegated work escapes `DENY_TOOLS` in both phases. The phases whose
  grant is genuinely narrower are the two fix sessions, via the hard `--disallowedTools` block.
- **`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`** (default 4 hours, finite). How long the session waits
  for its background workflow before the CLI terminates it. The CLI's own 600s default cuts a
  normal implementation run short — `wf-minimal` runs as a background task, so a real run routinely
  outlives 600s without being wedged. The configured default is generous but deliberately finite,
  not `0` (wait forever): an indefinite wait would let a genuinely wedged background task hold the
  executor's PID lock for the full `PRAUTO_AGENT_TIMEOUT_SECS`, and would make the
  truncation-refund classification in [Retry tracking](#retry-tracking) unreachable — a session
  that never returns can never emit the stderr signature that classifies it as a harness fault
  rather than a worker failure.

The fix sessions still lose the delegation tools (`Agent`, `Workflow`, `Task`) through the hard
`--disallowedTools` block regardless of either variable — that denial is unchanged and independent
of the implementation-phase environment.

| Phase | Tools | Turn cap |
|-------|-------|-----------|
| Analysis | Read + Write (plan file only) + limited git | `PRAUTO_MAX_TURNS_ANALYSIS` |
| Implementation | Read + Write + Edit + subagents + workflow + limited Bash (git; `uv sync`/`uv run` pytest, python3, ruff, mypy; `npm run`, `npx prettier`, `npx tsc`, `npx eslint`, `pnpm`) | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Integration fix / E2E fix | Same allowed-tools list as implementation; Claude sessions additionally add `Agent`, `Workflow`, and `Task` to `--disallowedTools` (see the denylist note below). The repair contract requires foreground verification within the bounded session; only the Claude delegation-tool denial is mechanically enforced. | `PRAUTO_MAX_TURNS_INTEGRATION_FIX` / `PRAUTO_MAX_TURNS_E2E_FIX` |
| PR review | Same as implementation | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Squash commit / Feedback response | No tools (text only) | 1 |

**Denylist (all phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`,
`gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`. Integration
and E2E fix sessions add `Agent`, `Workflow`, and `Task` on top of this list for Claude. Those
Claude `--disallowedTools` entries enforce that a fixed repair session cannot invoke the
corresponding delegation tools. The allowed-tools list (`--allowedTools`) is not itself a removal
under `--dangerously-skip-permissions`, so it is a manifest rather than a security boundary. Tool
flags bind the parent session only; Codex has no equivalent per-session tool-deny mechanism — see
[Security Model](#security-model) for what this does and does not enforce.

**Branch-based continuity**: On restart, the prompt instructs the agent to check for existing
commits on the branch and continue from there.

### The implementation phase runs the AGENTS.md workflow

Prauto's implementation phase is the unattended form of `AGENTS.md §Implementation Workflow`
steps 4–9: it drives the generator → adversarial-reviewer → one-fix-pass loop over the plan's
`stages` (with `security` flagging stages that need `security-reviewer` in parallel). In the
Claude binding this is `.claude/workflows/wf-minimal.js`; in the Codex binding it is the
equivalent orchestration expressed in the Codex worker prompt. The analysis phase emits
`stages` (in plan order, with inner arrays retained as grouping metadata; generator stages execute
serially because they commit in one shared worktree) and `security` alongside its plan.

Review is therefore **per-stage and adversarial** — each generator is evaluated by a separate
context before later stages build on its output, upholding the generator ≠ reviewer rule that
exists to prevent the self-praise failure mode.

Each generator commits its own stage to the branch as its final action — the workflow's
commit-per-stage contract — and a REVISE fix pass produces a follow-up commit. Generator stages
execute serially because they share one worktree and Git index; reviewer passes within a stage may
still run concurrently. Reviewers stay read-only and evaluate the committed changes. Commits land
on the private `prauto/I-*` worktree branch only, never `master`, and are attributed to the worker
via `--author`. Before integration or PR finalization, the parent requires the exact
`PRAUTO_WORKFLOW_OUTCOME: COMPLETE` sentinel and a clean worktree. Progress is therefore durable
per stage: a run that dies mid-workflow loses only the stage in flight, and a quota-pause resume
re-enters a branch whose committed state matches the session's memory. The executor publishes
checkpoint commits as soon as it regains control; these commits are unreviewed intermediate
progress, and each published commit is recorded as an idempotent issue comment.

An ESCALATE outcome halts the workflow at the escalating stage group, so later stages never run and
the branch holds a partial implementation. Prauto must not carry that forward to tests or a PR: it
abandons the job ([Job completion and abandonment](#job-completion-and-abandonment)) rather than
finalizing.

### Pinned evaluator authority capture

Each reviewer's role, the verdict schema, and its evaluator memory — the pinned evaluator authority
`wf-minimal` reads before every review pass — is captured by the **executor**, never by the worker
session it is about to dispatch. The executor writes one file per reviewer type (`reviewer`,
`test-reviewer`, `spec-reviewer`, `security-reviewer`) to `${PRAUTO_DIR}/authority/I-<issue>/<type>.md`
immediately before dispatching the implementation phase, sourced from `REPO_DIR` — the executor's
own checkout of the base branch, held outside every worktree and therefore outside anything a branch
under review can write. A branch that edits `scaffold/roles/`, `scaffold/memory/`, or the verdict
schema cannot weaken the reviewers judging it; those edits take effect only once merged and reviewed
by a human, like any other repository change. Each file's header names the commit it was captured
from, so the snapshot is reproducible from that commit rather than being an artifact that itself
needs preserving; capture happens fresh per attempt, with any stale directory from a prior attempt
removed first.

All four types are captured regardless of which stages the plan's metadata names, because deriving
that subset in shell is fragile, and a snapshot the run does not end up needing costs nothing next
to a missing one, which escalates the run.

The implementation prompt receives the snapshot's directory path, not its contents: the four
snapshots together run to several hundred kilobytes, well past what fits inline as workflow-tool
arguments. The worker builds a `{reviewerType: path}` map from that directory and passes it as
`wf-minimal`'s `authority` argument, so each reviewer subagent reads its own file directly rather
than the worker relaying or truncating the text on the reviewers' behalf. The Codex binding has no
workflow tool and therefore does relay the text itself, verbatim and in full — a weaker position the
binding cannot avoid. The snapshot deliberately sits outside `${PRAUTO_DIR}/state`, the path
`DENY_TOOLS` blocks: placing it there would make every review pass depend on whether a parent's
`--disallowedTools` rule reaches the subagents it dispatches. If it did, each reviewer's first action
would fail and the implementation phase would wedge for every issue. That risk buys nothing, since
the parent's own grant already makes a deny on this path advisory. The directory is gitignored —
derived output, not a source the repository tracks.

**What this does and does not establish.** It removes two things: branch content cannot reach the
reviewers judging it, and the worker no longer supplies its own reviewers' instructions. It does not
make the snapshot tamper-proof between capture and read. The worker runs unreviewed branch code as
the executor's own OS user with a shell (see
[Prauto executes unreviewed branch code](#prauto-executes-unreviewed-branch-code)), so it can rewrite
those files or replace the directory. `DENY_TOOLS` and the workflow's path validation raise the cost
and stop the accidental cases; neither is a boundary. Treat the capture as removing whole classes of
influence, not as an unforgeable channel.

A capture that cannot complete — a missing role file, an unwritable destination — fails closed
before the worker is ever invoked: the executor does not dispatch the implementation phase at all.
Because the counter is advanced at dispatch, that attempt is refunded
([Retry tracking](#retry-tracking)); a deterministic executor-side fault would otherwise consume an
attempt on every wake and abandon the job without a line of work attempted.

### Deploys stay executor-owned

Review of generated code happens inside the workflow, per stage: each generator is evaluated by a
separate reviewer context before later stages build on it
([The implementation phase runs the AGENTS.md workflow](#the-implementation-phase-runs-the-agentsmd-workflow)).
That per-stage adversarial pass is the review gate — the executor does not run a second,
whole-diff review of its own afterwards. A second pass over the merged result would re-read work
already judged, cost another full review, and add an escalation path without establishing anything
the per-stage passes do not.

Prauto's analysis phase never emits `k8s-helm` as a stage: that
stage would deploy under whatever its kubeconfig points at, ignoring the worker-cluster binding and
the api-then-frontend ordering ([Branch image deploys](#branch-image-deploys)), and it carries no
reviewer. All cluster mutation runs from the executor against `$PRAUTO_DEV_ENV_FILE`.

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
`install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE` and re-checks. A provisioning failure
blocks any required cluster regression, leaves the PR in `prauto:wip`, and is retried on a later
heartbeat; it never becomes a passing skip. Exit 2 is a local setup fault, not cluster evidence
(`HELM_CHART.md` §Health Check): prauto reports it, provisions nothing, and leaves required
cluster regression blocked for a later retry, so a missing `kubectl` or an unresolvable context
cannot trigger an unsupervised cluster build. Gated by
`PRAUTO_CLUSTER_PROVISION_ENABLED` (default `true`). The health check is run under a wall-clock
backstop and in a private `TMPDIR`, because this worker is unsupervised: nothing outside it would
notice a check that never returns, and a run the backstop stops must not leave behind the
kubeconfig copy the check writes. A fired backstop counts as exit 1. Every
`install.sh`/`health-check.sh` invocation
carries `--env-file $PRAUTO_DEV_ENV_FILE`; without it both default to `helm-charts/.env.dev`, the
shared cluster the per-worker binding exists to avoid. The health check additionally carries
`--keep-lock` and runs with stdin closed: an unattended gate must never wait on a release prompt,
and prauto meets a held lock through its own acquire. A lock conflict for required cluster tests
is infrastructure-blocked: the issue and PR remain `prauto:wip`, `prauto:review` is not applied,
and the same regression retries on a later heartbeat — except when the executor holds a persisted
**token** the lock service accepts for the reported lock, in which case it force-releases with that
token and re-acquires once before falling back to blocked. Reclaim is authorized by the token, never
by the owner name: `prauto-<worker id>` is printed on every GitHub comment and the lock service is
unauthenticated plain HTTP, so a name match alone would let anyone — or a second worker sharing the
default worker id — force-release a live peer's lock. No accepted token leaves the conflict blocked
as before, the same as any other lock conflict. The token comes from the acquire call and is
persisted in local state (mode `600`); prauto never calls the operator's `DELETE /lock` force-release
escape hatch, since its own reclaim always releases with the token it holds, never by name. The
executor keeps its owner and token state until a release call actually succeeds, so a release that
fails (network outage, endpoint unreachable) is retried rather than silently treated as done —
including by the heartbeat's own exit-time release, which otherwise gets no real second attempt.

While the lock is held, a background renewer extends its lease roughly every 300s
(`POST .../lock/renew` with the owner and token). No single trap bounds its lifetime — a SIGKILL or
an OOM kill skips every trap, and a renewer that outlives its lock would extend the lease forever,
a permanent wedge worse than no lease at all — so three independent bounds apply, any one of which
ends it: the parent heartbeat's PID disappearing, a hard wall-clock ceiling
(`PRAUTO_DEV_LOCK_RENEW_MAX_SECS`, default `21600`), and the service itself answering `403` or `409`
to a renew call, either of which means the acquisition no longer exists. A lock acquired without a
renewal token is treated as a failed acquisition — released immediately and reported
infrastructure-blocked — since a lock this worker cannot renew is not one it can rely on for the
stage's duration. The dev-env lock service's lease (see
[`TESTING.md` §Integration Testing](TESTING.md#integration-testing)) reclaims only a lock that has
stopped renewing — a live holder, prauto's own included, is never preempted by it — independent of
the token-based self-reclaim above, which applies specifically to this worker's own stale lock.

Provisioning cost does not count against `PRAUTO_MAX_RETRIES_PER_JOB` — standing up a cluster is
not an attempt at the issue, and charging it would abandon jobs for infrastructure latency that
says nothing about the work.

`install.sh` itself runs under a separate wall-clock backstop (`PRAUTO_PROVISION_TIMEOUT_SECS`,
default 3600s) that terminates its managed process tree when provisioning wedges. A fired
backstop is reported and handled exactly like any other provisioning failure (above). Provisioning
output is retained in a private, bounded local log so an unattended run has observable progress;
it is not published to GitHub or another external surface because it can contain operational
details not suitable for a PR record.

**Autopilot abort mode**: on GKE Autopilot, a GMS scale-up timeout aborts `install.sh` before the
DataHub ingress+PAT step. The resume is `--from-component datahub`, not `dataspoke-infra`. Note
that fragmented `--from-component` resumes skip the env-sync step and leave stale
`DATASPOKE_DEV_*` credentials in the env file; those are rebuilt from cluster secrets.

**Teardown verifies deletion before clearing its marker.** A heartbeat that provisions a cluster
records a durable local marker so a later heartbeat can find and finish a teardown this one did not
complete. A zero exit from `uninstall.sh` is not itself proof of deletion: the deletion evidence
belongs to the **executor**, not the uninstaller, because a namespace can still be stuck
terminating, a teardown can be partial, or the heartbeat can be killed before its exit trap ever
runs — none of which the invoking script's own exit code can speak to after the fact. So the
marker is cleared only once the executor has confirmed the dev namespaces are actually gone. If it
cannot confirm that, the marker survives and a later heartbeat retries the teardown from it,
exactly as it would after a heartbeat that never reached its exit trap at all.

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

After implementation, the executor (not the worker) pushes the branch, links it to the issue's
Development section, posts commit-link comments for any unpublished commits, and creates or
updates its PR. Creation keeps the issue and PR in `prauto:wip`; `prauto:review` is applied only
after the required post-PR readiness gate succeeds. The same checkpoint publication runs after
worker-led review and test-fix stages. A strict push remains the finalization gate; checkpoint
pushes are best-effort so a temporary GitHub outage does not erase local committed progress.

### PR review handling

Issues with `prauto:review` label are checked for unaddressed non-prauto comments. The
feedback-addressed marker breaks the re-pickup loop; new reviewer comments after the marker
make the PR actionable again. A feedback fix returns the PR to `prauto:wip` and must complete the
same post-PR readiness gate against its pushed head before `prauto:review` is restored.

### Test execution

Prauto runs the unattended form of the protocol in [`TESTING.md`](TESTING.md), which is
authoritative for layers, commands, and constraints. The executor classifies the final branch
diff and records the selected targets and classification in its test evidence. This deterministic
policy is tool-neutral: it applies identically whether the worker is Claude Code or Codex. Each
stage names its **actor**: the worker runs a stage from its prompt template inside the session's
tool whitelist, while the executor runs a stage directly and invokes the worker only for fix
sessions.

### Test-impact classification

The executor distinguishes code-affecting changes from documentation and scaffold-only changes.
Code-affecting changes include application, test, build, deployment, runtime-configuration, and
dependency changes. Documentation, AI-scaffold, plugin, and other non-runtime operational changes
are excluded only when the complete diff is confined to the executor's explicit exclusion set.
An unrecognised path, a mixed diff, or an unavailable classification is code-affecting. Test-only
changes are code-affecting. The classifier is centralized in the harness; a worker must not
declare its own change exempt.

### Pre-PR selected verification

Before a PR exists, Prauto runs the static gates and the unit tests required by the changed paths
and their declared test impact. Where a code-affecting change requires cluster verification, it
may similarly run only the related spot, api-wired, and E2E targets. The executor records why each
target was selected. If it cannot determine a complete safe selection for a required layer, it
runs that layer's full suite; uncertainty never becomes an omitted test.

**Pre-flight gate** *(executor)*: the health check in its
[Provisioning](#provisioning) form — `--env-file $PRAUTO_DEV_ENV_FILE --keep-lock` — runs before
any required cluster work. On exit 1, prauto provisions its own cluster and re-checks
([Provisioning](#provisioning)). A provisioning, health-check, or lock failure is a blocked
result: it is reported and prevents `prauto:review`; it is never converted to a passing skip.
Exit 2 remains a setup fault that provisions nothing, but is also blocked whenever the required
regression needs the cluster.

**Environment**: the executor's integration and E2E stages source the worker's env file
(`$PRAUTO_DEV_ENV_FILE`, resolved under `$REPO_DIR`) via `set -a` (the file carries no `export`
prefixes) and hold the dev-env lock at `$DATASPOKE_DEV_LOCK_URL`.

**Stage 1 -- Static gates** *(worker before PR; executor after PR)*: `uv run ruff check src/ tests/` and `uv run mypy src/`,
invoked as **checks, never `--fix`** — prauto verifies the author-run gate rather than mutating
the diff until it passes. Frontend-touching work adds `npx tsc --noEmit` and `pnpm run lint`
from `src/frontend/`; diffs touching `tests/e2e/` add `pnpm -C tests/e2e typecheck`. These are
the four author-run gates of [`TESTING.md §CI Behavior`](TESTING.md#ci-behavior); no
`.github/workflows/` exists, so they are the only thing standing between prauto and a red `dev`.

**Stage 2 -- Unit** *(worker before PR; executor after PR)*: `uv run pytest tests/unit/`; frontend-touching work also runs
`pnpm -C src/frontend test` (offline, mocked). Needs no cluster and no lock.

**Stage 3 -- Integration fix loop (pre-PR)** *(executor; worker for fixes)*: after
implementation, under the dev-env lock. A diff touching `src/{api,backend,shared}` deploys the
branch's API first ([Branch image deploys](#branch-image-deploys)) so the tests reach the
branch's code rather than a stale image. It runs the selected spot (`tests/integration/spot/`) and
api-wired (`tests/integration/api_wired/`) targets as **two separate groups**, never mixed — a
mixed run puts competing Airflow load on the cluster and flakes on timing. The split binds every
integration invocation, Stage 5 included. Failures feed the worker's fix loop up to
`PRAUTO_INTEGRATION_FIX_MAX_RETRIES`.

A session-start health-gate abort (`require_server`, same as above) on either group is checked
first and is likewise infrastructure-blocked here — never branch-attributable or a flake — and
ends the loop without dispatching a worker. Otherwise, before dispatching a worker, the executor
classifies every failed group against the deterministic environmental-flake exception's conditions
1, 2, 3, and 5 below (cluster-dependent stage, allowlisted transport signature, a passing
post-stage health probe, no assertion/contract-mismatch failure — condition 4 does not apply, since
no fix is being classified). When every failed group qualifies, the executor reruns the groups
itself on the next iteration instead of dispatching a worker, bounded separately by
`PRAUTO_INTEGRATION_FLAKE_RERUNS` (default 2) so flake reruns never consume the fix-loop budget;
exhausting it with flake-only failures ends the loop without dispatching a worker, the same as an
ordinary exhausted retry budget. A worker's structured verification record is never read as pass
evidence in this loop — only Stage 5's targeted retry consumes it. Any non-qualifying (non-flake)
failure dispatches a worker as before; after a dispatched fix session, the executor keeps the lock,
redeploys the branch API when the committed fix touched `src/api/`, `src/backend/`, or
`src/shared/`, and re-probes health before the next iteration rather than trusting a now-stale
pre-loop check.

**Stage 4 -- E2E (Playwright, pre-PR)** *(executor; worker for fixes)*: runs when the selector
identifies an affected E2E target, including the `src/frontend/`, `tests/e2e/`, or `src/api/`
surface ([Branch image deploys](#branch-image-deploys)). It acquires the dev-env lock for its own
run, strictly after the integration groups release theirs, deploys the branch's frontend, and runs
the selected Playwright target.

- This stage gives prauto a **cluster + browser dependency** no other stage has.
- The hold is separate rather than inherited because the integration loop has another caller.
  E2E's own setup reset-seeds rather than depending on state inherited from the integration groups.
- Ordering is a constraint, not a preference. Two reasons compound: the frontend deploy rolls the
  API pod, and `--components api` would delete the cluster frontend if it ran second. E2E must
  land strictly after the integration groups and never run concurrently with them.
- Before dispatching a fix session, a failed run is classified against the same deterministic
  environmental-flake exception as [Stage 3](#stage-3----integration-fix-loop-pre-pr) — Playwright
  crosses the same cluster ingress far more times per test than one integration call, so it is at
  least as exposed to transient transport drops. A qualifying failure is rerun by the executor
  itself, bounded separately by `PRAUTO_E2E_FLAKE_RERUNS` (default 2) so flake reruns never consume
  the fix-loop budget below.
- `PRAUTO_E2E_FIX_MAX_RETRIES` defaults to `3`: the executor invokes a fix session only on a
  non-final attempt, buying up to 2 fix passes before the stage reports its result without further
  fixing. Raising it buys more fix attempts at a full rebuild + redeploy each.

**Stage 5 -- Full regression and targeted-retry readiness gate (post-PR)** *(executor; worker for
fixes)*: every code-affecting PR first runs the complete static-gate and unit-test command
families, followed by the full spot suite, full api-wired suite, and full E2E suite. Spot and
api-wired remain separate groups; E2E remains strictly after both. The executor deploys the branch
artifacts needed to exercise all three cluster-dependent suites, regardless of whether a narrower
pre-PR selection ran.

The initial full regression is against the exact pushed PR head. The executor posts no raw
per-stage output. A clean initial run posts one brief PR success comment and permits
`prauto:review`. Subject only to the deterministic environmental-flake exception below, a
branch-attributable static, unit, spot, api-wired, E2E, or branch-deploy failure keeps the PR out
of `prauto:review`. The executor records the failed stage names and their evidence, and posts a
failure comment that lists, for each failed stage, the failing test identifiers — pytest node IDs,
Playwright test titles, or static-check diagnostics as `path:line` entries — each with a one-line
reason, bounded in size and secret-scrubbed. A deploy-stage failure names the stage only, since it
carries no test identifiers to extract. When no identifiers can be extracted from a failed stage's
output, the comment says so for that stage rather than omitting it. The executor then starts the
applicable coding-agent fix-and-test loop.

The coding-agent fix-and-test loop is bounded by the configured agent turn limit; it has no
separate `PRAUTO_REGRESSION_FIX_MAX_RETRIES` budget. In each turn, the agent diagnoses only the
recorded branch-attributable failures, reruns only their applicable checks, and commits the fix
when it has targeted-test evidence. Failed spot and api-wired targets remain separate integration
groups and are never combined. The agent returns the committed revision and structured targeted
test evidence to the executor; it does not push.

The executor retains checkpoint publication and strict-push ownership. After it publishes the
agent's committed revision, it rebuilds and deploys only the API and frontend branch artifacts
required by the recorded failed cluster stages, preserving the established API-then-frontend
ordering. It reacquires the required dev-environment lock and reruns only the recorded failed
cluster stages against the exact pushed fix head; static and unit stages are likewise rerun only
when recorded as failed. A successful targeted retry is readiness success once every recorded
failed stage has passed with exact-head evidence. The executor does not start another full
post-PR regression after that success. A targeted retry with no recorded failed stages is
infrastructure-blocked, is never readiness success, and never applies `prauto:review`. If
executor-targeted verification exposes a new branch-attributable failing stage, it reports that
failure explicitly and leaves the PR in `prauto:wip`; it does not launch another coding-agent fix
loop. An exhausted agent turn limit also leaves the PR in `prauto:wip` rather than looping
forever.

### Deterministic environmental-flake exception

The executor may treat a failure as an ignorable environmental flake only when **all** of the
following conditions hold:

1. The failure is in a cluster-dependent stage: spot integration, api-wired integration, or E2E.
2. Sanitized stage output matches an explicit allowlisted infrastructure or transport signature:
   - a GKE/Kubernetes control-plane request reports `429`, `500`, `502`, `503`, or `504`,
     `i/o timeout`, `context deadline exceeded`, `TLS handshake timeout`, or `connection reset`;
   - Kubernetes reports a pod as `Evicted`, `Preempted`, or `NodeNotReady`; or
   - the configured ingress or DNS client reports `ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`,
     `EAI_AGAIN`, `temporary failure in name resolution`, `upstream reset`, an ingress `502`,
     `503`, or `504`, `ConnectionRefusedError`, `Connection refused`, `Connect call failed`, httpx
     `ConnectError`, or `All connection attempts failed`.
3. The executor's required health check passes both immediately before and immediately after the
   failed stage, using the same worker environment and lock discipline. This post-stage check is a
   probe only — it never provisions or reinstalls the cluster; a failing post-stage probe makes the
   failure non-flake.
4. The failed stage is unrelated to paths changed by the current fix, or that same stage passed
   earlier in the same heartbeat.
5. The output contains no test assertion failure, API contract/schema mismatch, or application
   response failure. A first-run api-wired or E2E assertion failure is always blocking and is
   never auto-ignored.

For an initial full regression without a preceding fix, condition 4 can be satisfied only by an
earlier same-heartbeat pass; the absence of fix paths is not itself evidence of a flake.

The allowlist is exhaustive: an unrecognized message, an ambiguous source for a transport status,
or any failure outside these conditions remains blocking. A classified flake is non-blocking for
readiness only; it is not recorded as a passed test and never dispatches a coding agent. The
executor posts a visible, sanitized PR comment naming the stage, allowlisted category,
before/after health-check results, and path-or-earlier-pass basis. The comment excludes raw logs,
credentials, URLs, tokens, dataset values, and other environment-sensitive output, and may name
the failing test identifiers, bounded and secret-scrubbed, without per-test reasons. A
targeted-retry failure posts a bounded, secret-scrubbed executor-evidence block so reviewers can
distinguish test, deployment, and transport conditions without accessing local artifacts,
carrying the same per-stage failed-test listing described above for the stages that still fail
after the retry.

The final PR readiness comment reports whether the initial full regression passed directly, lists
the initial failed stages and targeted retry stages that later passed, and identifies any ignored
environmental flake. This makes partial-retry or flake-qualified success visible without
representing it as an unqualified clean full-regression result. A provisioning, health-check,
lock, or local setup failure is infrastructure-blocked: the executor posts a distinct brief
blocked comment, leaves the issue and PR in `prauto:wip`, refunds the retry the blocked attempt
consumed ([Refunding a truncated or infrastructure-blocked attempt](#refunding-a-truncated-or-infrastructure-blocked-attempt)),
and retries the blocked regression or targeted stage on a later heartbeat without promising a code
fix or asking a worker to change code. A lock conflict is reclaimed rather than left blocked when
the executor holds an accepted token for the reported lock ([Provisioning](#provisioning)); a
conflict it holds no accepted token for — another worker's lock, live or stale — remains blocked as
above. An issue that reaches `PRAUTO_MAX_RETRIES_PER_JOB` after accumulating one or more
infrastructure-blocked attempts carries those reasons into its abandonment record and the GitHub
abandonment comment
([Job completion and abandonment](#job-completion-and-abandonment)), so an infrastructure-driven
abandonment reads as distinct from a genuine code failure. A spot or api-wired stage whose
integration session-start health gate (`require_server` in
`tests/integration/conftest.py`) aborts before any test runs is likewise infrastructure-blocked —
never branch-attributable or a flake — and dispatches no coding agent. Only initial
full-regression success, completed targeted-retry success, or an otherwise-ready result qualified
solely by classified environmental flakes permits the executor to apply `prauto:review`. Excluded,
non-code PRs may bypass this initial full regression but still retain any selected pre-PR
verification required by their changed paths.

The cluster provisioned by a heartbeat remains available through PR creation, the initial post-PR
regression, and any targeted retries. The heartbeat tears down only the cluster it provisioned,
once at its exit; it never tears down and recreates that cluster between the pre-PR and post-PR
phases.

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
`git reset --soft` + commit -> force-push with lease -> post the squashed commit link on the
issue -> update PR title -> labels to `prauto:done` on issue + PR. Does **not** merge or close --
left to the human.

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
needed. Integration and E2E fix sessions narrow this differently only for Claude: their invocation
adds `Agent`, `Workflow`, and `Task` to `--disallowedTools`, so that session cannot invoke those
delegation tools. This is Claude-only defense-in-depth; Codex has no equivalent per-session tool
deny mechanism. It constrains delegation, not general shell reach-around or a child process created
by another route. The parent denylist binds the parent session and nothing beyond it. The same holds for
`Read(.prauto/config.local.env)`: a subagent reads that file directly, exposing
`ANTHROPIC_API_KEY` and `GH_TOKEN`. That denial was never the real boundary regardless — both are
already exported into every child's environment — so delegation widens an existing exposure
rather than opening a new one.

Turn and budget caps thin out the same way. A parent turn cap bounds the parent coding-agent
session only; subagents take their own limits, and no project agent sets one. Thus
`PRAUTO_MAX_TURNS_*` stops bounding delegated work. Whether a budget cap aggregates across
subagents is **unverified** —
treat delegated spend as unbounded until someone establishes otherwise.

### The executor boundary and its limits

The executor's reviewer is a **separate process** with its own context, so the "generator reviews
its own work" escape does not exist: the reviewer was never in the worker's session and inherits
no tool grants from it. This strengthens the generator ≠ reviewer rule.

The worker and reviewer agents have terminal and file access that is broader than the executor's
phase-specific tool whitelist. The cluster binding (`$PRAUTO_DEV_ENV_FILE` resolved from
`$REPO_DIR`) is therefore the primary containment boundary, not the tool whitelist.

| Layer | Restriction | Enforced? |
|-------|-------------|-----------|
| Coding-agent tools | Phase-specific whitelists | No — speed bump; `uv run python3` reaches around it, subagent delegation bypasses it |
| Network access | No web fetch, curl, wget | No — `npx`/`pnpm dlx` fetch and execute arbitrary packages |
| Cluster access | No kubectl, helm for the parent session | No — same reach-around; generators grant `Bash` outright; executor deploys via `install.sh` |
| Destructive ops | No rm -rf, sudo | No — speed bump only |
| Git push | Only executor pushes | No — speed bump; still valuable (see below) |
| Issue author | Org-member filter (on by default; disable in `config.local.env`) | Yes — `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` |
| Turn limits | Per-job caps | Parent only — subagents take their own limits; none set |
| Budget limits | Per-job cap | Unverified across subagents |
| Concurrency | Max open issues + a single live worker per wake | Yes — `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) + the executor's PID lock |
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

- Branch-authored `conftest.py` and `tests/e2e` package scripts run **under the executor** on the
  dev machine, not inside a builder.
- The branch's own `install.sh` / `build-image.sh` run **under the executor** during the deploy
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
| `scaffold/roles/` | Canonical generator/evaluator roles — the executor snapshots these as each reviewer's pinned authority |
| `scaffold/contracts/` | `reviewer-verdict.schema.json` is the verdict schema every stage reviewer emits against |
| `spec/` hierarchy | Analysis phase reads specs per `AGENTS.md` |

Prauto is self-contained — it does not modify `.claude/` files. The scaffold serves
interactive sessions; prauto serves unattended automation. The dependency runs one way and is
load-bearing: prauto's containment and turn bounds for delegated work are whatever the agent
definitions say they are.

---

## Executor and Scheduler

The contract above is executor-agnostic. The repository's executor is the Bash harness
(`.prauto/heartbeat.sh` + `.prauto/lib/*.sh`); a scheduler is any trigger that invokes it on a
cadence. They split work by durability: the executor owns lock, configuration, issue claiming and
phase derivation, agent probing and selection, dispatch, and finalization; the scheduler owns
cadence and launch only.

### The executor: the bash harness

`.prauto/heartbeat.sh` implements the seven-step [Executor Cycle](#executor-cycle)
deterministically. The reasoning surfaces stay on the coding agents (plan gate, adversarial
review, escalation); the executor owns the deterministic envelope:

| Concern | Mechanism |
|---|---|
| Concurrency gate | `lib/state.sh` PID lockfile with a `kill -0` stale-check — a second wake never runs a worker against a worktree another is mid-flight on |
| Cleanup | a `trap` removes the live worktree and releases the lock on any exit, so a dead worker leaves neither a dirty tree nor a stale lock |
| Ephemeral reset | each wake sweeps orphaned worktrees first — *uncommitted* work is invisible to resume; committed checkpoints and the Codex `native-sessions/` anchor survive the reset and gate a resume |
| GitHub identity | the executor resolves `gh api user` once at startup and asserts it against `PRAUTO_GITHUB_EXPECTED_ACTOR` (when set), so every comment/label/assignee is attributed to the worker account, never the keyring fallback |
| SSOT readers | `lib/issues.sh` derives phase, retry count, and plan approval as exact `gh`+`jq` readers — never prose-derived |
| Agent dispatch | `lib/agent.sh` invokes the coding agent per phase, honoring `PRAUTO_AGENT` and the [Quota-pause and resume](#quota-pause-and-resume) session-resume contract |

The executor is self-contained: run `bash .prauto/heartbeat.sh` directly for a manual tick.

### The scheduler

The scheduler invokes the executor on a cadence. It probes no agent and does not pre-set
`PRAUTO_AGENT`: agent selection is the executor's own job (`lib/agent.sh`
`select_agent`). A scheduler's responsibilities are cadence, launch, and — in the reference
Hermes binding — supervision and reporting (an agent that reports to Slack and detaches a
background monitor). It performs no issue work and holds no phase state; that remains the
executor's, re-derived from GitHub each tick.

### Reference binding: an agent-supervised Hermes cron job reporting to Slack

The reference scheduler binding is an **agent-supervised Hermes cron job**. Each tick wakes a
supervisor agent that (1) reports the trigger to Slack, (2) detaches the executor against the
durable local checkout when it is idle, and (3) spawns the background monitor
(`.prauto/scheduler/monitor.sh`) that keeps posting brief Slack notes until the coding agent
finishes. The supervisor itself performs no issue work and holds no state between ticks; the
detached executor and monitor are the long-running parts, and the executor's PID lock makes an
"already running" tick a no-op. This pattern is appropriate for a persistent, single-host
scheduler that permits child processes to outlive the trigger. Hermes is one such scheduler, not
a requirement.

**Prerequisites**

- Hermes Agent installed with its gateway running; the cron scheduler fires only while the
  gateway is available. Follow Hermes's operator documentation for gateway installation and
  status checks. Slack reporting reuses the gateway's Slack credentials via `hermes send` (no
  running gateway required for the bot-token path).

- The repo checked out locally (the job's `workdir`), with `config.local.env`, the `prauto:*`
  labels, and a Slack channel configured in place.

**Job definition (canonical)** — every field is preserved in the env files so the job is
reproducible from the repo. Repo-level fields live in `config.env` (committed); instance-identity
fields live in `config.local.env` (gitignored — the repo is public).

| Field | Env var | File | Value |
|---|---|---|---|
| `schedule` | `PRAUTO_SCHEDULER_HERMES_SCHEDULE` | config.env | `15 * * * *` (hourly at :15 past; a tick with no actionable issue is a no-op) |
| `name` | `PRAUTO_SCHEDULER_HERMES_NAME` | config.env | `DataSpoke PRauto heartbeat` |
| `skills` | `PRAUTO_SCHEDULER_HERMES_SKILL` | config.env | `prauto-executor` (the supervisor skill; source at `PRAUTO_SCHEDULER_HERMES_SKILL_FILE`) |
| `prompt` | `PRAUTO_SCHEDULER_HERMES_PROMPT_FILE` | config.env | `.prauto/scheduler/supervisor-prompt.md` — the canonical supervisor procedure |
| `workdir` | `PRAUTO_SCHEDULER_HERMES_WORKDIR` | config.local.env | the local checkout |
| `deliver` | `PRAUTO_SCHEDULER_HERMES_DELIVER` | config.local.env | `local` — the supervisor's final response is archived; Slack reporting is explicit via `hermes send` |

Create the Hermes job from the table's values using Hermes's cron interface. The supervisor
agent does not perform executor work: it launches the executor and monitors it. After the
executor and monitor are detached, executor success, failure, and phase state are observed
through GitHub and the executor log — the monitor relays those to Slack, so Slack is the human
surface, not the SSOT.

**Supervisor prompt and skill.** The two supervisor inputs the job references are committed to the
repo, not left to a hand-built profile:

- The canonical prompt is `.prauto/scheduler/supervisor-prompt.md` — the text placed verbatim in
  the job's `prompt` field. It is the supervisor procedure: report the trigger to Slack, run
  `bash .prauto/scheduler/launch.sh`, map the launcher's one-line status to a Slack report, and end
  the turn. It never performs executor work. Because it is placed verbatim, a change to this file
  must be followed by re-syncing the job's field, or the running supervisor keeps an older
  procedure than the repo it came from (the `hermes cron edit --prompt` command is in the
  supervisor skill).
- The supervisor skill is `.prauto/scheduler/prauto-executor/SKILL.md` — the `prauto-executor`
  skill the job's `skills` field loads. Install it into the Hermes profile that runs the cron job
  before creating the job, or the job rejects the unknown skill:

  ```bash
  mkdir -p ~/.hermes/skills/prauto-executor
  cp .prauto/scheduler/prauto-executor/SKILL.md ~/.hermes/skills/prauto-executor/SKILL.md
  ```

  For a non-default profile, target `~/.hermes/profiles/<name>/skills/…` instead.

The canonical monitor is `.prauto/scheduler/monitor.sh`; the supervisor detaches it through
`.prauto/scheduler/launch.sh`, which owns the mechanical envelope — check the executor lock,
detach the executor, verify it survived its first seconds, then detach the monitor. Both detaches
use `.prauto/scheduler/daemonize.py`, a setsid double-fork that runs the executor and monitor in
their own sessions (re-parented to launchd) so they survive the supervisor turn's process-group
teardown — the Hermes terminal tool tears down with `killpg`, which misses setsid children. The
launcher also validates the monitor detach: a non-numeric or immediately-dead monitor PID is a
`MONITOR_FAILED`/`MONITOR_EXITED_IMMEDIATELY` result (exit 1), never a silent success, so a run
that lost its Slack reporting is surfaced to the supervisor rather than reported as launched. The
monitor posts its own Slack notes via `hermes send` (no LLM, no running gateway required). The
executor's PID lock and GitHub idempotency make overlapping ticks safe.

Because Slack is the only human surface for a detached run, the monitor verifies its reporting
channel before it starts watching: `hermes send --list <platform>` must resolve `PRAUTO_SLACK_TARGET`
under the resolved profile home — the optional `PRAUTO_SCHEDULER_HERMES_HOME` (config.local.env; not
a job-create field), else the supervisor's inherited `HERMES_HOME`, else the default home. An
unresolvable target is a nonzero exit
with the reason recorded in `.prauto/state/monitor-slack-unresolved` and printed to the monitor log;
the launcher reads only that run's log output and reports it as
`MONITOR_EXITED_IMMEDIATELY … reason=…`, so misconfigured reporting is reported, never silent.
A send that still fails after one bounded retry is appended to
`.prauto/state/monitor-undelivered.log` (size-capped, newest kept) rather than dropped.

**The cron tick is not where the work happens.** The tick detaches the executor and the monitor;
the executor's agent invocations are the long-running part. GitHub is the SSOT for phase state
([Overview](#overview)) — each wake re-derives its next action from remote GitHub state — while
work-product continuity flows through committed branch checkpoints and agent-native session
anchors, never in-memory state between ticks.

### Other scheduler bindings

A persistent, single-host scheduler (such as a suitable cron, launchd, or systemd timer) may use
a detached launcher only when it targets one durable checkout and permits the detached executor to
survive after the trigger exits. It must not set `PRAUTO_AGENT`; the executor selects it.

An ephemeral CI scheduler or a multi-host scheduler must run the executor in the foreground, or
submit it to a durable worker. Such a binding must provide shared exclusion and persistent
continuity storage; a local PID lock and local native-session anchor alone do not coordinate
multiple hosts or survive an ephemeral workspace. Every binding preserves the contract: GitHub is
the phase-state SSOT, the evidence-based plan gate and generator ≠ reviewer are mandatory, and
cluster configuration remains anchored to `$REPO_DIR`.
