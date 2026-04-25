# PRauto: Autonomous PR Worker

> **Document Status**: Specification v0.5 (2026-03-07)
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
8. [PR Lifecycle](#pr-lifecycle)
9. [Write Idempotency](#write-idempotency)
10. [Security Model](#security-model)
11. [Integration with AI Scaffold](#integration-with-ai-scaffold)
12. [Future: GitHub Actions Migration](#future-github-actions-migration)

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
3. Secure secrets — back up `config.local.env`; protected by the Claude tool denylist.
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
| Max retries | Remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment |

---

## Issue Discovery Protocol

### Label lifecycle

A human sets `prauto:ready`. On claim, prauto removes `prauto:ready`, adds `prauto:wip`, sets
the assignee — and for non-minor issues also adds `prauto:plan-review` (removed on approval).
On success the issue and PR both move `prauto:wip` → `prauto:review`; once approved and
squash-finalized, both move to `prauto:done`. On failure, `prauto:wip` is replaced with
`prauto:failed`. An unclaimed issue simply stays `prauto:ready`.

### Search and claiming

Issues discovered via `gh issue list` filtered by `prauto:ready`, sorted oldest-first.
Org-member filter optional (`PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY`).

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
| Implementation | Read + Write + Edit + limited Bash (git, pytest, ruff, mypy) | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` |
| Integration fix | Same as implementation | `PRAUTO_CLAUDE_MAX_TURNS_INTEGRATION_FIX` |
| Code review | Read-only + pytest | `PRAUTO_CLAUDE_MAX_TURNS_CODE_REVIEW` |
| PR review | Same as implementation | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` |
| Squash commit / Feedback response | No tools (text only) | 1 |

**Denylist (all phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`,
`gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`.

**Branch-based continuity**: On restart, the prompt instructs Claude to check for existing
commits on the branch and continue from there.

### Code review phase (generator-evaluator pattern)

After implementation but before integration tests, a fresh Claude context independently reviews
the generated code (read-only tools). Evaluates against 5 criteria: spec compliance,
architecture adherence, code quality, completeness, consistency. Outputs `VERDICT: APPROVE` or
`VERDICT: REVISE`. If REVISE, a separate fix session runs (max 1 iteration). Controlled by
`PRAUTO_CODE_REVIEW_ENABLED` (default `true`).

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

**Stage 1 -- Integration fix loop (pre-push)**: After implementation and code review, acquires
dev-env lock, runs `pytest tests/integration/` up to `PRAUTO_INTEGRATION_FIX_MAX_RETRIES` times.
On failure, Claude diagnoses and fixes. Skips if dev-env unreachable.

**Stage 2 -- Final test report (post-push)**: Runs unit + integration tests and posts results as
collapsible PR comments.

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
| Code review REVISE | `Heartbeat -- code review: REVISE` | No |
| Integration fix | `Heartbeat -- integration test fix loop` | No |
| Review/Feedback response | `Review response` / `Feedback response` | No -- multiple valid |

### Optimistic claim locking

Check-then-add with timestamp-based verification window. Not fully atomic but catches most
races; a no-op safeguard for single-worker deployments.

---

## Security Model

| Layer | Restriction | Mechanism |
|-------|-------------|-----------|
| Claude CLI tools | Phase-specific whitelists | `--allowedTools` / `--disallowedTools` |
| Network access | No web fetch, curl, wget | Disallowed tools |
| Cluster access | No kubectl, helm | Disallowed tools |
| Destructive ops | No rm -rf, sudo | Disallowed tools |
| Git push | Only orchestrator pushes | Disallowed for Claude; `git-ops.sh` handles it |
| Issue author | Org-member filter (opt-in) | `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` |
| Turn/budget limits | Per-job caps | `--max-turns`, `--max-budget-usd` |
| Concurrency | Max open issues + PID lock | `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) |
| Secrets | Gitignored + denylist + temp backup | `config.local.env` blocked by `--disallowedTools` |

**Why Claude cannot push**: Separating "write code" from "push to remote" prevents pushing to
unexpected branches/remotes even under prompt injection.

---

## Integration with AI Scaffold

| Scaffold element | Integration |
|---|---|
| `CLAUDE.md` | Gives prauto full project context automatically |
| `.claude/settings.json` | Tool permissions apply equally |
| `.claude/agents/` | Prauto prompts can delegate to subagents |
| `.claude/skills/` | Available if Claude detects matching context |
| `spec/` hierarchy | Analysis phase reads specs per CLAUDE.md |

Prauto is self-contained in `.prauto/` -- does not modify `.claude/` files. The scaffold serves
interactive sessions; prauto serves unattended automation.

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
