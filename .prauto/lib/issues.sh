# Issue discovery, claiming, and plan lifecycle for prauto.
# Source this file — do not execute directly.
# Requires: helpers.sh sourced (for comment_exists/labels_contain), config loaded,
# gh CLI available. PRAUTO_GITHUB_ACTOR must already be resolved (see heartbeat.sh).
#
# GitHub is the single source of truth. Every reader here derives its answer from
# gh output, never from local state; each wake is a fresh process with nothing
# persisted in memory between ticks.

# get_ready_label_timestamp <issue_number>
# Fetch the timestamp of the LAST prauto:ready label event. This is the lifecycle
# anchor: every comment-scanning function ignores comments before it, so a
# restarted issue starts with a clean slate.
# Sets: READY_LABEL_TIMESTAMP (ISO 8601, or empty if not found).
get_ready_label_timestamp() {
  local issue_number="$1"
  READY_LABEL_TIMESTAMP=$(gh api "repos/${PRAUTO_GITHUB_REPO}/issues/${issue_number}/timeline" \
    --paginate \
    --jq '[.[] | select(.event == "labeled") | select(.label.name == "'"${PRAUTO_GITHUB_LABEL_READY}"'")] | last | .created_at // empty' \
    2>/dev/null) || READY_LABEL_TIMESTAMP=""
  if [[ -n "$READY_LABEL_TIMESTAMP" ]]; then
    info "Ready label timestamp for #${issue_number}: ${READY_LABEL_TIMESTAMP}"
  else
    warn "Could not determine ready label timestamp for #${issue_number}. No comment filtering."
  fi
}

# fetch_org_members
# Fetch organization member logins as a JSON array. Used by the org-member filter
# (PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY) — a drive-by issue author who is not
# an org member is silently skipped.
# Sets: ORG_MEMBERS_JSON. Returns 0 on success, 1 on failure.
fetch_org_members() {
  local org_name="${PRAUTO_GITHUB_REPO%%/*}"
  ORG_MEMBERS_JSON=$(gh api "orgs/${org_name}/members" --paginate --jq '[.[].login]' \
    2>/dev/null | jq -s 'add') || {
    warn "Failed to fetch org members for '${org_name}'."
    return 1
  }
  info "Org-member filter enabled: $(printf '%s' "$ORG_MEMBERS_JSON" | jq 'length') members in '${org_name}'."
  return 0
}

# post_feedback_response_comment <issue_number> <response_text>
post_feedback_response_comment() {
  local issue_number="$1" response_text="$2"
  [[ -z "$response_text" ]] && return 0
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Feedback response

${response_text}" \
    2>/dev/null || warn "Failed to post feedback response on issue #${issue_number}."
}

# find_eligible_issue
# Find the oldest open prauto:ready issue, applying the org-member filter when
# enabled. Sets FOUND_ISSUE_NUMBER/TITLE/BODY. Returns 0 if found, 1 if none.
find_eligible_issue() {
  local issues_json
  issues_json=$(gh issue list -R "$PRAUTO_GITHUB_REPO" \
    --label "$PRAUTO_GITHUB_LABEL_READY" --state open \
    --json number,title,body,labels,author --limit 50 2>/dev/null) || {
    warn "Failed to list issues from GitHub."
    return 1
  }

  local org_members=""
  if [[ "${PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY:-}" == "true" ]]; then
    fetch_org_members || return 1
    org_members="$ORG_MEMBERS_JSON"
  fi

  local filtered
  if [[ -n "$org_members" ]]; then
    filtered=$(printf '%s' "$issues_json" | jq -r \
      --argjson members "$org_members" '
      [.[] | select((.labels | map(.name) | any(startswith("prauto:wip") or startswith("prauto:review"))) | not)
           | select(.author.login as $a | $members | index($a) != null)]
      | sort_by(.number) | .[0] // empty')
  else
    filtered=$(printf '%s' "$issues_json" | jq -r '
      [.[] | select((.labels | map(.name) | any(startswith("prauto:wip") or startswith("prauto:review"))) | not)]
      | sort_by(.number) | .[0] // empty')
  fi

  if [[ -z "$filtered" ]]; then
    info "No eligible issues found with label ${PRAUTO_GITHUB_LABEL_READY}."
    return 1
  fi

  FOUND_ISSUE_NUMBER=$(printf '%s' "$filtered" | jq -r '.number')
  FOUND_ISSUE_TITLE=$(printf '%s' "$filtered" | jq -r '.title')
  FOUND_ISSUE_BODY=$(printf '%s' "$filtered" | jq -r '.body // ""')
  info "Found eligible issue: #${FOUND_ISSUE_NUMBER} — ${FOUND_ISSUE_TITLE}"
  return 0
}

# claim_issue <issue_number>
# Optimistic claim: check wip, add wip, race-check, remove ready, assign, post a
# fresh Claimed comment (anchors retry counting). Returns 0 on success.
claim_issue() {
  local issue_number="$1"

  local current_labels
  current_labels=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json labels --jq '.labels[].name' 2>/dev/null)
  if printf '%s' "$current_labels" | grep -q "^${PRAUTO_GITHUB_LABEL_WIP}$"; then
    warn "Issue #${issue_number} already has ${PRAUTO_GITHUB_LABEL_WIP} — another worker claimed it."
    return 1
  fi

  local pre_claim_ts
  pre_claim_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --add-label "$PRAUTO_GITHUB_LABEL_WIP" 2>/dev/null || {
    warn "Failed to add ${PRAUTO_GITHUB_LABEL_WIP} to issue #${issue_number}."
    return 1
  }

  # Brief delay, then verify no competing Claimed comment landed in the window.
  sleep 2
  local race_comments
  race_comments=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg ts "$pre_claim_ts" \
      '[.[] | select(.body | startswith("prauto(")) | select(.body | contains("Claimed")) | select(.createdAt > $ts)] | length' \
    ) || race_comments=0
  if [[ "$race_comments" -gt 0 ]]; then
    warn "Issue #${issue_number} was claimed by another worker during the race window."
    return 1
  fi

  gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --remove-label "$PRAUTO_GITHUB_LABEL_READY" \
    --add-assignee "$PRAUTO_GITHUB_ACTOR" 2>/dev/null || true
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Claimed this issue. Starting work." 2>/dev/null \
    || warn "Failed to post claim comment on issue #${issue_number}."
  info "Claimed issue #${issue_number}."
  return 0
}

# extract_change_size <issue_body>
# Read the author's `### Change Size` hint. Prints minor|major|medium.
extract_change_size() {
  local issue_body="$1" size_line
  size_line=$(printf '%s' "$issue_body" | sed -n '/^### Change Size/,/^###/{/^### Change Size/d;/^###/d;/^$/d;p;}' | head -1)
  case "$size_line" in
    Minor*|minor*) printf 'minor' ;;
    Major*|major*) printf 'major' ;;
    *)             printf 'medium' ;;
  esac
}

# analysis_confirms_skip_plan <analysis_output>
# Whether the analysis phase's own plan metadata says "Skip-plan eligible: yes".
# Conservative: only an explicit yes confirms; anything else requires approval.
analysis_confirms_skip_plan() {
  local analysis_output="$1" line
  line=$(printf '%s' "$analysis_output" | grep -iE 'Skip-plan eligible' | head -1 || true)
  [[ -n "$line" ]] || return 1
  printf '%s' "$line" | grep -qiE ':[[:space:]]*[*`> ]*yes\b'
}

# resolve_change_size <issue_body> <analysis_output>
# The evidence-based plan gate: minor requires BOTH the author's `minor` hint AND
# the analysis confirming its own plan meets the skip-plan criteria. The hint can
# never buy a skip the plan's evidence does not support.
resolve_change_size() {
  local issue_body="$1" analysis_output="$2" hint
  hint=$(extract_change_size "$issue_body")
  if [[ "$hint" == "minor" ]]; then
    if analysis_confirms_skip_plan "$analysis_output"; then printf 'minor'; else printf 'medium'; fi
  else
    printf '%s' "$hint"
  fi
}

# post_plan_comment <issue_number> <analysis_output> <change_size> [revision]
# Post the plan; add prauto:plan-review for non-minor plans.
post_plan_comment() {
  local issue_number="$1" analysis_output="$2" change_size="$3" revision="${4:-1}"
  local keyword="Plan"
  [[ "$revision" -gt 1 ]] && keyword="Plan (rev ${revision})"

  if comment_exists "issue" "$issue_number" "$keyword"; then
    info "Plan comment (${keyword}) already exists on issue #${issue_number}. Skipping."
    return 0
  fi

  local footer size_label
  if [[ "$change_size" == "minor" ]]; then
    footer='> This is a **Minor** change. Implementation will proceed automatically.'
  else
    size_label="$(tr '[:lower:]' '[:upper:]' <<< "${change_size:0:1}")${change_size:1}"
    footer="> This is a **${size_label}** change. Please review the plan above.
> Reply with \`go ahead\` to approve, or post a counter-proposal."
  fi

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): ${keyword}

## Implementation Plan

${analysis_output}

---
${footer}" \
    2>/dev/null || warn "Failed to post plan comment on issue #${issue_number}."

  if [[ "$change_size" != "minor" ]]; then
    gh issue edit "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
      --add-label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" 2>/dev/null \
      || warn "Failed to add ${PRAUTO_GITHUB_LABEL_PLAN_REVIEW} to issue #${issue_number}."
  fi
  info "Plan comment posted on issue #${issue_number} (change_size=${change_size})."
}

# find_all_claimed_issues
# List open issues claimed by this worker (any active prauto: label, excluding
# restarted issues that only carry prauto:ready). Sorted oldest-first.
# Sets: ALL_CLAIMED_ISSUES, ALL_CLAIMED_COUNT. Returns 0 if any, 1 if none.
find_all_claimed_issues() {
  local issues_json
  issues_json=$(gh issue list -R "$PRAUTO_GITHUB_REPO" \
    --assignee "$PRAUTO_GITHUB_ACTOR" --state open \
    --json number,title,labels --limit 50 2>/dev/null) || {
    warn "Failed to list claimed issues from GitHub."
    return 1
  }
  ALL_CLAIMED_ISSUES=$(printf '%s' "$issues_json" | jq \
    --arg ready "$PRAUTO_GITHUB_LABEL_READY" '
    [.[] | select(.labels | any(.name | startswith("prauto:")))
          | select((.labels | map(.name) | [.[] | select(startswith("prauto:"))]) as $pl
            | ($pl | length > 1) or ($pl[0] != $ready))]
    | sort_by(.number)')
  ALL_CLAIMED_COUNT=$(printf '%s' "$ALL_CLAIMED_ISSUES" | jq 'length')
  [[ "$ALL_CLAIMED_COUNT" -eq 0 ]] && return 1
  info "Found ${ALL_CLAIMED_COUNT} claimed issue(s) on GitHub."
  return 0
}

# count_heartbeat_comments <issue_number>
# Count heartbeat markers within the CURRENT lifecycle only: after the most
# recent "Claimed" comment by this worker AND after the last prauto:ready event.
# Sets: HEARTBEAT_COMMENT_COUNT.
count_heartbeat_comments() {
  local issue_number="$1"
  local hb_marker="prauto(${PRAUTO_WORKER_ID}): Heartbeat"
  local claim_marker="prauto(${PRAUTO_WORKER_ID}): Claimed"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  HEARTBEAT_COMMENT_COUNT=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg hb "$hb_marker" --arg cl "$claim_marker" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts)] as $scoped
      | ($scoped | [.[] | select(.body | startswith($cl))] | last | .createdAt // "") as $anchor
      | [$scoped[] | select(.body | startswith($hb)) | select(.createdAt > $anchor)] | length
    ') || HEARTBEAT_COMMENT_COUNT=0
}

# post_heartbeat_comment <issue_number> <phase> <attempt> <max>
post_heartbeat_comment() {
  local issue_number="$1" phase="$2" attempt="$3" max="$4"
  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "prauto(${PRAUTO_WORKER_ID}): Heartbeat — ${phase} (attempt ${attempt}/${max})" 2>/dev/null \
    || warn "Failed to post heartbeat comment on issue #${issue_number}."
}

# post_commit_checkpoint_comment <issue_number> <branch> <sha> <subject>
# Post one idempotent issue comment for a pushed checkpoint commit.
post_commit_checkpoint_comment() {
  local issue_number="$1" branch="$2" sha="$3" subject="$4"
  local short_sha="${sha:0:12}"
  local commit_url="https://github.com/${PRAUTO_GITHUB_REPO}/commit/${sha}"
  local branch_url="https://github.com/${PRAUTO_GITHUB_REPO}/tree/${branch}"

  if comment_exists "issue" "$issue_number" "Checkpoint commit ${sha}"; then
    info "Checkpoint comment already exists for ${short_sha} on issue #${issue_number}."
    return 0
  fi

  local body
  body="prauto(${PRAUTO_WORKER_ID}): Checkpoint commit ${sha}

[\`${short_sha}\`](${commit_url}) — ${subject}
Branch: [\`${branch}\`](${branch_url})"

  gh issue comment "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --body "$body" 2>/dev/null \
    || warn "Failed to post checkpoint comment for ${short_sha} on issue #${issue_number}."
}

# publish_commit_checkpoints <issue_number> <branch>
# Publish branch-only commits in oldest-first order. The merge-base keeps an
# existing branch that started from an older dev commit from replaying its base
# history; comment_exists makes retries safe when a push/comment partially wins.
publish_commit_checkpoints() {
  local issue_number="$1" branch="$2" repo_path="${WORKTREE_DIR:-.}"
  local base_sha commits sha subject

  base_sha=$(git -C "$repo_path" merge-base "origin/${PRAUTO_BASE_BRANCH}" "$branch" 2>/dev/null || printf '')
  [[ -n "$base_sha" ]] || {
    warn "Could not determine checkpoint base for ${branch}."
    return 1
  }

  commits=$(git -C "$repo_path" log --reverse --format='%H%x09%s' "${base_sha}..${branch}" 2>/dev/null || printf '')
  [[ -n "$commits" ]] || return 0

  while IFS=$'\t' read -r sha subject; do
    [[ -n "$sha" ]] || continue
    post_commit_checkpoint_comment "$issue_number" "$branch" "$sha" "$subject"
  done <<< "$commits"
}

# derive_phase_from_github <issue_number> <branch>
# Derive the current phase from GitHub signals only. Sets DERIVED_PHASE.
#   1. PR exists for branch -> pr
#   2. prauto:plan-review label -> plan-approval
#   3. Plan comment + go-ahead -> implementation
#   4. Plan comment, no approval -> plan-approval
#   5. No plan comment -> analysis
derive_phase_from_github() {
  local issue_number="$1" branch="$2"
  local pr_number
  pr_number=$(gh pr list -R "$PRAUTO_GITHUB_REPO" --head "$branch" \
    --json number --jq '.[0].number // empty' 2>/dev/null)
  if [[ -n "$pr_number" ]]; then
    DERIVED_PHASE="pr"; return 0
  fi

  local issue_labels has_plan_review
  issue_labels=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json labels --jq '[.labels[].name]' 2>/dev/null) || issue_labels="[]"
  has_plan_review=$(printf '%s' "$issue_labels" | jq -r --arg label "${PRAUTO_GITHUB_LABEL_PLAN_REVIEW}" \
    'index($label) != null')
  if [[ "$has_plan_review" == "true" ]]; then
    DERIVED_PHASE="plan-approval"; return 0
  fi

  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local plan_exists
  plan_exists=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg prefix "$plan_prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | length
    ') || plan_exists=0
  if [[ "$plan_exists" -gt 0 ]]; then
    local approval_status=0
    check_plan_approval "$issue_number" || approval_status=$?
    if [[ "$approval_status" -eq 0 ]]; then
      DERIVED_PHASE="implementation"
    else
      DERIVED_PHASE="plan-approval"
    fi
    return 0
  fi
  DERIVED_PHASE="analysis"
  return 0
}

# get_plan_revision_from_github <issue_number>
# Count plan comments in the current lifecycle; next revision = count + 1.
# Sets: GITHUB_PLAN_REVISION.
get_plan_revision_from_github() {
  local issue_number="$1"
  local prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"
  local plan_count
  plan_count=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null \
    | jq --arg prefix "$prefix" --arg ready_ts "$ready_ts" '
      [.[] | select($ready_ts == "" or .createdAt > $ready_ts) | select(.body | startswith($prefix))] | length
    ') || plan_count=0
  GITHUB_PLAN_REVISION=$(( plan_count + 1 ))
}

# check_plan_approval <issue_number>
# Returns: 0 = approved ("go ahead"), 1 = no response yet, 2 = counter-proposal
# (sets COUNTER_PROPOSAL), 3 = no plan comment. Scoped to the current lifecycle;
# approval is a non-prauto comment reading exactly "go ahead" after the plan.
check_plan_approval() {
  local issue_number="$1"
  local plan_prefix="prauto(${PRAUTO_WORKER_ID}): Plan"
  local ready_ts="${READY_LABEL_TIMESTAMP:-}"

  local comments_json
  comments_json=$(gh issue view "$issue_number" -R "$PRAUTO_GITHUB_REPO" \
    --json comments --jq '.comments' 2>/dev/null) || { warn "Failed to fetch comments for #${issue_number}."; return 1; }
  comments_json=$(printf '%s' "$comments_json" | jq --arg ready_ts "$ready_ts" \
    '[.[] | select($ready_ts == "" or .createdAt > $ready_ts)]')

  local plan_timestamp
  plan_timestamp=$(printf '%s' "$comments_json" | jq -r --arg prefix "$plan_prefix" \
    '[.[] | select(.body | startswith($prefix))] | last | .createdAt // empty')
  [[ -z "$plan_timestamp" ]] && { warn "No plan comment found on #${issue_number}."; return 3; }

  local after_comments
  after_comments=$(printf '%s' "$comments_json" | jq -r --arg ts "$plan_timestamp" \
    '[.[] | select(.createdAt > $ts) | select(.body | startswith("prauto(") | not)]')
  local comment_count
  comment_count=$(printf '%s' "$after_comments" | jq 'length')
  [[ "$comment_count" -eq 0 ]] && { info "No response to plan yet on #${issue_number}."; return 1; }

  local i body_trimmed
  for (( i = 0; i < comment_count; i++ )); do
    body_trimmed=$(printf '%s' "$after_comments" | jq -r ".[$i].body" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [[ "$body_trimmed" == "go ahead" ]]; then
      info "Plan approved on issue #${issue_number}."
      return 0
    fi
  done

  COUNTER_PROPOSAL=$(printf '%s' "$after_comments" | jq -r '.[-1].body')
  info "Counter-proposal on issue #${issue_number}."
  return 2
}
