---
name: prauto-check-status
description: Query GitHub to display prauto-managed issues and PRs across lifecycle labels (ready, wip, plan-review, review, done, failed) and predict the next heartbeat action. Use when the user asks about prauto work status, backlog, specific issue pickup, failed jobs, PR approval/merge readiness, reviewer feedback, quota-pause state, or wants a status dashboard.
allowed-tools: Bash(gh *), Bash(jq *), Bash(cat *), Bash(date *), Bash(export *), Read
---

## Setup

1. **Load prauto config**: Read `.prauto/config.env` (shared) and `.prauto/config.local.env` (instance-specific) to get:
   - `PRAUTO_GITHUB_REPO` — e.g. `selhorys/dataspoke-baseline`
   - `PRAUTO_GITHUB_LABEL_READY`, `PRAUTO_GITHUB_LABEL_WIP`, `PRAUTO_GITHUB_LABEL_REVIEW`, `PRAUTO_GITHUB_LABEL_FAILED`, `PRAUTO_GITHUB_LABEL_DONE`, `PRAUTO_GITHUB_LABEL_PLAN_REVIEW`
   - `PRAUTO_BASE_BRANCH`, `PRAUTO_BRANCH_PREFIX`
   - `PRAUTO_WORKER_ID`, `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY`, `PRAUTO_MAX_RETRIES_PER_JOB`
   - `GH_TOKEN` (export if non-empty, so `gh` authenticates correctly)

2. **Resolve GitHub actor**: Run `gh api user --jq '.login'` to get the authenticated user login.

3. **Fetch org members** (if `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` is `"true"`):
   ```bash
   ORG=$(echo "$PRAUTO_GITHUB_REPO" | cut -d/ -f1)
   gh api "orgs/${ORG}/members" --paginate --jq '[.[].login]'
   ```

---

## Part 1: Status Dashboard

Query GitHub and report each category. Use the repo and label values from config. **Always export GH_TOKEN before running any `gh` command if it is set in config.**

### 1a. Issues with `prauto:ready` (Available for pickup)

```bash
gh issue list -R "$REPO" --label "$LABEL_READY" --state open \
  --json number,title,author,createdAt,labels --limit 50
```

For each issue:
- Show `#number — title` (author, created date)
- If org-member filter is enabled, indicate whether the author is an org member (eligible) or not (will be skipped)

### 1b. Issues with `prauto:wip` (Work in progress)

```bash
gh issue list -R "$REPO" --label "$LABEL_WIP" --state open \
  --json number,title,assignees,labels --limit 50
```

For each issue:
- Show `#number — title` (assigned to whom)
- **Check `prauto:plan-review` label**: If present, the plan is posted and awaiting human approval (fast check, no need to scan comments).
- If no `prauto:plan-review` label, **check plan approval status**: Fetch issue comments and look for the plan comment pattern (`prauto(<worker>): Plan`). Then check if any non-prauto comment after the plan says `go ahead` (approved) or contains a counter-proposal.
  ```bash
  gh issue view <number> -R "$REPO" --json comments \
    --jq '.comments | [.[] | {body: .body, author: .author.login, createdAt: .createdAt}]'
  ```
- Report: "Plan posted, awaiting approval" / "Plan approved — ready for implementation" / "Counter-proposal received"
- **Count heartbeat comments for retry estimate**: Count comments matching `prauto(<worker>): Heartbeat` to determine attempt count (replaces local retries counter).
- **Check quota-paused status**: From the same comments, find the latest `prauto(<worker>):` comment. If its body contains `<!-- prauto:quota-paused -->`, report: "Quota-paused — waiting for Claude token quota to become available"

### 1c. PRs with `prauto:review` (Awaiting review + merge conditions)

```bash
gh pr list -R "$REPO" --label "$LABEL_REVIEW" --state open \
  --json number,title,headRefName,assignees,labels --limit 50
```

For each PR:
- Show `#number — title` (branch, assignee)
- **Check org-member approval**:
  ```bash
  gh pr view <number> -R "$REPO" --json reviews,mergeable,mergeStateStatus
  ```
  - List all reviews: who reviewed, what state (APPROVED, CHANGES_REQUESTED, COMMENTED)
  - Determine if any org member's **latest** review is APPROVED
  - Report `mergeable` status and `mergeStateStatus`
- Summarize: "Approved by <user>, mergeable=MERGEABLE, status=CLEAN — ready for squash-finalize" or what's still missing

### 1d. PRs/Issues with `prauto:done` (Squash-finalized, awaiting manual merge)

```bash
gh pr list -R "$REPO" --label "$LABEL_DONE" --state open \
  --json number,title,headRefName --limit 50
```

For each: show `#number — title` (branch). These are ready for a human to merge.

Also check closed PRs with `prauto:done` to show recently completed items:
```bash
gh pr list -R "$REPO" --label "$LABEL_DONE" --state merged \
  --json number,title,mergedAt --limit 10
```

### 1e. Issues/PRs with `prauto:failed`

```bash
gh issue list -R "$REPO" --label "$LABEL_FAILED" --state open \
  --json number,title,assignees --limit 50
```

For each: show `#number — title`. These need manual intervention.

---

## Part 2: Next Heartbeat Prediction

Based on the data collected above, simulate the heartbeat decision tree from `heartbeat.sh` and describe **exactly what would happen** on the next heartbeat run. The heartbeat has two stages: (1) claim new work if under limit, (2) process all claimed issues.

### Stage 1: Claim new issue

Count all open issues assigned to this worker with any `prauto:` label. Compare against `PRAUTO_OPEN_ISSUE_LIMIT`.

- If count >= limit: "Will **skip** new issue pickup (${count}/${limit} open issues)."
- If count < limit:
  - If eligible `prauto:ready` issue exists (from 1a — org member authored, no wip/review label): "Will **claim** issue #N."
  - If no eligible issue: "No eligible issues to claim."

### Stage 2: Process all claimed issues

List all claimed issues (oldest first). For each, predict the action:

**For `prauto:wip` issues** — derive phase from GitHub (PR exists → `pr`, `prauto:plan-review` label → `plan-approval`, plan approved → `implementation`, nothing → `analysis`), count heartbeat comments for retry estimate.
- If phase is `plan-approval` + no response: "Will **skip** — waiting for plan approval."
- If phase is `plan-approval` + approved: "Will start **implementation**."
- If phase is `plan-approval` + counter-proposal: "Will **revise plan** based on feedback."
- If phase is `analysis`: "Will re-run **analysis** from scratch (attempt N/max)."
- If phase is `implementation`: "Will start fresh **implementation** session — Claude checks branch for existing work (attempt N/max)."
- If phase is `pr`: "Will **push branch** and create/update PR (attempt N/max)."
- If heartbeat comment count >= max retries: "Will **abandon** issue — max retries exceeded."
- If the issue has a `<!-- prauto:quota-paused -->` marker: note quota-paused state.

**For `prauto:review` issues** — check PR status via `check_review_pr()`:
- If approved + mergeable + CLEAN: "Will **squash-finalize** PR #N."
- If unaddressed reviewer comments: "Will **address reviewer feedback** on PR #N."
- Otherwise: "Will **skip** — waiting for review."

**For `prauto:done` / `prauto:failed` issues**: "Will **skip** (terminal state)."

### Additional notes

- If token quota might be exhausted, mention it.
- If there's a lock file present (`.prauto/state/heartbeat.lock`), warn that another heartbeat may be running.
- **Quota-paused marker**: The `<!-- prauto:quota-paused -->` HTML comment is appended to "Paused" comments. Only the **latest** prauto comment is checked — this allows correct detection across Paused→Resumed→Paused cycles. A "Resumed" comment replaces the marker as the latest comment, clearing the paused state.

---

## Output Format

Present results as a structured report:

```
## Prauto Status Report — <timestamp>

**Repo**: <repo>  |  **Worker**: <worker_id>  |  **Actor**: <gh_actor>
**Base branch**: <base_branch>  |  **Org-member filter**: enabled/disabled

### prauto:ready — Available Issues
<table or "None">

### prauto:wip — Work in Progress
<table or "None">
<approval status details>
<heartbeat comment count for retry estimate>

### prauto:review — PRs Awaiting Review
<table or "None">
<approval + mergeable details>

### prauto:done — Finalized (Awaiting Merge)
<table or "None">

### prauto:failed — Failed (Needs Intervention)
<table or "None">

---

### Next Heartbeat Action
<prediction from Part 2, with explanation of why this action takes priority>
```

If `$ARGUMENTS` contains a filter (e.g., "ready", "review", "wip"), only show that section but always include the Next Heartbeat Action.
