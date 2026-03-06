---
name: prauto-check-status
description: Query GitHub to display prauto-managed issues and PRs across lifecycle labels (ready, wip, plan-review, review, done, failed) and predict the next heartbeat action. Use when the user asks about prauto work status, backlog, specific issue pickup, failed jobs, PR approval/merge readiness, reviewer feedback, quota-pause state, or wants a status dashboard.
allowed-tools: Bash(gh *), Bash(jq *), Bash(cat *), Bash(date *), Bash(export *), Bash(*status.sh*), Read
---

## Quick path: helper script

A helper script at `.claude/skills/prauto-check-status/status.sh` automates the full dashboard and next-heartbeat prediction. Run it first:

```bash
.claude/skills/prauto-check-status/status.sh          # full report
.claude/skills/prauto-check-status/status.sh next      # prediction only
.claude/skills/prauto-check-status/status.sh wip       # single section
```

Filters: `ready`, `wip`, `review`, `done`, `failed`, `next`, `all` (default).

If `$ARGUMENTS` contains a filter keyword, pass it to the script. Present the script output to the user, then add any additional analysis below (e.g., org-member eligibility details, specific comment inspection, quota status) using the manual queries described in the reference sections.

---

## Reference: manual queries

Use these when the script output needs deeper investigation (e.g., inspecting specific issue comments, verifying org-member eligibility, checking quota-paused markers).

### Config loading

Read `.prauto/config.env` (shared) and `.prauto/config.local.env` (instance-specific) to get:
- `PRAUTO_GITHUB_REPO`, label names, `PRAUTO_BASE_BRANCH`, `PRAUTO_BRANCH_PREFIX`
- `PRAUTO_WORKER_ID`, `PRAUTO_MAX_RETRIES_PER_JOB`, `PRAUTO_OPEN_ISSUE_LIMIT` (default: 1)
- `GH_TOKEN` (export if non-empty)

Resolve actor: `gh api user --jq '.login'`

### Issue comments (plan approval, retries, quota)

```bash
gh issue view <number> -R "$REPO" --json comments \
  --jq '.comments | [.[] | {body: .body, author: .author.login, createdAt: .createdAt}]'
```

- **Plan approval**: Look for `prauto(<worker>): Plan` comment, then check if any non-prauto comment after it says `go ahead` (approved) or contains a counter-proposal.
- **Retry count**: Count comments matching `prauto(<worker>): Heartbeat`.
- **Quota-paused**: Latest `prauto(<worker>):` comment body contains `<!-- prauto:quota-paused -->`.

### PR review status (for prauto:review issues)

Find PR by branch: `gh pr list -R "$REPO" --head "prauto/I-<issue_number>" --json number,title --jq '.[0]'`

Check merge readiness: `gh pr view <pr_number> -R "$REPO" --json reviews,mergeable,mergeStateStatus`

### Org members

```bash
ORG=$(echo "$PRAUTO_GITHUB_REPO" | cut -d/ -f1)
gh api "orgs/${ORG}/members" --paginate --jq '[.[].login]'
```

---

## Heartbeat decision model (for interpreting results)

The heartbeat has two stages:

**Stage 1 — Claim**: Count all open issues assigned to worker with any `prauto:` label. If under `PRAUTO_OPEN_ISSUE_LIMIT`, claim the oldest eligible `prauto:ready` issue.

**Stage 2 — Process all claimed issues** (oldest first). Per-issue state machine:

| Label | Phase derivation | Action |
|-------|-----------------|--------|
| `prauto:wip` + `prauto:plan-review` | plan-approval | Skip if no response; start impl if approved; revise plan if counter-proposal |
| `prauto:wip` (no plan-review) | PR exists → pr; plan approved → implementation; else → analysis | Run phase handler (retry-tracked via heartbeat comments) |
| `prauto:review` | check PR | Squash-finalize if approved+mergeable+CLEAN; address feedback if unaddressed comments; else skip |
| `prauto:done` / `prauto:failed` | terminal | Skip |
