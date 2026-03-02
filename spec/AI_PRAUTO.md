# PRauto: Autonomous PR Worker

> **Document Status**: Specification v0.3 (2026-03-02)
> This document specifies "prauto" — an autonomous PR worker that monitors GitHub issues, writes code via Claude Code CLI, and submits pull requests. Prauto extends the AI scaffold (`spec/AI_SCAFFOLD.md`) with unattended, cron-driven development automation.

---

## Table of Contents

1. [Overview](#overview)
2. [Directory Structure](#directory-structure)
3. [Worker Identity and Configuration](#worker-identity-and-configuration)
4. [Heartbeat Cycle](#heartbeat-cycle)
5. [Token Quota Checking](#token-quota-checking)
6. [Job State Machine](#job-state-machine)
7. [Issue Discovery Protocol](#issue-discovery-protocol)
8. [Claude Code Invocation](#claude-code-invocation)
9. [PR Lifecycle](#pr-lifecycle)
10. [Prompt Templates](#prompt-templates)
11. [Write Idempotency](#write-idempotency)
12. [Security Model](#security-model)
13. [Integration with AI Scaffold](#integration-with-ai-scaffold)
14. [Future: GitHub Actions Migration](#future-github-actions-migration)

---

## Overview

### What prauto is

Prauto is a cron-triggered bash-based worker that automates the issue-to-PR pipeline. Each heartbeat performs **at most one job** — it:

1. Checks whether Claude Code API tokens are available
2. Resumes any interrupted job from a prior heartbeat (if found, exits after completion)
3. Squash-finalizes approved PRs (if found, exits after completion)
4. Checks open PRs for reviewer comments that need action (skips PRs with a "feedback addressed" marker; if found, exits after addressing and posting marker)
5. Finds an eligible GitHub issue via label-based discovery
6. Invokes Claude Code CLI to analyze the issue, posts plan for approval (non-minor changes wait), then implements changes
7. Creates or updates a pull request with the results

### Relationship to `claude-code-action`

Anthropic's [`claude-code-action`](https://github.com/anthropics/claude-code-action) is a GitHub Action that embeds Claude Code into CI/CD workflows. It runs exclusively on GitHub Actions runners — it has no local execution mode.

Prauto uses the **Claude Code CLI** (`claude -p`), which is the same underlying engine. The CLI supports all features needed for autonomous operation: non-interactive print mode (`-p`), session resumption (`--resume`), structured output (`--output-format json`), tool restrictions (`--allowedTools`, `--disallowedTools`), budget caps (`--max-budget-usd`), and turn limits (`--max-turns`).

The prompt templates and tool restrictions designed here are portable to `claude-code-action` via its `claude_args` input (see [Future: GitHub Actions Migration](#future-github-actions-migration)).

### Execution environment

Prauto runs on a local developer machine. It requires:

- `claude` CLI (Claude Code) installed and authenticated
- `gh` CLI (GitHub CLI) installed and authenticated
- `git` configured for the repository
- `jq` for JSON processing
- `cron` (or equivalent scheduler) for heartbeat triggering

Docker, Kubernetes, and cloud runner deployments are out of scope for v1 and will be addressed in a future revision.

---

## Directory Structure

```
.prauto/
├── config.env                  # [COMMITTED] Shared settings: GitHub labels, tool lists
├── config.local.env            # [GITIGNORED] Instance-specific: identity, Claude limits, secrets
├── heartbeat.sh                # [COMMITTED] Main cron entry point
├── lib/
│   ├── helpers.sh              # [COMMITTED] Shared bash helpers (info, warn, error)
│   ├── quota.sh                # [COMMITTED] Token quota check
│   ├── issues.sh               # [COMMITTED] GitHub issue scanning and claiming
│   ├── claude.sh               # [COMMITTED] Claude Code CLI invocation wrapper
│   ├── git-ops.sh              # [COMMITTED] Branch creation, push, PR lifecycle
│   └── state.sh                # [COMMITTED] Job state management (lock, resume, complete)
├── prompts/
│   ├── system-append.md        # [COMMITTED] System prompt addendum for prauto identity
│   ├── issue-analysis.md       # [COMMITTED] Prompt template: analyze issue, produce plan
│   ├── implementation.md       # [COMMITTED] Prompt template: implement the plan
│   └── squash-commit.md        # [COMMITTED] Prompt template: generate squash commit message
├── state/                      # [GITIGNORED] Runtime state
│   ├── current-job.json        # Active job metadata
│   ├── heartbeat.lock          # PID-based lock file
│   ├── heartbeat.log           # Cron output log
│   ├── .system-append-rendered.md  # Rendered system prompt (substituted at runtime)
│   ├── history/                # Completed job summaries (YYYYMMDD_I-NNN.json)
│   └── sessions/               # Claude session outputs (analysis-I-NNN.txt, impl-I-NNN.json, review-I-NNN.json)
├── worktrees/                  # [GITIGNORED] Git worktrees for active jobs
└── README.md                   # [COMMITTED] Setup and usage instructions
```

### Gitignore additions

Two lines appended to the repository root `.gitignore`:

```
.prauto/config.local.env
.prauto/state/
.prauto/worktrees/
```

---

## Worker Identity and Configuration

### `config.env` — committed shared configuration

Settings here are shared across all prauto instances cloned from this repo. They define repository-level conventions that should stay consistent.

```bash
# GitHub
PRAUTO_GITHUB_REPO="selhorys/dataspoke-baseline"
PRAUTO_GITHUB_LABEL_READY="prauto:ready"
PRAUTO_GITHUB_LABEL_WIP="prauto:wip"
PRAUTO_GITHUB_LABEL_REVIEW="prauto:review"
PRAUTO_GITHUB_LABEL_FAILED="prauto:failed"
PRAUTO_GITHUB_LABEL_DONE="prauto:done"
PRAUTO_BASE_BRANCH="dev"
PRAUTO_BRANCH_PREFIX="prauto/"

# Security: restrict prauto:ready issue pickup to organization members only.
# Set to "true" to enable. Requires PRAUTO_GITHUB_REPO to be an org-owned repo.
# When enabled, issues authored by non-members are silently skipped.
PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY="true"

# Limits (defaults — can be overridden in config.local.env)
PRAUTO_MAX_RETRIES_PER_JOB=3
```

### `config.local.env` — instance-specific configuration (gitignored)

Each prauto instance (e.g., different repo-clone directories on the same machine or different machines) maintains its own `config.local.env`. This file holds the worker identity, Claude CLI limits, and secrets. A single developer machine may run multiple prauto instances (e.g., `prauto01` in `~/repos/dataspoke-a/`, `prauto02` in `~/repos/dataspoke-b/`) sharing the same GitHub credential but with distinct identities.

```bash
# Never commit this file

# Identity (unique per instance)
PRAUTO_WORKER_ID="prauto01"
PRAUTO_GIT_AUTHOR_NAME="prauto01"
PRAUTO_GIT_AUTHOR_EMAIL="prauto01@dataspoke.local"

# Claude Code CLI (tune per instance based on machine capacity)
PRAUTO_CLAUDE_MODEL="sonnet"
PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS=10
PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION=50
PRAUTO_HEARTBEAT_INTERVAL_MINUTES=30

# Budget caps (optional — only effective with API billing, ignored on Pro/Max plans)
# PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS="0.50"
# PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION="2.00"

# Secrets (optional — leave empty to use system auth / keyring credentials)
ANTHROPIC_API_KEY=""
GH_TOKEN=""
```

If `ANTHROPIC_API_KEY` is empty, `claude` falls back to system credentials (e.g., keyring or `claude auth login`). If `GH_TOKEN` is empty, `gh` uses its own authenticated session. Secrets are optional — a bare developer machine with both CLIs already authenticated needs no values here.

The `GH_TOKEN`, when provided, is a fine-grained personal access token with these permissions on the target repository:

| Permission | Access | Used for |
|------------|--------|----------|
| Issues | Read/Write | List, label, comment on issues |
| Pull requests | Read/Write | Create PRs, comment on PRs |
| Contents | Write | Push branches |

### Worker identity in git

All commits made during a prauto session use the worker identity:

```bash
git commit --author="${PRAUTO_GIT_AUTHOR_NAME} <${PRAUTO_GIT_AUTHOR_EMAIL}>"
```

The `PRAUTO_WORKER_ID` also appears in issue/PR comments to identify which worker instance produced the output.

---

## Heartbeat Cycle

The heartbeat is the top-level control flow executed by `heartbeat.sh` on each cron trigger.

```
crontab trigger
    │
    ├── 1. Acquire lock ──────────── (prevent concurrent runs)
    │       └── if locked → exit
    │
    ├── 2. Load config ───────────── (config.env + config.local.env)
    │
    ├── 3. Secure secrets ─────────── (move config.local.env out of repo tree)
    │
    ├── 4. Check token quota ─────── (lib/quota.sh)
    │       └── if exhausted → if active job → post quota-paused comment → exit
    │
    ├── 5. Resume interrupted job
    │       ├── load job state ─────── (lib/state.sh: has_active_job, load_job)
    │       ├── verify issue assignee ─ (gh CLI)
    │       ├── check retries ──────── (lib/state.sh: bump_heartbeat; skip for plan-approval)
    │       ├── quota-pause recovery ── (lib/issues.sh: has_quota_paused_comment → post resumed)
    │       │   └── detects pause via GitHub issue comment marker, NOT local state
    │       ├── create worktree ─────── (lib/git-ops.sh)
    │       └── resume from saved phase → exit
    │
    ├── 5.5 Squash-finalize approved PRs ─ (lib/git-ops.sh)
    │       ├── if approved + CLEAN PR found → rebase, squash, force-push → exit
    │       └── if none → continue
    │
    ├── 6. Check open PRs ────────── (lib/git-ops.sh)
    │       ├── PRs with "feedback addressed" marker → skipped
    │       ├── if PR has reviewer comments → checkout worktree, run pr-review, push, post marker, complete → exit
    │       └── if no actionable comments → continue
    │
    ├── 7. Find eligible issue ───── (lib/issues.sh)
    │       └── if none found → exit
    │
    ├── 8. Claim issue ───────────── (add prauto:wip label, comment)
    │
    ├── 9. Create branch + worktree  (lib/git-ops.sh → .prauto/worktrees/I-{N}, then cd into it)
    │
    ├── 10. Phase 1: Analysis ────── (lib/claude.sh, read-only, runs inside worktree)
    │
    ├── 10.5 Plan approval gate ──── (non-minor: post plan, wait for approval; minor: proceed)
    │       ├── if non-minor → post plan comment, set phase=plan-approval → exit
    │       ├── (next heartbeat) approved → continue to implementation
    │       ├── (next heartbeat) counter-proposal → re-run analysis with feedback → exit
    │       └── (next heartbeat) no response → wait → exit
    │
    ├── 11. Phase 2: Implementation  (lib/claude.sh, read+write, runs inside worktree)
    │
    ├── 12. Create/update PR ──────── (lib/git-ops.sh)
    │
    ├── 13. Complete job ──────────── (lib/state.sh)
    │
    └── 14-15. Restore secrets + release lock  (EXIT trap: cleanup())
```

**One job per heartbeat**: Steps 5, 5.5, and 6 each exit after completing their work. A single heartbeat never runs more than one Claude session to keep resource usage predictable and simplify state management.

**Worktree isolation**: Every Claude session (analysis, implementation, pr-review) runs inside a dedicated git worktree at `.prauto/worktrees/I-{N}` (new issue) or `.prauto/worktrees/{branch}` (PR review). The main repo directory is never the working directory during Claude invocations. The `cleanup()` EXIT trap removes the worktree unconditionally on exit.

**Secrets handling**: `ANTHROPIC_API_KEY` and `GH_TOKEN` are exported only if non-empty; otherwise the respective CLIs fall back to their own system authentication. Secrets are secured before Claude runs by copying `config.local.env` to `/tmp/.prauto-secrets-$$` as a backup; the original stays in place but is protected by the `--disallowedTools` denylist entry `Read(.prauto/config.local.env)`. The EXIT trap removes the temp backup on exit.

### Bash conventions

All scripts follow the project's established patterns from `dev_env/`:

- Shebang: `#!/usr/bin/env bash`
- Error handling: `set -euo pipefail`
- Location: `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`
- Shared helpers sourced from `lib/helpers.sh` (`info()`, `warn()`, `error()`)
- Idempotent operations where possible

### Cron setup

```bash
# Run heartbeat every 30 minutes, Mon-Fri 9:00-18:00 KST
*/30 9-18 * * 1-5 cd /path/to/dataspoke-baseline && .prauto/heartbeat.sh >> .prauto/state/heartbeat.log 2>&1
```

---

## Token Quota Checking

There is no dedicated Anthropic API endpoint to query remaining token balance from the CLI. Prauto uses a two-step probe:

### Step 1: Auth validation

```bash
claude auth status
```

Exits 0 if authenticated, non-zero if auth is invalid or expired.

### Step 2: Minimal dry-run

```bash
claude -p "Reply with exactly: OK" \
  --output-format json \
  --max-turns 1 \
  --max-budget-usd 0.01 \
  --allowedTools ""
```

This costs negligible tokens. If the call fails with a rate-limit or quota error, the heartbeat exits cleanly to wait for the next cycle. The specific error pattern checked is `rate limit` or `quota` in stderr.

### Behavior on exhaustion

When quota is exhausted:

- If no active job: release lock and exit cleanly. Next heartbeat retries.
- If active job exists: post a "Paused" comment on the issue (with `<!-- prauto:quota-paused -->` marker), release lock, and exit. The retry counter is **not** incremented — quota exhaustion is not a job failure.
- On next heartbeat (quota restored): if the latest prauto comment on the issue has the quota-paused marker, post a "Resumed" comment before continuing work. This makes the pause/resume cycle visible on the issue timeline.
- Idempotency: the pause check inspects only the **latest** prauto comment (not all comments), so Paused→Resumed→Paused cycles produce distinct comments.

---

## Job State Machine

### State file: `state/current-job.json`

```json
{
  "issue_number": 42,
  "issue_title": "Implement health check endpoint",
  "branch": "prauto/I-42",
  "source": "issue",
  "phase": "implementation",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "started_at": "2026-02-28T10:00:00Z",
  "retries": 0,
  "last_heartbeat": "2026-02-28T10:15:00Z",
  "replied_comment_ids": []
}
```

The `replied_comment_ids` array tracks which PR review comments have been replied to, preventing duplicate replies on resume (see [Reply Tracking](#reply-tracking)).

### Phases

There are two job entry points: **new issue** (full pipeline) and **PR review** (implementation only).

```
New issue (minor change):
  (no job) ──→ analysis ──→ implementation ──→ pr ──→ (complete)

New issue (non-minor change):
  (no job) ──→ analysis ──→ plan-approval ──→ implementation ──→ pr ──→ (complete)
                                  │  ↑
                                  │  └── counter-proposal → re-analysis ─┘
                                  └── no response → wait (next heartbeat)

PR review:
  (no job) ──→ pr-review ──→ pr ──→ (complete)
```

Any phase can be interrupted; the next heartbeat resumes from the saved phase.

| Phase | Description | On interruption |
|-------|-------------|-----------------|
| `analysis` | Claude reads issue + codebase, produces a plan | Restart analysis from scratch |
| `plan-approval` | Wait for human approval of the posted plan | Check again next heartbeat (retries not counted) |
| `implementation` | Claude writes code, runs tests, commits | Resume via `claude --resume <session_id>` |
| `pr-review` | Claude addresses reviewer feedback on existing PR, commits | Resume via `claude --resume <session_id>` |
| `pr` | Push branch, create/update PR, comment | Retry PR creation |

### Resume logic

When a heartbeat finds `current-job.json`:

1. Read `source`, `phase`, `session_id`, `retries`
2. If `retries >= PRAUTO_MAX_RETRIES_PER_JOB`: abandon job, comment on issue, apply `prauto:failed` label
3. Increment `retries`, update `last_heartbeat`
4. Resume from the saved phase:
   - `analysis`: re-run analysis from scratch (analysis is cheap)
   - `plan-approval`: check for approval comment; retries not incremented for this phase
   - `implementation`: if `session_id` exists, use `claude --resume <session_id>`; otherwise start fresh
   - `pr-review`: if `session_id` exists, use `claude --resume <session_id>`; otherwise start fresh
   - `pr`: retry PR creation/push

### Job completion

A job is considered successfully completed in two scenarios:

**New issue → PR creation:**

1. Push branch and create PR (with `prauto:review` label and assignee on PR)
2. Move `current-job.json` to `state/history/YYYYMMDD_I-{number}.json`
3. Update issue labels: remove `prauto:wip`, add `prauto:review`

**PR comment response → follow-up commits:**

1. Address reviewer feedback with additional commits on the PR branch
2. Push new commits
3. Record replied comment IDs in `current-job.json` (see [Reply Tracking](#reply-tracking))
4. Reply to each addressed reviewer comment (with idempotency check — see [Comment Idempotency](#comment-idempotency))
5. Move `current-job.json` to `state/history/YYYYMMDD_I-{number}.json`

### Job abandonment

After max retries:

1. Move `current-job.json` to `state/history/` **first** (state transition is the critical step)
2. Update issue labels: remove `prauto:wip`, add `prauto:failed`
3. Post comment (with idempotency check — see [Comment Idempotency](#comment-idempotency)): "prauto({worker_id}): Abandoning after {n} retries. Manual intervention needed."

Moving the state file before posting the comment ensures that if the comment call fails, the job is still properly completed and the next heartbeat will not re-abandon.

---

## Issue Discovery Protocol

### Label-based discovery

Prauto uses GitHub labels to track issue lifecycle. No GitHub bot account is required — labels are visible to all team members and controllable by anyone with triage access.

### Label lifecycle

```
[human adds prauto:ready]
    │
    ├── prauto claims → removes prauto:ready, adds prauto:wip, sets assignee
    │       │
    │       ├── success → removes prauto:wip, adds prauto:review (on issue + PR)
    │       │       │
    │       │       └── approved + squash-finalized → removes prauto:review, adds prauto:done (on issue + PR)
    │       │
    │       └── failure → removes prauto:wip, adds prauto:failed
    │
    └── (no prauto pickup yet → stays prauto:ready)
```

### Search priority

```bash
gh issue list \
  -R "$PRAUTO_GITHUB_REPO" \
  --label "$PRAUTO_GITHUB_LABEL_READY" \
  --state open \
  --json number,title,body,labels,author
```

Filter results to exclude issues already labeled `prauto:wip` or `prauto:review`. Sort by issue number ascending (oldest first) and take the first match.

### Organization-member filter

When `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` is set to a non-empty value (e.g. `"true"`), `find_eligible_issue()` adds an author-membership check before selecting an issue:

1. Derive the organization name from `PRAUTO_GITHUB_REPO` (the part before `/`)
2. Fetch the list of organization members via `gh api orgs/{org}/members --paginate`
3. Filter candidate issues to those whose `author.login` appears in the member list
4. Issues authored by non-members are silently skipped

This prevents external actors from injecting work into the prauto pipeline by opening issues with the `prauto:ready` label. The feature is designed for organization-owned repositories; if the repository owner is a personal account (not an organization), the GitHub API call will fail and `find_eligible_issue()` returns with a warning.

### Claiming an issue

1. **Optimistic lock via label swap**: Add `prauto:wip` label, then immediately re-fetch the issue labels
2. **Race detection**: If `prauto:wip` was already present before this worker added it (another worker claimed first), back off — do not proceed, exit this heartbeat
3. Remove label `prauto:ready`, set issue assignee to `PRAUTO_GITHUB_ACTOR`
4. Post comment (with idempotency check — see [Comment Idempotency](#comment-idempotency)): `prauto({worker_id}): Claimed this issue. Starting work.`

When resuming a `prauto:wip` issue, the worker verifies the issue assignee matches `PRAUTO_GITHUB_ACTOR` before proceeding. This ensures only the owning worker resumes its own jobs.

The add-then-verify pattern detects concurrent claims: if two workers both add `prauto:wip` to an issue that already had it, the second worker sees the label was present before its own addition and backs off. The worker that observes a clean add (label was not previously present) wins.

### Issue body conventions

For best results, issues should include:

- A clear description of what needs to be done
- References to relevant spec files (e.g., "See `spec/feature/API.md` section X")
- Acceptance criteria or expected behavior
- File paths if the scope is known

Prauto can work with minimal issue descriptions but produces better results with structured context.

---

## Claude Code Invocation

### Multi-phase execution model

Prauto splits each job into multiple Claude Code sessions with different tool permissions:

| Phase | Purpose | Tools | Max turns |
|-------|---------|-------|-----------|
| Analysis | Read codebase, understand issue, produce plan | Read-only | 10 |
| Implementation | Write code, run tests, commit | Read + Write + limited Bash | 50 |
| Squash commit | Generate final commit message from issue + diff | None (text generation only) | 1 |

`--max-turns` is the primary guard against runaway sessions. On Pro/Max subscription plans, this is the only effective limiter. `--max-budget-usd` can be added as an additional cap for API (pay-per-token) billing but has no effect on subscription plans.

### CLI invocation pattern

The analysis and implementation phases use the same invocation structure with phase-specific variables:

```bash
# Analysis phase: PHASE_MAX_TURNS=$PRAUTO_CLAUDE_MAX_TURNS_ANALYSIS, PHASE_BUDGET=$PRAUTO_CLAUDE_MAX_BUDGET_ANALYSIS
# Implementation phase: PHASE_MAX_TURNS=$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION, PHASE_BUDGET=$PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION

claude -p "<prompt>" \
  --append-system-prompt-file ".prauto/state/.system-append-rendered.md" \
  --model "$PRAUTO_CLAUDE_MODEL" \
  --output-format json \
  --max-turns "$PHASE_MAX_TURNS" \
  ${PHASE_BUDGET:+--max-budget-usd "$PHASE_BUDGET"} \
  --allowedTools <phase whitelist> \
  --disallowedTools <denylist> \
  --dangerously-skip-permissions
```

### Tool whitelists by phase

**Phase 1 — Analysis (read-only)**:

```
Read, Glob, Grep,
Bash(git log *), Bash(git diff *), Bash(git status *), Bash(git branch *)
```

**Phase 2 — Implementation (read + write)**:

```
Read, Write, Edit, Glob, Grep,
Bash(git log *), Bash(git diff *), Bash(git status *), Bash(git branch *),
Bash(git add *), Bash(git commit *),
Bash(pytest *), Bash(python3 *),
Bash(npm run *), Bash(npx prettier *), Bash(npx tsc *),
Bash(ruff *)
```

### Tool denylist (both phases)

The denylist is **defense-in-depth**: the whitelist already restricts Claude to only the listed tools, so unlisted tools like `curl` are blocked regardless. The denylist provides an explicit second layer that remains effective even if the whitelist is accidentally broadened.

```
Bash(git push *), Bash(rm -rf *), Bash(sudo *),
Bash(kubectl *), Bash(helm *),
Bash(curl *), Bash(wget *), Bash(gh *),
Read(.prauto/config.local.env), Read(.prauto/state/*),
WebFetch, WebSearch
```

**Rationale**:

- **No git push**: only `git-ops.sh` pushes, preventing Claude from pushing to unexpected remotes or branches
- **No gh**: only orchestrator scripts interact with GitHub (issues, PRs, labels, comments)
- **No network**: prevents data exfiltration and uncontrolled API calls
- **No cluster ops**: prauto works on code, not infrastructure
- **No destructive shell**: aligned with the project's "never destroy" principle

### Why `--dangerously-skip-permissions`

In non-interactive (`-p`) mode, Claude Code cannot prompt for tool approval. The `--dangerously-skip-permissions` flag is required. However, the combination of `--allowedTools` (explicit whitelist) and `--disallowedTools` (explicit denylist) ensures Claude can only use approved tools. This is the unattended equivalent of the project's `settings.json` permission model.

### Session files

Each Claude phase saves its output to `state/sessions/`:

| Phase | File | Format |
|-------|------|--------|
| Analysis | `analysis-I-{N}.txt` | Plain text (analysis output) |
| Implementation | `impl-I-{N}.json` | JSON (full Claude output) |
| PR review | `review-I-{N}.json` | JSON (full Claude output) |

The session ID (`session_id` field from JSON output) is extracted and stored in `current-job.json` for resume support.

### Session resumption

When a job is interrupted mid-implementation:

1. The Claude session output (JSON) is saved to `state/sessions/impl-I-{number}.json`
2. The session ID is extracted and stored in `current-job.json`
3. On resume, Claude is invoked with `--resume <session_id>` plus a continuation prompt

```bash
claude --resume "$SESSION_ID" \
  -p "Continue the implementation. Check what has been done so far and pick up where you left off." \
  --output-format json \
  --max-turns "$PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION" \
  ${PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION:+--max-budget-usd "$PRAUTO_CLAUDE_MAX_BUDGET_IMPLEMENTATION"} \
  --allowedTools <implementation whitelist> \
  --disallowedTools <denylist> \
  --dangerously-skip-permissions
```

---

## PR Lifecycle

### Branch naming

```
prauto/I-{issue_number}
```

Example: issue #42 produces branch `prauto/I-42`.

### Branch creation

Branches are created as isolated git worktrees, not in the main repo directory. `create_branch` in `lib/git-ops.sh` sets both `BRANCH_NAME` and `WORKTREE_DIR`:

```bash
BRANCH_NAME="prauto/I-${ISSUE_NUMBER}"
WORKTREE_DIR=".prauto/worktrees/I-${ISSUE_NUMBER}"

git fetch origin
# New branch:
git worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "origin/${PRAUTO_BASE_BRANCH}"
# Existing branch (retry scenario):
git worktree add "$WORKTREE_DIR" "$BRANCH_NAME"
```

After `create_branch`, `heartbeat.sh` immediately `cd`s into `$WORKTREE_DIR` so all subsequent git and Claude operations run there. The EXIT trap (`cleanup()`) removes the worktree via `git worktree remove --force` on exit.

### Push and PR creation

After implementation completes, `git-ops.sh` handles:

1. **Push**: `git push -u origin prauto/I-{number}`
2. **Check for existing PR**: `gh pr list --head prauto/I-{number}`
3. **Create PR** (if none exists):

```bash
gh pr create \
  -R "$PRAUTO_GITHUB_REPO" \
  --base "$PRAUTO_BASE_BRANCH" \
  --head "prauto/I-${ISSUE_NUMBER}" \
  --title "${ISSUE_TITLE}" \
  --body "<generated PR body>" \
  --assignee "${PRAUTO_GITHUB_ACTOR}" \
  --label "${PRAUTO_GITHUB_LABEL_REVIEW}"
```

4. **Update PR** (if exists): push new commits and add a comment

### PR body format

```markdown
## Summary

Automated implementation for #{issue_number}.
Generated by `prauto({worker_id})` using Claude Code CLI.

## Changes

<commit log: git log --oneline origin/{base}..HEAD>

## Test plan

- [ ] Review automated changes
- [ ] Verify tests pass in CI
- [ ] Check spec compliance

---
*Generated by prauto -- autonomous PR worker*
```

### PR review handling

Heartbeat step 6 scans for open PRs on branches matching the `prauto/` prefix:

1. List all open PRs via `gh pr list --state open --json number,headRefName,reviews,labels,assignees --limit 50`, then filter client-side to entries whose `headRefName` starts with `PRAUTO_BRANCH_PREFIX` (`prauto/`), have the `prauto:review` label, and are assigned to this worker (`PRAUTO_GITHUB_ACTOR`), sorted by number ascending.
2. For each candidate, fetch both inline review comments (`pulls/{N}/comments`) and issue-level comments (`issues/{N}/comments`). Before evaluating feedback, check the latest issue-level comment from the prauto actor — if it starts with `"Reviewer feedback addressed"`, skip this PR (prevents infinite re-pickup loops).
3. A PR is actionable if it has unaddressed non-prauto comments **and** at least one `CHANGES_REQUESTED`/`COMMENTED` review or external issue-level comment.
4. If no PR has actionable feedback: continue to step 7 (find new issue)
5. If a PR has actionable feedback (take the oldest PR first):
   a. Create `current-job.json` with `"source": "pr-review"`, `"phase": "pr-review"`, the linked issue number, existing branch name, and `"replied_comment_ids": []`
   b. Create a worktree for the existing PR branch at `.prauto/worktrees/{branch}` via `checkout_branch_worktree`, then `cd` into it
   c. Run the `pr-review` phase (same tool whitelist as implementation) with reviewer comments as context; save session output to `state/sessions/review-I-{N}.json`
   d. Push additional commits to the PR branch; call `create_or_update_pr` to add a commit-log comment to the existing PR
   e. Post a `"prauto(...): Reviewer feedback addressed."` marker comment via `post_feedback_addressed_comment`
   f. Complete the job (move to history) and **exit the heartbeat**

The feedback-addressed marker is the key mechanism that breaks the re-pickup loop: once prauto addresses feedback and posts the marker, subsequent heartbeats see the marker and skip the PR. When a reviewer posts new comments after the marker, the marker is no longer the latest prauto comment, and the PR becomes actionable again.

The `pr-review` phase uses `PRAUTO_CLAUDE_MAX_TURNS_IMPLEMENTATION` and the implementation tool whitelist, since it performs the same kind of work (writing code, running tests, committing).

### Squash-finalize action

Heartbeat step 5.5 runs **before** the PR review check, so finalizing approved work takes priority over addressing feedback on unapproved work.

**Trigger conditions** (all must be true):

- Branch matches `PRAUTO_BRANCH_PREFIX` (`prauto/`)
- PR has `prauto:review` label
- PR is assigned to `PRAUTO_GITHUB_ACTOR`
- `mergeable == "MERGEABLE"` — no merge conflicts
- `mergeStateStatus == "CLEAN"` — all branch-protection rules satisfied (required reviews, required CI checks)
- Latest review from an organization member has state `APPROVED`

**Steps**:

1. `find_mergeable_prs()` scans open prauto PRs sorted by number ascending, returning the oldest qualifying PR
2. `checkout_branch_worktree` creates a worktree for the PR branch
3. `squash_and_finalize_pr()` executes:
   a. `git fetch origin <base_branch>`
   b. `git rebase origin/<base_branch>` — if conflict, `git rebase --abort` and return 1
   c. Find merge base
   d. Fetch the linked issue's title and body from GitHub (`gh issue view`)
   e. Generate `git diff --stat` and `git diff` from the merge base for change context
   f. Invoke Claude (1-turn, no tools) with the issue description and diff to generate a conventional commit message (see format below)
   g. Always rebuild as a single commit: `git reset --soft <merge_base>` then `git commit --author=... --file=<msg>` — regardless of current commit count
   h. `git push --force-with-lease origin <branch>` — aborts if remote changed unexpectedly
   i. Update PR title to the final squashed commit title
   j. Set `prauto:done` label on PR (remove `prauto:review`)
   k. Set `prauto:done` label on linked issue (remove `prauto:review`)
   l. Does **NOT** merge the PR or close the issue — leaves that for the human

**Commit message generation**:

The squash commit message is generated by Claude using a focused single-turn invocation with the `prompts/squash-commit.md` template. Inputs: issue title, issue body, `git diff --stat`, and truncated `git diff` (≤4000 chars). Claude produces a conventional commit message following the project's commit convention.

**Commit message format**:

```
<type>: <subject>

<brief description — max 5 lines>
(issue #<issue_number>, PR #<pr_number>)

Co-Authored-By: <approver name> <approver email>
```

The `Co-Authored-By` trailers are appended automatically for all organization-member PR approvers. The function queries the PR's reviews via `gh pr view`, extracts approvers who are org members, and appends a trailer for each.

If Claude fails to generate a message, the fallback is the issue title with the issue/PR reference appended.

All git write operations (rebase, commit, push) use explicit `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` env vars from `config.local.env` to ensure consistent authorship.

---

## Prompt Templates

### `prompts/system-append.md` — Worker identity

Contains `{PRAUTO_WORKER_ID}`, `{PRAUTO_GIT_AUTHOR_NAME}`, and `{PRAUTO_GIT_AUTHOR_EMAIL}` placeholders. At runtime `prepare_system_prompt()` in `lib/claude.sh` substitutes these variables and writes the result to `state/.system-append-rendered.md`. That rendered file is what is passed to `--append-system-prompt-file`. This preserves all built-in capabilities while adding prauto-specific constraints.

```markdown
## Prauto Worker Identity

You are operating as an autonomous PR worker named `{PRAUTO_WORKER_ID}` for the DataSpoke project.
You are NOT in an interactive session. Complete your work independently.

### Constraints
- Do NOT ask questions or wait for user input. Make reasonable decisions.
- Do NOT push to git. Stage and commit; the orchestrator handles pushing.
- Follow the commit convention: `<type>: <subject>` (Conventional Commits).
- Keep commits concise: 1-line subject, max 3-line body if needed.
- Read the spec hierarchy before coding (per CLAUDE.md instructions).
- Follow existing code patterns in the repository.
- Run tests after changes to verify correctness.
- If blocked, commit what you have with a clear TODO comment.

### Git identity
Use: git commit --author="{PRAUTO_GIT_AUTHOR_NAME} <{PRAUTO_GIT_AUTHOR_EMAIL}>"
```

Variables are substituted at runtime by `lib/claude.sh`.

### `prompts/issue-analysis.md` — Phase 1

```markdown
Analyze the following GitHub issue and produce an implementation plan.

## Issue #{number}: {title}

{body}

## Instructions

1. Read the DataSpoke spec hierarchy to understand context:
   - `spec/MANIFESTO_en.md` for product identity
   - `spec/ARCHITECTURE.md` for system architecture
   - Relevant feature specs in `spec/feature/` or `spec/feature/spoke/`
2. Examine the current codebase to understand what exists.
3. Produce an implementation plan:
   - Files to create or modify
   - Order of changes
   - Existing patterns to follow
   - Tests needed
   - Risks or open questions

Do NOT make code changes. Analysis only.
```

### `prompts/implementation.md` — Phase 2

```markdown
Implement changes for GitHub issue #{number} on branch `{branch}`.

## Instructions

1. Follow the implementation plan from the analysis phase (provided below).
2. Read relevant specs before writing code.
3. Follow existing code patterns.
4. Write tests for your changes.
5. Run tests to verify (pytest for Python, npx tsc for TypeScript).
6. Run formatters (ruff for Python, npx prettier for TypeScript).
7. Stage and commit with conventional commit messages.
   Use: git commit --author="{author_name} <{author_email}>"
8. Do NOT push. The orchestrator handles pushing.

## Analysis Output

{analysis_output}
```

### `prompts/squash-commit.md` — Phase 4 (squash-finalize)

```markdown
Generate a single conventional commit message for the following squash commit.

## Issue #{issue_number}: {issue_title}

{issue_body}

## Changed files

{diff_stat}

## Diff (truncated)

{diff}

## Rules

1. First line: `<type>: <subject>` — conventional commit format (feat, fix, docs, refactor, etc.)
2. Second line: blank
3. Body: brief description of what was done (max 5 lines). Focus on the "why" and key changes.
4. Last line of body MUST be: `(issue #{issue_number}, PR #{pr_number})`
5. Output ONLY the raw commit message text. No markdown fences, no explanations.
```

This prompt is invoked with `--max-turns 1` and no allowed tools (text generation only). The diff is truncated to ≤4000 characters to stay within prompt budget.

---

## Write Idempotency

Autonomous workers must be resilient to crashes and restarts. Every write action (comment, label change, PR creation) can potentially be interrupted, leaving partial state. This section defines the safeguards that prevent repeated writes.

### Comment idempotency

Before posting any comment on an issue or PR, the worker **must** check for an existing comment that matches:

1. **Author**: the worker's GitHub user (derived from `GH_TOKEN`)
2. **Prefix**: `prauto({PRAUTO_WORKER_ID}):` followed by the action keyword

Action keywords by context:

| Context | Keyword | Example prefix |
|---------|---------|----------------|
| Issue claim | `Claimed` | `prauto(prauto01): Claimed this issue.` |
| Abandonment | `Abandoning` | `prauto(prauto01): Abandoning after 3 retries.` |
| PR review reply | `Addressed` | `prauto(prauto01): Addressed feedback in latest commits.` |
| Feedback marker | `Reviewer feedback addressed` | `prauto(prauto01): Reviewer feedback addressed.` |
| Quota pause | `Paused` | `prauto(prauto01): Paused — Claude token quota exhausted.` |
| Quota resume | `Resumed` | `prauto(prauto01): Resumed — Claude token quota is now available.` |

Implementation:

```bash
# Check before posting. Returns 0 (found) or 1 (not found).
comment_exists() {
  local target_type="$1"  # "issue" or "pr"
  local target_number="$2"
  local keyword="$3"
  local prefix="prauto(${PRAUTO_WORKER_ID}): ${keyword}"

  gh "${target_type}" view "$target_number" \
    -R "$PRAUTO_GITHUB_REPO" \
    --json comments \
    --jq ".comments[] | select(.body | startswith(\"${prefix}\")) | .id" \
  | head -1 | grep -q .
}

# Usage: only post if no matching comment exists
if ! comment_exists "issue" "$ISSUE_NUMBER" "Claimed"; then
  gh issue comment "$ISSUE_NUMBER" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Claimed this issue. Starting work."
fi
```

### Reply tracking

The `replied_comment_ids` array in `current-job.json` is initialized to `[]` on every new job and is loaded by `load_job`. The helper `add_replied_comment_id` in `lib/state.sh` and `reply_to_comments` in `lib/git-ops.sh` are defined for per-comment reply tracking, but **the PR review flow in the current implementation does not post individual replies to reviewer comments**. After addressing feedback via Claude, prauto pushes commits and calls `create_or_update_pr`, which adds a commit-log comment to the PR body — this is the only reply mechanism currently wired in.

The infrastructure (field, helpers) is in place for future fine-grained reply tracking.

### Optimistic claim locking

Multi-worker environments risk two workers claiming the same issue simultaneously. The claim protocol uses an optimistic locking pattern:

```bash
claim_issue() {
  local issue_number="$1"

  # Step 1: Check if prauto:wip is already present (another worker got there first)
  local current_labels
  current_labels=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json labels --jq '.labels[].name')
  if echo "$current_labels" | grep -q "^${PRAUTO_GITHUB_LABEL_WIP}$"; then
    warn "Issue #${issue_number} already has ${PRAUTO_GITHUB_LABEL_WIP} — another worker claimed it"
    return 1
  fi

  # Step 2: Add prauto:wip label
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP"

  # Step 3: Re-fetch and verify this worker won the race
  sleep 2  # brief delay to allow concurrent writers to settle
  local wip_comments
  wip_comments=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '[.comments[] | select(.body | startswith("prauto(")) | select(.body | contains("Claimed"))] | length')

  if [[ "$wip_comments" -gt 0 ]]; then
    warn "Issue #${issue_number} was claimed by another worker during race window"
    return 1
  fi

  # Step 4: Remove prauto:ready, post claim comment
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_READY"

  if ! comment_exists "issue" "$issue_number" "Claimed"; then
    gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --body "prauto(${PRAUTO_WORKER_ID}): Claimed this issue. Starting work."
  fi
}
```

The two-step check-then-add pattern is not fully atomic, but the verification window (step 3) catches most races. For single-worker deployments this is a no-op safeguard.

---

## Security Model

### Principle: Least privilege for autonomous operation

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
| Concurrency | One job at a time | PID-based lock file |
| GitHub access | Fine-grained PAT | Scoped to issues, PRs, contents only |
| Secrets (git) | Gitignored local env | `config.local.env` never committed |
| Secrets (runtime) | Original protected by denylist; backup removed on exit | `heartbeat.sh` copies `config.local.env` to `/tmp`; denylist blocks `Read(.prauto/config.local.env)` |

### Why Claude cannot push

Separating "write code" from "push to remote" is a deliberate safety boundary. Claude commits locally; the bash orchestrator decides whether and where to push. This prevents Claude from pushing to unexpected branches or remotes, even if a prompt injection attempts to override instructions.

### Secrets isolation

The `Read` tool (whitelisted in both phases) can access any file by path. To prevent Claude from reading secrets, `heartbeat.sh` performs a **pre-invocation lockdown**:

1. Source `config.local.env` into shell variables (so `ANTHROPIC_API_KEY`, `GH_TOKEN`, etc. are in the shell environment)
2. Copy `config.local.env` to `/tmp/.prauto-secrets-$$` as a backup (original stays in place)
3. Export `ANTHROPIC_API_KEY` and `GH_TOKEN` only if non-empty; otherwise unset them so CLIs use system auth
4. Invoke Claude — the `--disallowedTools` denylist entry `Read(.prauto/config.local.env)` prevents Claude from reading the file even though it remains on disk
5. Exit: the EXIT trap (`cleanup()`) removes the temp backup (`rm -f`)

Additionally, `.prauto/state/` is added to the denylist to prevent reading lock files or job history:

```
Read(.prauto/config.local.env), Read(.prauto/state/*)
```

These patterns are appended to the `--disallowedTools` list in both phases.

**Other secret paths**:

- `ANTHROPIC_API_KEY` is exported into the shell environment (not as a CLI arg); omitted entirely if empty
- `GH_TOKEN` is used only by `gh` CLI calls in the bash scripts; omitted entirely if empty

---

## Integration with AI Scaffold

### What prauto uses from `.claude/`

| Scaffold element | Integration |
|---|---|
| `CLAUDE.md` | Claude reads this automatically, giving prauto full project context |
| `.claude/settings.json` hooks | `auto-format.sh` fires after Write/Edit in prauto sessions |
| `.claude/agents/` | Prauto prompts can instruct Claude to delegate to existing subagents |
| `.claude/skills/` | Skills are available if Claude detects matching context |
| `spec/` hierarchy | Analysis phase reads specs per CLAUDE.md instructions |

### What prauto does NOT modify

| Element | Reason |
|---|---|
| `.claude/settings.json` | Prauto uses CLI flags for tool restrictions |
| `.claude/settings.local.json` | Prauto has its own config |
| `.claude/agents/` | No new subagents added |
| `.claude/commands/` | Prauto is not a Claude Code command |

### Coexistence

Prauto is self-contained in `.prauto/`. The only changes to existing files are two lines in `.gitignore`. The scaffold and prauto operate independently: the scaffold serves interactive Claude Code sessions; prauto serves unattended cron-driven automation. Both use the same Claude Code engine, the same `CLAUDE.md` context, and the same auto-format hook.

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
| `lib/git-ops.sh` (gh pr create) | Built-in: claude-code-action creates branches and provides PR prefill links |

The prompt templates in `.prauto/prompts/` and the tool restrictions can be reused without modification. The main difference is that `claude-code-action` does not create PRs directly (it provides prefill links), so the GH Actions version would use a separate workflow step for `gh pr create`.

### Migration path

1. Create `.github/workflows/prauto.yml` with `schedule:` and `issues:` triggers
2. Move secrets to GitHub Actions secrets
3. Reference `.prauto/prompts/` for prompt content
4. Use `claude-code-action` with `claude_args` mirroring the CLI flags from `config.env`
5. Keep `.prauto/` scripts as the local development/testing path
