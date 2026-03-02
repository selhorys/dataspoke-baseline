---
name: prauto-check-status
description: Check prauto issue/PR status across all lifecycle labels and predict next heartbeat action.
argument-hint: [filter]
allowed-tools: Bash(gh *), Bash(jq *), Bash(cat *), Bash(date *), Bash(export *), Read
---

## Setup

1. **Load prauto config**: Read `.prauto/config.env` (shared) and `.prauto/config.local.env` (instance-specific) to get:
   - `PRAUTO_GITHUB_REPO` — e.g. `selhorys/dataspoke-baseline`
   - `PRAUTO_GITHUB_LABEL_READY`, `PRAUTO_GITHUB_LABEL_WIP`, `PRAUTO_GITHUB_LABEL_REVIEW`, `PRAUTO_GITHUB_LABEL_FAILED`, `PRAUTO_GITHUB_LABEL_DONE`
   - `PRAUTO_BASE_BRANCH`, `PRAUTO_BRANCH_PREFIX`
   - `PRAUTO_WORKER_ID`, `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY`
   - `GH_TOKEN` (export if non-empty, so `gh` authenticates correctly)

2. **Resolve GitHub actor**: Run `gh api user --jq '.login'` to get the authenticated user login.

3. **Read current job state**: If `.prauto/state/current-job.json` exists, read it to understand any in-progress job (issue number, phase, retries, branch, last heartbeat).

4. **Fetch org members** (if `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` is `"true"`):
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

### 1b. Issues with `prauto:wip` (Work in progress — awaiting plan approval)

```bash
gh issue list -R "$REPO" --label "$LABEL_WIP" --state open \
  --json number,title,assignees,labels --limit 50
```

For each issue:
- Show `#number — title` (assigned to whom)
- **Check plan approval status**: Fetch issue comments and look for the plan comment pattern (`prauto(<worker>): Plan`). Then check if any non-prauto comment after the plan says `go ahead` (approved) or contains a counter-proposal.
  ```bash
  gh issue view <number> -R "$REPO" --json comments \
    --jq '.comments | [.[] | {body: .body, author: .author.login, createdAt: .createdAt}]'
  ```
- Report: "Plan posted, awaiting approval" / "Plan approved — ready for implementation" / "Counter-proposal received"
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

Based on the data collected above, simulate the heartbeat decision tree from `heartbeat.sh` and describe **exactly what would happen** on the next heartbeat run. Follow this priority order (heartbeat processes only ONE action per run):

### Decision tree

1. **Active job exists** (`.prauto/state/current-job.json` present)?
   - Yes → "Heartbeat will **resume** job for issue #N (phase: X, retry: Y/max)."
     - If phase is `plan-approval`: "Will check if plan approval has been given."
     - If phase is `analysis`: "Will re-run analysis from scratch."
     - If phase is `implementation`: "Will resume implementation (session: S)."
     - If phase is `pr-review`: "Will address reviewer feedback and push fixes."
     - If phase is `pr`: "Will push branch and create/update PR."
     - If the issue has a `<!-- prauto:quota-paused -->` marker in its latest prauto comment: "Note: heartbeat will first check quota — if still exhausted, it will re-pause without doing work."
   - No → continue to step 1.5.

1.5. **No local job, but orphaned WIP issue** (`prauto:wip` + assigned to this worker, no `current-job.json`)?
   - Yes → "Heartbeat will **recover** orphaned issue #N. It will derive phase from GitHub
     (PR exists → `pr`, plan approved → `implementation`, plan posted → `plan-approval`,
     nothing → `analysis`), rebuild the job file, and exit. The **next** heartbeat will resume work."
   - No → continue to step 2.

2. **Approved + mergeable PR exists** (from 1c above — org-member approved, MERGEABLE, CLEAN)?
   - Yes → "Heartbeat will **squash-finalize** PR #N (branch: B). Will rebase, squash commits, generate commit message via Claude, force-push, and label as `prauto:done`."
   - No → continue to step 3.

3. **PR with unaddressed reviewer comments** (from 1c — CHANGES_REQUESTED or COMMENTED reviews with unaddressed comments)?
   - Yes → "Heartbeat will **address reviewer feedback** on PR #N. Will create a job, run Claude in PR-review mode, push fixes."
   - No → continue to step 4.

4. **Eligible issue with `prauto:ready`** (from 1a — org member authored, no wip/review label)?
   - Yes → "Heartbeat will **claim** issue #N and start analysis phase."
   - No → "Heartbeat will find **no work to do** and exit."

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
