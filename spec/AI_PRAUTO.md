# PRauto: Autonomous PR Worker

> **Document Status**: Specification v0.5 (2026-03-07)
> This document specifies "prauto" — an autonomous PR worker that monitors GitHub issues, writes code via Claude Code CLI, and submits pull requests. Prauto extends the AI scaffold (`spec/AI_SCAFFOLD.md`) with unattended, cron-driven development automation.

---

## Table of Contents

1. [Overview](#overview)
2. [Directory Structure](#directory-structure)
3. [Worker Identity and Configuration](#worker-identity-and-configuration)
4. [Heartbeat Cycle](#heartbeat-cycle)
5. [Token Quota Checking](#token-quota-checking)
6. [Job State Machine](#job-state-machine)
7. [Issue Discovery Protocol](#issue-discovery-protocol) (includes [Issue Restart Protocol](#issue-restart-protocol))
8. [Claude Code Invocation](#claude-code-invocation)
9. [PR Lifecycle](#pr-lifecycle)
10. [Prompt Templates](#prompt-templates)
11. [Write Idempotency](#write-idempotency)
12. [Monitoring](#monitoring)
13. [Security Model](#security-model)
14. [Integration with AI Scaffold](#integration-with-ai-scaffold)
15. [Future: GitHub Actions Migration](#future-github-actions-migration)

---

## Overview

### What prauto is

Prauto is a cron-triggered bash-based worker that automates the issue-to-PR pipeline. Each heartbeat:

1. Checks whether Claude Code API tokens are available
2. Claims new work if under `PRAUTO_OPEN_ISSUE_LIMIT` — finds the oldest eligible `prauto:ready` issue
3. Processes **all** claimed issues (oldest first, including any newly claimed), each via a self-contained state machine:
   - WIP issues: derives the correct phase (analysis, plan-approval, implementation, pr) and handles it
   - Review issues: squash-finalizes approved PRs, or addresses reviewer feedback
   - Waiting/terminal issues: skipped

### Relationship to `claude-code-action`

Anthropic's [`claude-code-action`](https://github.com/anthropics/claude-code-action) runs exclusively on GitHub Actions runners. Prauto uses the **Claude Code CLI** (`claude -p`) directly, which supports non-interactive print mode, structured output, tool restrictions, budget caps, and turn limits. The prompt templates and tool restrictions are portable to `claude-code-action` via its `claude_args` input (see [Future: GitHub Actions Migration](#future-github-actions-migration)).

### Execution environment

Prauto runs on a local developer machine. Requires: `claude` CLI (authenticated), `gh` CLI (authenticated), `git`, `jq`, and `cron`. Docker/K8s/cloud deployments are out of scope for v1.

### GitHub as single source of truth

Every heartbeat derives its next action from **remote GitHub state** — labels, assignees, issue/PR comments, and review status. Local state files (session outputs, history records) exist for **debugging only** and are never read to determine routing.

**Key implications**:
- No `--resume` flag — every Claude session starts fresh. The implementation prompt instructs Claude to check the branch for existing work and continue from there.
- Retry tracking uses **heartbeat marker comments** on the GitHub issue, not a local counter.
- Phase is always derived fresh from GitHub — no local/remote cross-check.
- `prauto:plan-review` label provides fast signal for plan-approval state (visible on issue boards, filterable).

### Ready-label timestamp as lifecycle anchor

When an issue has the `prauto:ready` label set (or re-set for restarts), the timestamp of that label event marks the **start of the current lifecycle**. All comment-scanning functions — phase derivation, plan approval checks, retry counting, quota-pause detection, and idempotency guards — **ignore comments posted before the last `prauto:ready` label event**.

This is implemented via `get_ready_label_timestamp()`, which queries the GitHub timeline API (`/issues/{N}/timeline`) for the most recent `labeled` event with `label.name == "prauto:ready"`. The resulting `READY_LABEL_TIMESTAMP` is fetched once per issue at the start of each heartbeat iteration and used as a floor filter in all downstream functions.

**Why this matters**: When an issue is restarted (re-labeled `prauto:ready`), stale comments from the previous lifecycle — old plans, heartbeat markers, quota-pause notices — are automatically invisible to the new lifecycle without requiring manual cleanup.

---

## Directory Structure

```
.prauto/
├── config.env                  # [COMMITTED] Shared settings: GitHub labels, tool lists
├── config.local.env            # [GITIGNORED] Instance-specific: identity, Claude limits, secrets
├── config.local.env.example    # [COMMITTED] Template for config.local.env
├── heartbeat.sh                # [COMMITTED] Main cron entry point
├── lib/
│   ├── helpers.sh              # Shared bash helpers (info, warn, error)
│   ├── quota.sh                # Token quota check
│   ├── issues.sh               # GitHub issue scanning, claiming, WIP detection
│   ├── claude.sh               # Claude Code CLI invocation wrapper
│   ├── git-ops.sh              # Branch creation, worktree, push operations
│   ├── pr.sh                   # PR creation, feedback handling, squash-finalize
│   ├── phases.sh               # Phase-specific handlers (analysis → pr)
│   └── state.sh                # Monitoring state, lock, complete
├── prompts/
│   ├── system-append.md        # System prompt addendum for prauto identity
│   ├── issue-analysis.md       # Prompt: analyze issue, produce plan
│   ├── implementation.md       # Prompt: implement the plan
│   ├── pr-review.md            # Prompt: address PR reviewer feedback
│   ├── feedback-response.md    # Prompt: respond to plan feedback
│   └── squash-commit.md        # Prompt: generate squash commit message
├── state/                      # [GITIGNORED] Runtime state
│   ├── heartbeat.lock          # PID-based lock file
│   ├── heartbeat.log           # Cron output log
│   ├── .system-append-rendered.md
│   └── sessions/               # Per-issue session directories
│       └── issue-{N}/          # One dir per issue number
│           └── {uuid}/         # One dir per heartbeat session
│               ├── claude-output-{pid}.json  # Raw Claude CLI output
│               ├── analysis.txt              # Analysis phase output
│               ├── implementation.json       # Implementation phase output
│               ├── review.json               # PR review phase output
│               ├── complete.json             # Job completion record
│               ├── abandon.json              # Job abandonment record
│               └── squash-msg.txt            # Squash commit message (temp)
├── worktrees/                  # [GITIGNORED] Git worktrees for active jobs
└── README.md
```

Gitignored paths: `config.local.env`, `state/`, `worktrees/`.

---

## Worker Identity and Configuration

### Two configuration tiers

| File | Committed | Purpose | Key variables |
|------|-----------|---------|---------------|
| `config.env` | Yes | Repo-level conventions shared across instances | `PRAUTO_GITHUB_REPO`, label names (`prauto:ready/wip/review/failed/done/plan-review`), `PRAUTO_BASE_BRANCH`, `PRAUTO_BRANCH_PREFIX`, `PRAUTO_MAX_RETRIES_PER_JOB`, `PRAUTO_CLAUDE_MODEL`, org-member filter flag, reviewer login |
| `config.local.env` | No | Instance identity, Claude limits, secrets | `PRAUTO_WORKER_ID`, git author name/email, `PRAUTO_CLAUDE_MODEL`, `PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS`, `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION`, `PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS`, `PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION`, `PRAUTO_OPEN_ISSUE_LIMIT`, `PRAUTO_HEARTBEAT_INTERVAL_MINUTES`, `PRAUTO_QUOTA_TIMEOUT` (default 45s), reviewer override, `ANTHROPIC_API_KEY`, `GH_TOKEN` |

A single developer machine may run multiple prauto instances (e.g., `prauto01` in one clone, `prauto02` in another) sharing the same GitHub credential but with distinct identities.

If `ANTHROPIC_API_KEY` or `GH_TOKEN` is empty, the respective CLI falls back to system authentication (keyring or prior login). `GH_TOKEN`, when provided, needs Issues (R/W), Pull requests (R/W), and Contents (W) permissions.

All commits use `git commit --author="${PRAUTO_GIT_AUTHOR_NAME} <${PRAUTO_GIT_AUTHOR_EMAIL}>"`.

---

## Heartbeat Cycle

```
crontab trigger
    │
    ├── 1. Acquire lock ──────────── (prevent concurrent runs)
    │       └── if locked → exit
    │
    ├── 2. Load config ───────────── (config.env + config.local.env)
    │
    ├── 3. Secure secrets ─────────── (back up config.local.env; original stays, protected by denylist)
    │
    ├── 4. Check token quota ─────── (lib/quota.sh)
    │       └── if exhausted → post quota-paused on WIP issues → exit
    │
    ├── 5. Claim new issue ─────── (lib/issues.sh: find_all_claimed_issues, find_eligible_issue, claim_issue)
    │       ├── count ALL open issues held by worker (any prauto: label) via find_all_claimed_issues
    │       ├── if count >= PRAUTO_OPEN_ISSUE_LIMIT → skip pickup
    │       └── otherwise → find oldest prauto:ready issue → claim (add prauto:wip, comment)
    │
    ├── 6. Process all claimed issues ── (lib/issues.sh: find_all_claimed_issues)
    │       ├── query GitHub for ALL open issues assigned to worker (any prauto: label)
    │       ├── includes newly claimed issue from step 5
    │       ├── for each issue (oldest first), self-contained state machine:
    │       │   ├── prauto:done / prauto:failed → skip (terminal)
    │       │   ├── prauto:wip:
    │       │   │   ├── derive phase from GitHub
    │       │   │   ├── plan-approval + no response → pending, skip
    │       │   │   ├── plan-approval + actionable → handle, clean up worktree → next
    │       │   │   ├── max retries reached → abandon, skip
    │       │   │   ├── post heartbeat comment, create worktree → phase handler → clean up worktree → next
    │       │   ├── prauto:review:
    │       │   │   ├── check PR (lib/pr.sh: check_review_pr)
    │       │   │   ├── approved + clean → squash-finalize → clean up worktree → next
    │       │   │   ├── unaddressed feedback → address comments → clean up worktree → next
    │       │   │   └── otherwise → pending, skip
    │       │   └── other label → skip
    │       └── after all issues processed/skipped → done
    │
    └── 7. Restore secrets + release lock  (EXIT trap: cleanup())
```

**Claim-first, then process-all**: Step 5 calls `find_all_claimed_issues()` to count all open issues assigned to this worker with any `prauto:` label. If under `PRAUTO_OPEN_ISSUE_LIMIT`, it finds and claims a new issue. Step 6 re-fetches claimed issues (if a new one was claimed) and loops over **all** of them (oldest first). Each iteration is a self-contained state machine for one issue: WIP issues route through phase derivation (analysis, plan-approval, implementation, pr); review issues check for squash-finalize or feedback. Each issue needing active work is processed; issues in terminal (`done`/`failed`) or waiting states are skipped. The heartbeat exits after all claimed issues are processed or skipped.

**Worktree isolation**: Every Claude session runs inside a dedicated git worktree at `.prauto/worktrees/I-{N}` (new issue) or `.prauto/worktrees/{branch}` (PR review). The main repo directory is never the working directory during Claude invocations. Each iteration cleans up its worktree before moving to the next issue; the EXIT trap serves as a safety net for unexpected exits.

**Secrets handling**: `config.local.env` is copied to a temporary backup under `state/.secrets-$$/` before Claude runs. The original stays in place, protected by the `--disallowedTools` denylist entry. Secrets are sourced into shell env vars. The EXIT trap removes the temp backup.

**Bash conventions**: All scripts follow `dev_env/` patterns — `set -euo pipefail`, `SCRIPT_DIR` idiom, shared helpers from `lib/helpers.sh`, idempotent operations.

**Cron**: Recommended schedule is every 30 minutes during working hours. See `.prauto/README.md` for setup.

---

## Token Quota Checking

There is no dedicated Anthropic API endpoint to query remaining token balance. Prauto uses a two-step probe: (1) `claude auth status` for auth validation, (2) a minimal 1-turn dry-run with negligible token cost. If either fails with a rate-limit or quota error, the heartbeat exits.

**Behavior on exhaustion**:
- No WIP issue: exit cleanly, retry next heartbeat
- WIP issue exists: post a "Paused" comment (with `<!-- prauto:quota-paused -->` marker), exit. Retry counter is **not** incremented — quota exhaustion is not a job failure.
- On next heartbeat (quota restored): post a "Resumed" comment before continuing. The pause/resume cycle uses the **latest** prauto comment for idempotent detection.

---

## Job State Machine

### Phases

```
New issue (minor change):
  (no job) ──→ analysis ──→ implementation ──→ pr ──→ (complete)

New issue (non-minor change):
  (no job) ──→ analysis ──→ plan-approval ──→ implementation ──→ pr ──→ (complete)
                                  │  ↑
                                  │  ├── counter-proposal → re-analysis ─┘
                                  │  ├── plan missing → re-analysis ─────┘
                                  └── no response → wait (next heartbeat)

PR review:
  (no job) ──→ pr-review ──→ pr ──→ (complete)
```

Phase is always derived fresh from GitHub — never read from local state.

| Phase | Description | On next heartbeat |
|-------|-------------|-------------------|
| `analysis` | Claude reads issue + codebase, produces a plan | Restart analysis from scratch |
| `plan-approval` | Wait for human approval of the posted plan | Check again (retries not counted). If plan comment missing, fall back to re-analysis. |
| `implementation` | Claude writes code, runs tests, commits | Start fresh session — Claude checks branch for existing work |
| `pr-review` | Claude addresses reviewer feedback on existing PR, commits | Start fresh session with full reviewer comments |
| `pr` | Push branch, create/update PR, comment | Retry PR creation |

### Phase derivation from GitHub

On every heartbeat, `derive_phase_from_github()` inspects GitHub state in priority order. Comment checks (steps 3–5) only consider comments posted after the last `prauto:ready` label event:

1. PR exists for this branch → `pr`
2. `prauto:plan-review` label present on issue → `plan-approval`
3. Plan comment exists (in current lifecycle) + "go ahead" reply → `implementation`
4. Plan comment exists (in current lifecycle) + no approval → `plan-approval`
5. No plan comment (in current lifecycle) → `analysis`

### Retry tracking

Each heartbeat posts a marker comment on the GitHub issue: `prauto({worker_id}): Heartbeat — {phase} (attempt N/max)`. The function `count_heartbeat_comments()` counts these markers **within the current lifecycle only** — it only counts heartbeat comments posted after the last `prauto:ready` label event (see [Ready-label timestamp as lifecycle anchor](#ready-label-timestamp-as-lifecycle-anchor)) and after the most recent `Claimed` comment by this worker. This ensures restarted issues (see [Issue Restart Protocol](#issue-restart-protocol)) begin with a fresh retry counter.

When the count reaches `PRAUTO_MAX_RETRIES_PER_JOB`, the issue is abandoned.

**Exception**: The `plan-approval` phase does **not** post heartbeat comments or count retries — waiting for human approval is not a failure.

### Job completion

| Scenario | Actions |
|----------|---------|
| New issue → PR creation | Push branch, create PR (with `prauto:review` label), move job to history, update issue labels: remove `prauto:wip` (+ `prauto:plan-review`), add `prauto:review` |
| PR comment response | Address feedback with commits, push, post "feedback addressed" marker, move job to history |

### Job abandonment

After max retries: move job to history, update issue labels (remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`), post abandonment comment.

---

## Issue Discovery Protocol

### Label lifecycle

```
[human adds prauto:ready]
    │
    ├── prauto claims → removes prauto:ready, adds prauto:wip, sets assignee
    │       │
    │       ├── if non-minor: adds prauto:plan-review (plan posted, awaiting approval)
    │       │       │
    │       │       └── on approval: removes prauto:plan-review (implementation starts)
    │       │
    │       ├── success → removes prauto:wip (+ prauto:plan-review if present), adds prauto:review (on issue + PR)
    │       │       │
    │       │       └── approved + squash-finalized → removes prauto:review, adds prauto:done (on issue + PR)
    │       │
    │       └── failure → removes prauto:wip (+ prauto:plan-review if present), adds prauto:failed
    │
    └── (no prauto pickup yet → stays prauto:ready)
```

### GitHub labels

| Label | Color | Description |
|-------|-------|-------------|
| `prauto:ready` | Green `#85E89D` | Ready for prauto to pick up |
| `prauto:wip` | Yellow `#FBCA04` | Prauto is working on this issue |
| `prauto:plan-review` | Purple `#D4A5FF` | Plan posted, awaiting human approval |
| `prauto:review` | Blue `#1D76DB` | PR is in review |
| `prauto:failed` | Red `#D93F0B` | Abandoned after max retries |
| `prauto:done` | Dark green `#006400` | PR approved, ready to merge |

### Issue restart protocol

When a previously processed issue needs to be retried from scratch (e.g., after a failed attempt produced unusable results), a human resets the issue to its initial state:

1. Remove all `prauto:` labels except `prauto:ready`
2. Unassign the prauto worker
3. Delete the working branch, PR, and optionally stale plan/heartbeat comments

The prauto worker treats this as a fresh issue. The **ready-label timestamp** (see [Ready-label timestamp as lifecycle anchor](#ready-label-timestamp-as-lifecycle-anchor)) ensures all comment-scanning functions automatically ignore stale comments from the previous attempt:

- **Ready-label anchor**: `get_ready_label_timestamp()` fetches the timestamp of the last `prauto:ready` label event from the GitHub timeline API. All comment-scanning functions use this as a floor — comments before this timestamp are invisible to the new lifecycle.
- **Claim race check**: Uses a timestamp-based window — only considers `Claimed` comments posted after the current claim attempt began (ignores stale comments from previous runs).
- **Claimed comment**: Always posts a fresh `Claimed` comment (no idempotency guard), providing a secondary timestamp anchor within the lifecycle.
- **Retry counter**: `count_heartbeat_comments()` only counts heartbeat comments posted after both the ready-label timestamp and the most recent `Claimed` comment, so the counter resets automatically on re-claim.
- **Phase derivation**: Derives phase from current GitHub state, scoped to comments after the ready-label timestamp. Stale plan comments from previous lifecycles are automatically ignored — no manual cleanup needed.

### Search and claiming

Issues are discovered via `gh issue list` filtered by `prauto:ready` label, excluding already-WIP/review issues, sorted oldest-first. When `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` is enabled, issues from non-members are silently skipped (requires org-owned repo).

**Optimistic claim protocol**: (1) Check if `prauto:wip` already present — if so, back off. (2) Record pre-claim timestamp, add `prauto:wip` label. (3) Re-fetch after brief delay; only consider `Claimed` comments posted after the pre-claim timestamp (ignores stale comments from previous attempts). (4) If another worker claimed during the window, back off. (5) Remove `prauto:ready`, set assignee, post a fresh claim comment (always, even on re-claim). The timestamp-based race check supports issue restarts where old comments remain.

When finding claimed issues, `find_all_claimed_issues()` returns all open issues assigned to this worker's `PRAUTO_GITHUB_ACTOR` that carry any `prauto:` label, sorted oldest-first.

### Issue body conventions

For best results, issues should include a clear description, references to relevant spec files, acceptance criteria, and file paths if scope is known.

---

## Claude Code Invocation

### Multi-phase execution model

| Phase | Purpose | Tools | Max turns (configurable) |
|-------|---------|-------|--------------------------|
| Analysis | Read codebase, understand issue, produce plan | Read-only | `PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS` |
| Implementation | Write code, run tests, commit | Read + Write + limited Bash | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` |
| PR review | Address reviewer feedback (same tools as implementation) | Read + Write + limited Bash | `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` |
| Squash commit | Generate final commit message | None (text generation only) | 1 |
| Feedback response | Respond to plan counter-proposal | None (text generation only) | 1 |

`--max-turns` is the primary guard against runaway sessions. Values are set in `config.local.env` (example defaults: 50 analysis, 150 implementation). `--max-budget-usd` can be added via `PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS` / `PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION` for API billing but has no effect on subscription plans.

Every invocation starts a fresh session — no `--resume`. The rendered system prompt (`state/.system-append-rendered.md`) is passed via `--append-system-prompt-file`.

### Tool restrictions by phase

**Analysis (read-only)**: `Read`, `Glob`, `Grep`, and `Bash` limited to `git log/diff/status/branch`.

**Implementation / PR review (read+write)**: Above plus `Write`, `Edit`, and `Bash` for `git add/commit`, `uv sync`, `uv run pytest`, `uv run python3`, `uv run ruff`, `uv run mypy`, `npm run`, `npx prettier/tsc`.

**Denylist (both phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`, `gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`. The denylist is defense-in-depth — the whitelist already restricts Claude, but the denylist provides a second layer that remains effective if the whitelist is accidentally broadened.

**Rationale**: Claude commits locally; only the bash orchestrator pushes. No `gh` access prevents Claude from directly modifying issues/PRs. No network access prevents data exfiltration. `--dangerously-skip-permissions` is required for non-interactive mode but is constrained by the whitelist/denylist combination.

### Branch-based continuity

When an implementation is interrupted and restarted on the next heartbeat, the prompt instructs Claude to check for existing commits on the branch (`git log --oneline origin/{base_branch}..HEAD`) and continue from where it left off. This is simpler and more reliable than session resumption — git commits are durable state that survives across sessions and machines.

---

## PR Lifecycle

### Branch naming

Branches follow the pattern `prauto/I-{issue_number}` (e.g., `prauto/I-42`). Created as isolated git worktrees via `lib/git-ops.sh`.

### Push and PR creation

After implementation, `pr.sh` pushes the branch, checks for an existing PR via `gh pr list`, and creates one if none exists (with `prauto:review` label, assignee, optional reviewer). If a PR already exists, new commits are pushed and a comment is added.

### PR review handling

In the step 6 processing loop, issues with the `prauto:review` label are checked via `check_review_pr()`. A PR is actionable if it has unaddressed non-prauto comments and at least one `CHANGES_REQUESTED`/`COMMENTED` review or external comment. The feedback-addressed marker breaks the re-pickup loop: once posted, subsequent heartbeats skip the PR. New reviewer comments after the marker make the PR actionable again.

### Test execution

After implementation or PR review feedback, the worker runs available test suites and posts results as collapsible PR comments before swapping labels.

- **Unit tests**: If `tests/unit/` exists, runs `uv run pytest tests/unit/ --tb=short`. Always runs unconditionally.
- **Integration tests**: If `tests/integration/` exists, follows the dev-env lock protocol (best-effort):
  1. Check if the dev-env lock endpoint (`http://localhost:9221/lock/status`) is reachable — skip gracefully if not.
  2. Acquire the advisory lock with owner `prauto-{worker_id}`.
  3. Run `dev_env/dummy-data-reset.sh` before tests.
  4. Run `uv run pytest tests/integration/ --tb=short` with `DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1`.
  5. Run `dev_env/dummy-data-reset.sh` after tests.
  6. Release the advisory lock.
- Results are posted on the PR as `prauto({worker_id}): {Type} Test Results — {Passed|Failed}` inside a `<details>` block.

### Squash-finalize action

Runs inside the step 6 loop when a `prauto:review` issue's PR meets the trigger conditions. Squash-ready takes priority over feedback-needed within `check_review_pr()`.

**Trigger conditions** (all must be true): branch matches `prauto/` prefix, PR has `prauto:review` label, assigned to this worker, `mergeable == "MERGEABLE"`, `mergeStateStatus == "CLEAN"`, latest org-member review is `APPROVED`.

**Steps**: Rebase on base branch (abort on conflict), generate squash commit message via Claude (1-turn, no tools, using issue description + diff as context), rebuild as a single commit via `git reset --soft` + `git commit`, force-push with lease, update PR title to match the squashed commit's subject line, update labels to `prauto:done` on both PR and issue. Does **NOT** merge the PR or close the issue — that is left to the human.

**Label updates via REST API**: The squash-finalize step uses `gh api` REST calls (not `gh issue edit` / `gh pr edit`) for label operations, because the latter require `read:org` scope which the bot's classic PAT may lack.

**Commit message format**: Conventional commit (`<type>: <subject>`) with max 5-line body, issue/PR reference, and `Co-Authored-By` trailers for all org-member PR approvers.

---

## Prompt Templates

Six prompt templates live in `.prauto/prompts/`. Variables (issue number, title, body, branch, analysis output, reviewer comments) are substituted at runtime by `lib/claude.sh`.

| Template | Phase | Purpose | Key instructions |
|----------|-------|---------|------------------|
| `system-append.md` | All | Worker identity addendum | Declares autonomous mode, forbids pushing, requires conventional commits, mandates spec reading |
| `issue-analysis.md` | Analysis | Read issue + codebase, produce plan | Read spec hierarchy, examine codebase, list files/order/patterns/tests/risks. No code changes. |
| `implementation.md` | Implementation | Write code per plan | Check branch for existing work first, follow specs and patterns, write tests, run formatters, commit but don't push |
| `pr-review.md` | PR review | Address reviewer feedback | Make requested changes, answer questions, produce reviewer-facing response summary |
| `feedback-response.md` | Plan feedback | Respond to plan counter-proposal | Address each feedback point, acknowledge suggestions, keep under 500 words (1-turn, no tools) |
| `squash-commit.md` | Squash-finalize | Generate conventional commit message | From issue description + diff, produce `<type>: <subject>` with max 5-line body (1-turn, no tools) |

---

## Write Idempotency

Autonomous workers must be resilient to crashes and restarts. Every write action uses idempotency guards.

### Comment idempotency

Before posting certain comments, the worker checks for an existing comment matching (1) the worker's GitHub user and (2) the prefix `prauto({worker_id}):` followed by an action keyword.

| Context | Keyword | Idempotency guard |
|---------|---------|-------------------|
| Issue claim | `Claimed` | **No** — always posts a fresh comment to anchor retry counting for restarts |
| Abandonment | `Abandoning` | Yes — `comment_exists` |
| Plan | `Plan` (or `Plan (rev N)`) | Yes — `comment_exists` |
| Quota pause | `Paused` | Yes — `has_quota_paused_comment` |
| Heartbeat | `Heartbeat` | **No** — each heartbeat intentionally posts a new marker for retry counting |
| Review response | `Review response` | **No** — multiple responses are valid across review rounds |
| Feedback response | `Feedback response` | **No** — multiple responses are valid across plan revisions |
| Feedback marker | `Reviewer feedback addressed` | **No** — gated externally by `check_review_pr()` |
| Quota resume | `Resumed` | **No** — gated externally by `has_quota_paused_comment()` |

The `plan-approval` phase is exempt from heartbeat comments (waiting for human approval is not a failure).

### Optimistic claim locking

The claim protocol uses check-then-add with a timestamp-based verification window: (1) check for existing `prauto:wip` label, (2) record pre-claim timestamp, add label, (3) re-fetch after brief delay, only consider `Claimed` comments created after the pre-claim timestamp, (4) check for competing claims within the window. The timestamp scoping allows issues with stale `Claimed` comments from previous attempts to be re-claimed cleanly. Not fully atomic, but catches most races. For single-worker deployments this is a no-op safeguard.

---

## Monitoring

### Session directories

Each heartbeat run creates a per-issue session directory at `state/sessions/issue-{N}/{uuid}/`. All artifacts for that run are stored there: raw Claude output (`claude-output-{pid}.json`), phase outputs (`analysis.txt`, `implementation.json`, `review.json`), job outcome records (`complete.json` or `abandon.json`), and temporary files (`squash-msg.txt`). Session directories are organized by issue number, with each heartbeat attempt getting a unique UUID subdirectory. These files are for **debugging only** — not used for routing or resumption. The heartbeat log is written to `state/heartbeat.log`.

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
| Turn limit | Per-job turn cap (primary) | `--max-turns` |
| Budget | Per-job dollar cap (API billing only) | `--max-budget-usd` (optional) |
| Concurrency | Max open issues per worker | `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) + PID-based lock file |
| GitHub access | Fine-grained PAT | Scoped to issues, PRs, contents only |
| Secrets (git) | Gitignored local env | `config.local.env` never committed |
| Secrets (runtime) | Protected by denylist + temp backup removed on exit | `--disallowedTools` blocks `Read(.prauto/config.local.env)` |

**Why Claude cannot push**: Separating "write code" from "push to remote" is a deliberate safety boundary. Prevents pushing to unexpected branches/remotes even under prompt injection.

**Secrets isolation**: `config.local.env` stays on disk but is blocked by denylist. Secrets are sourced into shell env vars before Claude runs. `ANTHROPIC_API_KEY` and `GH_TOKEN` are exported only if non-empty; otherwise CLIs use system auth. The `.prauto/state/` directory is also on the denylist.

---

## Integration with AI Scaffold

| Scaffold element | Integration |
|---|---|
| `CLAUDE.md` | Claude reads this automatically, giving prauto full project context |
| `.claude/settings.json` hooks | `auto-format.sh` fires after Write/Edit in prauto sessions |
| `.claude/agents/` | Prauto prompts can instruct Claude to delegate to existing subagents |
| `.claude/skills/` | Skills are available if Claude detects matching context |
| `spec/` hierarchy | Analysis phase reads specs per CLAUDE.md instructions |

Prauto does not modify `.claude/settings.json`, `.claude/settings.local.json`, or `.claude/agents/`. It is self-contained in `.prauto/` — the only change to existing files is three lines in `.gitignore`. The scaffold serves interactive sessions; prauto serves unattended automation. Both use the same Claude Code engine, `CLAUDE.md` context, and auto-format hook.

---

## Future: GitHub Actions Migration

When the project adds `.github/workflows/`, prauto's design maps directly to `claude-code-action`:

| Prauto (local) | `claude-code-action` (GH Actions) |
|---|---|
| `heartbeat.sh` (cron) | `schedule:` trigger in workflow YAML |
| `lib/issues.sh` (gh CLI) | `issues: [labeled]` event trigger |
| `prompts/system-append.md` | `claude_args: --append-system-prompt-file` |
| `prompts/implementation.md` | `prompt:` input |
| `--allowedTools` / `--disallowedTools` | `claude_args: --allowedTools ...` |
| `--max-turns`, `--max-budget-usd` | `claude_args: --max-turns ... --max-budget-usd ...` |
| `config.env` | Workflow environment variables |
| `config.local.env` | GitHub Actions secrets |
| `lib/git-ops.sh` (gh pr create) | Built-in branch creation + PR prefill links |

Prompt templates and tool restrictions can be reused without modification. The main difference is that `claude-code-action` provides PR prefill links rather than creating PRs directly, so the GH Actions version would use a separate workflow step for `gh pr create`.
